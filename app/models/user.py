import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    String,
    text,
)
from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    OWNER = "owner"
    STYLIST = "stylist"
    CLIENT = "client"
    # The platform owner. Never creatable through the API: only the make_admin job grants it.
    ADMIN = "admin"


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "salon_id IS NULL OR role = 'stylist'",
            name="ck_users_salon_id_only_for_stylist",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=100)
    email: str = Field(max_length=254, unique=True, index=True)
    password_hash: str
    role: UserRole = Field(
        default=UserRole.CLIENT,
        sa_column=Column(
            Enum(
                UserRole,
                name="userrole",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )
    # Set only for stylists: the salon they work at. salons.owner_id points back at
    # users, so this FK is a cycle: use_alter + a name lets create_all/drop_all
    # (and Alembic) add and drop it separately from the tables.
    salon_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey(
                "salons.id",
                ondelete="RESTRICT",
                use_alter=True,
                name="fk_users_salon_id_salons",
            ),
            nullable=True,
            index=True,
        ),
    )
    # Profile photo URL (stylists show one on the public list). Only the URL is stored.
    image_url: str | None = Field(
        default=None, sa_column=Column(String(500), nullable=True)
    )
    # Owners only: the Lagos date of the last daily summary email sent (or given up on after
    # a permanent rejection). Claimed with an atomic UPDATE, so one summary per owner per day.
    daily_summary_sent_on: date | None = Field(
        default=None, sa_column=Column(Date, nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
