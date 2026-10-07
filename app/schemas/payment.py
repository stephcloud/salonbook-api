import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.payment import PaymentStatus


class PaymentResponse(BaseModel):
    """What the client needs to pay. Never includes Paystack secrets or refund internals."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    booking_id: uuid.UUID
    amount: int
    currency: str
    status: PaymentStatus
    paystack_reference: str
    authorization_url: str | None
    created_at: datetime


class RefundEventData(BaseModel):
    """The part of a Paystack refund.* event we read: which transaction it is about."""

    model_config = ConfigDict(extra="ignore")

    transaction_reference: str = Field(min_length=1, max_length=100)


class ChargeSuccessData(BaseModel):
    """The parts of a Paystack charge.success event we read. Anything else is ignored."""

    model_config = ConfigDict(extra="ignore")

    reference: str = Field(min_length=1, max_length=100)
    amount: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
