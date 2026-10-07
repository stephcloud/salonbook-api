import uuid
from datetime import UTC, datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import ACTIVE_STATUSES, Booking, BookingStatus
from app.models.salon import Salon
from app.models.user import User
from app.schemas.booking import BookingCreate
from app.services.refund_policy import is_refundable
from app.services.slots import (
    MAX_ADVANCE_DAYS,
    get_slots,
    load_stylist_and_service,
    not_found,
    salon_timezone,
)

# An unpaid pending booking holds its slot this long, then it is cancelled.
PENDING_EXPIRY_MINUTES = 15
# Unexpired pending bookings one client may hold at once (slot-griefing guard).
MAX_PENDING_PER_CLIENT = 3
# Postgres sqlstates.
EXCLUSION_VIOLATION = "23P01"
FOREIGN_KEY_VIOLATION = "23503"
SERIALIZATION_FAILURE = "40001"
DEADLOCK_DETECTED = "40P01"
# Losing a race for a slot. Two simultaneous inserts of the same range can end in
# an exclusion violation, or in a deadlock the server resolves by aborting one.
LOST_RACE_STATES = frozenset(
    {EXCLUSION_VIOLATION, SERIALIZATION_FAILURE, DEADLOCK_DETECTED}
)


def slot_unavailable() -> HTTPException:
    # Deliberately vague: don't reveal why, or who holds the slot.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="That time slot is no longer available. Please pick another time.",
    )


def too_many_pending() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"You already have {MAX_PENDING_PER_CLIENT} unpaid pending bookings. "
            f"Complete one or wait {PENDING_EXPIRY_MINUTES} minutes for it to expire."
        ),
    )


def sqlstate(exc: DBAPIError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def is_overlap(exc: DBAPIError) -> bool:
    return sqlstate(exc) == EXCLUSION_VIOLATION


def is_lost_race(exc: DBAPIError) -> bool:
    return sqlstate(exc) in LOST_RACE_STATES


def _expiry_cutoff(now: datetime) -> datetime:
    """Pending bookings created before this are lapsed (exactly 15:00 still holds)."""
    return now - timedelta(minutes=PENDING_EXPIRY_MINUTES)


async def expire_pending_bookings(
    session: AsyncSession,
    now: datetime | None = None,
    stylist_id: uuid.UUID | None = None,
) -> int:
    """Cancel lapsed pending bookings; return how many. The caller commits.

    Idempotent and safe to run concurrently (several workers, the job plus a booking
    request): rows are locked in id order, so runs queue instead of deadlocking, and
    a row another run already cancelled no longer matches once the lock is granted.
    With `stylist_id`, only that stylist's bookings are touched.
    """
    now = now or datetime.now(UTC)
    lapsed = (
        select(Booking.id)
        .where(
            Booking.status == BookingStatus.PENDING,
            Booking.created_at < _expiry_cutoff(now),
        )
        .order_by(Booking.id)
        # NO KEY UPDATE: only `status` changes, and a plain FOR UPDATE would block
        # the FOR KEY SHARE that rows referencing a booking (payments) take.
        .with_for_update(key_share=True)
    )
    if stylist_id is not None:
        lapsed = lapsed.where(Booking.stylist_id == stylist_id)
    ids = list((await session.execute(lapsed)).scalars())
    if not ids:
        return 0
    await session.execute(
        update(Booking)
        .where(Booking.id.in_(ids), Booking.status == BookingStatus.PENDING)
        .values(status=BookingStatus.CANCELLED, updated_at=now)
    )
    return len(ids)


def _forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _not_cancellable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="This booking can no longer be cancelled.",
    )


def _already_started() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="This booking has already started and can no longer be cancelled.",
    )


async def cancel_booking(
    session: AsyncSession,
    booking_id: uuid.UUID,
    user: User,
    now: datetime | None = None,
) -> Booking:
    """Cancel a pending or confirmed booking and record the refund decision.

    Only the booking's client or the owner of its salon may cancel. The row is locked
    and its status checked in one transaction, so concurrent cancels queue up and the
    later ones find it already cancelled: that returns the booking unchanged (no
    write), so cancelling twice is safe. Completed and no-show bookings, and bookings
    whose start time has passed, are 409. A salon owner's cancel of a confirmed booking
    is always refund_due; a client's follows the salon's cancellation window.
    `refund_due` is only recorded here; the Paystack refund belongs to the payments step.
    """
    now = now or datetime.now(UTC)
    try:
        return await _cancel_booking(session, booking_id, user, now)
    except Exception:
        await session.rollback()  # release the row lock at once
        raise


