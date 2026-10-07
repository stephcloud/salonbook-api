"""create_payments

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07 12:00:00.000000

Hand-written: the partial unique index and the partial sweeper index are not things
autogenerate gets right. New table only: bookings and its exclusion constraint are
untouched. paystack_reference is UNIQUE (hard part #3), amounts are kobo, and at most
one non-failed payment may exist per booking.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("paystack_reference", sa.String(length=100), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "paid",
                "refund_pending",
                "refunded",
                "failed",
                name="paymentstatus",
            ),
            nullable=False,
        ),
        sa.Column("authorization_url", sa.String(length=512), nullable=True),
        sa.Column("access_code", sa.String(length=64), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_amount", sa.Integer(), nullable=True),
        sa.Column("refund_amount", sa.Integer(), nullable=True),
        sa.Column("paystack_refund_id", sa.String(length=64), nullable=True),
        sa.Column("refund_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "refund_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "refund_amount IS NULL OR refund_amount > 0",
            name="ck_payments_refund_amount_positive",
        ),
        sa.CheckConstraint("refund_attempts >= 0", name="ck_payments_refund_attempts"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "paystack_reference", name="uq_payments_paystack_reference"
        ),
    )
    op.create_index(op.f("ix_payments_booking_id"), "payments", ["booking_id"])
    # One live (non-failed) payment per booking; a failed initialize can be retried.
    op.create_index(
        "uq_payments_booking_id_live",
        "payments",
        ["booking_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'failed'"),
    )
    op.create_index(
        "ix_payments_refund_pending",
        "payments",
        ["refund_attempted_at"],
        postgresql_where=sa.text("status = 'refund_pending'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_payments_refund_pending", table_name="payments")
    op.drop_index("uq_payments_booking_id_live", table_name="payments")
    op.drop_index(op.f("ix_payments_booking_id"), table_name="payments")
    op.drop_table("payments")
    # drop_table leaves the enum type behind; a later upgrade would fail without this.
    sa.Enum(name="paymentstatus").drop(op.get_bind(), checkfirst=True)
