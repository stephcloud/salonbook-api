from datetime import UTC, datetime, timedelta

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import JWT_ALGORITHM
from app.models.user import User

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"

VALID = {
    "name": "Ada Obi",
    "email": "ada@example.com",
    "password": "correct-horse",
    "role": "client",
}


async def _register_and_login(client: AsyncClient, **overrides: str) -> str:
    payload = {**VALID, **overrides}
    assert (await client.post(REGISTER, json=payload)).status_code == 201
    resp = await client.post(
        LOGIN, json={"email": payload["email"], "password": payload["password"]}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- register ---


@pytest.mark.parametrize("role", ["client", "owner"])
async def test_register_success(db_client: AsyncClient, role: str) -> None:
    resp = await db_client.post(REGISTER, json={**VALID, "role": role})
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == VALID["email"]
    assert body["role"] == role
    assert "id" in body
    assert "password" not in body
    assert "password_hash" not in body


async def test_register_duplicate_email_conflicts(db_client: AsyncClient) -> None:
    assert (await db_client.post(REGISTER, json=VALID)).status_code == 201
    resp = await db_client.post(REGISTER, json={**VALID, "email": "ADA@example.com"})
    assert resp.status_code == 409


@pytest.mark.parametrize("role", ["stylist", "admin", ""])
async def test_register_rejects_non_public_roles(
    db_client: AsyncClient, role: str
) -> None:
    resp = await db_client.post(REGISTER, json={**VALID, "role": role})
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "override",
    [
        {"password": "short"},
        {"password": "x" * 129},
        {"email": "not-an-email"},
        {"name": ""},
        {"name": "   "},
        {"name": "n" * 101},
    ],
)
async def test_register_validates_input(
    db_client: AsyncClient, override: dict[str, str]
) -> None:
    resp = await db_client.post(REGISTER, json={**VALID, **override})
    assert resp.status_code == 422


async def test_register_stores_hash_not_password(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_client.post(REGISTER, json=VALID)
    user = (await db_session.execute(User.__table__.select())).first()
    assert user is not None
    assert user.password_hash != VALID["password"]
    assert user.password_hash.startswith("$argon2")


# --- login ---


async def test_login_success_returns_jwt_with_30_minute_expiry(
    db_client: AsyncClient,
) -> None:
    token = await _register_and_login(db_client)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    lifetime = claims["exp"] - claims["iat"]
    assert lifetime == 30 * 60


async def test_login_wrong_password_and_unknown_email_look_identical(
    db_client: AsyncClient,
) -> None:
    await db_client.post(REGISTER, json=VALID)
    wrong_pw = await db_client.post(
        LOGIN, json={"email": VALID["email"], "password": "wrong-pass"}
    )
    no_user = await db_client.post(
        LOGIN, json={"email": "nobody@example.com", "password": "wrong-pass"}
    )
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json() == no_user.json() == {"detail": "invalid credentials"}


# --- /me ---


async def test_me_returns_current_user(db_client: AsyncClient) -> None:
    token = await _register_and_login(db_client)
    resp = await db_client.get(ME, headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == VALID["email"]
    assert "password_hash" not in body


async def test_me_returns_only_the_callers_own_data(db_client: AsyncClient) -> None:
    token_a = await _register_and_login(db_client)
    await _register_and_login(db_client, email="bola@example.com", name="Bola")
    resp = await db_client.get(ME, headers=_auth(token_a))
    assert resp.json()["email"] == VALID["email"]


async def test_me_without_token_is_401(db_client: AsyncClient) -> None:
    assert (await db_client.get(ME)).status_code == 401


async def test_me_with_garbage_token_is_401(db_client: AsyncClient) -> None:
    assert (await db_client.get(ME, headers=_auth("not.a.jwt"))).status_code == 401


async def test_me_with_expired_token_is_401(db_client: AsyncClient) -> None:
    token = await _register_and_login(db_client)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    expired = jwt.encode(
        {**claims, "exp": datetime.now(UTC) - timedelta(minutes=1)},
        settings.SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    assert (await db_client.get(ME, headers=_auth(expired))).status_code == 401


async def test_me_with_token_signed_by_other_key_is_401(db_client: AsyncClient) -> None:
    token = await _register_and_login(db_client)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    forged = jwt.encode(
        claims, "some-other-secret-key-0123456789abcdef", algorithm="HS256"
    )
    assert (await db_client.get(ME, headers=_auth(forged))).status_code == 401


async def test_me_with_alg_none_token_is_401(db_client: AsyncClient) -> None:
    token = await _register_and_login(db_client)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[JWT_ALGORITHM])
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    assert (await db_client.get(ME, headers=_auth(unsigned))).status_code == 401


async def test_me_after_user_deleted_is_401(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    token = await _register_and_login(db_client)
    await db_session.execute(delete(User))
    await db_session.commit()
    assert (await db_client.get(ME, headers=_auth(token))).status_code == 401
