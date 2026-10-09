import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.salon import Salon
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon

STYLIST = {"name": "Ada", "email": "ada@example.com", "password": "correct-horse"}


def stylists_url(salon: Salon) -> str:
    return f"/api/v1/salons/{salon.id}/stylists"


def services_url(stylist: User) -> str:
    return f"/api/v1/stylists/{stylist.id}/services"


async def make_stylist(
    db_session: AsyncSession, salon: Salon, email: str | None = None
) -> User:
    stylist, _ = await make_user(db_session, UserRole.STYLIST)
    stylist.salon_id = salon.id
    if email:
        stylist.email = email
    db_session.add(stylist)
    await db_session.commit()
    return stylist


async def offered(db_session: AsyncSession, stylist: User) -> set[uuid.UUID]:
    result = await db_session.execute(
        select(StylistService.service_id).where(StylistService.stylist_id == stylist.id)
    )
    return set(result.scalars())


# --- POST /salons/{id}/stylists ---


async def test_owner_creates_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(stylists_url(salon), json=STYLIST, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "stylist"
    assert body["salon_id"] == str(salon.id)
    assert "password" not in body and "password_hash" not in body
    login = await db_client.post(
        "/api/v1/auth/login",
        json={"email": STYLIST["email"], "password": STYLIST["password"]},
    )
    assert login.status_code == 200


async def test_create_stylist_duplicate_email_conflicts(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    first = await db_client.post(stylists_url(salon), json=STYLIST, headers=headers)
    shouty = {**STYLIST, "email": STYLIST["email"].upper()}
    second = await db_client.post(stylists_url(salon), json=shouty, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 409


async def test_owner_a_cannot_add_stylist_to_owner_b_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    _, headers_a = await make_user(db_session, UserRole.OWNER)
    resp = await db_client.post(stylists_url(salon_b), json=STYLIST, headers=headers_a)
    assert resp.status_code == 403
    count = await db_session.execute(select(User).where(User.role == UserRole.STYLIST))
    assert count.first() is None


async def test_create_stylist_missing_salon_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session, UserRole.OWNER)
    resp = await db_client.post(
        f"/api/v1/salons/{uuid.uuid4()}/stylists", json=STYLIST, headers=headers
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.STYLIST])
async def test_create_stylist_requires_owner_role(
    db_client: AsyncClient, db_session: AsyncSession, role: UserRole
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    _, headers = await make_user(db_session, role)
    resp = await db_client.post(stylists_url(salon), json=STYLIST, headers=headers)
    assert resp.status_code == 403


async def test_create_stylist_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    resp = await db_client.post(stylists_url(salon), json=STYLIST)
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "override",
    [{"email": "nope"}, {"password": "short"}, {"role": "owner"}],
)
async def test_create_stylist_validates_input(
    db_client: AsyncClient, db_session: AsyncSession, override: dict[str, str]
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(
        stylists_url(salon), json={**STYLIST, **override}, headers=headers
    )
    assert resp.status_code == 422


# --- PUT /stylists/{id}/services ---


async def test_owner_sets_stylist_services_idempotent_and_replacing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    s1 = await make_service(db_session, salon, name="Wash")
    s2 = await make_service(db_session, salon, name="Braids")
    url = services_url(stylist)

    body = {"service_ids": [str(s1.id), str(s2.id), str(s1.id)]}
    first = await db_client.put(url, json=body, headers=headers)
    again = await db_client.put(url, json=body, headers=headers)
    assert first.status_code == again.status_code == 200
    assert set(first.json()["service_ids"]) == {str(s1.id), str(s2.id)}
    assert await offered(db_session, stylist) == {s1.id, s2.id}

    replaced = await db_client.put(
        url, json={"service_ids": [str(s2.id)]}, headers=headers
    )
    assert replaced.status_code == 200
    assert await offered(db_session, stylist) == {s2.id}

    cleared = await db_client.put(url, json={"service_ids": []}, headers=headers)
    assert cleared.status_code == 200
    assert await offered(db_session, stylist) == set()


async def test_owner_a_cannot_set_services_for_owner_b_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    stylist_b = await make_stylist(db_session, salon_b)
    service_b = await make_service(db_session, salon_b)
    _, headers_a = await make_user(db_session, UserRole.OWNER)
    resp = await db_client.put(
        services_url(stylist_b),
        json={"service_ids": [str(service_b.id)]},
        headers=headers_a,
    )
    assert resp.status_code == 403
    assert await offered(db_session, stylist_b) == set()


async def test_set_services_rejects_other_salon_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    own = await make_service(db_session, salon)
    _, _, other_salon = await owner_and_salon(db_session)
    foreign = await make_service(db_session, other_salon)
    url = services_url(stylist)
    await db_client.put(url, json={"service_ids": [str(own.id)]}, headers=headers)

    resp = await db_client.put(
        url, json={"service_ids": [str(foreign.id)]}, headers=headers
    )
    assert resp.status_code == 422
    assert await offered(db_session, stylist) == {own.id}


async def test_set_services_unknown_or_non_stylist_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, _ = await owner_and_salon(db_session)
    client_user, _ = await make_user(db_session, UserRole.CLIENT)
    body = {"service_ids": []}
    missing = await db_client.put(
        f"/api/v1/stylists/{uuid.uuid4()}/services", json=body, headers=headers
    )
    not_stylist = await db_client.put(
        services_url(client_user), json=body, headers=headers
    )
    assert missing.status_code == 404
    assert not_stylist.status_code == 404


async def test_set_services_requires_owner_role_and_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    _, client_headers = await make_user(db_session, UserRole.CLIENT)
    body = {"service_ids": []}
    forbidden = await db_client.put(
        services_url(stylist), json=body, headers=client_headers
    )
    anonymous = await db_client.put(services_url(stylist), json=body)
    assert forbidden.status_code == 403
    assert anonymous.status_code == 401


# --- GET /salons/{id}/stylists (public) ---


async def test_public_list_has_only_id_name_service_ids(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon, email="secret@example.com")
    service = await make_service(db_session, salon)
    db_session.add(StylistService(stylist_id=stylist.id, service_id=service.id))
    await db_session.commit()
    # A stylist at another salon must not appear.
    _, _, other = await owner_and_salon(db_session)
    await make_stylist(db_session, other)

    resp = await db_client.get(stylists_url(salon))  # no auth
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert set(data[0]) == {"id", "name", "service_ids", "image_url"}
    assert data[0]["id"] == str(stylist.id)
    assert data[0]["service_ids"] == [str(service.id)]
    assert "email" not in data[0]
    assert "secret@example.com" not in resp.text


async def test_public_list_paginates(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    for _ in range(3):
        await make_stylist(db_session, salon)
    page1 = await db_client.get(stylists_url(salon), params={"limit": 2})
    page2 = await db_client.get(stylists_url(salon), params={"limit": 2, "offset": 2})
    assert len(page1.json()) == 2
    assert len(page2.json()) == 1
    ids = {s["id"] for s in page1.json() + page2.json()}
    assert len(ids) == 3


async def test_public_list_rejects_limit_over_100(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    resp = await db_client.get(stylists_url(salon), params={"limit": 101})
    assert resp.status_code == 422


async def test_public_list_missing_salon_404(db_client: AsyncClient) -> None:
    resp = await db_client.get(f"/api/v1/salons/{uuid.uuid4()}/stylists")
    assert resp.status_code == 404


# --- DB constraint ---


@pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.OWNER])
async def test_salon_id_only_allowed_for_stylists(
    db_session: AsyncSession, role: UserRole
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    user, _ = await make_user(db_session, role)
    user.salon_id = salon.id
    db_session.add(user)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
