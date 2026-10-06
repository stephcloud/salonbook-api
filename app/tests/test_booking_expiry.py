import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.jobs import expire_pending
from app.main import app
from app.models.booking import Booking, BookingStatus
from app.models.service import Service
from app.models.user import User, UserRole
from app.schemas.booking import BookingCreate
from app.services import bookings as booking_service
from app.services.bookings import (
    MAX_PENDING_PER_CLIENT,
    PENDING_EXPIRY_MINUTES,
    create_booking,
    expire_pending_bookings,
    too_many_pending,
)
from app.tests.test_salons_api import make_user
from app.tests.test_slots import (
    NOW,
    WAT,
    add_rules,
    at,
    setup_stylist,
    slots_for,
    working,
)
from app.tests.test_stylists_api import make_stylist

LAPSED = timedelta(minutes=16)


async def open_stylist(db_session: AsyncSession) -> tuple[User, Service]:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "17:00"))
    return stylist, service


async def add_booking(
    db_session: AsyncSession,
    stylist: User,
    service: Service,
    client: User,
    hhmm: str,
    status: BookingStatus = BookingStatus.PENDING,
    age: timedelta = timedelta(0),
) -> Booking:
    start = at(hhmm)
    created = NOW - age
    booking = Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=service.duration_minutes),
        status=status,
        created_at=created,
        updated_at=created,
    )
    db_session.add(booking)
    await db_session.commit()
    return booking


async def book(
    db_session: AsyncSession, client: User, stylist: User, service: Service, hhmm: str
) -> Booking:
    data = BookingCreate(
        stylist_id=stylist.id, service_id=service.id, starts_at=at(hhmm)
    )
    return await create_booking(db_session, client, data, now=NOW, tz=WAT)


async def status_of(db_session: AsyncSession, booking: Booking) -> BookingStatus:
    await db_session.refresh(booking)
    return booking.status


async def new_client(db_session: AsyncSession) -> User:
    client, _ = await make_user(db_session, UserRole.CLIENT)
    return client


# --- a lapsed pending booking never blocks a slot (inside the booking transaction) ---


async def test_lapsed_pending_booking_does_not_block_and_is_cancelled(
    db_session: AsyncSession,
) -> None:
    stylist, service = await open_stylist(db_session)
    old_client, new = await new_client(db_session), await new_client(db_session)
    old = await add_booking(
        db_session, stylist, service, old_client, "10:00", age=LAPSED
    )
    before = old.updated_at

    created = await book(db_session, new, stylist, service, "10:00")

    assert created.status == BookingStatus.PENDING
    assert await status_of(db_session, old) == BookingStatus.CANCELLED
    assert old.updated_at == NOW  # stamped from the same clock as the cutoff
    assert old.updated_at > before


@pytest.mark.parametrize(
    "age",
    [
        timedelta(minutes=14),
        timedelta(minutes=PENDING_EXPIRY_MINUTES),  # exactly 15:00 still holds
    ],
)
async def test_unexpired_pending_booking_still_blocks(
    db_session: AsyncSession, age: timedelta
) -> None:
    stylist, service = await open_stylist(db_session)
    old_client, new = await new_client(db_session), await new_client(db_session)
    old = await add_booking(db_session, stylist, service, old_client, "10:00", age=age)

    with pytest.raises(HTTPException) as exc:
        await book(db_session, new, stylist, service, "10:00")

    assert exc.value.status_code == 409
    assert await status_of(db_session, old) == BookingStatus.PENDING


async def test_one_second_past_fifteen_minutes_is_lapsed(
    db_session: AsyncSession,
) -> None:
    stylist, service = await open_stylist(db_session)
    old_client, new = await new_client(db_session), await new_client(db_session)
    age = timedelta(minutes=PENDING_EXPIRY_MINUTES, seconds=1)
    await add_booking(db_session, stylist, service, old_client, "10:00", age=age)

    created = await book(db_session, new, stylist, service, "10:00")

    assert created.status == BookingStatus.PENDING


