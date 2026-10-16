"""Migrations 0013/0014 on a database that already has data.

The normal suite builds its schema from the models, so it never runs a migration. This
test creates its own scratch database on the test server, migrates it to 0012, adds a
salon the way production has one, upgrades to head, and checks what happened to it.
It never touches any other database.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import settings
from app.tests.conftest import _test_database_url

ROOT = Path(__file__).resolve().parents[2]
SCRATCH_DB = "salonbook_migration_test"
OWNER_ID = "11111111-1111-1111-1111-111111111111"
OLD_SALON_ID = "22222222-2222-2222-2222-222222222222"


def _alembic(url: str, monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(cfg, target)


@pytest_asyncio.fixture
async def scratch_url() -> AsyncIterator[str]:
    server = make_url(_test_database_url())
    admin = create_async_engine(
        server.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {SCRATCH_DB}"))
    try:
        yield server.set(database=SCRATCH_DB).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as conn:
            await conn.execute(
                text(f"DROP DATABASE IF EXISTS {SCRATCH_DB} WITH (FORCE)")
            )
        await admin.dispose()


async def test_existing_salons_stay_approved_and_new_ones_start_pending(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await asyncio.to_thread(_alembic, scratch_url, monkeypatch, "0012")
    engine = create_async_engine(scratch_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, name, email, password_hash, role) "
                "VALUES (:id, 'Old Owner', 'old@example.test', 'x', 'owner')"
            ),
            {"id": OWNER_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO salons (id, owner_id, name, address, phone) "
                "VALUES (:id, :owner, 'Existing', '1 Test St', '0800000000')"
            ),
            {"id": OLD_SALON_ID, "owner": OWNER_ID},
        )

    await asyncio.to_thread(_alembic, scratch_url, monkeypatch, "head")

    async with engine.begin() as conn:
        old = await conn.scalar(
            text("SELECT status::text FROM salons WHERE id = :id"),
            {"id": OLD_SALON_ID},
        )
        await conn.execute(
            text(
                "INSERT INTO salons (id, owner_id, name, address, phone) "
                "VALUES (:id, :owner, 'New', '2 Test St', '0800000001')"
            ),
            {"id": str(uuid.uuid4()), "owner": OWNER_ID},
        )
        new = await conn.scalar(
            text("SELECT status::text FROM salons WHERE name = 'New'")
        )
        roles = (
            await conn.scalars(
                text(
                    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                    "WHERE t.typname = 'userrole'"
                )
            )
        ).all()
        version = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()

    assert old == "approved"  # existing salons keep working after the deploy
    assert new == "pending"  # new ones wait for an admin
    assert "admin" in roles
    assert version == "0014"
