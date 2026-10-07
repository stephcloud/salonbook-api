"""Deposit payments: starting one, and applying Paystack's word that it succeeded.

Lock order for every path that touches both: booking row first (FOR NO KEY UPDATE, the
same as the expiry job, so payment inserts are never blocked), then the payment row.
Extends the order documented in `_create_booking` (client, booking rows by id) with the
payment row last. Nothing here holds a lock across a Paystack call.
"""

import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.user import User
from app.schemas.payment import ChargeSuccessData
from app.services.bookings import expiry_cutoff, sqlstate
from app.services.paystack import (
    InitializedTransaction,
    PaystackClient,
    PaystackError,
)
from app.services.slots import not_found

logger = logging.getLogger(__name__)

# Deposits are charged in one currency. A charge in anything else is a mismatch.
CURRENCY = "NGN"
UNIQUE_VIOLATION = "23505"
# At most one payment per booking may be in these states. Keep in sync with the
# `uq_payments_booking_id_live` partial index (model and migration 0009).
LIVE_STATUSES = (PaymentStatus.PENDING, PaymentStatus.PAID)
# A payment in one of these states has already been dealt with: a repeat webhook is a no-op.
SETTLED_STATUSES = frozenset(
    {PaymentStatus.PAID, PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED}
)


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """True if `signature` is the HMAC-SHA512 of the raw body under `secret`.

    Fails closed: a missing signature or an unset secret is never valid.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def _is_lapsed(booking: Booking, now: datetime) -> bool:
    """An unpaid pending booking past its window. Same predicate as the expiry job."""
    return booking.created_at < expiry_cutoff(now)


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


# --- starting a payment ---


async def start_payment(
    session: AsyncSession,
    paystack: PaystackClient,
    client: User,
    booking_id: uuid.UUID,
    now: datetime | None = None,
) -> Payment:
    """Return the Paystack checkout for a client's pending booking.

    The payment row is committed BEFORE Paystack is called, so any money that moves is
    traceable to a row. If Paystack fails the row is marked `failed` and a retry gets a
    fresh reference. Calling again while a checkout is open returns that same checkout.
    """
    now = now or datetime.now(UTC)
    email = client.email  # read before any rollback expires the instance
    try:
        payment, created = await _reserve_payment(session, client.id, booking_id, now)
    except Exception:
        await session.rollback()
        raise
    if not created:
        return payment

    # No transaction is open here: nothing is locked while we wait on Paystack.
    try:
        started = await paystack.initialize_transaction(
            email=email,
            amount=payment.amount,
            currency=payment.currency,
            reference=payment.paystack_reference,
        )
    except PaystackError:
        await _mark_failed(session, payment.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The payment provider is unavailable. Please try again.",
        ) from None
    return await _store_checkout(session, payment.id, started)


async def _reserve_payment(
    session: AsyncSession, client_id: uuid.UUID, booking_id: uuid.UUID, now: datetime
) -> tuple[Payment, bool]:
    booking = await session.get(Booking, booking_id)
    if booking is None:
        raise not_found()
    if booking.client_id != client_id:
        raise _forbidden()
    if booking.status != BookingStatus.PENDING or _is_lapsed(booking, now):
        raise _conflict("This booking can no longer be paid for.")

    existing = (
        await session.execute(
            select(Payment).where(
                Payment.booking_id == booking.id,
                Payment.status.in_(LIVE_STATUSES),  # same set as the unique index
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.status == PaymentStatus.PENDING
            and existing.authorization_url is not None
        ):
            return existing, False  # checkout already open: hand back the same one
        raise _conflict("A payment for this booking is already in progress or done.")

    deposit = await _deposit_for(session, booking)
    if deposit <= 0:
        raise _conflict("This salon does not take a deposit.")  # backstop; see schemas
    payment = Payment(
        booking_id=booking.id,
        paystack_reference=f"sb_{uuid.uuid4().hex}",
        amount=deposit,
        currency=CURRENCY,
        status=PaymentStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    session.add(payment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if sqlstate(exc) == UNIQUE_VIOLATION:  # a concurrent double-click won
            raise _conflict(
                "A payment for this booking is already in progress."
            ) from None
        raise
    return payment, True


async def _deposit_for(session: AsyncSession, booking: Booking) -> int:
    """The salon's deposit in kobo, always read from the database."""
    stylist = await session.get(User, booking.stylist_id)
    if stylist is None or stylist.salon_id is None:
        return 0
    salon = await session.get(Salon, stylist.salon_id)
    return salon.deposit_amount if salon is not None else 0


