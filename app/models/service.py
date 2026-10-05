import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Column, DateTime, Enum, ForeignKey, Index, text
from sqlmodel import Field, SQLModel


class PriceType(StrEnum):
    FIXED = "fixed"
    QUOTE = "quote"


class Service(SQLModel, table=True):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("duration_minutes > 0", name="ck_services_duration_minutes"),
        CheckConstraint(
            "(price_type = 'quote' AND price IS NULL) "
            "OR (price_type = 'fixed' AND price IS NOT NULL AND price > 0)",
            name="ck_services_price_matches_type",
        ),
        Index("ix_services_salon_id_created_at_id", "salon_id", "created_at", "id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    salon_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("salons.id", ondelete="CASCADE"), nullable=False)
    )
    name: str = Field(max_length=100)
    duration_minutes: int
    price_type: PriceType = Field(
        sa_column=Column(
            Enum(
                PriceType,
                name="pricetype",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )
    # Minor currency units (kobo); NULL when price_type is quote.
    price: int | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
