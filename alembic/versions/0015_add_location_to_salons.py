"""add_location_to_salons

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-02 10:00:00.000000

Hand-written. Additive only:

- salons.latitude and salons.longitude, DOUBLE PRECISION, nullable, no default: a
  catalog-only change, no table rewrite, every existing salon gets NULL (no location).
- ck_salons_location: both NULL, or both set and in range. Every existing row is NULL/NULL,
  so it validates. Adding it scans salons under a brief ACCESS EXCLUSIVE lock, which is
  short at this table size.

Runs on container start, before the new code serves traffic. The old code neither selects
nor writes these columns, so it keeps working during the window. lock_timeout caps the lock
wait at 5s and fails the deploy (the old version keeps serving) instead of queueing reads
behind it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("salons", sa.Column("latitude", sa.Double(), nullable=True))
    op.add_column("salons", sa.Column("longitude", sa.Double(), nullable=True))
    op.create_check_constraint(
        "ck_salons_location",
        "salons",
        # The IS NOT NULLs matter: a CHECK that evaluates to NULL passes, so without them
        # (6.5, NULL) would be accepted.
        "(latitude IS NULL AND longitude IS NULL) OR "
        "(latitude IS NOT NULL AND longitude IS NOT NULL"
        " AND latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_salons_location", "salons", type_="check")
    op.drop_column("salons", "longitude")
    op.drop_column("salons", "latitude")
