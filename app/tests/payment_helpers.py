"""Scenario builders for payment tests: a booking plus its payment, straight in the DB."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.user import User, UserRole
from app.tests.test_salons_api import make_user
from app.tests.test_slots import setup_stylist


@dataclass
class Scenario:
    salon: Salon
    stylist: User
    client: User
    headers: dict[str, str]
    booking: Booking
    payment: Payment | None

    @property
    def reference(self) -> str:
        assert self.payment is not None
        return self.payment.paystack_reference

    @property
    def amount(self) -> int:
        assert self.payment is not None
        return self.payment.amount


async def make_scenario(
    db_session: AsyncSession,
    *,
    age: timedelta = timedelta(0),
    now: datetime | None = None,
    booking_status: BookingStatus = BookingStatus.PENDING,
    payment_status: PaymentStatus | None = PaymentStatus.PENDING,
    authorization_url: str | None = "https://checkout.paystack.test/x",
) -> Scenario:
    """A client's booking created `age` before `now`, and (optionally) its payment."""
    now = now or datetime.now(UTC)
    salon, stylist, service = await setup_stylist(db_session, 60)
    client, headers = await make_user(db_session, UserRole.CLIENT)
    starts_at = now + timedelta(days=7)
    booking = Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(hours=1),
        status=booking_status,
        created_at=now - age,
        updated_at=now - age,
    )
    db_session.add(booking)
    await db_session.commit()
    payment = None
    if payment_status is not None:
        payment = Payment(
            booking_id=booking.id,
            paystack_reference=f"sb_{uuid.uuid4().hex}",
            amount=salon.deposit_amount,
            currency="NGN",
            status=payment_status,
            authorization_url=authorization_url,
            created_at=now - age,
            updated_at=now - age,
        )
        db_session.add(payment)
        await db_session.commit()
    return Scenario(salon, stylist, client, headers, booking, payment)


def row_dict(obj: Any) -> dict[str, Any]:
    """Every column value of a row, for before/after comparisons."""
    return {
        attr.key: getattr(obj, attr.key) for attr in inspect(obj).mapper.column_attrs
    }


async def reload(
    db_session: AsyncSession, scenario: Scenario
) -> tuple[Booking, Payment]:
    """Fresh copies of the scenario's booking and payment, straight from the database."""
    assert scenario.payment is not None
    booking = (
        await db_session.execute(
            select(Booking)
            .where(Booking.id == scenario.booking.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    payment = (
        await db_session.execute(
            select(Payment)
            .where(Payment.id == scenario.payment.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return booking, payment


async def all_payments(db_session: AsyncSession) -> list[Payment]:
    # Refresh in place rather than expire_all(): that would expire a scenario's own
    # objects, and reading them afterwards would try to load lazily outside a greenlet.
    result = await db_session.execute(
        select(Payment).execution_options(populate_existing=True)
    )
    return list(result.scalars())
