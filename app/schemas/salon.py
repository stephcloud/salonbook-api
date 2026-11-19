import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from app.models.salon import SalonStatus
from app.schemas.image import ImageUrl

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Address = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]
Phone = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=30)
]
CancellationHours = Annotated[int, Field(ge=0, le=24 * 30)]
# Kobo. Must be positive: a deposit is what confirms a booking, and Paystack can't charge 0.
DepositAmount = Annotated[int, Field(gt=0, le=100_000_000)]

Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class _Location(BaseModel):
    """Optional salon location, always sent as a pair. On update, both null clears it."""

    latitude: Latitude | None = None
    longitude: Longitude | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> Self:
        sent = self.model_fields_set
        for field, other in (("latitude", "longitude"), ("longitude", "latitude")):
            missing = field not in sent or (
                getattr(self, field) is None and getattr(self, other) is not None
            )
            if other in sent and missing:
                # Raised as a ValidationError so the 422 points at the missing field.
                raise ValidationError.from_exception_data(
                    type(self).__name__,
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "location_pair",
                                "{field} is required when {other} is set",
                                {"field": field, "other": other},
                            ),
                            loc=(field,),
                            input=getattr(self, field),
                        )
                    ],
                )
        return self


class SalonCreate(_Location):
    model_config = ConfigDict(extra="forbid")

    name: Name
    address: Address
    phone: Phone
    image_url: ImageUrl | None = None
    cancellation_hours: CancellationHours = 24
    deposit_amount: DepositAmount


class SalonUpdate(_Location):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    address: Address | None = None
    phone: Phone | None = None
    # Send null to clear it (see update_salon).
    image_url: ImageUrl | None = None
    cancellation_hours: CancellationHours | None = None
    deposit_amount: DepositAmount | None = None


class SalonResponse(BaseModel):
    """The public shape: no moderation status."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    address: str
    phone: str
    image_url: str | None
    cancellation_hours: int
    deposit_amount: int
    created_at: datetime


class SalonListItem(SalonResponse):
    """A public list row. distance_km is only sent when the visitor gave lat and lng."""

    distance_km: float | None = None


class OwnerSalonResponse(SalonResponse):
    """What an owner sees of their own salon: the public fields plus status and location."""

    status: SalonStatus
    latitude: float | None
    longitude: float | None