async def _cancel_booking(
    session: AsyncSession, booking_id: uuid.UUID, user: User, now: datetime
) -> Booking:
    booking = (
        await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .with_for_update()
            .execution_options(populate_existing=True)  # never act on a stale status
        )
    ).scalar_one_or_none()
    if booking is None:
        raise not_found()

    stylist = await session.get(User, booking.stylist_id)
    salon = (
        await session.get(Salon, stylist.salon_id)
        if stylist is not None and stylist.salon_id is not None
        else None
    )
    is_client = booking.client_id == user.id
    is_salon_owner = salon is not None and salon.owner_id == user.id
    if not (is_client or is_salon_owner):
        raise _forbidden()

    if booking.status == BookingStatus.CANCELLED:
        # Nothing to write. Commit just ends the transaction (releasing the row lock);
        # rollback would expire the instance and break serialising it.
        await session.commit()
        return booking
    if booking.status not in ACTIVE_STATUSES:
        raise _not_cancellable()
    if booking.starts_at <= now:
        raise _already_started()

    # A pending booking has paid no deposit, so there is nothing to refund. When the
    # salon owner cancels, the client always gets the deposit back: the late-cancel
    # window only applies to a client who backs out.
    refund_due = booking.status == BookingStatus.CONFIRMED and (
        is_salon_owner
        or (
            salon is not None
            and is_refundable(booking.starts_at, now, salon.cancellation_hours)
        )
    )
    booking.status = BookingStatus.CANCELLED
    booking.cancelled_at = now
    booking.refund_due = refund_due
    booking.updated_at = now
    await session.commit()
    return booking


async def _count_unexpired_pending(
    session: AsyncSession, client_id: uuid.UUID, now: datetime
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Booking)
        .where(
            Booking.client_id == client_id,
            Booking.status == BookingStatus.PENDING,
            Booking.created_at >= _expiry_cutoff(now),
        )
    )
    return result.scalar_one()


def _check_start_range(starts_at: datetime, now: datetime, tz: timezone) -> None:
    """422 unless `starts_at` is in the future and within the advance limit.

    Compares the aware values as given, before any conversion: a client can send
    year 1 or 9999, which would overflow `astimezone`.
    """
    if starts_at <= now:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="starts_at must be in the future",
        )
    too_far = HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"starts_at is more than {MAX_ADVANCE_DAYS} days ahead",
    )
    if starts_at - now > timedelta(days=MAX_ADVANCE_DAYS + 2):
        raise too_far
    last_day = now.astimezone(tz).date() + timedelta(days=MAX_ADVANCE_DAYS)
    if starts_at.astimezone(tz).date() > last_day:
        raise too_far


async def create_booking(
    session: AsyncSession,
    client: User,
    data: BookingCreate,
    now: datetime | None = None,
    tz: timezone | None = None,
) -> Booking:
    """Create a pending booking for `client`, re-checking the slot server-side.

    Everything runs in one transaction: lapsed pending bookings for the stylist are
    cancelled first, so they can never block the slot, whether or not the scheduled
    expiry job has run. The exclusion constraint is the final arbiter of a race.
    A rejected request rolls back at once, releasing the client row lock and
    discarding the cleanup, instead of waiting for the session to be closed.
    """
    tz = tz or salon_timezone()
    now = now or datetime.now(UTC)
    _check_start_range(data.starts_at, now, tz)  # before any DB work
    try:
        return await _create_booking(session, client, data, now, tz)
    except HTTPException:
        # Rolling back expires the loaded client, stylist and service: don't read
        # their attributes after a rejection.
        await session.rollback()
        raise


async def _create_booking(
    session: AsyncSession,
    client: User,
    data: BookingCreate,
    now: datetime,
    tz: timezone,
) -> Booking:
    stylist, service = await load_stylist_and_service(
        session, data.stylist_id, data.service_id
    )
    starts_at = data.starts_at.astimezone(UTC)
    local_start = starts_at.astimezone(tz)

    # Serialize this client's concurrent requests so the pending cap can't be raced.
    # Lock order everywhere: the client row first, then booking rows in id order.
    # Any future path (cancel, reschedule, webhook) must follow it to stay
    # deadlock-free.
    await session.execute(
        select(User.id).where(User.id == client.id).with_for_update(key_share=True)
    )
    pending = await _count_unexpired_pending(session, client.id, now)
    if pending >= MAX_PENDING_PER_CLIENT:
        raise too_many_pending()

    await expire_pending_bookings(session, now, stylist_id=stylist.id)

    # Don't trust the slot list the client saw: recompute and require an exact match.
    slots = await get_slots(
        session, stylist.id, service.id, local_start.date(), now=now, tz=tz
    )
    if local_start not in slots:
        raise slot_unavailable()

    booking = Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=starts_at,
        ends_at=starts_at + timedelta(minutes=service.duration_minutes),
        status=BookingStatus.PENDING,
        created_at=now,
        updated_at=now,
    )
    session.add(booking)
    try:
        await session.commit()
    except DBAPIError as exc:
        await session.rollback()
        if is_lost_race(exc):
            raise slot_unavailable() from None
        if sqlstate(exc) == FOREIGN_KEY_VIOLATION:
            raise not_found() from None  # stylist or service deleted mid-request
        raise
    return booking
