import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.availability_rule import AvailabilityRule, RuleKind
from app.models.booking import ACTIVE_STATUSES, Booking
from app.models.salon import Salon
from app.models.service import Service
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole

# Candidate start times are offered on this grid, whatever the service length.
SLOT_STEP_MINUTES = 15
# Slots can be listed at most this far ahead (also keeps date maths in range).
MAX_ADVANCE_DAYS = 365


def salon_timezone() -> timezone:
    return timezone(timedelta(minutes=settings.SALON_UTC_OFFSET_MINUTES))


def compute_slots(
    day: date,
    duration_minutes: int,
    working: Sequence[tuple[time, time]],
    breaks: Sequence[tuple[time, time]],
    bookings: Sequence[tuple[datetime, datetime]],
    tz: timezone,
    now: datetime,
) -> list[datetime]:
    """Start times on `day` where a service of `duration_minutes` fits.

    A start is valid when [start, start + duration) lies inside one working window,
    overlaps no break or booking, and starts after `now`. Ranges are half-open, so a
    slot may touch the edge of a break or booking. `working` and `breaks` are local
    wall-clock times; `bookings` are aware datetimes. Pure: no I/O.
    """
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=SLOT_STEP_MINUTES)
    blocked = [
        (datetime.combine(day, start, tz), datetime.combine(day, end, tz))
        for start, end in breaks
    ]
    blocked.extend(bookings)

    slots: set[datetime] = set()
    for window_start, window_end in working:
        start = datetime.combine(day, window_start, tz)
        window_close = datetime.combine(day, window_end, tz)
        while start + duration <= window_close:
            end = start + duration
            if start > now and not any(
                start < b_end and b_start < end for b_start, b_end in blocked
            ):
                slots.add(start)
            start += step
    return sorted(slots)


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")


async def load_stylist_and_service(
    session: AsyncSession, stylist_id: uuid.UUID, service_id: uuid.UUID
) -> tuple[User, Service]:
    """The stylist and a service they offer, both in the same existing salon.

    One identical 404 for every failure, so a signed-in user can't tell stylist ids
    from service ids or probe other salons.
    """
    stylist = await session.get(User, stylist_id)
    if stylist is None or stylist.role != UserRole.STYLIST or stylist.salon_id is None:
        raise not_found()
    if await session.get(Salon, stylist.salon_id) is None:
        raise not_found()
    # Only services the stylist offers (so the duration is the one being booked).
    service = (
        await session.execute(
            select(Service)
            .join(StylistService, StylistService.service_id == Service.id)
            .where(
                StylistService.stylist_id == stylist_id,
                Service.id == service_id,
                Service.salon_id == stylist.salon_id,
            )
        )
    ).scalar_one_or_none()
    if service is None:
        raise not_found()
    return stylist, service


async def get_slots(
    session: AsyncSession,
    stylist_id: uuid.UUID,
    service_id: uuid.UUID,
    day: date,
    now: datetime | None = None,
    tz: timezone | None = None,
) -> list[datetime]:
    """Bookable start times for a stylist offering a service on a salon-local date."""
    tz = tz or salon_timezone()
    now = now or datetime.now(UTC)

    _, service = await load_stylist_and_service(session, stylist_id, service_id)

    today = now.astimezone(tz).date()
    if day < today:
        return []
    if day > today + timedelta(days=MAX_ADVANCE_DAYS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"date is more than {MAX_ADVANCE_DAYS} days ahead",
        )

    rules = (
        (
            await session.execute(
                select(AvailabilityRule).where(
                    AvailabilityRule.stylist_id == stylist_id,
                    or_(
                        and_(
                            AvailabilityRule.kind == RuleKind.DAY_OFF,
                            AvailabilityRule.off_date == day,
                        ),
                        and_(
                            AvailabilityRule.kind.in_(
                                [RuleKind.WORKING, RuleKind.BREAK]
                            ),
                            AvailabilityRule.weekday == day.weekday(),
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    working: list[tuple[time, time]] = []
    breaks: list[tuple[time, time]] = []
    for rule in rules:
        if rule.kind == RuleKind.DAY_OFF:
            return []
        if rule.start_time is None or rule.end_time is None:
            continue  # unreachable: a DB CHECK gives timed rules both times
        target = working if rule.kind == RuleKind.WORKING else breaks
        target.append((rule.start_time, rule.end_time))
    if not working:
        return []

    day_start = datetime.combine(day, time.min, tz)
    day_end = day_start + timedelta(days=1)
    booked = (
        await session.execute(
            select(Booking.starts_at, Booking.ends_at).where(
                Booking.stylist_id == stylist_id,
                Booking.status.in_(ACTIVE_STATUSES),
                Booking.starts_at < day_end,
                Booking.ends_at > day_start,
            )
        )
    ).all()

    return compute_slots(
        day,
        service.duration_minutes,
        working,
        breaks,
        [(row.starts_at, row.ends_at) for row in booked],
        tz,
        now,
    )
