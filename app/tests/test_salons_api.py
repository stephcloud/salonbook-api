import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.models.salon import Salon
from app.models.user import User, UserRole

SALONS = "/api/v1/salons"

SALON = {
    "name": "Glow Studio",
    "address": "12 Admiralty Way, Lekki",
    "phone": "+2348012345678",
    "deposit_amount": 500000,
}


async def make_user(
    db_session: AsyncSession, role: UserRole = UserRole.OWNER
) -> tuple[User, dict[str, str]]:
    user = User(
        name="T",
        email=f"{uuid.uuid4().hex}@example.com",
        password_hash=hash_password("correct-horse"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def make_salon(
    db_session: AsyncSession, owner: User, **overrides: object
) -> Salon:
    fields: dict[str, object] = {**SALON, **overrides}
    salon = Salon(owner_id=owner.id, **fields)
    db_session.add(salon)
    await db_session.commit()
    return salon


# --- create ---


async def test_owner_creates_salon_with_default_cancellation_hours(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session)
    resp = await db_client.post(SALONS, json=SALON, headers=headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner_id"] == str(owner.id)
    assert body["cancellation_hours"] == 24
    assert body["deposit_amount"] == 500000


async def test_create_salon_rejects_owner_id_in_body(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS, json={**SALON, "owner_id": str(uuid.uuid4())}, headers=headers
    )
    # Unknown fields (owner_id) are rejected, never trusted.
    assert resp.status_code == 422


async def test_client_and_stylist_cannot_create_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    for role in (UserRole.CLIENT, UserRole.STYLIST):
        _, headers = await make_user(db_session, role)
        resp = await db_client.post(SALONS, json=SALON, headers=headers)
        assert resp.status_code == 403


async def test_create_salon_requires_auth(db_client: AsyncClient) -> None:
    assert (await db_client.post(SALONS, json=SALON)).status_code == 401


@pytest.mark.parametrize("deposit", [-1, 0])
async def test_create_salon_rejects_zero_or_negative_deposit(
    db_client: AsyncClient, db_session: AsyncSession, deposit: int
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS, json={**SALON, "deposit_amount": deposit}, headers=headers
    )
    assert resp.status_code == 422


# --- read (public) ---


async def test_list_salons_is_public(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    await make_salon(db_session, owner)
    resp = await db_client.get(SALONS)
    assert resp.status_code == 200
    assert len(resp.json()) == 1


async def test_get_salon_is_public_and_404_when_missing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    assert (await db_client.get(f"{SALONS}/{salon.id}")).status_code == 200
    assert (await db_client.get(f"{SALONS}/{uuid.uuid4()}")).status_code == 404


# --- pagination ---


async def test_list_salons_default_limit_is_20(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    for i in range(25):
        db_session.add(Salon(owner_id=owner.id, **{**SALON, "name": f"S{i}"}))
    await db_session.commit()
    resp = await db_client.get(SALONS)
    assert resp.status_code == 200
    assert len(resp.json()) == 20


async def test_list_salons_limit_and_offset(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    for i in range(5):
        db_session.add(Salon(owner_id=owner.id, **{**SALON, "name": f"S{i}"}))
    await db_session.commit()
    first = (await db_client.get(SALONS, params={"limit": 2})).json()
    rest = (await db_client.get(SALONS, params={"limit": 2, "offset": 2})).json()
    assert len(first) == 2 and len(rest) == 2
    assert {s["id"] for s in first}.isdisjoint({s["id"] for s in rest})


async def test_list_salons_rejects_limit_above_100(db_client: AsyncClient) -> None:
    assert (await db_client.get(SALONS, params={"limit": 101})).status_code == 422


async def test_list_salons_rejects_negative_offset(db_client: AsyncClient) -> None:
    assert (await db_client.get(SALONS, params={"offset": -1})).status_code == 422


# --- update / ownership ---


async def test_owner_updates_own_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"cancellation_hours": 48}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["cancellation_hours"] == 48
    assert resp.json()["name"] == SALON["name"]


@pytest.mark.parametrize("deposit", [-1, 0])
async def test_update_salon_rejects_zero_or_negative_deposit(
    db_client: AsyncClient, db_session: AsyncSession, deposit: int
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"deposit_amount": deposit}, headers=headers
    )
    assert resp.status_code == 422
    await db_session.refresh(salon)
    assert salon.deposit_amount == SALON["deposit_amount"]


async def test_owner_a_cannot_edit_owner_b_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner_b, _ = await make_user(db_session)
    _, headers_a = await make_user(db_session)
    salon = await make_salon(db_session, owner_b)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"name": "Hijacked"}, headers=headers_a
    )
    assert resp.status_code == 403
    await db_session.refresh(salon)
    assert salon.name == SALON["name"]


async def test_update_missing_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.patch(
        f"{SALONS}/{uuid.uuid4()}", json={"name": "X"}, headers=headers
    )
    assert resp.status_code == 404


async def test_update_salon_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.patch(f"{SALONS}/{salon.id}", json={"name": "X"})
    assert resp.status_code == 401
