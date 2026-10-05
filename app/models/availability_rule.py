import uuid
from datetime import UTC, date, datetime, time
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Time,
    text,
)
from sqlmodel import Field, SQLModel


class RuleKind(StrEnum):
    WORKING = "working"  # a window the stylist can take bookings
    BREAK = "break"  # a recurring gap (e.g. lunch) on a weekday
    DAY_OFF = "day_off"  # a specific calendar date with no bookings


class AvailabilityRule(SQLModel, table=True):
    __tablename__ = "availability_rules"
    __table_args__ = (
        CheckConstraint(
            "weekday IS NULL OR weekday BETWEEN 0 AND 6",
            name="ck_availability_rules_weekday_range",
        ),
        CheckConstraint(
            "start_time IS NULL OR end_time > start_time",
            name="ck_availability_rules_end_after_start",
        ),
        # working/break: weekday + times, no date. day_off: date only.
        CheckConstraint(
            "(kind = 'day_off' AND off_date IS NOT NULL AND weekday IS NULL "
            "AND start_time IS NULL AND end_time IS NULL) "
            "OR (kind IN ('working', 'break') AND off_date IS NULL "
            "AND weekday IS NOT NULL AND start_time IS NOT NULL "
            "AND end_time IS NOT NULL)",
            name="ck_availability_rules_shape_matches_kind",
        ),
        Index("ix_availability_rules_stylist_id_weekday", "stylist_id", "weekday"),
        # A date can be a day off only once per stylist.
        Index(
            "uq_availability_rules_stylist_id_off_date",
            "stylist_id",
            "off_date",
            unique=True,
            postgresql_where=text("kind = 'day_off'"),
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    stylist_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    )
    kind: RuleKind = Field(
        sa_column=Column(
            Enum(
                RuleKind,
                name="rulekind",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )
    # 0 = Monday ... 6 = Sunday (Python's date.weekday()). NULL for day_off.
    weekday: int | None = Field(default=None, sa_column=Column(SmallInteger))
    # Naive local salon time; NULL for day_off.
    start_time: time | None = Field(default=None, sa_column=Column(Time))
    end_time: time | None = Field(default=None, sa_column=Column(Time))
    off_date: date | None = Field(default=None, sa_column=Column(Date))
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
