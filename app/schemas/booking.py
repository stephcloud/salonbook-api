import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus


class BookingCreate(BaseModel):
    """The client is always the caller, so there is no client_id field."""

    model_config = ConfigDict(extra="forbid")

    stylist_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: AwareDatetime  # must carry a UTC offset


class BookingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    client_id: uuid.UUID
    stylist_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    status: BookingStatus
    refund_due: bool | None
    cancelled_at: datetime | None
    created_at: datetime


class BookingDetailResponse(BookingResponse):
    """One booking plus its latest payment (both None until the client starts paying)."""

    payment_status: PaymentStatus | None
    payment_amount: int | None
