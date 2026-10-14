"""add_reminder_markers_and_summary_date

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14 10:00:00.000000

Hand-written. Phase 2 of booking emails (reminders and the owner's daily summary):

- bookings.reminder_24h_email_sent_at and bookings.reminder_2h_email_sent_at: nullable
  timestamptz, one per client reminder. NULL means "not sent".
- users.daily_summary_sent_on: nullable date, the Lagos day of the owner's last summary.
- ix_bookings_confirmed_starts_at: partial index on bookings(starts_at) for confirmed rows,
  which is all the reminder query reads.

Safe to run before the new code is live. The three columns are nullable with no default
and no backfill (a catalog-only change in Postgres, no table rewrite), and the running code
neither selects nor inserts them. Existing confirmed bookings are not reminded about
retroactively beyond the windows: a booking is only due while its reminder moment is
within the tolerance. The index is a plain CREATE INDEX: it blocks writes to bookings for
the moment it takes to build, which is short at this table size. lock_timeout caps the wait
for each lock at 5s and fails the deploy (the old version keeps serving) instead of letting
a queue build behind it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BOOKING_COLUMNS = ("reminder_24h_email_sent_at", "reminder_2h_email_sent_at")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in BOOKING_COLUMNS:
        op.add_column(
            "bookings", sa.Column(name, sa.DateTime(timezone=True), nullable=True)
        )
    op.add_column("users", sa.Column("daily_summary_sent_on", sa.Date(), nullable=True))
    op.create_index(
        "ix_bookings_confirmed_starts_at",
        "bookings",
        ["starts_at"],
        postgresql_where=sa.text("status = 'confirmed'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_bookings_confirmed_starts_at", table_name="bookings")
    op.drop_column("users", "daily_summary_sent_on")
    for name in reversed(BOOKING_COLUMNS):
        op.drop_column("bookings", name)
