import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings, settings
from app.core.security import (
    JWT_ALGORITHM,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_hash_is_argon2_and_verifies() -> None:
    hashed = hash_password("correct-horse")
    assert hashed.startswith("$argon2")
    assert hashed != "correct-horse"
    assert verify_password("correct-horse", hashed)
    assert not verify_password("wrong-horse", hashed)


def test_token_roundtrip() -> None:
    user_id = uuid.uuid4()
    assert decode_access_token(create_access_token(user_id)) == user_id


def test_tampered_token_is_rejected() -> None:
    token = create_access_token(uuid.uuid4())
    head, payload, sig = token.split(".")
    assert decode_access_token(f"{head}.{payload}.{sig[:-2]}xx") is None


def test_token_without_exp_is_rejected() -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4())},
        settings.SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    assert decode_access_token(token) is None


def test_token_with_non_uuid_subject_is_rejected() -> None:
    token = jwt.encode(
        {"sub": "not-a-uuid", "exp": datetime.now(UTC) + timedelta(minutes=5)},
        settings.SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )
    assert decode_access_token(token) is None


@pytest.mark.parametrize("secret", ["change-me", "too-short"])
def test_production_rejects_weak_secret_key(secret: str) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, ENVIRONMENT="production", SECRET_KEY=secret)


def test_production_accepts_strong_secret_key() -> None:
    assert (
        Settings(
            _env_file=None, ENVIRONMENT="production", SECRET_KEY="x" * 40
        ).ENVIRONMENT
        == "production"
    )
