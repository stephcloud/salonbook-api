"""add_image_url_to_salons_and_users

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09 10:00:00.000000

Hand-written. Adds a nullable image_url (max 500 chars) to salons and to users (stylists
are users with role 'stylist'). Plain add-column: no default, no backfill, so existing
rows get NULL and nothing is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "salons", sa.Column("image_url", sa.String(length=500), nullable=True)
    )
    op.add_column("users", sa.Column("image_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "image_url")
    op.drop_column("salons", "image_url")
