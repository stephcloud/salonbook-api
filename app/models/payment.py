import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlmodel import Field, SQLModel


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    FAILED = "failed"


class Payment(SQLModel, table=True):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("paystack_reference", name="uq_payments_paystack_reference"),
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "refund_amount IS NULL OR refund_amount > 0",
            name="ck_payments_refund_amount_positive",
        ),
        CheckConstraint("refund_attempts >= 0", name="ck_payments_refund_attempts"),
        # One live payment per booking: a double-click can't create two, while a failed
        # initialize leaves room for a retry with a fresh row and reference.
        Index(
            "uq_payments_booking_id_live",
            "booking_id",
            unique=True,
            postgresql_where=text("status <> 'failed'"),
        ),
        # The refund sweeper only ever looks at rows waiting for a refund.
        Index(
            "ix_payments_refund_pending",
            "refund_attempted_at",
            postgresql_where=text("status = 'refund_pending'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # RESTRICT: payment history must outlive booking deletion.
    booking_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("bookings.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    paystack_reference: str = Field(sa_column=Column(String(100), nullable=False))
    # Minor currency units (kobo). Always taken from the salon's deposit, never a request.
    amount: int = Field(sa_column=Column(Integer, nullable=False))
    currency: str = Field(sa_column=Column(String(3), nullable=False))
    status: PaymentStatus = Field(
        default=PaymentStatus.PENDING,
        sa_column=Column(
            Enum(
                PaymentStatus,
                name="paymentstatus",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )
    authorization_url: str | None = Field(
        default=None, sa_column=Column(String(512), nullable=True)
    )
    access_code: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    paid_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # What Paystack says it collected, recorded only when it differs from `amount`
    # (audit trail for a mismatch; never used to confirm anything).
    received_amount: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    # What we send back to the client: the deposit normally, `received_amount` when the
    # payment didn't match what we expected.
    refund_amount: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    paystack_refund_id: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    # Claim marker for refund retries: a worker stamps this before calling Paystack, so
    # two workers never refund the same payment at once and no lock spans the HTTP call.
    refund_attempted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    refund_attempts: int = Field(
        default=0,
        sa_column=Column(Integer, server_default=text("0"), nullable=False),
    )
    refunded_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("now()"),
            onupdate=lambda: datetime.now(UTC),
            nullable=False,
        ),
    )
