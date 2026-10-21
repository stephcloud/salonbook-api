"""Only approved salons are public, bookable and payable. Owners always see their own."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import JWT_ALGORITHM, create_access_token
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.salon import Salon, SalonStatus
from app.models.stylist_service import StylistService
from app.models.user import UserRole
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
from app.tests.test_services_api import make_service
from app.tests.test_slots import upcoming
from app.tests.test_stylists_api import make_stylist

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


SETUP_READS = ("/services", "/stylists")


async def salon_with_setup(
    db_session: AsyncSession, salon_status: SalonStatus
) -> tuple[Salon, dict[str, str]]:
    """A salon with one service and one stylist offering it, and its owner's headers."""
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=salon_status)
    service = await make_service(db_session, salon)
    stylist = await make_stylist(db_session, salon)
    db_session.add(StylistService(stylist_id=stylist.id, service_id=service.id))
    await db_session.commit()
    return salon, headers


@pytest.mark.parametrize("hidden", HIDDEN)
@pytest.mark.parametrize("suffix", SETUP_READS)
async def test_the_owner_can_read_a_hidden_salons_services_and_stylists(
    db_client: AsyncClient, db_session: AsyncSession, hidden: SalonStatus, suffix: str
) -> None:
    salon, headers = await salon_with_setup(db_session, hidden)
    resp = await db_client.get(f"{SALONS}/{salon.id}{suffix}", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# Each outsider gets the hidden salon, so the salon's own stylist can be one of them.
Outsider = Callable[[AsyncSession, Salon], Awaitable[dict[str, str]]]


async def _other_owner(db_session: AsyncSession, _: Salon) -> dict[str, str]:
    other, headers = await make_user(db_session)
    await make_salon(db_session, other, name="Other")
    return headers


async def _client(db_session: AsyncSession, _: Salon) -> dict[str, str]:
    return (await make_user(db_session, UserRole.CLIENT))[1]


async def _admin(db_session: AsyncSession, _: Salon) -> dict[str, str]:
    return (await make_user(db_session, UserRole.ADMIN))[1]


async def _the_salons_stylist(db_session: AsyncSession, salon: Salon) -> dict[str, str]:
    stylist = await make_stylist(db_session, salon)
    return {"Authorization": f"Bearer {create_access_token(stylist.id)}"}


async def _anonymous(_: AsyncSession, __: Salon) -> dict[str, str]:
    return {}


async def _garbage_token(_: AsyncSession, __: Salon) -> dict[str, str]:
    return {"Authorization": "Bearer not-a-jwt"}


async def _basic_scheme(_: AsyncSession, __: Salon) -> dict[str, str]:
    return {"Authorization": "Basic dXNlcjpwYXNz"}


async def _unknown_user_token(_: AsyncSession, __: Salon) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(uuid.uuid4())}"}


async def _owners_expired_token(_: AsyncSession, salon: Salon) -> dict[str, str]:
    # The right owner, but the token has lapsed: treated as anonymous, not a 401.
    past = datetime.now(UTC) - timedelta(hours=2)
    token = jwt.encode(
        {"sub": str(salon.owner_id), "iat": past, "exp": past + timedelta(hours=1)},
        settings.SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    return {"Authorization": f"Bearer {token}"}


OUTSIDERS: list[Outsider] = [
    _other_owner,
    _client,
    _admin,
    _the_salons_stylist,
    _anonymous,
    _garbage_token,
    _basic_scheme,
    _unknown_user_token,
    _owners_expired_token,
]


def _name(f: Outsider) -> str:
    return f.__name__.strip("_")


@pytest.mark.parametrize("hidden", HIDDEN)
@pytest.mark.parametrize("suffix", SETUP_READS)
@pytest.mark.parametrize("outsider", OUTSIDERS, ids=_name)
async def test_anyone_but_the_owner_gets_404_for_a_hidden_salon(
    db_client: AsyncClient,
    db_session: AsyncSession,
    hidden: SalonStatus,
    suffix: str,
    outsider: Outsider,
) -> None:
    salon, _ = await salon_with_setup(db_session, hidden)
    headers = await outsider(db_session, salon)
    resp = await db_client.get(f"{SALONS}/{salon.id}{suffix}", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "salon not found"}


@pytest.mark.parametrize("suffix", SETUP_READS)
@pytest.mark.parametrize("viewer", [_anonymous, _client, _garbage_token], ids=_name)
async def test_an_approved_salons_services_and_stylists_stay_public(
    db_client: AsyncClient,
    db_session: AsyncSession,
    suffix: str,
    viewer: Outsider,
) -> None:
    salon, owner_headers = await salon_with_setup(db_session, SalonStatus.APPROVED)
    for headers in (await viewer(db_session, salon), owner_headers):
        resp = await db_client.get(f"{SALONS}/{salon.id}{suffix}", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1


async def test_an_unknown_salon_is_still_404_for_a_signed_in_owner(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    for suffix in SETUP_READS:
        resp = await db_client.get(f"{SALONS}/{uuid.uuid4()}{suffix}", headers=headers)
        assert resp.status_code == 404


@pytest.mark.parametrize(
    "path", ["/salons/{salon_id}/services", "/salons/{salon_id}/stylists"]
)
async def test_the_owner_reads_add_nothing_to_the_openapi_schema(
    client: AsyncClient, path: str
) -> None:
    operation = (await client.get("/openapi.json")).json()["paths"][f"/api/v1{path}"][
        "get"
    ]
    assert "security" not in operation
    assert sorted(p["name"] for p in operation["parameters"]) == [
        "limit",
        "offset",
        "salon_id",
    ]


async def test_the_owners_view_of_a_setup_read_is_never_shared_cacheable(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    # The owner gets 200 where everyone else gets 404 for the same URL. Today nothing
    # sets cache headers; if anything ever does, a signed-in response must stay
    # private and no response may carry a validator a shared cache could reuse.
    salon, headers = await salon_with_setup(db_session, SalonStatus.PENDING)
    for suffix in SETUP_READS:
        for h in (headers, {}):
            resp = await db_client.get(f"{SALONS}/{salon.id}{suffix}", headers=h)
            for name in ("etag", "last-modified", "expires"):
                assert name not in resp.headers, (suffix, name)
            cache_control = resp.headers.get("cache-control")
            if h and cache_control is not None:
                assert "private" in cache_control and "no-store" in cache_control
            if cache_control is not None:
                assert "public" not in cache_control and "s-maxage" not in cache_control


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
