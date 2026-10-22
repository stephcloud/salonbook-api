import asyncio
import uuid
from datetime import UTC, date, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.availability_rule import AvailabilityRule, RuleKind
from app.models.booking import BookingStatus
from app.models.salon import Salon, SalonStatus
from app.models.service import Service
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole
from app.schemas.stylist import StylistUpdate
from app.services import stylists as stylist_service
from app.tests.test_booking_cancel import add_booking, salon_owner
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import owner_and_salon
from app.tests.test_slots import setup_stylist
from app.tests.test_stylists_api import make_stylist, stylists_url

URL = "https://cdn.example.com/stylists/ada.jpg"


def stylist_url(stylist_id: uuid.UUID) -> str:
    return f"/api/v1/stylists/{stylist_id}"


async def reloaded(db_session: AsyncSession, stylist: User) -> User | None:
    return await db_session.get(User, stylist.id, populate_existing=True)


async def count(db_session: AsyncSession, model: type, stylist: User) -> int:
    result = await db_session.scalar(
        select(func.count()).select_from(model).where(model.stylist_id == stylist.id)
    )
    return result or 0


async def add_rules(db_session: AsyncSession, stylist: User) -> None:
    """One of each kind: working hours, a break and a day off."""
    db_session.add_all(
        [
            AvailabilityRule(
                stylist_id=stylist.id,
                kind=RuleKind.WORKING,
                weekday=0,
                start_time=time(9),
                end_time=time(17),
            ),
            AvailabilityRule(
                stylist_id=stylist.id,
                kind=RuleKind.BREAK,
                weekday=0,
                start_time=time(12),
                end_time=time(13),
            ),
            AvailabilityRule(
                stylist_id=stylist.id, kind=RuleKind.DAY_OFF, off_date=date(2030, 1, 1)
            ),
        ]
    )
    await db_session.commit()


# --- PATCH /stylists/{id} ---


