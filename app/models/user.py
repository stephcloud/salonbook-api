import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Column, DateTime, Enum, ForeignKey, text
from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    OWNER = "owner"
    STYLIST = "stylist"
    CLIENT = "client"


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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
