import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.availability_rule import RuleKind
from app.models.booking import Booking, BookingStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole
from app.services import bookings as booking_service
from app.services.bookings import (
    MAX_PENDING_PER_CLIENT,
    is_lost_race,
    is_overlap,
    too_many_pending,
)
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_slots import WAT, add_rules, lunch, setup_stylist, upcoming, working

URL = "/api/v1/bookings"
UNAVAILABLE = "That time slot is no longer available. Please pick another time."


@pytest.fixture(autouse=True)
def _pin_salon_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SALON_UTC_OFFSET_MINUTES", 60)  # the +01:00 below


def body(
    stylist: User, service_id: uuid.UUID, day: date, hhmm: str = "10:00"
) -> dict[str, str]:
    return {
        "stylist_id": str(stylist.id),
        "service_id": str(service_id),
        "starts_at": f"{day.isoformat()}T{hhmm}:00+01:00",
    }


async def open_stylist(
    db_session: AsyncSession, duration: int = 60
) -> tuple[Salon, User, Service]:
    """Works Mondays 09:00-17:00 with a 12:00-13:00 break."""
    salon, stylist, service = await setup_stylist(db_session, duration)
    await add_rules(
        db_session, stylist, working("09:00", "17:00"), lunch("12:00", "13:00")
    )
    return salon, stylist, service


async def client_with_headers(
    db_session: AsyncSession,
) -> tuple[User, dict[str, str]]:
    return await make_user(db_session, UserRole.CLIENT)


async def bookings_in_db(db_session: AsyncSession) -> list[Booking]:
    db_session.expire_all()
    return list((await db_session.execute(select(Booking))).scalars())


async def insert_booking(
    db_session: AsyncSession,
    stylist: User,
    service_id: uuid.UUID,
    client: User,
    day: date,
    hhmm: str,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> None:
    start = datetime.combine(day, time.fromisoformat(hhmm), WAT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service_id,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            status=status,
        )
    )
    await db_session.commit()


# --- success ---


async def test_client_creates_a_pending_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session, 90)
    client, headers = await client_with_headers(db_session)
    day = upcoming(0)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, day), headers=headers
    )

    assert resp.status_code == 201
    data = resp.json()
    assert set(data) == {
        "id",
        "client_id",
        "stylist_id",
        "service_id",
        "starts_at",
        "ends_at",
        "status",
        "created_at",
    }
    assert data["status"] == "pending"
    assert data["client_id"] == str(client.id)
    (stored,) = await bookings_in_db(db_session)
    assert stored.starts_at == datetime.combine(day, time(9, 0), UTC)  # 10:00 at +01:00
    assert stored.ends_at - stored.starts_at == timedelta(minutes=90)  # service length
    assert stored.status == BookingStatus.PENDING


# --- auth and roles ---


async def test_booking_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)

    resp = await db_client.post(URL, json=body(stylist, service.id, upcoming(0)))

    assert resp.status_code == 401


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.STYLIST])
async def test_only_clients_can_book(
    db_client: AsyncClient, db_session: AsyncSession, role: UserRole
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await make_user(db_session, role)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, upcoming(0)), headers=headers
    )

    assert resp.status_code == 403
    assert await bookings_in_db(db_session) == []


async def test_client_cannot_book_on_behalf_of_another_client(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    victim, _ = await client_with_headers(db_session)
    _, attacker_headers = await client_with_headers(db_session)
    payload = {**body(stylist, service.id, upcoming(0)), "client_id": str(victim.id)}

    resp = await db_client.post(URL, json=payload, headers=attacker_headers)

    assert resp.status_code == 422
    assert await bookings_in_db(db_session) == []


# --- lookups: stylist, service, salon ---


async def test_unknown_stylist_and_non_stylist_ids_are_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, service = await open_stylist(db_session)
    owner, _ = await make_user(db_session, UserRole.OWNER)
    _, headers = await client_with_headers(db_session)
    day = upcoming(0)

    for bad_id in ("00000000-0000-4000-8000-000000000000", str(owner.id)):
        payload = {**body(owner, service.id, day), "stylist_id": bad_id}
        resp = await db_client.post(URL, json=payload, headers=headers)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "not found"}


async def test_service_the_stylist_does_not_offer_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, _ = await open_stylist(db_session)
    unlinked = await make_service(db_session, salon, name="Other")
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(
        URL, json=body(stylist, unlinked.id, upcoming(0)), headers=headers
    )

    assert resp.status_code == 404
    assert await bookings_in_db(db_session) == []


