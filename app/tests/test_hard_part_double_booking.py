"""Hard part #1: two people cannot hold the same stylist at the same time.

The service re-checks the slot, but only the database constraint can decide a true
race, so these tests prove the constraint is what stops the double booking.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.user import UserRole
from app.services import bookings as booking_service
from app.services.bookings import LOST_RACE_STATES, sqlstate
from app.tests.test_bookings_api import (
    UNAVAILABLE,
    URL,
    body,
    client_with_headers,
    open_stylist,
)
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_slots import upcoming
from app.tests.test_stylists_api import make_stylist

CONSTRAINT = "ex_bookings_stylist_id_no_overlap"
RACE_TIMEOUT_SECONDS = 15
BARRIER_TIMEOUT_SECONDS = 5


@pytest.fixture(autouse=True)
def _pin_salon_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SALON_UTC_OFFSET_MINUTES", 60
    )  # the +01:00 in body()


# --- API level: the constraint decides a genuine race ---


async def test_two_clients_booking_the_same_slot_at_the_same_moment_exactly_one_wins(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both requests must pass the slot re-check before either inserts.

    A plain gather often lets one request finish before the other starts, which
    would only test the re-check. The wrapper runs the real get_slots (the slot
    looks free to both) and then parks on a two-party barrier, so neither request
    can reach its INSERT until both have seen the slot free. The second INSERT then
    blocks on the first transaction and is rejected by the database.
    """
    _, stylist, service = await open_stylist(db_session)
    client_a, headers_a = await client_with_headers(db_session)
    client_b, headers_b = await client_with_headers(db_session)
    payload = body(stylist, service.id, upcoming(0))
    stylist_id, id_a, id_b = stylist.id, client_a.id, client_b.id  # survive expire_all

    barrier = asyncio.Barrier(2)
    real_get_slots = booking_service.get_slots

    async def get_slots_then_wait(*args: Any, **kwargs: Any) -> Any:
        slots = await real_get_slots(*args, **kwargs)
        try:
            async with asyncio.timeout(BARRIER_TIMEOUT_SECONDS):
                await barrier.wait()
        except (TimeoutError, asyncio.BrokenBarrierError):
            # Fail with the cause instead of a bare timeout that hides a 4xx/5xx.
            raise AssertionError(
                "the other request never reached the slot re-check"
            ) from None
        return slots

    # Record why the database turned the loser away.
    real_is_lost_race = booking_service.is_lost_race
    rejections: list[str | None] = []

    def spy_is_lost_race(exc: DBAPIError) -> bool:
        rejections.append(sqlstate(exc))
        return real_is_lost_race(exc)

    monkeypatch.setattr(booking_service, "get_slots", get_slots_then_wait)
    monkeypatch.setattr(booking_service, "is_lost_race", spy_is_lost_race)

    resp_a, resp_b = await asyncio.wait_for(
        asyncio.gather(
            db_client.post(URL, json=payload, headers=headers_a),
            db_client.post(URL, json=payload, headers=headers_b),
        ),
        timeout=RACE_TIMEOUT_SECONDS,
    )

    outcomes = [(resp_a, id_a), (resp_b, id_b)]
    assert sorted(r.status_code for r, _ in outcomes) == [201, 409]
    ((winner, winner_id),) = [o for o in outcomes if o[0].status_code == 201]
    ((loser, _),) = [o for o in outcomes if o[0].status_code == 409]
    assert loser.json()["detail"] == UNAVAILABLE
    # The 409 came from the database (exclusion violation, or the deadlock Postgres
    # can use to abort one of two simultaneous inserts), not from the re-check.
    assert len(rejections) == 1
    assert rejections[0] in LOST_RACE_STATES

    db_session.expire_all()
    rows = list((await db_session.execute(select(Booking))).scalars())
    assert len(rows) == 1
    (stored,) = rows
    assert stored.stylist_id == stylist_id
    assert stored.status == BookingStatus.PENDING
    assert stored.client_id == winner_id
    assert str(stored.id) == winner.json()["id"]


# --- database level: the constraint alone, no service code ---

START = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)

# Plain SQL: relies on the `bookingstatus` enum type name and gen_random_uuid() (PG13+).
INSERT = text(
    "INSERT INTO bookings (id, client_id, stylist_id, service_id, starts_at, ends_at,"
    " status) VALUES (gen_random_uuid(), :client_id, :stylist_id, :service_id,"
    " :starts_at, :ends_at, CAST(:status AS bookingstatus))"
)


class Ids(NamedTuple):
    """Plain ids: ORM instances expire on rollback, ids stay usable."""

    stylist: uuid.UUID
    other_stylist: uuid.UUID
    service: uuid.UUID
    client: uuid.UUID


async def _seed(db_session: AsyncSession) -> Ids:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    other = await make_stylist(db_session, salon)
    service = await make_service(db_session, salon)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    return Ids(stylist.id, other.id, service.id, client.id)


async def raw_insert(
    session: AsyncSession,
    ids: Ids,
    start_offset_min: int,
    minutes: int = 60,
    status: str = "pending",
    stylist_id: uuid.UUID | None = None,
) -> None:
    """Plain SQL INSERT, then commit; bypasses the service and the ORM."""
    start = START + timedelta(minutes=start_offset_min)
    params: dict[str, Any] = {
        "client_id": ids.client,
        "stylist_id": stylist_id or ids.stylist,
        "service_id": ids.service,
        "starts_at": start,
        "ends_at": start + timedelta(minutes=minutes),
        "status": status,
    }
    await session.execute(INSERT, params)
    await session.commit()


async def count_rows(session: AsyncSession, stylist_id: uuid.UUID) -> int:
    result = await session.execute(
        text("SELECT count(*) FROM bookings WHERE stylist_id = :s"),
        {"s": stylist_id},
    )
    return result.scalar_one()


@pytest.mark.parametrize("second_status", ["pending", "confirmed"])
async def test_database_constraint_alone_rejects_an_overlapping_insert(
    db_session: AsyncSession, second_status: str
) -> None:
    ids = await _seed(db_session)
    await raw_insert(db_session, ids, 0)  # 10:00-11:00

    with pytest.raises(IntegrityError) as exc:
        await raw_insert(db_session, ids, 30, status=second_status)  # 10:30-11:30
    await db_session.rollback()

    assert getattr(exc.value.orig, "sqlstate", None) == "23P01"
    assert CONSTRAINT in str(exc.value.orig)
    assert await count_rows(db_session, ids.stylist) == 1


async def test_raw_back_to_back_insert_is_accepted(db_session: AsyncSession) -> None:
    ids = await _seed(db_session)
    await raw_insert(db_session, ids, 0)  # 10:00-11:00

    await raw_insert(db_session, ids, 60)  # 11:00-12:00
    await raw_insert(db_session, ids, -60)  # 09:00-10:00

    assert await count_rows(db_session, ids.stylist) == 3


async def test_raw_overlapping_insert_with_cancelled_status_is_accepted(
    db_session: AsyncSession,
) -> None:
    ids = await _seed(db_session)
    await raw_insert(db_session, ids, 0)

    await raw_insert(db_session, ids, 30, status="cancelled")

    assert await count_rows(db_session, ids.stylist) == 2


async def test_raw_overlapping_insert_for_a_different_stylist_is_accepted(
    db_session: AsyncSession,
) -> None:
    ids = await _seed(db_session)
    await raw_insert(db_session, ids, 0)

    await raw_insert(db_session, ids, 0, stylist_id=ids.other_stylist)

    assert await count_rows(db_session, ids.stylist) == 1
    assert await count_rows(db_session, ids.other_stylist) == 1
