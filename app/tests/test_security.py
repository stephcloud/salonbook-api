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


PROD = {
    "_env_file": None,
    "ENVIRONMENT": "production",
    "SECRET_KEY": "x" * 40,
    "PAYSTACK_SECRET_KEY": "sk_test_not_a_real_key",
}


@pytest.mark.parametrize("secret", ["change-me", "too-short"])
def test_production_rejects_weak_secret_key(secret: str) -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(**{**PROD, "SECRET_KEY": secret})


def test_production_accepts_strong_secret_key() -> None:
    assert Settings(**PROD).ENVIRONMENT == "production"


@pytest.mark.parametrize("key", ["", "   "])
def test_production_rejects_a_missing_paystack_key(key: str) -> None:
    with pytest.raises(ValueError, match="PAYSTACK_SECRET_KEY"):
        Settings(**{**PROD, "PAYSTACK_SECRET_KEY": key})


def test_production_rejects_an_unset_paystack_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "PAYSTACK_SECRET_KEY", raising=False
    )  # not from the shell either
    unset = {k: v for k, v in PROD.items() if k != "PAYSTACK_SECRET_KEY"}
    with pytest.raises(ValueError, match="PAYSTACK_SECRET_KEY"):
        Settings(**unset)


@pytest.mark.parametrize("environment", ["local", "test"])
def test_local_and_test_boot_without_a_paystack_key(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.delenv("PAYSTACK_SECRET_KEY", raising=False)
    unset = {k: v for k, v in PROD.items() if k != "PAYSTACK_SECRET_KEY"}
    assert Settings(**{**unset, "ENVIRONMENT": environment}).PAYSTACK_SECRET_KEY == ""
