import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, DateTime, Enum, text
from sqlmodel import Field, SQLModel


class UserRole(StrEnum):
    OWNER = "owner"
    STYLIST = "stylist"
    CLIENT = "client"


class User(SQLModel, table=True):
    __tablename__ = "users"

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
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
