from collections.abc import AsyncIterator
from typing import Annotated

import pytest_asyncio
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import require_role
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.models.user import User, UserRole


@pytest_asyncio.fixture
async def role_client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    test_app = FastAPI()

    @test_app.get("/owner-only")
    async def owner_only(
        user: Annotated[User, Depends(require_role(UserRole.OWNER))],
    ) -> dict[str, str]:
        return {"role": user.role}

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    test_app.dependency_overrides[get_session] = override_get_session
    async with AsyncClient(
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as ac:
        yield ac


async def _token_for(db_session: AsyncSession, role: UserRole) -> str:
    user = User(
        name="T",
        email=f"{role.value}@example.com",
        password_hash=hash_password("correct-horse"),
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    return create_access_token(user.id)


async def test_require_role_allows_matching_role(
    role_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _token_for(db_session, UserRole.OWNER)
    resp = await role_client.get(
        "/owner-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"role": "owner"}


async def test_require_role_forbids_other_role(
    role_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _token_for(db_session, UserRole.CLIENT)
    resp = await role_client.get(
        "/owner-only", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403


async def test_require_role_without_token_is_401(role_client: AsyncClient) -> None:
    assert (await role_client.get("/owner-only")).status_code == 401
