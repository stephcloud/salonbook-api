"""narrow_live_payment_index

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07 15:00:00.000000

Hand-written. 0008 made `uq_payments_booking_id_live` cover every non-failed payment.
That breaks a stale charge.success on a failed row: moving it to refund_pending while a
newer payment is open violates the index, so the webhook would 500 and Paystack would
retry forever without the money ever being refunded. "Live" now means pending or paid
only. A payment waiting for a refund, or already refunded, no longer blocks a booking
from having another payment. Index change only: no table, column or data change.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("uq_payments_booking_id_live", table_name="payments")
    op.create_index(
        "uq_payments_booking_id_live",
        "payments",
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'paid')"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Fails if some booking now has both a live and a refund_pending/refunded payment;
    that data is valid under 0009 but not under the stricter 0008 index.
    """
    op.drop_index("uq_payments_booking_id_live", table_name="payments")
    op.create_index(
        "uq_payments_booking_id_live",
        "payments",
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'failed'"),
    )
