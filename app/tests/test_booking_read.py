import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.user import UserRole
from app.tests.payment_helpers import make_scenario
from app.tests.test_booking_cancel import add_booking, salon_owner
from app.tests.test_bookings_api import URL, client_with_headers, open_stylist
from app.tests.test_salons_api import make_user
from app.tests.test_slots import setup_stylist

# --- GET /bookings/{id} ---


async def test_client_reads_own_booking_with_payment(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(
        db_session,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == str(s.booking.id)
    assert data["status"] == "confirmed"
    assert data["refund_due"] is None
    assert data["starts_at"]
    assert data["payment_status"] == "paid"
    assert data["payment_amount"] == s.amount


async def test_booking_without_a_payment_has_null_payment_fields(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session, payment_status=None)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.status_code == 200
    assert r.json()["payment_status"] is None
    assert r.json()["payment_amount"] is None


async def test_salon_owner_reads_a_booking_at_their_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    _, owner_headers = await salon_owner(db_session, s.salon)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=owner_headers)

    assert r.status_code == 200
    assert r.json()["payment_status"] == "pending"


async def test_another_client_cannot_read_the_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    _, other_headers = await client_with_headers(db_session)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=other_headers)

    assert r.status_code == 403


async def test_another_salons_owner_cannot_read_the_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    _, other_owner_headers = await make_user(db_session, UserRole.OWNER)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=other_owner_headers)

    assert r.status_code == 403


async def test_reading_an_unknown_booking_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await client_with_headers(db_session)

    r = await db_client.get(f"{URL}/{uuid.uuid4()}", headers=headers)

    assert r.status_code == 404


async def test_reading_a_booking_requires_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)

    r = await db_client.get(f"{URL}/{s.booking.id}")

    assert r.status_code == 401


# --- GET /bookings ---


async def test_client_lists_only_their_own_bookings_newest_first(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    me, my_headers = await client_with_headers(db_session)
    other, _ = await client_with_headers(db_session)
    soon = datetime.now(UTC) + timedelta(days=2)
    later = soon + timedelta(days=3)
    first = await add_booking(db_session, stylist, service.id, me, soon)
    second = await add_booking(db_session, stylist, service.id, me, later)
    await add_booking(db_session, stylist, service.id, other, soon + timedelta(days=9))

    r = await db_client.get(URL, headers=my_headers)

    assert r.status_code == 200
    assert [b["id"] for b in r.json()] == [str(second), str(first)]


async def test_booking_list_is_paginated(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    me, headers = await client_with_headers(db_session)
    base = datetime.now(UTC) + timedelta(days=2)
    for i in range(3):
        await add_booking(db_session, stylist, service.id, me, base + timedelta(days=i))

    page1 = await db_client.get(URL, headers=headers, params={"limit": 2})
    page2 = await db_client.get(URL, headers=headers, params={"limit": 2, "offset": 2})

    assert len(page1.json()) == 2
    assert len(page2.json()) == 1
    assert not {b["id"] for b in page1.json()} & {b["id"] for b in page2.json()}


async def test_booking_list_rejects_a_bad_limit(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await client_with_headers(db_session)

    r = await db_client.get(URL, headers=headers, params={"limit": 0})

    assert r.status_code == 422


async def test_owner_cannot_use_the_client_booking_list(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session, UserRole.OWNER)

    r = await db_client.get(URL, headers=headers)

    assert r.status_code == 403


async def test_booking_list_requires_login(db_client: AsyncClient) -> None:
    r = await db_client.get(URL)

    assert r.status_code == 401


# --- review follow-ups ---


async def test_detail_response_has_exactly_the_expected_keys(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(
        db_session,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert set(r.json()) == {
        "id",
        "client_id",
        "stylist_id",
        "service_id",
        "starts_at",
        "ends_at",
        "status",
        "refund_due",
        "cancelled_at",
        "created_at",
        "payment_status",
        "payment_amount",
    }
    assert s.reference not in r.text
    assert "checkout.paystack" not in r.text


async def test_the_bookings_stylist_cannot_read_it_or_list_bookings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    stylist_headers = {"Authorization": f"Bearer {create_access_token(s.stylist.id)}"}

    detail = await db_client.get(f"{URL}/{s.booking.id}", headers=stylist_headers)
    listing = await db_client.get(URL, headers=stylist_headers)

    assert detail.status_code == 403
    assert listing.status_code == 403


async def test_owner_of_a_different_salon_cannot_read_the_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    other_salon, _, _ = await setup_stylist(db_session, 60)
    _, other_owner_headers = await salon_owner(db_session, other_salon)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=other_owner_headers)

    assert r.status_code == 403


async def add_payment(
    db_session: AsyncSession,
    booking_id: uuid.UUID,
    status: PaymentStatus,
    created_at: datetime,
    amount: int = 1000,
) -> Payment:
    payment = Payment(
        booking_id=booking_id,
        paystack_reference=f"sb_{uuid.uuid4().hex}",
        amount=amount,
        currency="NGN",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )
    db_session.add(payment)
    await db_session.commit()
    return payment


async def test_the_newest_payment_is_the_one_shown(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session, payment_status=None)
    now = datetime.now(UTC)
    await add_payment(
        db_session, s.booking.id, PaymentStatus.FAILED, now - timedelta(hours=2), 1111
    )
    await add_payment(
        db_session, s.booking.id, PaymentStatus.PENDING, now - timedelta(hours=1), 2222
    )

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.json()["payment_status"] == "pending"
    assert r.json()["payment_amount"] == 2222


async def test_a_refunded_payment_is_shown_when_it_is_the_newest(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session, payment_status=None)
    now = datetime.now(UTC)
    await add_payment(
        db_session, s.booking.id, PaymentStatus.FAILED, now - timedelta(hours=2)
    )
    await add_payment(
        db_session, s.booking.id, PaymentStatus.REFUNDED, now - timedelta(hours=1)
    )

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.json()["payment_status"] == "refunded"


async def test_payments_with_the_same_timestamp_resolve_by_id(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session, payment_status=None)
    same = datetime.now(UTC)
    first = await add_payment(db_session, s.booking.id, PaymentStatus.FAILED, same)
    second = await add_payment(db_session, s.booking.id, PaymentStatus.PENDING, same)
    winner = max(first, second, key=lambda p: p.id)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.json()["payment_status"] == winner.status.value


async def test_malformed_booking_id_is_422(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await client_with_headers(db_session)

    r = await db_client.get(f"{URL}/not-a-uuid", headers=headers)

    assert r.status_code == 422


@pytest.mark.parametrize(
    "params", [{"limit": 101}, {"limit": -1}, {"offset": -1}, {"offset": 10001}]
)
async def test_booking_list_rejects_out_of_range_paging(
    db_client: AsyncClient, db_session: AsyncSession, params: dict[str, int]
) -> None:
    _, headers = await client_with_headers(db_session)

    r = await db_client.get(URL, headers=headers, params=params)

    assert r.status_code == 422


async def test_booking_list_includes_cancelled_bookings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await open_stylist(db_session)
    me, headers = await client_with_headers(db_session)
    when = datetime.now(UTC) + timedelta(days=2)
    cancelled = await add_booking(
        db_session, stylist, service.id, me, when, BookingStatus.CANCELLED
    )

    r = await db_client.get(URL, headers=headers)

    assert [(b["id"], b["status"]) for b in r.json()] == [(str(cancelled), "cancelled")]
