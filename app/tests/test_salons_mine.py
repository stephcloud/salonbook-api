"""GET /salons/mine: the signed-in owner's own salons."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.user import UserRole
from app.tests.test_salons_api import SALON, SALONS, make_salon, make_user
from app.tests.test_stylists_api import make_stylist

MINE = f"{SALONS}/mine"


async def test_owner_sees_only_their_own_salons(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session, UserRole.OWNER)
    other_owner, _ = await make_user(db_session, UserRole.OWNER)
    first = await make_salon(db_session, owner, name="First")
    second = await make_salon(db_session, owner, name="Second")
    await make_salon(db_session, other_owner, name="Not mine")

    r = await db_client.get(MINE, headers=headers)

    assert r.status_code == 200
    assert len(r.json()) == 2
    by_id = {s["id"]: s for s in r.json()}
    assert set(by_id) == {str(first.id), str(second.id)}
    for salon in (first, second):
        body = dict(by_id[str(salon.id)])
        assert body.pop("created_at")
        assert body == {
            "id": str(salon.id),
            "owner_id": str(owner.id),
            "name": salon.name,
            "address": SALON["address"],
            "phone": SALON["phone"],
            "image_url": None,
            "cancellation_hours": 24,
            "deposit_amount": SALON["deposit_amount"],
        }
    assert by_id[str(first.id)]["name"] == "First"
    assert by_id[str(second.id)]["name"] == "Second"


async def test_owner_without_salons_gets_an_empty_list(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session, UserRole.OWNER)

    r = await db_client.get(MINE, headers=headers)

    assert r.status_code == 200
    assert r.json() == []


async def test_mine_is_paginated(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session, UserRole.OWNER)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    salons = [
        await make_salon(
            db_session, owner, name=f"S{i}", created_at=base + timedelta(hours=i)
        )
        for i in range(3)
    ]

    page1 = await db_client.get(MINE, headers=headers, params={"limit": 2})
    page2 = await db_client.get(MINE, headers=headers, params={"limit": 2, "offset": 2})

    # Explicit created_at values make the order deterministic: oldest first.
    assert [s["id"] for s in page1.json()] == [str(salons[0].id), str(salons[1].id)]
    assert [s["id"] for s in page2.json()] == [str(salons[2].id)]


async def test_mine_is_ordered_by_creation_time(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session, UserRole.OWNER)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    # Insert the newer salon first so insertion order cannot explain the result.
    newer = await make_salon(
        db_session, owner, name="Newer", created_at=base + timedelta(days=1)
    )
    older = await make_salon(db_session, owner, name="Older", created_at=base)

    r = await db_client.get(MINE, headers=headers)

    assert [s["id"] for s in r.json()] == [str(older.id), str(newer.id)]


async def test_mine_is_not_parsed_as_a_salon_id(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session, UserRole.OWNER)
    salon = await make_salon(db_session, owner)

    r = await db_client.get(MINE, headers=headers)

    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == [str(salon.id)]


async def test_client_gets_403_on_mine(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session, UserRole.CLIENT)

    r = await db_client.get(MINE, headers=headers)

    assert r.status_code == 403


async def test_stylist_gets_403_on_mine(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session, UserRole.OWNER)
    salon = await make_salon(db_session, owner)
    stylist = await make_stylist(db_session, salon)
    headers = {"Authorization": f"Bearer {create_access_token(stylist.id)}"}

    r = await db_client.get(MINE, headers=headers)

    assert r.status_code == 403


async def test_mine_without_a_token_is_401(db_client: AsyncClient) -> None:
    r = await db_client.get(MINE)

    assert r.status_code == 401
