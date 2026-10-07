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
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import Interval, case, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.user import User
from app.schemas.payment import ChargeSuccessData, RefundEventData
from app.services import bookings as booking_service
from app.services.bookings import expiry_cutoff, sqlstate
from app.services.paystack import (
    ACCEPTED_REFUND_STATUSES,
    InitializedTransaction,
    PaystackClient,
    PaystackError,
    RefundResult,
)
from app.services.slots import not_found

logger = logging.getLogger(__name__)

# Deposits are charged in one currency. A charge in anything else is a mismatch.
CURRENCY = "NGN"
UNIQUE_VIOLATION = "23505"
# At most one payment per booking may be in these states. Keep in sync with the
# `uq_payments_booking_id_live` partial index (model and migration 0009).
LIVE_STATUSES = (PaymentStatus.PENDING, PaymentStatus.PAID)
# Refund retries. A refund attempt that got no answer is not retried before this long has
# passed, so a slow Paystack can't be hit twice at once and the sweeper can't trip over a
# live attempt. The first few retries are quick (a blip); after that hourly, so a long
# outage or an empty Paystack balance can't use up the budget in minutes and quietly
# abandon the customer's money.
REFUND_RETRY_AFTER = timedelta(minutes=5)
REFUND_FAST_ATTEMPTS = 3
REFUND_SLOW_RETRY_AFTER = timedelta(hours=1)
# About two days of trying. After this a refund is left for a human, and every failed
# attempt from the slow phase on is logged at error level so it is seen long before.
# To retry an exhausted refund after fixing the cause:
#   UPDATE payments SET refund_attempts = 0 WHERE status = 'refund_pending';
MAX_REFUND_ATTEMPTS = 50
SessionFactory = async_sessionmaker[AsyncSession]
REFUND_EVENTS = frozenset({"refund.processed", "refund.failed"})
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
    # Booking row first (then the payment insert), as everywhere: a cancel can't slip in
    # between the status check and the insert.
    booking = (
        await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
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
) -> list[uuid.UUID]:
    """Apply a Paystack event; return the payments now waiting for a refund to be sent.

    401 if the signature is bad; otherwise returns normally. The signature is checked on
    the raw bytes before anything else, so a forged request reaches neither the parser
    nor the database. Events we don't act on are acknowledged (returning normally makes
    the router answer 200) so Paystack stops retrying them. The caller starts the
    refunds after this has committed: nothing here calls Paystack.
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
    kind = event.get("event") if isinstance(event, dict) else None
    if not isinstance(kind, str):
        return []
    if kind in REFUND_EVENTS:
        return await _handle_refund_event(session, kind, event.get("data"), now)
    if kind != "charge.success":
        return []
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
        return []
    try:
        payment_id = await _apply_charge_success(session, data, now)
    except Exception:
        await session.rollback()  # release the row locks at once
        raise
    return [payment_id] if payment_id is not None else []


async def _apply_charge_success(
    session: AsyncSession, data: ChargeSuccessData, now: datetime
) -> uuid.UUID | None:
    """Apply a charge.success. Returns the payment id if it now needs a refund sent."""
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
        return None
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
        return None

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
    needs_refund = payment.status == PaymentStatus.REFUND_PENDING
    await session.commit()
    return payment.id if needs_refund else None


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
        # Overwrites a recorded "no refund" (a late client cancel): that decision was
        # about a deposit the salon kept, and this is a different, unwanted payment.
        booking.refund_due = True
        booking.updated_at = now


# --- refund events from Paystack ---


async def _handle_refund_event(
    session: AsyncSession, kind: str, raw_data: object, now: datetime
) -> list[uuid.UUID]:
    """refund.processed settles a queued refund; refund.failed reopens a finished one."""
    try:
        data = RefundEventData.model_validate(raw_data)
    except ValidationError:
        logger.error("signed %s could not be parsed; needs manual review", kind)
        return []
    try:
        # Only the payment row is touched, so no booking lock (lock order stays valid).
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.paystack_reference == data.transaction_reference)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if payment is None:
            logger.warning(
                "%s for unknown reference %s", kind, data.transaction_reference
            )
            await session.rollback()
            return []
        if (
            kind == "refund.processed"
            and payment.status == PaymentStatus.REFUND_PENDING
        ):
            payment.status = PaymentStatus.REFUNDED
            payment.refunded_at = now
        elif kind == "refund.failed" and payment.status in (
            PaymentStatus.REFUNDED,
            PaymentStatus.REFUND_PENDING,
        ):
            # Paystack could not deliver it: we still owe it. Reopen it so it is retried
            # (the attempt budget is deliberately not reset, so a refund that fails for
            # good ends up with a human instead of looping). Clearing the claim stamp also
            # tells an attempt that is mid-flight, which will see this on finalize, that
            # the refund it is about to record has failed.
            logger.error("refund failed at Paystack for payment %s", payment.id)
            payment.status = PaymentStatus.REFUND_PENDING
            payment.refunded_at = None
            payment.refund_attempted_at = None
        else:
            await session.rollback()  # replay or nothing to change
            return []
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return []


# --- sending refunds ---


async def cancel_booking_and_list_refunds(
    session: AsyncSession, booking_id: uuid.UUID, user: User
) -> tuple[Booking, list[uuid.UUID]]:
    """Cancel a booking, then say which refunds the cancel queued.

    The cancel (and its refund decision) commits first and can fail on its own terms.
    Listing what to send is best effort: if it fails the cancel still stands, and the
    retry job sends the refund, so the client never gets a 500 for a cancel that worked.
    """
    booking = await booking_service.cancel_booking(session, booking_id, user)
    try:
        queued = await refunds_waiting_for_booking(session, booking.id)
    except Exception:
        logger.exception("could not list queued refunds for booking %s", booking.id)
        queued = []
    return booking, queued


async def refunds_waiting_for_booking(
    session: AsyncSession, booking_id: uuid.UUID
) -> list[uuid.UUID]:
    """Ids of this booking's payments whose refund is queued (e.g. just after a cancel)."""
    result = await session.execute(
        select(Payment.id).where(
            Payment.booking_id == booking_id,
            Payment.status == PaymentStatus.REFUND_PENDING,
        )
    )
    return list(result.scalars())


