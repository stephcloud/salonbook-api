import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.user import User, UserRole
from app.tests.test_booking_cancel import add_booking, salon_owner
from app.tests.test_salons_api import make_user
from app.tests.test_slots import setup_stylist

SECRET_NEEDLES = (
    "email",
    "password",
    "paystack_reference",
    "reference",
    "access_code",
    "accesscode999",
    "authorization_url",
    "checkout.paystack",
    "secretref123",
)


@pytest.fixture(autouse=True)
def _pin_salon_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SALON_UTC_OFFSET_MINUTES", 60)


def url(salon: Salon) -> str:
    return f"/api/v1/salons/{salon.id}/bookings"


def at(days: int, hour: int = 10) -> datetime:
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return base.replace(hour=hour) + timedelta(days=days)


async def salon_with_bookings(
    db_session: AsyncSession, starts: list[datetime]
) -> tuple[Salon, User, Service, dict[str, str], list[uuid.UUID]]:
    """A salon with one stylist and one booking per start (distinct client each)."""
    salon, stylist, service = await setup_stylist(db_session, 60)
    _, headers = await salon_owner(db_session, salon)
    ids = []
    for starts_at in starts:
        client, _ = await make_user(db_session, UserRole.CLIENT)
        ids.append(
            await add_booking(db_session, stylist, service.id, client, starts_at)
        )
    return salon, stylist, service, headers, ids


async def add_payment(
    db_session: AsyncSession, booking_id: uuid.UUID, status: PaymentStatus
) -> None:
    db_session.add(
        Payment(
            booking_id=booking_id,
            paystack_reference="sb_secretref123",
            amount=1000,
            currency="NGN",
            status=status,
            authorization_url="https://checkout.paystack.test/abc",
            access_code="accesscode999",
        )
    )
    await db_session.commit()


async def test_owner_sees_only_their_salons_bookings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, headers, ids = await salon_with_bookings(
        db_session, [at(3), at(1), at(2)]
    )
    await salon_with_bookings(db_session, [at(1), at(2)])  # another salon

    r = await db_client.get(url(salon), headers=headers)

    assert r.status_code == 200
    got = [item["id"] for item in r.json()]
    assert got == [str(ids[1]), str(ids[2]), str(ids[0])]  # ordered by starts_at


