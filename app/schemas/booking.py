import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.models.service import PriceType


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


class BookingSalonSummary(BaseModel):
    """Explicit field list: no owner_id, nothing private."""

    id: uuid.UUID
    name: str
    address: str
    phone: str
    cancellation_hours: int
    deposit_amount: int
    image_url: str | None


class BookingServiceSummary(BaseModel):
    id: uuid.UUID
    name: str
    duration_minutes: int
    price_type: PriceType
    price: int | None  # null when price_type is quote


class BookingStylistSummary(BaseModel):
    """No email or role: just what the client needs to recognise the stylist."""

    id: uuid.UUID
    name: str
    image_url: str | None


class ClientBookingResponse(BookingResponse):
    """A booking with the salon, service and stylist summaries nested in."""

    salon: BookingSalonSummary
    service: BookingServiceSummary
    stylist: BookingStylistSummary


class ClientBookingDetailResponse(ClientBookingResponse):
    """Plus the latest payment (both None until the client starts paying)."""

    payment_status: PaymentStatus | None
    payment_amount: int | None


class OwnerBookingStylist(BaseModel):
    id: uuid.UUID
    name: str


class OwnerBookingService(BaseModel):
    id: uuid.UUID
    name: str
    duration_minutes: int


class OwnerBookingItem(BaseModel):
    """One row of a salon owner's booking list.

    Explicit field list on purpose: the client is shown by name only (no email, no id),
    and nothing from Paystack (reference, access code, checkout URL) is included.
    """

    id: uuid.UUID
    status: BookingStatus
    starts_at: datetime
    ends_at: datetime
    refund_due: bool | None
    payment_status: PaymentStatus | None
    stylist: OwnerBookingStylist
    service: OwnerBookingService
    client_name: str
