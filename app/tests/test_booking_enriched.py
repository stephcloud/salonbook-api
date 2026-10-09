"""GET /bookings and GET /bookings/{id}: nested salon, service and stylist summaries."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self

from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.service import PriceType, Service
from app.tests.payment_helpers import make_scenario
from app.tests.test_booking_cancel import add_booking, salon_owner
from app.tests.test_bookings_api import URL, client_with_headers, open_stylist

SALON_KEYS = {
    "id",
    "name",
    "address",
    "phone",
    "cancellation_hours",
    "deposit_amount",
    "image_url",
}
SERVICE_KEYS = {"id", "name", "duration_minutes", "price_type", "price"}
STYLIST_KEYS = {"id", "name", "image_url"}


def assert_nothing_private(text: str, *secrets: str) -> None:
    for secret in secrets:
        assert secret not in text
    for word in ("email", "password", "argon2", "owner_id", "paystack", "checkout"):
        assert word not in text.lower()


async def test_detail_includes_salon_service_and_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(
        db_session,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )
    service = await db_session.get(Service, s.booking.service_id)
    assert service is not None
    s.salon.image_url = "https://img.example.com/salon.jpg"
    s.stylist.image_url = "https://img.example.com/stylist.jpg"
    await db_session.commit()

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.status_code == 200
    data = r.json()
    assert data["salon"] == {
        "id": str(s.salon.id),
        "name": s.salon.name,
        "address": s.salon.address,
        "phone": s.salon.phone,
        "cancellation_hours": s.salon.cancellation_hours,
        "deposit_amount": s.salon.deposit_amount,
        "image_url": "https://img.example.com/salon.jpg",
    }
    assert data["service"] == {
        "id": str(service.id),
        "name": service.name,
        "duration_minutes": service.duration_minutes,
        "price_type": "fixed",
        "price": service.price,
    }
    assert data["stylist"] == {
        "id": str(s.stylist.id),
        "name": s.stylist.name,
        "image_url": "https://img.example.com/stylist.jpg",
    }
    # The flat fields and payment summary are unchanged.
    assert data["payment_status"] == "paid"
    assert data["payment_amount"] == s.amount
    assert data["stylist_id"] == str(s.stylist.id)


async def test_quote_service_has_null_price_and_images_may_be_null(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session, payment_status=None)
    service = await db_session.get(Service, s.booking.service_id)
    assert service is not None
    service.price_type = PriceType.QUOTE
    service.price = None
    await db_session.commit()

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    data = r.json()
    assert data["service"]["price_type"] == "quote"
    assert data["service"]["price"] is None
    assert data["salon"]["image_url"] is None
    assert data["stylist"]["image_url"] is None
    assert data["payment_status"] is None


async def test_list_includes_the_nested_summaries(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    me, headers = await client_with_headers(db_session)
    await add_booking(
        db_session, stylist, service.id, me, datetime.now(UTC) + timedelta(days=2)
    )

    r = await db_client.get(URL, headers=headers)

    assert r.status_code == 200
    [item] = r.json()
    assert item["salon"]["id"] == str(salon.id)
    assert item["salon"]["name"] == salon.name
    assert item["service"]["id"] == str(service.id)
    assert item["service"]["duration_minutes"] == service.duration_minutes
    assert item["stylist"]["id"] == str(stylist.id)
    assert item["stylist"]["name"] == stylist.name
    assert "payment_status" not in item  # the list is unchanged apart from the nesting


async def test_nested_objects_have_exactly_the_expected_keys(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)

    detail = (await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)).json()
    listing = (await db_client.get(URL, headers=s.headers)).json()[0]

    for body in (detail, listing):
        assert set(body["salon"]) == SALON_KEYS
        assert set(body["service"]) == SERVICE_KEYS
        assert set(body["stylist"]) == STYLIST_KEYS


async def test_responses_leak_no_private_field(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(
        db_session,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )
    owner, _ = await salon_owner(db_session, s.salon)

    detail = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)
    listing = await db_client.get(URL, headers=s.headers)

    for r in (detail, listing):
        assert r.status_code == 200
        assert_nothing_private(
            r.text,
            s.reference,
            s.client.email,
            s.client.password_hash,
            s.stylist.email,
            s.stylist.password_hash,
            owner.email,
            str(owner.id),
        )


async def test_another_client_cannot_read_my_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    _, other_headers = await client_with_headers(db_session)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=other_headers)

    assert r.status_code == 403
    assert s.salon.name not in r.text


async def test_another_client_does_not_see_my_booking_in_their_list(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await make_scenario(db_session)
    _, other_headers = await client_with_headers(db_session)

    r = await db_client.get(URL, headers=other_headers)

    assert r.status_code == 200
    assert r.json() == []


async def test_salon_owner_still_reads_the_enriched_booking(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    _, owner_headers = await salon_owner(db_session, s.salon)

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=owner_headers)

    assert r.status_code == 200
    assert r.json()["salon"]["id"] == str(s.salon.id)


async def test_enriched_reads_require_login(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)

    detail = await db_client.get(f"{URL}/{s.booking.id}")
    listing = await db_client.get(URL)

    assert detail.status_code == 401
    assert listing.status_code == 401


async def test_detail_shows_the_latest_payment_when_there_are_several(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    now = datetime.now(UTC)
    s = await make_scenario(db_session, payment_status=None)
    for age_days, status, amount in (
        # Only one pending/paid payment may exist per booking, so the older ones are
        # not live. Listed out of order so row order cannot explain the result.
        (1, PaymentStatus.PAID, 333),  # the latest
        (3, PaymentStatus.FAILED, 111),
        (2, PaymentStatus.REFUNDED, 222),
    ):
        db_session.add(
            Payment(
                booking_id=s.booking.id,
                paystack_reference=f"sb_{uuid.uuid4().hex}",
                amount=amount,
                currency="NGN",
                status=status,
                created_at=now - timedelta(days=age_days),
                updated_at=now - timedelta(days=age_days),
            )
        )
    await db_session.commit()

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.status_code == 200
    assert r.json()["payment_status"] == "paid"
    assert r.json()["payment_amount"] == 333


async def test_salon_comes_from_the_service_not_the_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    s = await make_scenario(db_session)
    service = await db_session.get(Service, s.booking.service_id)
    assert service is not None

    r = await db_client.get(f"{URL}/{s.booking.id}", headers=s.headers)

    assert r.json()["salon"]["id"] == str(service.salon_id) == str(s.salon.id)


# Auth user lookup + the joined booking query (+ the latest payment on the detail).
LIST_MAX_QUERIES = 2
DETAIL_MAX_QUERIES = 3


class QueryCounter:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine.sync_engine
        self.statements: list[str] = []

    def _record(self, *args: Any) -> None:
        self.statements.append(args[2])

    def __enter__(self) -> Self:
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    @property
    def count(self) -> int:
        return len(self.statements)


async def test_list_query_count_does_not_grow_with_the_number_of_bookings(
    db_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    _, stylist, service = await open_stylist(db_session)
    me, headers = await client_with_headers(db_session)
    base = datetime.now(UTC) + timedelta(days=2)

    await add_booking(db_session, stylist, service.id, me, base)
    with QueryCounter(db_engine) as with_one:
        r1 = await db_client.get(URL, headers=headers)
    for i in range(1, 5):
        await add_booking(db_session, stylist, service.id, me, base + timedelta(days=i))
    with QueryCounter(db_engine) as with_five:
        r5 = await db_client.get(URL, headers=headers)

    assert len(r1.json()) == 1
    assert len(r5.json()) == 5
    assert with_five.count == with_one.count
    assert with_five.count <= LIST_MAX_QUERIES


async def test_detail_query_count_does_not_grow_with_the_number_of_bookings(
    db_client: AsyncClient, db_session: AsyncSession, db_engine: AsyncEngine
) -> None:
    _, stylist, service = await open_stylist(db_session)
    me, headers = await client_with_headers(db_session)
    base = datetime.now(UTC) + timedelta(days=2)

    first = await add_booking(db_session, stylist, service.id, me, base)
    with QueryCounter(db_engine) as with_one:
        r1 = await db_client.get(f"{URL}/{first}", headers=headers)
    for i in range(1, 5):
        await add_booking(db_session, stylist, service.id, me, base + timedelta(days=i))
    with QueryCounter(db_engine) as with_five:
        r5 = await db_client.get(f"{URL}/{first}", headers=headers)

    assert r1.status_code == r5.status_code == 200
    assert with_five.count == with_one.count
    assert with_five.count <= DETAIL_MAX_QUERIES
