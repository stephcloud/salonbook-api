import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("ENVIRONMENT", "test")  # before app settings load

from app.core.config import settings
from app.db.base import metadata
from app.db.session import get_session
from app.main import app
from app.services.paystack import get_paystack_client
from app.tests.paystack_fakes import TEST_PAYSTACK_SECRET, FakePaystack

TEST_DB_NAME = "salonbook_test"


def _test_database_url() -> str:
    url = make_url(settings.DATABASE_URL)
    if url.database == TEST_DB_NAME:
        return url.render_as_string(hide_password=False)
    return url.set(database=TEST_DB_NAME).render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def _paystack_test_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test signs and verifies with a known, non-empty test key."""
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", TEST_PAYSTACK_SECRET)


@pytest.fixture
def fake_paystack() -> Iterator[FakePaystack]:
    """Replaces the Paystack HTTP client: no network, and calls are recorded."""
    fake = FakePaystack()
    app.dependency_overrides[get_paystack_client] = lambda: fake
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_paystack_client, None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """HTTP client with no database. For routes that never touch the DB."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Fresh schema in the salonbook_test database for every test."""
    url = _test_database_url()
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any connection failure should fail clearly
        await engine.dispose()
        pytest.fail(
            f"Cannot connect to test database '{TEST_DB_NAME}'. Create it first "
            f"(createdb {TEST_DB_NAME}) and make sure Postgres is running "
            f"(docker compose up -d db). Cause: {type(exc).__name__}: {exc}",
            pytrace=False,
        )
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        # bookings' exclusion constraint needs btree_gist (migration 0006 does this too).
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        await conn.run_sync(metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest_asyncio.fixture
async def session_maker(
    db_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Opens extra, independent sessions (a second request, a second worker)."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_client(db_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    """HTTP client whose get_session dependency points at the test database."""
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac
    finally:
        app.dependency_overrides.pop(get_session, None)