def _refund_due_clause(now: datetime) -> list[object]:
    """Rows a refund attempt may pick up: waiting, not exhausted, not mid-attempt."""
    retry_after = case(
        (
            Payment.refund_attempts < REFUND_FAST_ATTEMPTS,
            literal(REFUND_RETRY_AFTER, Interval()),
        ),
        else_=literal(REFUND_SLOW_RETRY_AFTER, Interval()),
    )
    return [
        Payment.status == PaymentStatus.REFUND_PENDING,
        Payment.refund_attempts < MAX_REFUND_ATTEMPTS,
        or_(
            Payment.refund_attempted_at.is_(None),
            Payment.refund_attempted_at + retry_after < now,
        ),
    ]


async def list_refunds_due(
    session_factory: SessionFactory, now: datetime | None = None, limit: int = 20
) -> list[uuid.UUID]:
    """Ids of payments whose refund should be (re)tried now, oldest first."""
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        result = await session.execute(
            select(Payment.id)
            .where(*_refund_due_clause(now))
            .order_by(Payment.created_at)
            .limit(limit)
        )
        return list(result.scalars())


async def try_refund(
    session_factory: SessionFactory,
    paystack: PaystackClient,
    payment_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """`process_refund` for callers that must not raise: background tasks, the sweeper.

    Whatever goes wrong is logged with its traceback and the refund simply stays
    `refund_pending` for the next sweeper run.
    """
    try:
        return await process_refund(session_factory, paystack, payment_id, now)
    except Exception:
        logger.exception("refund for payment %s crashed; will be retried", payment_id)
        return False


async def process_refund(
    session_factory: SessionFactory,
    paystack: PaystackClient,
    payment_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Send the refund for one `refund_pending` payment. True if this call settled it.

    Three short steps, and no lock is ever held while Paystack is called:
      1. claim: lock the row (skipping it if another worker has it), stamp
         `refund_attempted_at`, commit. The stamp is what keeps two workers, or a
         worker and the sweeper, from refunding the same payment at once.
      2. ask Paystack. Every attempt first asks whether a refund already exists for this
         transaction (an earlier attempt's answer may have been lost, or a replayed
         refund.failed may be stale) and only creates one if not.
      3. finalize: lock again, and only if it is still `refund_pending`, and no
         refund.failed arrived meanwhile, mark it refunded.
    Safe to call repeatedly and concurrently. Any Paystack failure leaves the payment
    `refund_pending`, to be retried later (quickly at first, then hourly).
    """
    now = now or datetime.now(UTC)
    claim = await _claim_refund(session_factory, payment_id, now)
    if claim is None:
        return False
    reference, amount, attempt = claim
    try:
        result = await paystack.find_refund(reference=reference)
        if result is None:
            result = await paystack.create_refund(reference=reference, amount=amount)
    except PaystackError as exc:
        # From the slow phase on, something lasting is wrong (an outage, an empty
        # balance): make it loud while there are still attempts left.
        level = logging.ERROR if attempt > REFUND_FAST_ATTEMPTS else logging.WARNING
        logger.log(
            level,
            "refund for payment %s not sent (attempt %d of %d): %s",
            payment_id,
            attempt,
            MAX_REFUND_ATTEMPTS,
            exc,
        )
        if attempt >= MAX_REFUND_ATTEMPTS:
            logger.error(
                "refund for payment %s gave up after %d attempts; needs manual review",
                payment_id,
                attempt,
            )
        return False
    return await _finalize_refund(session_factory, payment_id, result, now)


async def _claim_refund(
    session_factory: SessionFactory, payment_id: uuid.UUID, now: datetime
) -> tuple[str, int, int] | None:
    """(reference, amount to refund, attempt number), or None if nothing is due."""
    async with session_factory() as session:
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.id == payment_id, *_refund_due_clause(now))
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if payment is None:
            return None
        payment.refund_attempted_at = now
        payment.refund_attempts += 1
        claim = (
            payment.paystack_reference,
            payment.refund_amount
            if payment.refund_amount is not None
            else payment.amount,
            payment.refund_attempts,
        )
        await session.commit()
        return claim


async def _finalize_refund(
    session_factory: SessionFactory,
    payment_id: uuid.UUID,
    result: RefundResult,
    now: datetime,
) -> bool:
    async with session_factory() as session:
        payment = (
            await session.execute(
                select(Payment)
                .where(Payment.id == payment_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        if payment.status != PaymentStatus.REFUND_PENDING:
            # refund.processed beat us to it. Keep the audit id, but we didn't settle it.
            if payment.paystack_refund_id is None and result.refund_id is not None:
                payment.paystack_refund_id = result.refund_id
                await session.commit()
            else:
                await session.rollback()
            return False
        if payment.refund_attempted_at is None:
            # A refund.failed cleared our claim while we were talking to Paystack: the
            # refund we just saw is not the one that will arrive. Leave it due.
            logger.warning("refund for payment %s failed mid-attempt", payment_id)
            await session.rollback()
            return False
        if result.status not in ACCEPTED_REFUND_STATUSES:
            logger.error(
                "refund for payment %s is in state %r at Paystack; needs review",
                payment_id,
                result.status,
            )
            await session.rollback()
            return False
        payment.status = PaymentStatus.REFUNDED
        payment.refunded_at = now
        payment.paystack_refund_id = result.refund_id
        await session.commit()
        return True
