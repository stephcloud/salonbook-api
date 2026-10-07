import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlmodel import Field, SQLModel


class BookingStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


# Only these statuses hold a slot. Keep in sync with the exclusion constraint's WHERE
# clause below and in migration 0006.
ACTIVE_STATUSES = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


class Booking(SQLModel, table=True):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="ck_bookings_end_after_start"),
        Index("ix_bookings_stylist_id_starts_at", "stylist_id", "starts_at"),
        # No two active (pending/confirmed) bookings for one stylist may overlap. [) ranges let
        # back-to-back bookings touch. Needs the btree_gist extension (migration 0006).
        ExcludeConstraint(
            ("stylist_id", "="),
            (text("tstzrange(starts_at, ends_at, '[)')"), "&&"),
            name="ex_bookings_stylist_id_no_overlap",
            where=text("status IN ('pending', 'confirmed')"),
            using="gist",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # RESTRICT: booking history (and its payments) must outlive account deletion.
    client_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    stylist_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    )
    service_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("services.id", ondelete="RESTRICT"), nullable=False)
    )
    starts_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    ends_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    status: BookingStatus = Field(
        default=BookingStatus.PENDING,
        sa_column=Column(
            Enum(
                BookingStatus,
                name="bookingstatus",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )
    # Refund decision recorded at cancellation. NULL = no decision yet (not cancelled, or
    # cancelled by the pending-expiry job). The real Paystack refund happens in the
    # payments step.
    refund_due: bool | None = Field(
        default=None, sa_column=Column(Boolean, nullable=True)
    )
    cancelled_at: datetime | None = Field(
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