async def test_old_confirmed_booking_is_never_expired(db_session: AsyncSession) -> None:
    stylist, service = await open_stylist(db_session)
    old_client, new = await new_client(db_session), await new_client(db_session)
    confirmed = await add_booking(
        db_session,
        stylist,
        service,
        old_client,
        "10:00",
        BookingStatus.CONFIRMED,
        age=timedelta(days=2),
    )

    with pytest.raises(HTTPException) as exc:
        await book(db_session, new, stylist, service, "10:00")

    assert exc.value.status_code == 409
    assert await status_of(db_session, confirmed) == BookingStatus.CONFIRMED


async def test_booking_cleanup_only_touches_that_stylists_bookings(
    db_session: AsyncSession,
) -> None:
    salon, stylist_a, service = await setup_stylist(db_session, 60)
    stylist_b = await make_stylist(db_session, salon)
    await add_rules(db_session, stylist_a, working("09:00", "17:00"))
    client, new = await new_client(db_session), await new_client(db_session)
    old_a = await add_booking(
        db_session, stylist_a, service, client, "10:00", age=LAPSED
    )
    old_b = await add_booking(
        db_session, stylist_b, service, client, "10:00", age=LAPSED
    )

    await book(db_session, new, stylist_a, service, "10:00")

    assert await status_of(db_session, old_a) == BookingStatus.CANCELLED
    assert await status_of(db_session, old_b) == BookingStatus.PENDING


# --- expire_pending_bookings (the scheduled-task function) ---


async def test_expiry_function_cancels_all_lapsed_and_is_idempotent(
    db_session: AsyncSession,
) -> None:
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    lapsed = [
        await add_booking(db_session, stylist, service, client, h, age=LAPSED)
        for h in ("09:00", "10:00")
    ]
    fresh = await add_booking(db_session, stylist, service, client, "11:00")

    first = await expire_pending_bookings(db_session, NOW)
    await db_session.commit()
    second = await expire_pending_bookings(db_session, NOW)
    await db_session.commit()

    assert (first, second) == (2, 0)
    assert [await status_of(db_session, b) for b in lapsed] == [
        BookingStatus.CANCELLED
    ] * 2
    assert await status_of(db_session, fresh) == BookingStatus.PENDING


