import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

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
DepositAmount = Annotated[int, Field(ge=0, le=100_000_000)]


class SalonCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    address: Address
    phone: Phone
    cancellation_hours: CancellationHours = 24
    deposit_amount: DepositAmount


class SalonUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    address: Address | None = None
    phone: Phone | None = None
    cancellation_hours: CancellationHours | None = None
    deposit_amount: DepositAmount | None = None


class SalonResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    name: str
    address: str
    phone: str
    cancellation_hours: int
    deposit_amount: int
    created_at: datetime
