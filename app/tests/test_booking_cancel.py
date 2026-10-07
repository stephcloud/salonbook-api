import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.salon import Salon
from app.models.user import User
from app.services import bookings as booking_service
from app.services.bookings import cancel_booking
from app.tests.test_bookings_api import (
    URL,
    body,
    bookings_in_db,
    client_with_headers,
    open_stylist,
)
from app.tests.test_services_api import owner_and_salon
from app.tests.test_slots import upcoming


@pytest.fixture(autouse=True)
def _pin_salon_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "SALON_UTC_OFFSET_MINUTES", 60
    )  # the +01:00 in body()


def cancel_url(booking_id: uuid.UUID) -> str:
    return f"{URL}/{booking_id}/cancel"


async def add_booking(
    db_session: AsyncSession,
    stylist: User,
    service_id: Any,
    client: User,
    starts_at: datetime,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> uuid.UUID:
    booking = Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service_id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=status,
    )
    db_session.add(booking)
    await db_session.commit()
    return booking.id


async def stored(db_session: AsyncSession, booking_id: uuid.UUID) -> Booking:
    db_session.expire_all()
    booking = await db_session.get(Booking, booking_id)
    assert booking is not None
    return booking


async def salon_owner(
    db_session: AsyncSession, salon: Salon
) -> tuple[User, dict[str, str]]:
    owner = await db_session.get(User, salon.owner_id)
    assert owner is not None
    return owner, {"Authorization": f"Bearer {create_access_token(owner.id)}"}