async def test_concurrent_expiry_runs_cancel_each_booking_exactly_once(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    for hhmm in ("09:00", "10:00", "11:00", "12:00", "13:00"):
        await add_booking(db_session, stylist, service, client, hhmm, age=LAPSED)
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def run() -> int:
        async with maker() as session:
            cancelled = await expire_pending_bookings(session, NOW)
            await session.commit()
            return cancelled

    counts = await asyncio.wait_for(
        asyncio.gather(*(run() for _ in range(4))), timeout=30
    )

    assert sum(counts) == 5  # no booking handled twice, none missed, no deadlock
    db_session.expire_all()
    statuses = (await db_session.execute(select(Booking.status))).scalars().all()
    assert set(statuses) == {BookingStatus.CANCELLED}


async def test_slot_list_shows_the_slot_again_once_the_job_has_run(
    db_session: AsyncSession,
) -> None:
    """GET never writes: a lapsed pending hides its slot until the job cancels it."""
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    await add_booking(db_session, stylist, service, client, "10:00", age=LAPSED)

    assert at("10:00") not in await slots_for(db_session, stylist, service)
    await expire_pending_bookings(db_session, NOW)
    await db_session.commit()

    assert at("10:00") in await slots_for(db_session, stylist, service)


# --- pending cap counts only unexpired pending bookings ---


async def test_cap_blocks_the_next_unexpired_pending_booking(
    db_session: AsyncSession,
) -> None:
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    for i in range(MAX_PENDING_PER_CLIENT):
        await add_booking(
            db_session,
            stylist,
            service,
            client,
            f"{9 + i:02d}:00",
            age=timedelta(minutes=1),
        )

    with pytest.raises(HTTPException) as exc:
        await book(db_session, client, stylist, service, "16:00")

    assert exc.value.status_code == 409
    assert exc.value.detail == too_many_pending().detail


async def test_cap_ignores_lapsed_confirmed_and_other_clients_bookings(
    db_session: AsyncSession,
) -> None:
    stylist, service = await open_stylist(db_session)
    client, other = await new_client(db_session), await new_client(db_session)
    # Three of mine that don't count (lapsed, confirmed, cancelled), plus another
    # client's fresh pending ones.
    await add_booking(db_session, stylist, service, client, "09:00", age=LAPSED)
    await add_booking(
        db_session, stylist, service, client, "10:00", BookingStatus.CONFIRMED
    )
    await add_booking(
        db_session, stylist, service, client, "11:00", BookingStatus.CANCELLED
    )
    for hhmm in ("15:00", "16:00"):
        await add_booking(db_session, stylist, service, other, hhmm)

    created = await book(db_session, client, stylist, service, "14:00")

    assert created.status == BookingStatus.PENDING


# --- the scheduled job wrapper ---


async def test_run_once_cancels_lapsed_pending_bookings(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    old = await add_booking(db_session, stylist, service, client, "10:00", age=LAPSED)
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(expire_pending, "async_session_maker", maker)

    assert await expire_pending.run_once(NOW) == 1
    assert await expire_pending.run_once(NOW) == 0  # nothing left: idempotent
    assert await status_of(db_session, old) == BookingStatus.CANCELLED


async def test_run_forever_survives_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    second_run = asyncio.Event()

    async def flaky(now: datetime | None = None) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database hiccup")
        second_run.set()
        return 0

    monkeypatch.setattr(expire_pending, "run_once", flaky)
    task = asyncio.create_task(expire_pending.run_forever(interval_seconds=0.01))

    try:
        await asyncio.wait_for(second_run.wait(), timeout=5)  # looped past the error
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert calls >= 2


async def test_app_lifespan_starts_and_stops_the_expiry_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_loop() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(expire_pending, "run_forever", fake_loop)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=2)

    assert cancelled.is_set()


# --- a rejected booking leaves nothing behind ---


async def test_rejected_booking_rolls_back_cleanup_and_releases_the_client_lock(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    stylist, service = await open_stylist(db_session)
    other, client = await new_client(db_session), await new_client(db_session)
    lapsed = await add_booking(db_session, stylist, service, other, "10:00", age=LAPSED)
    await add_booking(
        db_session, stylist, service, other, "11:00", BookingStatus.CONFIRMED
    )
    client_id = client.id

    with pytest.raises(HTTPException) as exc:
        await book(db_session, client, stylist, service, "11:00")  # taken -> 409
    assert exc.value.status_code == 409

    # Nothing persisted: the cleanup of the lapsed row was rolled back with it.
    assert await status_of(db_session, lapsed) == BookingStatus.PENDING
    # And the client row is already unlocked, before the session is closed.
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as other_session:
        await other_session.execute(
            select(User.id).where(User.id == client_id).with_for_update(nowait=True)
        )


async def test_service_deleted_mid_request_is_404_not_500(
    db_engine: AsyncEngine, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign-key violation at insert time maps to the generic 404."""
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    service_id = service.id

    async def delete_service_then_report_free(
        session: AsyncSession, *args: Any, **kwargs: Any
    ) -> list[datetime]:
        async with maker() as other_session:  # another request deletes the service
            await other_session.execute(delete(Service).where(Service.id == service_id))
            await other_session.commit()
        return [at("10:00")]

    monkeypatch.setattr(booking_service, "get_slots", delete_service_then_report_free)

    with pytest.raises(HTTPException) as exc:
        await book(db_session, client, stylist, service, "10:00")

    assert exc.value.status_code == 404
    assert exc.value.detail == "not found"


async def test_expiry_does_not_block_foreign_key_checks_on_booking_rows(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    """The cleanup locks FOR NO KEY UPDATE, so a row that references a booking
    (a payment, later) can still take its FOR KEY SHARE while a cleanup is in flight.
    A plain FOR UPDATE would make this lock attempt fail."""
    stylist, service = await open_stylist(db_session)
    client = await new_client(db_session)
    lapsed = await add_booking(
        db_session, stylist, service, client, "10:00", age=LAPSED
    )
    lapsed_id = lapsed.id
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as cleaning, maker() as referencing:
        assert await expire_pending_bookings(cleaning, NOW) == 1  # uncommitted
        await referencing.execute(
            select(Booking.id)
            .where(Booking.id == lapsed_id)
            .with_for_update(read=True, key_share=True, nowait=True)  # FOR KEY SHARE
        )
        await cleaning.rollback()