async def test_item_shape_and_payment_status(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service, headers, ids = await salon_with_bookings(
        db_session, [at(2)]
    )
    booking = await db_session.get(Booking, ids[0])
    assert booking is not None
    client = await db_session.get(User, booking.client_id)
    assert client is not None
    await add_payment(db_session, ids[0], PaymentStatus.PAID)

    r = await db_client.get(url(salon), headers=headers)

    [item] = r.json()
    assert set(item) == {
        "id",
        "status",
        "starts_at",
        "ends_at",
        "refund_due",
        "payment_status",
        "stylist",
        "service",
        "client_name",
    }
    assert item["status"] == "confirmed"
    assert item["payment_status"] == "paid"
    assert item["stylist"] == {"id": str(stylist.id), "name": stylist.name}
    assert item["service"] == {
        "id": str(service.id),
        "name": service.name,
        "duration_minutes": 60,
    }
    assert item["client_name"] == client.name


async def test_booking_without_payment_has_null_payment_status(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, headers, _ = await salon_with_bookings(db_session, [at(1)])
    r = await db_client.get(url(salon), headers=headers)
    assert r.json()[0]["payment_status"] is None


async def test_response_leaks_no_email_or_paystack_fields(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, _, headers, ids = await salon_with_bookings(db_session, [at(1)])
    booking = await db_session.get(Booking, ids[0])
    assert booking is not None
    client = await db_session.get(User, booking.client_id)
    assert client is not None
    await add_payment(db_session, ids[0], PaymentStatus.PENDING)

    text = (await db_client.get(url(salon), headers=headers)).text.lower()

    for secret in (client.email, stylist.email, client.password_hash):
        assert secret.lower() not in text
    for needle in SECRET_NEEDLES:
        assert needle not in text


async def test_includes_cancelled_and_expired_bookings(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service, headers, _ = await salon_with_bookings(db_session, [])
    statuses = (BookingStatus.CANCELLED, BookingStatus.PENDING, BookingStatus.COMPLETED)
    for i, st in enumerate(statuses):
        client, _ = await make_user(db_session, UserRole.CLIENT)
        await add_booking(db_session, stylist, service.id, client, at(i + 1), st)

    r = await db_client.get(url(salon), headers=headers)

    assert [i["status"] for i in r.json()] == ["cancelled", "pending", "completed"]


async def test_another_owner_gets_403(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, _, _ = await salon_with_bookings(db_session, [at(1)])
    _, other_headers = await make_user(db_session, UserRole.OWNER)
    assert (await db_client.get(url(salon), headers=other_headers)).status_code == 403


async def test_client_gets_403(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, _, _ = await salon_with_bookings(db_session, [at(1)])
    _, client_headers = await make_user(db_session, UserRole.CLIENT)
    assert (await db_client.get(url(salon), headers=client_headers)).status_code == 403


async def test_stylist_gets_403(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, _, _, _ = await salon_with_bookings(db_session, [at(1)])
    headers = {"Authorization": f"Bearer {create_access_token(stylist.id)}"}
    assert (await db_client.get(url(salon), headers=headers)).status_code == 403


async def test_no_token_gets_401(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, _, _ = await salon_with_bookings(db_session, [at(1)])
    assert (await db_client.get(url(salon))).status_code == 401


async def test_unknown_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session, UserRole.OWNER)
    r = await db_client.get(f"/api/v1/salons/{uuid.uuid4()}/bookings", headers=headers)
    assert r.status_code == 404


async def test_pagination(db_client: AsyncClient, db_session: AsyncSession) -> None:
    salon, _, _, headers, ids = await salon_with_bookings(
        db_session, [at(1), at(2), at(3)]
    )

    first = await db_client.get(url(salon), params={"limit": 2}, headers=headers)
    second = await db_client.get(
        url(salon), params={"limit": 2, "offset": 2}, headers=headers
    )

    assert [i["id"] for i in first.json()] == [str(ids[0]), str(ids[1])]
    assert [i["id"] for i in second.json()] == [str(ids[2])]


async def test_default_limit_is_20_and_max_is_100(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, headers, _ = await salon_with_bookings(
        db_session, [at(d) for d in range(1, 23)]
    )
    default = await db_client.get(url(salon), headers=headers)
    assert len(default.json()) == 20
    too_big = await db_client.get(url(salon), params={"limit": 101}, headers=headers)
    assert too_big.status_code == 422


async def test_status_filter(db_client: AsyncClient, db_session: AsyncSession) -> None:
    salon, stylist, service, headers, _ = await salon_with_bookings(db_session, [at(1)])
    client, _ = await make_user(db_session, UserRole.CLIENT)
    cancelled = await add_booking(
        db_session, stylist, service.id, client, at(2), BookingStatus.CANCELLED
    )

    r = await db_client.get(url(salon), params={"status": "cancelled"}, headers=headers)

    assert [i["id"] for i in r.json()] == [str(cancelled)]
    bad = await db_client.get(url(salon), params={"status": "nope"}, headers=headers)
    assert bad.status_code == 422


async def test_stylist_filter_and_other_salons_stylist_returns_nothing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, _, headers, ids = await salon_with_bookings(db_session, [at(1)])
    _, foreign_stylist, _, _, _ = await salon_with_bookings(db_session, [at(1)])

    mine = await db_client.get(
        url(salon), params={"stylist_id": str(stylist.id)}, headers=headers
    )
    foreign = await db_client.get(
        url(salon), params={"stylist_id": str(foreign_stylist.id)}, headers=headers
    )

    assert [i["id"] for i in mine.json()] == [str(ids[0])]
    assert foreign.status_code == 200
    assert foreign.json() == []


async def test_date_range_filter_is_inclusive_in_salon_time(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # Salon offset is +01:00. 23:30 UTC on the 10th is 00:30 local on the 11th.
    t = datetime(2031, 3, 10, 23, 30, tzinfo=UTC)
    starts = [t - timedelta(days=1), t, t + timedelta(days=1), t + timedelta(days=2)]
    salon, _, _, headers, ids = await salon_with_bookings(db_session, starts)

    r = await db_client.get(
        url(salon), params={"from": "2031-03-11", "to": "2031-03-12"}, headers=headers
    )
    only_from = await db_client.get(
        url(salon), params={"from": "2031-03-12"}, headers=headers
    )
    only_to = await db_client.get(
        url(salon), params={"to": "2031-03-10"}, headers=headers
    )

    assert [i["id"] for i in r.json()] == [str(ids[1]), str(ids[2])]
    assert [i["id"] for i in only_from.json()] == [str(ids[2]), str(ids[3])]
    assert [i["id"] for i in only_to.json()] == [str(ids[0])]


async def test_from_after_to_is_422(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, _, headers, _ = await salon_with_bookings(db_session, [])
    r = await db_client.get(
        url(salon), params={"from": "2031-03-12", "to": "2031-03-11"}, headers=headers
    )
    assert r.status_code == 422