def hours_from_now(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


# --- refund decision, through the endpoint ---


async def test_client_cancels_a_confirmed_booking_well_before_the_limit_and_is_due_a_refund(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(booking_id)
    assert data["status"] == "cancelled"
    assert data["refund_due"] is True
    assert data["cancelled_at"] is not None
    assert "password_hash" not in str(data)
    row = await stored(db_session, booking_id)
    assert row.status == BookingStatus.CANCELLED
    assert row.refund_due is True


async def test_cancelling_well_after_the_limit_is_not_due_a_refund(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(3)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["refund_due"] is False


async def test_cancelling_exactly_at_the_boundary_is_not_due_a_refund(
    db_session: AsyncSession,
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    assert salon.cancellation_hours == 24
    client, _ = await client_with_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(days=3)
    exact = await add_booking(db_session, stylist, service.id, client, starts_at)
    just_before = await add_booking(
        db_session, stylist, service.id, client, starts_at + timedelta(hours=2)
    )

    # Service called directly so `now` can be pinned to the exact instant.
    await cancel_booking(db_session, exact, client, now=starts_at - timedelta(hours=24))
    await cancel_booking(
        db_session,
        just_before,
        client,
        now=starts_at + timedelta(hours=2) - timedelta(hours=24, seconds=1),
    )

    assert (await stored(db_session, exact)).refund_due is False
    assert (await stored(db_session, just_before)).refund_due is True


async def test_the_salons_own_cancellation_hours_decide_the_refund(
    db_session: AsyncSession,
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    salon.cancellation_hours = 48
    db_session.add(salon)
    await db_session.commit()
    client, _ = await client_with_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(days=5)
    booking_id = await add_booking(db_session, stylist, service.id, client, starts_at)

    await cancel_booking(
        db_session, booking_id, client, now=starts_at - timedelta(hours=30)
    )

    assert (await stored(db_session, booking_id)).refund_due is False  # inside 48h


async def test_cancelling_a_pending_booking_records_no_refund_as_nothing_was_paid(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session,
        stylist,
        service.id,
        client,
        hours_from_now(24 * 5),
        BookingStatus.PENDING,
    )

    resp = await db_client.post(cancel_url(booking_id), headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["refund_due"] is False


# --- who may cancel ---


async def test_the_salon_owner_can_cancel_a_clients_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    _, owner_headers = await salon_owner(db_session, salon)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=owner_headers)

    assert resp.status_code == 200
    row = await stored(db_session, booking_id)
    assert row.status == BookingStatus.CANCELLED
    assert row.refund_due is True


async def test_an_owner_cancel_inside_the_window_still_refunds_the_deposit(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The late-cancel window only applies to a client who backs out."""
    salon, stylist, service = await open_stylist(db_session)
    client, client_headers = await client_with_headers(db_session)
    _, owner_headers = await salon_owner(db_session, salon)
    by_owner = await add_booking(
        db_session,
        stylist,
        service.id,
        client,
        hours_from_now(3),  # inside 24h
    )
    by_client = await add_booking(
        db_session,
        stylist,
        service.id,
        client,
        hours_from_now(6),  # inside 24h
    )

    owner_resp = await db_client.post(cancel_url(by_owner), headers=owner_headers)
    client_resp = await db_client.post(cancel_url(by_client), headers=client_headers)

    assert owner_resp.status_code == 200
    assert owner_resp.json()["refund_due"] is True
    assert client_resp.status_code == 200
    assert client_resp.json()["refund_due"] is False  # same window, client cancels


async def test_an_owner_cancelling_a_pending_booking_records_no_refund(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    _, owner_headers = await salon_owner(db_session, salon)
    booking_id = await add_booking(
        db_session,
        stylist,
        service.id,
        client,
        hours_from_now(3),
        BookingStatus.PENDING,
    )

    resp = await db_client.post(cancel_url(booking_id), headers=owner_headers)

    assert resp.status_code == 200
    assert resp.json()["refund_due"] is False  # nothing was paid


async def test_another_client_cannot_cancel_it(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client_a, _ = await client_with_headers(db_session)
    _, headers_b = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client_a, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=headers_b)

    assert resp.status_code == 403
    row = await stored(db_session, booking_id)
    assert row.status == BookingStatus.CONFIRMED
    assert row.cancelled_at is None
    assert row.refund_due is None


async def test_the_owner_of_a_different_salon_cannot_cancel_it(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    _, other_owner_headers, _ = await owner_and_salon(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=other_owner_headers)

    assert resp.status_code == 403
    assert (await stored(db_session, booking_id)).status == BookingStatus.CONFIRMED


async def test_the_stylist_cannot_cancel_it(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    stylist_headers = {"Authorization": f"Bearer {create_access_token(stylist.id)}"}
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id), headers=stylist_headers)

    assert resp.status_code == 403
    assert (await stored(db_session, booking_id)).status == BookingStatus.CONFIRMED


async def test_cancel_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    resp = await db_client.post(cancel_url(booking_id))

    assert resp.status_code == 401
    assert (await stored(db_session, booking_id)).status == BookingStatus.CONFIRMED


async def test_cancelling_an_unknown_booking_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(cancel_url(uuid.uuid4()), headers=headers)

    assert resp.status_code == 404
    assert resp.json() == {"detail": "not found"}


async def test_a_malformed_booking_id_is_422(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await client_with_headers(db_session)

    resp = await db_client.post(f"{URL}/not-a-uuid/cancel", headers=headers)

    assert resp.status_code == 422


# --- status rules and idempotency ---


@pytest.mark.parametrize("final", [BookingStatus.COMPLETED, BookingStatus.NO_SHOW])
async def test_completed_and_no_show_bookings_cannot_be_cancelled(
    db_client: AsyncClient, db_session: AsyncSession, final: BookingStatus
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5), final
    )

    resp = await db_client.post(cancel_url(booking_id), headers=headers)

    assert resp.status_code == 409
    row = await stored(db_session, booking_id)
    assert row.status == final
    assert row.refund_due is None


async def test_a_booking_that_has_already_started_cannot_be_cancelled(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    client, client_headers = await client_with_headers(db_session)
    _, owner_headers = await salon_owner(db_session, salon)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(-1)
    )

    by_client = await db_client.post(cancel_url(booking_id), headers=client_headers)
    by_owner = await db_client.post(cancel_url(booking_id), headers=owner_headers)

    for resp in (by_client, by_owner):
        assert resp.status_code == 409
        assert resp.json()["detail"] == (
            "This booking has already started and can no longer be cancelled."
        )
    row = await stored(db_session, booking_id)
    assert row.status == BookingStatus.CONFIRMED
    assert row.cancelled_at is None
    assert row.refund_due is None


async def test_the_start_time_itself_counts_as_started(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(days=1)
    booking_id = await add_booking(db_session, stylist, service.id, client, starts_at)

    with pytest.raises(HTTPException) as late:
        await cancel_booking(db_session, booking_id, client, now=starts_at)
    await db_session.refresh(client)  # the rejected call rolled back and expired it
    await cancel_booking(
        db_session, booking_id, client, now=starts_at - timedelta(seconds=1)
    )

    assert late.value.status_code == 409
    assert (await stored(db_session, booking_id)).status == BookingStatus.CANCELLED


async def test_repeating_a_cancel_after_the_start_time_is_still_the_idempotent_200(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Already cancelled wins over already started: a retry must not turn into a 409."""
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(days=1)
    booking_id = await add_booking(db_session, stylist, service.id, client, starts_at)
    await cancel_booking(
        db_session, booking_id, client, now=starts_at - timedelta(days=1)
    )
    row = await stored(db_session, booking_id)
    row.starts_at = hours_from_now(-1)  # the appointment time has since passed
    db_session.add(row)
    await db_session.commit()

    resp = await db_client.post(cancel_url(booking_id), headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


async def test_cancelling_twice_changes_nothing_the_second_time(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )

    before = await stored(db_session, booking_id)
    assert before.cancelled_at is None
    created_updated_at = before.updated_at

    first = await db_client.post(cancel_url(booking_id), headers=headers)
    after_first = await stored(db_session, booking_id)
    # The first cancel is a real write: both timestamps move.
    assert after_first.cancelled_at is not None
    assert after_first.updated_at > created_updated_at
    snapshot = (
        after_first.status,
        after_first.refund_due,
        after_first.cancelled_at,
        after_first.updated_at,
    )
    second = await db_client.post(cancel_url(booking_id), headers=headers)
    after_second = await stored(db_session, booking_id)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert (
        after_second.status,
        after_second.refund_due,
        after_second.cancelled_at,
        after_second.updated_at,
    ) == snapshot


async def test_a_repeat_cancel_never_flips_the_refund_decision(
    db_session: AsyncSession,
) -> None:
    """A late retry (now inside the window) must not turn refund_due true into false."""
    _, stylist, service = await open_stylist(db_session)
    client, _ = await client_with_headers(db_session)
    starts_at = datetime.now(UTC) + timedelta(days=3)
    booking_id = await add_booking(db_session, stylist, service.id, client, starts_at)

    await cancel_booking(
        db_session, booking_id, client, now=starts_at - timedelta(days=2)
    )
    await cancel_booking(
        db_session, booking_id, client, now=starts_at - timedelta(hours=1)
    )

    row = await stored(db_session, booking_id)
    assert row.refund_due is True
    assert row.cancelled_at == starts_at - timedelta(days=2)


async def test_a_repeat_cancel_by_the_wrong_user_is_still_403(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The idempotent path must not leak a cancelled booking to a stranger."""
    _, stylist, service = await open_stylist(db_session)
    client_a, headers_a = await client_with_headers(db_session)
    _, headers_b = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client_a, hours_from_now(24 * 5)
    )
    await db_client.post(cancel_url(booking_id), headers=headers_a)

    resp = await db_client.post(cancel_url(booking_id), headers=headers_b)

    assert resp.status_code == 403


async def test_parallel_double_cancel_changes_state_exactly_once(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, stylist, service = await open_stylist(db_session)
    client, headers = await client_with_headers(db_session)
    booking_id = await add_booking(
        db_session, stylist, service.id, client, hours_from_now(24 * 5)
    )
    barrier = asyncio.Barrier(2)
    real = booking_service._cancel_booking

    async def meet_then_cancel(*args: Any, **kwargs: Any) -> Booking:
        # Both requests reach the row lock together; the lock must serialise them.
        await barrier.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(booking_service, "_cancel_booking", meet_then_cancel)

    resp_a, resp_b = await asyncio.wait_for(
        asyncio.gather(
            db_client.post(cancel_url(booking_id), headers=headers),
            db_client.post(cancel_url(booking_id), headers=headers),
        ),
        timeout=20,
    )

    assert (resp_a.status_code, resp_b.status_code) == (200, 200)
    assert resp_a.json() == resp_b.json()  # one transition, one cancelled_at
    row = await stored(db_session, booking_id)
    assert row.status == BookingStatus.CANCELLED
    assert row.refund_due is True
    assert row.cancelled_at is not None
    assert resp_a.json()["cancelled_at"] == row.cancelled_at.isoformat().replace(
        "+00:00", "Z"
    )


# --- the slot is free again ---


async def test_a_cancelled_booking_frees_its_slot_for_someone_else(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    _, headers_a = await client_with_headers(db_session)
    _, headers_b = await client_with_headers(db_session)
    day = upcoming(0)
    payload = body(stylist, service.id, day)

    held = await db_client.post(URL, json=payload, headers=headers_a)
    blocked = await db_client.post(URL, json=payload, headers=headers_b)
    cancelled = await db_client.post(
        cancel_url(uuid.UUID(held.json()["id"])), headers=headers_a
    )
    rebooked = await db_client.post(URL, json=payload, headers=headers_b)

    assert (held.status_code, blocked.status_code) == (201, 409)
    assert cancelled.status_code == 200
    assert rebooked.status_code == 201
    rows = await bookings_in_db(db_session)
    assert len(rows) == 2
    assert {r.status for r in rows} == {BookingStatus.PENDING, BookingStatus.CANCELLED}


async def test_service_rejects_an_unknown_booking_without_leaving_a_lock(
    db_session: AsyncSession,
) -> None:
    client, _ = await client_with_headers(db_session)

    with pytest.raises(HTTPException) as exc:
        await cancel_booking(db_session, uuid.uuid4(), client)

    assert exc.value.status_code == 404
