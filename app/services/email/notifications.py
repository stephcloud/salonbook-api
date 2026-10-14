"""Booking emails: load what to say, claim the send, send, release on failure.

Both entry points run as background tasks after the request has committed and answered,
in their own sessions, so nothing here holds a lock or can touch the booking, the payment
or the response. They never raise: any failure is logged and dropped.

Each email has a `*_sent_at` marker on the booking. The send is claimed first with one
atomic UPDATE ... WHERE marker IS NULL, so of any number of concurrent callers (a replayed
webhook, a double click) exactly one gets to send. If the send fails the claim is undone,
so the marker is clean again. Nothing retries it in Phase 1 (a failed email is lost; a
sweeper over NULL markers is a later step). A send that times out after the provider delivered it is
also released, so an ambiguous failure can mean a repeat rather than a loss. The gap is a
hard crash between claim and send (SIGKILL), which loses one email instead of sending two;
for a notification that is the better failure.

Logs carry the booking id and the kind of email only: never an address, a name, a body,
the API key, or a provider response.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.user import User
from app.services.email import templates
from app.services.email.base import EmailMessage, EmailSender
from app.services.email.templates import BookingEmailContext

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]
# Payments that actually took the deposit, whatever has happened to it since.
DEPOSIT_STATUSES = (
    PaymentStatus.PAID,
    PaymentStatus.REFUND_PENDING,
    PaymentStatus.REFUNDED,
)


@dataclass(frozen=True)
class _Email:
    kind: str
    marker: str  # the Booking column that records it was sent
    for_cancelled: bool  # sent only while the booking is (not) cancelled
    build: Callable[[BookingEmailContext], EmailMessage]


CLIENT_CONFIRMATION = _Email(
    "client_confirmation",
    "confirmation_email_sent_at",
    False,
    templates.client_confirmation,
)
OWNER_CONFIRMATION = _Email(
    "owner_confirmation",
    "owner_confirmation_email_sent_at",
    False,
    templates.owner_new_booking,
)
CLIENT_CANCELLATION = _Email(
    "client_cancellation",
    "cancellation_email_sent_at",
    True,
    templates.client_cancellation,
)
OWNER_CANCELLATION = _Email(
    "owner_cancellation",
    "owner_cancellation_email_sent_at",
    True,
    templates.owner_cancellation,
)


async def notify_booking_confirmed(
    session_maker: SessionFactory, sender: EmailSender | None, booking_id: uuid.UUID
) -> None:
    """Tell the client and the salon owner that the booking is confirmed."""
    await _run(
        "confirmed",
        session_maker,
        sender,
        booking_id,
        cancelled=False,
        cancelled_by_owner=False,
        emails=(CLIENT_CONFIRMATION, OWNER_CONFIRMATION),
    )


async def notify_booking_cancelled(
    session_maker: SessionFactory,
    sender: EmailSender | None,
    booking_id: uuid.UUID,
    *,
    cancelled_by_owner: bool,
) -> None:
    """Tell the client and the salon owner that a confirmed (paid) booking was cancelled.

    Callers only use this for bookings that had been confirmed. Cancelling an unpaid
    pending booking sends nothing: no deposit moved and the owner never heard of it.
    """
    await _run(
        "cancelled",
        session_maker,
        sender,
        booking_id,
        cancelled=True,
        cancelled_by_owner=cancelled_by_owner,
        emails=(CLIENT_CANCELLATION, OWNER_CANCELLATION),
    )


async def _run(
    event: str,
    session_maker: SessionFactory,
    sender: EmailSender | None,
    booking_id: uuid.UUID,
    *,
    cancelled: bool,
    cancelled_by_owner: bool,
    emails: tuple[_Email, ...],
) -> None:
    try:
        if sender is None:
            logger.info(
                "booking %s emails skipped: email is not configured "
                "(set BREVO_API_KEY and EMAIL_FROM_ADDRESS); booking=%s",
                event,
                booking_id,
            )
            return
        async with session_maker() as session:
            ctx = await _load_context(
                session,
                booking_id,
                cancelled=cancelled,
                cancelled_by_owner=cancelled_by_owner,
            )
        if ctx is None:
            logger.info(
                "booking %s emails skipped: booking not in the expected state; "
                "booking=%s",
                event,
                booking_id,
            )
            return
        for email in emails:
            await _send_once(session_maker, sender, booking_id, email, ctx)
    except Exception as exc:  # noqa: BLE001 - never break the caller's work
        logger.warning(
            "booking %s emails failed: %s; booking=%s",
            event,
            type(exc).__name__,
            booking_id,
        )


async def _load_context(
    session: AsyncSession,
    booking_id: uuid.UUID,
    *,
    cancelled: bool,
    cancelled_by_owner: bool,
) -> BookingEmailContext | None:
    """Everything the templates need, in two reads. None if the state is wrong.

    A confirmation is skipped if the booking has been cancelled since; a cancellation is
    skipped unless the booking is cancelled.
    """
    stylist, client, owner = aliased(User), aliased(User), aliased(User)
    row = (
        await session.execute(
            select(Booking, Service, Salon, stylist, client, owner)
            .join(Service, Service.id == Booking.service_id)
            .join(Salon, Salon.id == Service.salon_id)
            .join(stylist, stylist.id == Booking.stylist_id)
            .join(client, client.id == Booking.client_id)
            .join(owner, owner.id == Salon.owner_id)
            .where(Booking.id == booking_id)
        )
    ).one_or_none()
    if row is None:
        return None
    booking, service, salon, stylist_row, client_row, owner_row = row
    is_cancelled = booking.status == BookingStatus.CANCELLED
    if is_cancelled != cancelled:
        return None
    payment = (
        await session.execute(
            select(Payment)
            .where(
                Payment.booking_id == booking_id,
                Payment.status.in_(DEPOSIT_STATUSES),
            )
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return BookingEmailContext(
        salon_name=salon.name,
        salon_address=salon.address,
        salon_phone=salon.phone,
        service_name=service.name,
        stylist_name=stylist_row.name,
        client_name=client_row.name,
        client_email=client_row.email,
        owner_name=owner_row.name,
        owner_email=owner_row.email,
        starts_at=booking.starts_at,
        cancellation_hours=salon.cancellation_hours,
        deposit_amount=payment.amount if payment is not None else None,
        refund_due=booking.refund_due,
        payment_status=payment.status if payment is not None else None,
        cancelled_by_owner=cancelled_by_owner,
    )


async def _send_once(
    session_maker: SessionFactory,
    sender: EmailSender,
    booking_id: uuid.UUID,
    email: _Email,
    ctx: BookingEmailContext,
) -> None:
    """Claim, send, and give the claim back if the send fails. Never raises."""
    try:
        # Render first: a template bug must not eat the claim.
        message = email.build(ctx)
        claimed_at = await _claim(session_maker, booking_id, email)
        if claimed_at is None:
            logger.info(
                "booking email %s already sent or claimed; booking=%s",
                email.kind,
                booking_id,
            )
            return
        try:
            await sender.send(message)
        except BaseException as exc:  # undo the claim, even on shutdown
            await _release(session_maker, booking_id, email, claimed_at)
            if not isinstance(exc, Exception):
                raise  # cancellation (a redeploy) must still cancel
            logger.warning(
                "booking email %s not sent: %s; booking=%s",
                email.kind,
                type(exc).__name__,
                booking_id,
            )
    except Exception as exc:  # noqa: BLE001 - a notification must never raise
        logger.warning(
            "booking email %s failed: %s; booking=%s",
            email.kind,
            type(exc).__name__,
            booking_id,
        )


async def _claim(
    session_maker: SessionFactory, booking_id: uuid.UUID, email: _Email
) -> datetime | None:
    """Stamp the marker if it is still NULL; the stamp for exactly one caller, else None.

    The booking's status is part of the same UPDATE, so a booking cancelled (or, for a
    cancellation, not yet cancelled) since the context was loaded is not emailed about.
    """
    column = getattr(Booking, email.marker)
    stamp = datetime.now(UTC)
    status_ok = (
        Booking.status == BookingStatus.CANCELLED
        if email.for_cancelled
        else Booking.status != BookingStatus.CANCELLED
    )
    async with session_maker() as session:
        claimed = (
            await session.execute(
                update(Booking)
                .where(Booking.id == booking_id, column.is_(None), status_ok)
                # updated_at is set to itself so the column's onupdate does not fire:
                # sending an email is not a change to the booking.
                .values({email.marker: stamp, "updated_at": Booking.updated_at})
                .returning(Booking.id)
            )
        ).scalar_one_or_none()
        await session.commit()
    return stamp if claimed is not None else None


async def _release(
    session_maker: SessionFactory,
    booking_id: uuid.UUID,
    email: _Email,
    claimed_at: datetime,
) -> None:
    """Undo our own claim only. If this fails the email is lost, and that is logged."""
    column = getattr(Booking, email.marker)
    try:
        async with session_maker() as session:
            await session.execute(
                update(Booking)
                .where(Booking.id == booking_id, column == claimed_at)
                .values({email.marker: None, "updated_at": Booking.updated_at})
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - report it, never raise from cleanup
        logger.error(
            "booking email %s claim not released, so it will not be retried: %s; "
            "booking=%s",
            email.kind,
            type(exc).__name__,
            booking_id,
        )
