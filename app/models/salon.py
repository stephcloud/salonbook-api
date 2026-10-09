import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlmodel import Field, SQLModel


class Salon(SQLModel, table=True):
    __tablename__ = "salons"
    __table_args__ = (
        CheckConstraint("cancellation_hours >= 0", name="ck_salons_cancellation_hours"),
        CheckConstraint("deposit_amount >= 0", name="ck_salons_deposit_amount"),
        Index("ix_salons_created_at_id", "created_at", "id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    owner_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    name: str = Field(max_length=100)
    address: str = Field(max_length=255)
    phone: str = Field(max_length=30)
    image_url: str | None = Field(
        default=None, sa_column=Column(String(500), nullable=True)
    )
    cancellation_hours: int = Field(
        default=24, sa_column=Column(Integer, server_default=text("24"), nullable=False)
    )
    # Minor currency units (kobo).
    deposit_amount: int = Field(
        default=0, sa_column=Column(Integer, server_default=text("0"), nullable=False)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
