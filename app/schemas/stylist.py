import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.schemas.image import ImageUrl
from app.schemas.user import Name, Password


class StylistCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    email: EmailStr
    password: Password
    image_url: ImageUrl | None = None


class StylistResponse(BaseModel):
    """Owner-facing view of a stylist."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: str
    salon_id: uuid.UUID
    image_url: str | None


class StylistServicesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_ids: list[uuid.UUID] = Field(max_length=100)


class StylistServicesResponse(BaseModel):
    stylist_id: uuid.UUID
    service_ids: list[uuid.UUID]


class PublicStylistResponse(BaseModel):
    """Public view: deliberately no email or other personal fields."""

    id: uuid.UUID
    name: str
    image_url: str | None
    service_ids: list[uuid.UUID]
