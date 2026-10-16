import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.models.salon import SalonStatus
from app.models.user import UserRole

Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]


class SuspendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Free text typed by the admin; kept in the audit log only.
    reason: Reason | None = None

    @field_validator("reason")
    @classmethod
    def blank_reason_is_none(cls, value: str | None) -> str | None:
        return value or None


class AdminSalonOwner(BaseModel):
    id: uuid.UUID
    name: str


class AdminSalonItem(BaseModel):
    """One salon in the admin list. The owner's name only: no email."""

    id: uuid.UUID
    name: str
    status: SalonStatus
    owner: AdminSalonOwner
    booking_count: int
    active_booking_count: int
    created_at: datetime


class AdminSalonStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    status: SalonStatus


class AdminUserItem(BaseModel):
    """One account in the admin list. Never any password data."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: UserRole
    created_at: datetime