async def test_service_from_another_salon_is_404_even_if_linked(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, _ = await open_stylist(db_session)
    _, _, other_salon = await owner_and_salon(db_session)
    foreign = await make_service(db_session, other_salon, name="Foreign")
    db_session.add(StylistService(stylist_id=stylist.id, service_id=foreign.id))
    await db_session.commit()
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(
        URL, json=body(stylist, foreign.id, upcoming(0)), headers=headers
    )

    assert resp.status_code == 404
    assert await bookings_in_db(db_session) == []


async def test_stylist_without_a_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, service = await open_stylist(db_session)
    orphan, _ = await make_user(db_session, UserRole.STYLIST)  # salon_id is NULL
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(
        URL, json=body(orphan, service.id, upcoming(0)), headers=headers
    )

    assert resp.status_code == 404


# --- starts_at validation ---


@pytest.mark.parametrize(
    "starts_at",
    [
        "0001-01-01T00:00:00+05:00",  # would overflow a UTC conversion
        "9999-12-31T23:00:00-05:00",
        "9999-12-31T23:59:59+00:00",
    ],
)
async def test_extreme_starts_at_is_422_not_500(
    db_client: AsyncClient, db_session: AsyncSession, starts_at: str
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    payload = {**body(stylist, service.id, upcoming(0)), "starts_at": starts_at}

    resp = await db_client.post(URL, json=payload, headers=headers)

    assert resp.status_code == 422
    assert await bookings_in_db(db_session) == []


@pytest.mark.parametrize(
    "starts_at",
    [
        "2030-01-07T10:00:00",  # no UTC offset
        "not-a-date",
        "",
    ],
)
async def test_starts_at_must_be_a_timezone_aware_datetime(
    db_client: AsyncClient, db_session: AsyncSession, starts_at: str
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    payload = {**body(stylist, service.id, upcoming(0)), "starts_at": starts_at}

    resp = await db_client.post(URL, json=payload, headers=headers)

    assert resp.status_code == 422


async def test_starts_at_in_the_past_is_422(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    yesterday = datetime.now(UTC).date() - timedelta(days=1)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, yesterday), headers=headers
    )

    assert resp.status_code == 422


async def test_starts_at_beyond_the_advance_limit_is_422(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    far = datetime.now(UTC).date() + timedelta(days=400)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, far), headers=headers
    )

    assert resp.status_code == 422


# --- the server re-checks the slot ---


@pytest.mark.parametrize(
    "hhmm",
    [
        "08:00",  # before opening
        "12:00",  # inside the break
        "12:30",
        "16:30",  # would run past closing
        "10:07",  # not on the 15-minute grid
    ],
)
async def test_time_outside_the_slot_list_is_409(
    db_client: AsyncClient, db_session: AsyncSession, hhmm: str
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, upcoming(0), hhmm), headers=headers
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == UNAVAILABLE
    assert await bookings_in_db(db_session) == []


async def test_day_off_is_409(db_client: AsyncClient, db_session: AsyncSession) -> None:
    _, stylist, service = await open_stylist(db_session)
    day = upcoming(0)
    await add_rules(db_session, stylist, {"kind": RuleKind.DAY_OFF, "off_date": day})
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, day), headers=headers
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == UNAVAILABLE


