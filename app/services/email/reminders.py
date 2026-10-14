"""Client reminders (24h and 2h before) and the owner's daily summary.

`run_due` is called by an external scheduler every few minutes (see the cron endpoint), so
the app may sleep in between. Everything here is a pure function of the database and the
`now` it is given, safe to call repeatedly and concurrently:

- A reminder is due while its moment (`starts_at - lead`) has arrived and is less than the
  tolerance old, so a missed run catches up but a stale reminder is dropped. A booking
  confirmed after that moment (its payment's `paid_at`) never gets that reminder, so a
  booking made 1 hour ahead gets no 24h one.
- Each send is claimed first by an atomic UPDATE on a sent-at marker (the same
  claim-then-release pattern as the booking emails), so concurrent runs send once. A
  temporary failure releases the claim and the next run retries, inside the tolerance
  window only; a permanent rejection (HTTP 4xx) keeps it, so a bad address is not retried.
- One failed email never stops the others, and nothing here raises. Batches are capped, and
  what is left over is picked up by the next run.

Logs carry ids and counts only: never an address, a name or a body.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import aliased

from app.models.booking import Booking, BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.user import User
from app.services.email import templates
from app.services.email.base import EmailSender, EmailSendError
from app.services.email.notifications import SessionFactory, _Email, _send_once
from app.services.email.templates import (
    LAGOS,
    BookingEmailContext,
    OwnerSummaryContext,
    SummaryEntry,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 25  # emails per kind per run
SUMMARY_START = time(7, 0)  # Lagos
SUMMARY_END = time(9, 0)  # a summary missed until here is skipped, not sent at noon
SUMMARY_MAX_ENTRIES = 50
# A provider that is down fails every send; after this many in a row the batch stops and the
# next run tries again, instead of waiting out a timeout per email.
MAX_CONSECUTIVE_FAILURES = 3
_run_lock = asyncio.Lock()  # one run at a time in this process


@dataclass(frozen=True)
class _Window:
    email: _Email
    lead: timedelta
    tolerance: timedelta


WINDOWS = (
    _Window(
        _Email(
            "client_reminder_24h",
            "reminder_24h_email_sent_at",
            False,
            templates.client_reminder_24h,
            confirmed_and_upcoming=True,
        ),
        timedelta(hours=24),
        timedelta(hours=3),
    ),
    _Window(
        _Email(
            "client_reminder_2h",
            "reminder_2h_email_sent_at",
            False,
            templates.client_reminder_2h,
            confirmed_and_upcoming=True,
        ),
        timedelta(hours=2),
        timedelta(minutes=45),
    ),
)


@dataclass
class RunCounts:
    reminder_24h: int = 0
    reminder_2h: int = 0
    owner_summaries: int = 0


async def run_due(
    session_maker: SessionFactory,
    sender: EmailSender | None,
    now: datetime | None = None,
) -> RunCounts:
    """Send every reminder and summary that is due at `now`. Never raises."""
    counts = RunCounts()
    now = now or datetime.now(UTC)
    if sender is None:
        logger.info(
            "reminders skipped: email is not configured "
            "(set BREVO_API_KEY and EMAIL_FROM_ADDRESS)"
        )
        return counts
    for window in WINDOWS:
        try:
            sent = await _send_reminders(session_maker, sender, window, now)
        except Exception as exc:  # noqa: BLE001 - the next kind must still run
            logger.warning(
                "reminder run %s failed: %s", window.email.kind, type(exc).__name__
            )
            continue
        if window.email.kind == "client_reminder_24h":
            counts.reminder_24h = sent
        else:
            counts.reminder_2h = sent
    try:
        counts.owner_summaries = await _send_owner_summaries(session_maker, sender, now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("owner summary run failed: %s", type(exc).__name__)
    logger.info(
        "reminders sent: 24h=%d 2h=%d owner_summaries=%d",
        counts.reminder_24h,
        counts.reminder_2h,
        counts.owner_summaries,
    )
    return counts


async def run_due_safely(
    session_maker: SessionFactory,
    sender: EmailSender | None,
    now: datetime | None = None,
) -> None:
    """`run_due` for a background task: whatever goes wrong is logged, never raised."""
    if _run_lock.locked():
        # The previous run is still going (a slow provider): stacking another would only
        # add load. The scheduler calls again in a few minutes.
        logger.info("reminder run skipped: the previous run is still going")
        return
    async with _run_lock:
        try:
            await run_due(session_maker, sender, now)
        except Exception as exc:  # noqa: BLE001 - a scheduler call must not take the app down
            logger.error("reminder run crashed: %s", type(exc).__name__)


async def _send_reminders(
    session_maker: SessionFactory,
    sender: EmailSender,
    window: _Window,
    now: datetime,
) -> int:
    column = getattr(Booking, window.email.marker)
    stylist, client, owner = aliased(User), aliased(User), aliased(User)
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(
                    Booking.id,
                    Booking.starts_at,
                    Salon.name,
                    Salon.address,
                    Salon.phone,
                    Salon.cancellation_hours,
                    Service.name,
                    stylist.name,
                    client.name,
                    client.email,
                    owner.name,
                    owner.email,
                    Payment.amount,
                )
                .join(Service, Service.id == Booking.service_id)
                .join(Salon, Salon.id == Service.salon_id)
                .join(stylist, stylist.id == Booking.stylist_id)
                .join(client, client.id == Booking.client_id)
                .join(owner, owner.id == Salon.owner_id)
                .join(
                    Payment,
                    and_(
                        Payment.booking_id == Booking.id,
                        Payment.status == PaymentStatus.PAID,
                    ),
                )
                .where(
                    Booking.status == BookingStatus.CONFIRMED,
                    column.is_(None),
                    Booking.starts_at > now,
                    # The reminder moment has arrived, and is not older than the tolerance.
                    Booking.starts_at <= now + window.lead,
                    Booking.starts_at > now + window.lead - window.tolerance,
                    # Confirmed before that moment: a late-made booking is not reminded.
                    Payment.paid_at <= Booking.starts_at - window.lead,
                )
                .order_by(Booking.starts_at, Booking.id)
                .limit(BATCH_SIZE)
            )
        ).all()
    sent = failures = 0
    for (
        booking_id,
        starts_at,
        salon_name,
        salon_address,
        salon_phone,
        cancellation_hours,
        service_name,
        stylist_name,
        client_name,
        client_email,
        owner_name,
        owner_email,
        amount,
    ) in rows:
        ctx = BookingEmailContext(
            salon_name=salon_name,
            salon_address=salon_address,
            salon_phone=salon_phone,
            service_name=service_name,
            stylist_name=stylist_name,
            client_name=client_name,
            client_email=client_email,
            owner_name=owner_name,
            owner_email=owner_email,
            starts_at=starts_at,
            cancellation_hours=cancellation_hours,
            deposit_amount=amount,
        )
        if await _send_once(session_maker, sender, booking_id, window.email, ctx, now):
            sent += 1
            failures = 0
        else:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning("reminder run %s stopped early", window.email.kind)
                break
    return sent


async def _send_owner_summaries(
    session_maker: SessionFactory, sender: EmailSender, now: datetime
) -> int:
    local_now = now.astimezone(LAGOS)
    today = local_now.date()
    if not SUMMARY_START <= local_now.time() < SUMMARY_END:
        return 0
    day_end = datetime.combine(today, time.min, LAGOS) + timedelta(days=1)

    stylist, client = aliased(User), aliased(User)
    not_done = or_(
        User.daily_summary_sent_on.is_(None), User.daily_summary_sent_on < today
    )
    upcoming_today = and_(
        Booking.status == BookingStatus.CONFIRMED,
        Booking.starts_at >= now,
        Booking.starts_at < day_end,
    )
    async with session_maker() as session:
        # Only owners who have something to hear about: an empty day sends nothing.
        owners = (
            await session.execute(
                select(User.id, User.name, User.email)
                .where(
                    not_done,
                    User.id.in_(
                        select(Salon.owner_id)
                        .join(Service, Service.salon_id == Salon.id)
                        .join(Booking, Booking.service_id == Service.id)
                        .where(upcoming_today)
                    ),
                )
                .order_by(User.id)
                .limit(BATCH_SIZE)
            )
        ).all()
        if not owners:
            return 0
        rows = (
            await session.execute(
                select(
                    Salon.owner_id,
                    Booking.starts_at,
                    Salon.name,
                    Service.name,
                    stylist.name,
                    client.name,
                )
                .join(Service, Service.id == Booking.service_id)
                .join(Salon, Salon.id == Service.salon_id)
                .join(stylist, stylist.id == Booking.stylist_id)
                .join(client, client.id == Booking.client_id)
                .where(upcoming_today, Salon.owner_id.in_([o[0] for o in owners]))
                .order_by(Booking.starts_at, Booking.id)
            )
        ).all()
    entries: dict[uuid.UUID, list[SummaryEntry]] = {}
    for (
        owner_id,
        starts_at,
        salon_name,
        service_name,
        stylist_name,
        client_name,
    ) in rows:
        entries.setdefault(owner_id, []).append(
            SummaryEntry(starts_at, salon_name, service_name, stylist_name, client_name)
        )
    sent = failures = 0
    for owner_id, owner_name, owner_email in owners:
        owner_entries = entries.get(owner_id, [])
        if not owner_entries:
            continue
        ctx = OwnerSummaryContext(
            owner_name=owner_name,
            owner_email=owner_email,
            day=today,
            entries=tuple(owner_entries[:SUMMARY_MAX_ENTRIES]),
            more=max(0, len(owner_entries) - SUMMARY_MAX_ENTRIES),
        )
        if await _send_summary_once(session_maker, sender, owner_id, today, ctx):
            sent += 1
            failures = 0
        else:
            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.warning("owner summary run stopped early")
                break
    return sent


async def _send_summary_once(
    session_maker: SessionFactory,
    sender: EmailSender,
    owner_id: uuid.UUID,
    today: date,
    ctx: OwnerSummaryContext,
) -> bool:
    """Claim the owner's day, send, release on a temporary failure. Never raises."""
    try:
        message = templates.owner_daily_summary(ctx)  # render first, as for bookings
        async with session_maker() as session:
            claimed = (
                await session.execute(
                    update(User)
                    .where(
                        User.id == owner_id,
                        or_(
                            User.daily_summary_sent_on.is_(None),
                            User.daily_summary_sent_on < today,
                        ),
                    )
                    .values(daily_summary_sent_on=today)
                    .returning(User.id)
                )
            ).scalar_one_or_none()
            await session.commit()
        if claimed is None:
            logger.info("owner summary already sent or claimed; owner=%s", owner_id)
            return False
        try:
            await sender.send(message)
        except BaseException as exc:  # undo the claim, even on shutdown
            if isinstance(exc, EmailSendError) and exc.permanent:
                logger.warning(
                    "owner summary rejected for good, not retried; owner=%s", owner_id
                )
                return False
            await _release_summary(session_maker, owner_id, today)
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "owner summary not sent: %s; owner=%s", type(exc).__name__, owner_id
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - a notification must never raise
        logger.warning(
            "owner summary failed: %s; owner=%s", type(exc).__name__, owner_id
        )
        return False


async def _release_summary(
    session_maker: SessionFactory, owner_id: uuid.UUID, today: date
) -> None:
    try:
        async with session_maker() as session:
            await session.execute(
                update(User)
                .where(User.id == owner_id, User.daily_summary_sent_on == today)
                .values(daily_summary_sent_on=None)
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - report it, never raise from cleanup
        logger.error(
            "owner summary claim not released, so it will not be retried: %s; owner=%s",
            type(exc).__name__,
            owner_id,
        )
