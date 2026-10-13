"""add_email_sent_markers_to_bookings

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-13 10:00:00.000000

Hand-written. Adds four nullable timestamptz columns to bookings, one per booking email
(client confirmation, owner confirmation, client cancellation, owner cancellation). NULL
means "not sent". Plain add-column: no default, no backfill, so existing rows get NULL.

Safe to run before the new code is live. Postgres adds a nullable column without a default
as a catalog-only change (no table rewrite), and the code still running during a deploy
neither selects nor inserts these columns, so it keeps working against the new schema.
Existing bookings are never emailed: emails are sent only when a booking changes state.
The ALTER needs a brief ACCESS EXCLUSIVE lock on bookings. While it waits for that lock,
other queries on bookings queue behind it; lock_timeout caps the wait at 5s and fails the
deploy (the old version keeps serving) instead of letting that queue grow without limit.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    "confirmation_email_sent_at",
    "owner_confirmation_email_sent_at",
    "cancellation_email_sent_at",
    "owner_cancellation_email_sent_at",
)


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in COLUMNS:
        op.add_column(
            "bookings", sa.Column(name, sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in reversed(COLUMNS):
        op.drop_column("bookings", name)