async def test_owner_updates_name_and_image(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    resp = await db_client.patch(
        stylist_url(stylist.id),
        json={"name": "  Ada Lovelace  ", "image_url": URL},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Ada Lovelace"
    assert body["image_url"] == URL
    assert body["email"] == stylist.email
    assert body["salon_id"] == str(salon.id)
    assert "password" not in body and "password_hash" not in body
    saved = await reloaded(db_session, stylist)
    assert saved is not None
    assert (saved.name, saved.image_url) == ("Ada Lovelace", URL)


async def test_patch_null_image_clears_it_and_null_name_is_ignored(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    stylist.image_url = URL
    db_session.add(stylist)
    await db_session.commit()

    resp = await db_client.patch(
        stylist_url(stylist.id), json={"name": None, "image_url": None}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["image_url"] is None
    assert resp.json()["name"] == stylist.name


async def test_patch_leaves_unsent_fields_alone(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    stylist.image_url = URL
    db_session.add(stylist)
    await db_session.commit()

    resp = await db_client.patch(
        stylist_url(stylist.id), json={"name": "Bea"}, headers=headers
    )

    assert resp.status_code == 200
    assert resp.json()["image_url"] == URL


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"image_url": "http://cdn.example.com/a.jpg"},
        {"image_url": "https://cdn.example.com/a b.jpg"},
        {"image_url": "https://" + "a" * 493},
        {"email": "new@example.com"},
        {"password": "correct-horse"},
        {"role": "owner"},
        {"salon_id": str(uuid.uuid4())},
    ],
)
async def test_patch_validates_input(
    db_client: AsyncClient, db_session: AsyncSession, body: dict[str, str]
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    resp = await db_client.patch(stylist_url(stylist.id), json=body, headers=headers)

    assert resp.status_code == 422
    saved = await reloaded(db_session, stylist)
    assert saved is not None
    assert (saved.name, saved.image_url, saved.salon_id) == ("T", None, salon.id)


async def test_owner_a_cannot_patch_owner_b_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon_b)
    _, headers_a = await make_user(db_session, UserRole.OWNER)

    resp = await db_client.patch(
        stylist_url(stylist.id), json={"name": "Hacked"}, headers=headers_a
    )

    assert resp.status_code == 403
    saved = await reloaded(db_session, stylist)
    assert saved is not None and saved.name == "T"


# --- DELETE /stylists/{id} ---


async def test_owner_deletes_stylist_and_their_rules_and_links(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist)
    _, headers = await salon_owner(db_session, salon)
    assert await count(db_session, AvailabilityRule, stylist) == 3
    assert await count(db_session, StylistService, stylist) == 1

    resp = await db_client.delete(stylist_url(stylist.id), headers=headers)

    assert resp.status_code == 204
    assert resp.content == b""
    assert await reloaded(db_session, stylist) is None
    assert await count(db_session, AvailabilityRule, stylist) == 0
    assert await count(db_session, StylistService, stylist) == 0
    # The service itself stays.
    assert await db_session.get(Service, service.id, populate_existing=True)
    listed = await db_client.get(stylists_url(salon))
    assert listed.json() == []


async def test_deleted_stylist_can_no_longer_sign_in(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist, stylist_headers = await make_user(db_session, UserRole.STYLIST)
    stylist.salon_id = salon.id
    db_session.add(stylist)
    await db_session.commit()

    resp = await db_client.delete(stylist_url(stylist.id), headers=headers)

    assert resp.status_code == 204
    me = await db_client.get("/api/v1/auth/me", headers=stylist_headers)
    assert me.status_code == 401


@pytest.mark.parametrize(
    "booking_status",
    [
        BookingStatus.PENDING,
        BookingStatus.CONFIRMED,
        BookingStatus.CANCELLED,
        BookingStatus.COMPLETED,
        BookingStatus.NO_SHOW,
    ],
)
async def test_deleting_a_stylist_with_bookings_is_409_and_keeps_everything(
    db_client: AsyncClient, db_session: AsyncSession, booking_status: BookingStatus
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    await add_booking(
        db_session,
        stylist,
        service.id,
        client,
        datetime.now(UTC) + timedelta(days=3),
        status=booking_status,
    )
    _, headers = await salon_owner(db_session, salon)

    resp = await db_client.delete(stylist_url(stylist.id), headers=headers)

    assert resp.status_code == 409
    assert resp.json() == {"detail": "This stylist has bookings and cannot be deleted."}
    assert await reloaded(db_session, stylist) is not None
    assert await count(db_session, AvailabilityRule, stylist) == 3
    assert await count(db_session, StylistService, stylist) == 1
    # The rolled-back request leaves the API usable for the same owner.
    patch = await db_client.patch(
        stylist_url(stylist.id), json={"name": "Still here"}, headers=headers
    )
    assert patch.status_code == 200


async def test_owner_a_cannot_delete_owner_b_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon_b)
    _, headers_a = await make_user(db_session, UserRole.OWNER)

    resp = await db_client.delete(stylist_url(stylist.id), headers=headers_a)

    assert resp.status_code == 403
    assert await reloaded(db_session, stylist) is not None


# --- both routes: auth, roles, unknown ids, salon status ---

METHODS = ["PATCH", "DELETE"]


def _request_kwargs(method: str) -> dict[str, object]:
    return {"json": {"name": "Bea"}} if method == "PATCH" else {}


@pytest.mark.parametrize("method", METHODS)
async def test_anonymous_is_401(
    db_client: AsyncClient, db_session: AsyncSession, method: str
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    resp = await db_client.request(
        method, stylist_url(stylist.id), **_request_kwargs(method)
    )

    assert resp.status_code == 401
    assert await reloaded(db_session, stylist) is not None


@pytest.mark.parametrize("method", METHODS)
async def test_bad_token_is_401(
    db_client: AsyncClient, db_session: AsyncSession, method: str
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    resp = await db_client.request(
        method,
        stylist_url(stylist.id),
        headers={"Authorization": "Bearer not-a-token"},
        **_request_kwargs(method),
    )

    assert resp.status_code == 401


@pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.STYLIST, UserRole.ADMIN])
@pytest.mark.parametrize("method", METHODS)
async def test_other_roles_are_403(
    db_client: AsyncClient, db_session: AsyncSession, method: str, role: UserRole
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    _, headers = await make_user(db_session, role)

    resp = await db_client.request(
        method, stylist_url(stylist.id), headers=headers, **_request_kwargs(method)
    )

    assert resp.status_code == 403
    saved = await reloaded(db_session, stylist)
    assert saved is not None and saved.name == "T"


@pytest.mark.parametrize("method", METHODS)
async def test_stylist_cannot_edit_or_delete_themself(
    db_client: AsyncClient, db_session: AsyncSession, method: str
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist, stylist_headers = await make_user(db_session, UserRole.STYLIST)
    stylist.salon_id = salon.id
    db_session.add(stylist)
    await db_session.commit()

    resp = await db_client.request(
        method,
        stylist_url(stylist.id),
        headers=stylist_headers,
        **_request_kwargs(method),
    )

    assert resp.status_code == 403
    assert await reloaded(db_session, stylist) is not None


@pytest.mark.parametrize("method", METHODS)
async def test_unknown_or_non_stylist_id_is_404(
    db_client: AsyncClient, db_session: AsyncSession, method: str
) -> None:
    owner, headers, _ = await owner_and_salon(db_session)
    client_user, _ = await make_user(db_session, UserRole.CLIENT)

    for target in (uuid.uuid4(), client_user.id, owner.id):
        resp = await db_client.request(
            method, stylist_url(target), headers=headers, **_request_kwargs(method)
        )
        assert resp.status_code == 404
        assert resp.json() == {"detail": "stylist not found"}
    assert await db_session.get(User, owner.id, populate_existing=True) is not None


@pytest.mark.parametrize("method", METHODS)
async def test_malformed_id_is_422(
    db_client: AsyncClient, db_session: AsyncSession, method: str
) -> None:
    _, headers, _ = await owner_and_salon(db_session)

    resp = await db_client.request(
        method,
        "/api/v1/stylists/not-a-uuid",
        headers=headers,
        **_request_kwargs(method),
    )

    assert resp.status_code == 422


@pytest.mark.parametrize("salon_status", [SalonStatus.PENDING, SalonStatus.SUSPENDED])
async def test_owner_manages_stylists_of_a_non_approved_salon(
    db_client: AsyncClient, db_session: AsyncSession, salon_status: SalonStatus
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    salon.status = salon_status
    db_session.add(salon)
    await db_session.commit()
    stylist = await make_stylist(db_session, salon)

    patch = await db_client.patch(
        stylist_url(stylist.id), json={"name": "Bea", "image_url": URL}, headers=headers
    )
    delete = await db_client.delete(stylist_url(stylist.id), headers=headers)

    assert patch.status_code == 200
    assert patch.json()["name"] == "Bea"
    assert delete.status_code == 204
    assert await reloaded(db_session, stylist) is None
    status = await db_session.scalar(
        select(Salon.status)
        .where(Salon.id == salon.id)
        .execution_options(populate_existing=True)
    )
    assert status == salon_status


async def test_another_owner_is_still_403_on_a_pending_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    salon.status = SalonStatus.PENDING
    db_session.add(salon)
    await db_session.commit()
    stylist = await make_stylist(db_session, salon)
    _, other_headers = await make_user(db_session, UserRole.OWNER)

    patch = await db_client.patch(
        stylist_url(stylist.id), json={"name": "Bea"}, headers=other_headers
    )
    delete = await db_client.delete(stylist_url(stylist.id), headers=other_headers)

    assert (patch.status_code, delete.status_code) == (403, 403)
    assert await reloaded(db_session, stylist) is not None


async def test_public_stylist_list_shape_is_unchanged(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 60)
    _, headers = await salon_owner(db_session, salon)
    await db_client.patch(
        stylist_url(stylist.id), json={"name": "Bea", "image_url": URL}, headers=headers
    )

    listed = await db_client.get(stylists_url(salon))

    assert listed.json() == [
        {
            "id": str(stylist.id),
            "name": "Bea",
            "image_url": URL,
            "service_ids": [str(service.id)],
        }
    ]


async def test_empty_patch_changes_nothing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    resp = await db_client.patch(stylist_url(stylist.id), json={}, headers=headers)

    assert resp.status_code == 200
    assert (resp.json()["name"], resp.json()["image_url"]) == ("T", None)


# --- a write racing a delete is a 404, not a 500 ---


@pytest.mark.parametrize("write", ["update", "set_services"])
async def test_a_write_waiting_on_a_delete_gets_404(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    write: str,
) -> None:
    owner, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)

    async def run_write() -> object:
        async with session_maker() as session:
            if write == "update":
                return await stylist_service.update_stylist(
                    session, stylist.id, owner, StylistUpdate(name="Bea")
                )
            return await stylist_service.set_stylist_services(
                session, stylist.id, owner, []
            )

    async with session_maker() as deleter:
        # Holds the row lock until commit, as a DELETE request mid-transaction would.
        await deleter.execute(delete(User).where(User.id == stylist.id))
        task = asyncio.create_task(run_write())
        await asyncio.sleep(0.5)
        assert not task.done()  # blocked on the stylist row
        await deleter.commit()

    with pytest.raises(HTTPException) as exc_info:
        await task
    assert exc_info.value.status_code == 404
    assert await reloaded(db_session, stylist) is None
