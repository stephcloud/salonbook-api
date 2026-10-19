"""add_admin_role

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16 10:00:00.000000

Hand-written. Adds the `admin` value to the native Postgres enum `userrole`.

ALTER TYPE ... ADD VALUE cannot be used in the same transaction that then uses the new
value, so this runs in an autocommit block and lives in its own migration: 0014 and the
rest of the admin work stay fully transactional. No row is touched and nobody becomes an
admin here: the only way to grant the role is the make_admin job.

Safe to run before the new code is live: the old code never reads or writes the value.
ADD VALUE takes no table rewrite and only a brief lock on the type; lock_timeout caps that
wait at 5s (set and reset inside the block, since SET LOCAL has no effect in autocommit).

env.py runs all migrations in one transaction, and the autocommit block commits whatever
came before it. If 0014 then fails, the 0013 version stamp rolls back while the enum value
stays: the database is left at 0012 with `admin` already present. Re-running is safe
because of IF NOT EXISTS.

Downgrade is a no-op: Postgres cannot drop a value from an enum. The leftover value is
harmless to the database, but after a CODE rollback any user already holding `admin` would
make the old UserRole enum fail on that row; change such users by hand first.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        try:
            op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'admin'")
        finally:
            op.execute("RESET lock_timeout")


def downgrade() -> None:
    """Downgrade schema."""
    # Postgres cannot remove an enum value. Any user holding 'admin' must be changed by hand
    # before an older version of the code can read that row.
