"""add_cancellation_fields_to_bookings

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07 10:00:00.000000

Hand-written. Records the cancellation time and the refund decision on the booking.
Both columns are nullable: NULL refund_due means no decision yet (not cancelled, or
cancelled by the pending-expiry job). No constraint or enum changes, so the no-overlap exclusion
constraint is untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("bookings", sa.Column("refund_due", sa.Boolean(), nullable=True))
    op.add_column(
        "bookings", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("bookings", "cancelled_at")
    op.drop_column("bookings", "refund_due")
