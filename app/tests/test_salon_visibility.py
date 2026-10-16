"""Only approved salons are public, bookable and payable. Owners always see their own."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.salon import Salon, SalonStatus
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import make_scenario
from app.tests.paystack_fakes import FakePaystack
from app.tests.test_booking_emails import pay
from app.tests.test_bookings_api import (
    URL as BOOKINGS,
)
from app.tests.test_bookings_api import (
    body,
    client_with_headers,
    open_stylist,
)
from app.tests.test_salons_api import SALON, SALONS, make_salon, make_user
from app.tests.test_slots import upcoming

NOT_ACCEPTING = "salon is not accepting bookings"
HIDDEN = [SalonStatus.PENDING, SalonStatus.SUSPENDED]


@pytest.fixture(autouse=True)
def _pin_salon_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SALON_UTC_OFFSET_MINUTES", 60)


async def set_status(
    db_session: AsyncSession, salon: Salon, new_status: SalonStatus
) -> None:
    salon.status = new_status
    db_session.add(salon)
    await db_session.commit()


# --- a new salon starts pending ---


async def test_a_new_salon_starts_pending_and_the_owner_is_told(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(SALONS, json=SALON, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["status"] == "pending"
    public = await db_client.get(SALONS)
    assert public.json() == []


async def test_an_owner_cannot_create_a_salon_as_approved(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS, json={**SALON, "status": "approved"}, headers=headers
    )
    assert resp.status_code == 422


# --- public reads ---


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_non_approved_salons_are_404_publicly(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=hidden)
    shown = await make_salon(db_session, owner, name="Visible")

    listing = await db_client.get(SALONS)
    assert [s["id"] for s in listing.json()] == [str(shown.id)]
    for suffix in ("", "/services", "/stylists"):
        resp = await db_client.get(f"{SALONS}/{salon.id}{suffix}")
        assert resp.status_code == 404, suffix
    for suffix in ("", "/services", "/stylists"):
        resp = await db_client.get(f"{SALONS}/{shown.id}{suffix}")
        assert resp.status_code == 200, suffix


async def test_public_salon_responses_never_carry_a_status(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    for url in (SALONS, f"{SALONS}/{salon.id}"):
        resp = await db_client.get(url)
        assert "status" not in resp.text


# --- the owner still sees and edits their own ---


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_the_owner_sees_their_hidden_salon_with_its_status(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=hidden)
    mine = await db_client.get(f"{SALONS}/mine", headers=headers)
    assert [(s["id"], s["status"]) for s in mine.json()] == [(str(salon.id), hidden)]


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_the_owner_can_still_edit_and_set_up_a_hidden_salon(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=hidden)
    patched = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == hidden
    service = await db_client.post(
        f"{SALONS}/{salon.id}/services",
        json={"name": "Braids", "duration_minutes": 60, "price_type": "quote"},
        headers=headers,
    )
    assert service.status_code == 201
    owner_bookings = await db_client.get(
        f"{SALONS}/{salon.id}/bookings", headers=headers
    )
    assert owner_bookings.status_code == 200


# --- slots, booking and payment are refused ---


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_slots_are_refused_for_a_non_approved_salon(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    params = {"service_id": str(service.id), "date": upcoming(0).isoformat()}
    url = f"/api/v1/stylists/{stylist.id}/slots"
    assert (await db_client.get(url, params=params, headers=headers)).status_code == 200

    await set_status(db_session, salon, hidden)

    resp = await db_client.get(url, params=params, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == NOT_ACCEPTING


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_booking_is_refused_for_a_non_approved_salon(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus
) -> None:
    salon, stylist, service = await open_stylist(db_session)
    _, headers = await client_with_headers(db_session)
    await set_status(db_session, salon, hidden)

    resp = await db_client.post(
        BOOKINGS, json=body(stylist, service.id, upcoming(0)), headers=headers
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == NOT_ACCEPTING
    assert (await db_session.execute(select(Booking))).first() is None


@pytest.mark.parametrize("hidden", HIDDEN)
async def test_payment_is_refused_for_a_non_approved_salon(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_paystack: FakePaystack,
    hidden: SalonStatus,
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    await set_status(db_session, scenario.salon, hidden)

    resp = await db_client.post(
        f"{BOOKINGS}/{scenario.booking.id}/pay", headers=scenario.headers
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == NOT_ACCEPTING
    assert fake_paystack.initialize_calls == []


async def test_payment_works_again_once_the_salon_is_approved(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    await set_status(db_session, scenario.salon, SalonStatus.SUSPENDED)
    url = f"{BOOKINGS}/{scenario.booking.id}/pay"
    assert (await db_client.post(url, headers=scenario.headers)).status_code == 409

    await set_status(db_session, scenario.salon, SalonStatus.APPROVED)

    assert (await db_client.post(url, headers=scenario.headers)).status_code == 200


# --- existing bookings stay untouched ---


async def test_suspending_a_salon_leaves_existing_bookings_alone(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )
    await set_status(db_session, scenario.salon, SalonStatus.SUSPENDED)

    detail = await db_client.get(
        f"{BOOKINGS}/{scenario.booking.id}", headers=scenario.headers
    )
    assert detail.status_code == 200
    assert detail.json()["status"] == "confirmed"
    mine = await db_client.get(BOOKINGS, headers=scenario.headers)
    assert [b["id"] for b in mine.json()] == [str(scenario.booking.id)]

    cancelled = await db_client.post(
        f"{BOOKINGS}/{scenario.booking.id}/cancel", headers=scenario.headers
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


async def test_the_webhook_still_confirms_a_payment_for_a_suspended_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)  # pending booking, pending payment
    await set_status(db_session, scenario.salon, SalonStatus.SUSPENDED)

    resp = await pay(db_client, scenario)

    assert resp.status_code == 200
    detail = await db_client.get(
        f"{BOOKINGS}/{scenario.booking.id}", headers=scenario.headers
    )
    assert detail.json()["status"] == "confirmed"