async def test_overlapping_an_existing_booking_is_409_and_back_to_back_is_ok(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    other, _ = await client_with_headers(db_session)
    _, headers = await client_with_headers(db_session)
    day = upcoming(0)
    await insert_booking(db_session, stylist, service.id, other, day, "10:00")

    overlap = await db_client.post(
        URL, json=body(stylist, service.id, day, "10:30"), headers=headers
    )
    touching = await db_client.post(
        URL, json=body(stylist, service.id, day, "11:00"), headers=headers
    )

    assert overlap.status_code == 409
    assert overlap.json()["detail"] == UNAVAILABLE  # no hint about who holds it
    assert touching.status_code == 201


# --- the exclusion constraint is the final arbiter ---


async def test_concurrent_requests_for_one_slot_book_it_exactly_once(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Whichever way the requests interleave, one wins. (The constraint itself is
    proved by the forced-bypass test below.)"""
    _, stylist, service = await open_stylist(db_session)
    _, headers_a = await client_with_headers(db_session)
    _, headers_b = await client_with_headers(db_session)
    payload = body(stylist, service.id, upcoming(0))

    resp_a, resp_b = await asyncio.gather(
        db_client.post(URL, json=payload, headers=headers_a),
        db_client.post(URL, json=payload, headers=headers_b),
    )

    assert sorted([resp_a.status_code, resp_b.status_code]) == [201, 409]
    loser = resp_a if resp_a.status_code == 409 else resp_b
    assert loser.json()["detail"] == UNAVAILABLE
    assert len(await bookings_in_db(db_session)) == 1


async def test_exclusion_violation_is_409_not_500_when_the_recheck_passes(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate losing the race: the slot re-check says free, the constraint says no."""
    _, stylist, service = await open_stylist(db_session)
    other, _ = await client_with_headers(db_session)
    _, headers = await client_with_headers(db_session)
    day = upcoming(0)
    await insert_booking(db_session, stylist, service.id, other, day, "10:00")

    async def always_free(*args: Any, **kwargs: Any) -> list[datetime]:
        return [datetime.combine(day, time(10, 0), WAT)]

    monkeypatch.setattr(booking_service, "get_slots", always_free)

    resp = await db_client.post(
        URL, json=body(stylist, service.id, day), headers=headers
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == UNAVAILABLE
    assert len(await bookings_in_db(db_session)) == 1


class _Orig:
    def __init__(self, sqlstate: str | None) -> None:
        self.sqlstate = sqlstate


def _err(sqlstate: str | None) -> DBAPIError:
    return DBAPIError("stmt", {}, _Orig(sqlstate))  # type: ignore[arg-type]


def test_only_exclusion_violations_are_overlaps() -> None:
    assert is_overlap(_err("23P01"))
    assert not is_overlap(_err("23503"))  # foreign key violation
    assert not is_overlap(_err("23505"))  # unique violation
    assert not is_overlap(_err(None))


@pytest.mark.parametrize("state", ["23P01", "40P01", "40001"])
def test_losing_a_race_covers_deadlock_and_serialization_aborts(state: str) -> None:
    """Simultaneous inserts of one range can end in a deadlock abort, still a 409."""
    assert is_lost_race(_err(state))


@pytest.mark.parametrize("state", ["23503", "23505", "23502", "22003", None])
def test_other_database_errors_are_not_a_lost_race(state: str | None) -> None:
    assert not is_lost_race(_err(state))


# --- repeat requests and the pending cap ---


async def test_repeating_an_identical_request_is_409(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The client's own pending booking holds the slot (idempotency key: later)."""
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    payload = body(stylist, service.id, upcoming(0))

    first = await db_client.post(URL, json=payload, headers=headers)
    second = await db_client.post(URL, json=payload, headers=headers)

    assert (first.status_code, second.status_code) == (201, 409)
    assert len(await bookings_in_db(db_session)) == 1


CAP_HOURS = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]


async def test_client_is_capped_on_unexpired_pending_bookings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    _, other_headers = await client_with_headers(db_session)
    day = upcoming(0)
    spare_hour = CAP_HOURS[MAX_PENDING_PER_CLIENT]

    for hhmm in CAP_HOURS[:MAX_PENDING_PER_CLIENT]:
        ok = await db_client.post(
            URL, json=body(stylist, service.id, day, hhmm), headers=headers
        )
        assert ok.status_code == 201

    capped = await db_client.post(
        URL, json=body(stylist, service.id, day, spare_hour), headers=headers
    )
    other_client = await db_client.post(
        URL, json=body(stylist, service.id, day, spare_hour), headers=other_headers
    )

    assert capped.status_code == 409
    assert capped.json()["detail"] == too_many_pending().detail
    assert other_client.status_code == 201  # the cap is per client


async def test_cap_holds_under_concurrent_requests_from_one_client(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The client row lock makes the cap race-free: every extra request is refused."""
    _, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    day = upcoming(0)
    extra = 2

    responses = await asyncio.gather(
        *(
            db_client.post(
                URL, json=body(stylist, service.id, day, hhmm), headers=headers
            )
            for hhmm in CAP_HOURS[: MAX_PENDING_PER_CLIENT + extra]
        )
    )

    created = [r for r in responses if r.status_code == 201]
    refused = [r for r in responses if r.status_code == 409]
    assert len(created) == MAX_PENDING_PER_CLIENT
    assert len(refused) == extra
    # Refused because of the cap, not because the slots collided.
    assert {r.json()["detail"] for r in refused} == {too_many_pending().detail}
    assert len(await bookings_in_db(db_session)) == MAX_PENDING_PER_CLIENT


async def test_post_cancels_the_stylists_lapsed_pending_booking_itself(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """End to end with the real clock: no job has run, yet the slot is bookable."""
    _, stylist, service = await open_stylist(db_session)
    day = upcoming(0)
    abandoned_by, _ = await client_with_headers(db_session)
    _, headers = await client_with_headers(db_session)
    start = datetime.combine(day, time(10, 0), WAT)
    stale = datetime.now(UTC) - timedelta(minutes=20)
    old = Booking(
        client_id=abandoned_by.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        status=BookingStatus.PENDING,
        created_at=stale,
        updated_at=stale,
    )
    db_session.add(old)
    await db_session.commit()

    resp = await db_client.post(
        URL, json=body(stylist, service.id, day), headers=headers
    )

    assert resp.status_code == 201
    await db_session.refresh(old)
    assert old.status == BookingStatus.CANCELLED
