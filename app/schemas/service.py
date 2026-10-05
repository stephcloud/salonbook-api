import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models.service import PriceType

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Duration = Annotated[int, Field(gt=0, le=24 * 60)]
Price = Annotated[int, Field(gt=0, le=100_000_000)]


def check_price_matches_type(price_type: PriceType, price: int | None) -> None:
    """Quote services have no price; fixed services must have one."""
    if price_type == PriceType.QUOTE and price is not None:
        raise ValueError("price must be null when price_type is quote")
    if price_type == PriceType.FIXED and price is None:
        raise ValueError("price is required when price_type is fixed")


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    duration_minutes: Duration
    price_type: PriceType
    price: Price | None = None

    @model_validator(mode="after")
    def _price_matches_type(self) -> Self:
        check_price_matches_type(self.price_type, self.price)
        return self


class ServiceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    duration_minutes: Duration | None = None
    price_type: PriceType | None = None
    price: Price | None = None


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    salon_id: uuid.UUID
    name: str
    duration_minutes: int
    price_type: PriceType
    price: int | None
    created_at: datetime