async def _mark_failed(session: AsyncSession, payment_id: uuid.UUID) -> None:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    if payment is not None and payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.FAILED
    await session.commit()


async def _store_checkout(
    session: AsyncSession, payment_id: uuid.UUID, started: InitializedTransaction
) -> Payment:
    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert (
        payment is not None
    )  # we committed it moments ago; payments are never deleted
    payment.authorization_url = started.authorization_url
    payment.access_code = started.access_code
    await session.commit()
    return payment


# --- the webhook ---


async def handle_paystack_event(
    session: AsyncSession,
    raw_body: bytes,
    signature: str | None,
    now: datetime | None = None,
) -> None:
    """Apply a Paystack event. 401 if the signature is bad; otherwise returns normally.

    The signature is checked on the raw bytes before anything else, so a forged request
    reaches neither the parser nor the database. Events we don't act on are acknowledged
    (returning normally makes the router answer 200) so Paystack stops retrying them.
    """
    if not verify_signature(raw_body, signature, settings.PAYSTACK_SECRET_KEY):
        logger.warning("paystack webhook rejected: bad signature")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature"
        )
    now = now or datetime.now(UTC)
    try:
        event = json.loads(raw_body)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid body"
        ) from None
    if not isinstance(event, dict) or event.get("event") != "charge.success":
        return
    try:
        data = ChargeSuccessData.model_validate(event.get("data"))
    except ValidationError:
        # Retrying can't fix a bad shape, but this may be a real payment: make it findable.
        raw_data = event.get("data")
        reference = raw_data.get("reference") if isinstance(raw_data, dict) else None
        logger.error(
            "signed charge.success could not be parsed; needs manual review "
            "(reference=%r)",
            reference if isinstance(reference, str) else None,
        )
        return
    try:
        await _apply_charge_success(session, data, now)
    except Exception:
        await session.rollback()  # release the row locks at once
        raise


async def _apply_charge_success(
    session: AsyncSession, data: ChargeSuccessData, now: datetime
) -> None:
    ids = (
        await session.execute(
            select(Payment.id, Payment.booking_id).where(
                Payment.paystack_reference == data.reference
            )
        )
    ).one_or_none()
    if ids is None:
        logger.warning("charge.success for unknown reference %s", data.reference)
        await session.rollback()
        return
    payment_id, booking_id = ids

    # Lock booking, then payment. populate_existing: act on the current status, never a
    # stale read, so two copies of one webhook queue here and the second sees the result.
    booking = (
        await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    payment = (
        await session.execute(
            select(Payment)
            .where(Payment.id == payment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if payment.status in SETTLED_STATUSES:
        await session.rollback()  # replay: nothing to write, just release the locks
        return

    if data.amount != payment.amount or data.currency.upper() != payment.currency:
        # Money arrived but not what we asked for. Never confirm on it: give it back.
        # The booking is left as it is and lapses on its own; until then the queued
        # refund keeps this payment "live", so /pay answers 409 for the booking.
        logger.warning("charge.success mismatch for payment %s", payment.id)
        payment.status = PaymentStatus.REFUND_PENDING
        payment.paid_at = now
        payment.received_amount = data.amount
        payment.refund_amount = data.amount
    elif (
        payment.status == PaymentStatus.PENDING
        and booking.status == BookingStatus.PENDING
        and not _is_lapsed(booking, now)
    ):
        payment.status = PaymentStatus.PAID
        payment.paid_at = now
        booking.status = BookingStatus.CONFIRMED
        booking.updated_at = now
    else:
        _settle_late_payment(booking, payment, now)
    await session.commit()


def _settle_late_payment(booking: Booking, payment: Payment, now: datetime) -> None:
    """The money is in but the booking can't have it: refund in full, never revive.

    Covers a cancelled booking, one the expiry job cancelled, a pending one past its
    window that the job hasn't reached yet, and a payment we had already marked failed.
    The booking is never confirmed, so the slot (and the exclusion constraint) is left
    alone. A lapsed pending booking is cancelled now so it stops holding the slot. A
    still-valid pending booking is left alone: its client may be paying through another
    payment, and a stale event on a failed row must not cost them the slot. Confirmed,
    completed and no-show bookings are untouched: they were paid by some other payment.
    """
    payment.status = PaymentStatus.REFUND_PENDING
    payment.paid_at = now
    payment.refund_amount = payment.amount
    if booking.status == BookingStatus.PENDING and _is_lapsed(booking, now):
        booking.status = BookingStatus.CANCELLED
        booking.cancelled_at = now
        booking.refund_due = True
        booking.updated_at = now
    elif booking.status == BookingStatus.CANCELLED:
        booking.refund_due = True
        booking.updated_at = now
