import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings

# Placeholder credentials only; no real database URL appears here.
HOST = "ep-example-123456.eu-central-1.aws.neon.tech"


def _database_url(raw: str) -> str:
    return Settings(DATABASE_URL=raw, ENVIRONMENT="test").DATABASE_URL


def test_neon_url_gets_ssl_and_loses_channel_binding() -> None:
    url = make_url(
        _database_url(
            f"postgresql://app:pw@{HOST}/neondb?sslmode=require&channel_binding=require"
        )
    )
    assert url.drivername == "postgresql+asyncpg"
    assert dict(url.query) == {"ssl": "require"}
    assert (url.username, url.password, url.host, url.database) == (
        "app",
        "pw",
        HOST,
        "neondb",
    )


def test_already_fixed_url_only_changes_scheme() -> None:
    raw = f"postgresql://app:p%2Bw@{HOST}/neondb?ssl=require"
    assert (
        _database_url(raw)
        == f"postgresql+asyncpg://app:p%2Bw@{HOST}/neondb?ssl=require"
    )


def test_postgres_scheme_is_rewritten() -> None:
    assert _database_url(f"postgres://app:pw@{HOST}/db") == (
        f"postgresql+asyncpg://app:pw@{HOST}/db"
    )


def test_special_character_password_round_trips() -> None:
    raw = f"postgresql://app:p%40ss%2Fw%25rd%2Bx@{HOST}/neondb?sslmode=require&channel_binding=require"
    url = make_url(_database_url(raw))
    assert url.password == "p@ss/w%rd+x"
    assert dict(url.query) == {"ssl": "require"}


def test_unparseable_url_does_not_echo_the_value() -> None:
    with pytest.raises(ValidationError) as err:
        _database_url("not a url secret-pw")
    assert "secret-pw" not in str(err.value)
