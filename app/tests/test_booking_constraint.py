"""Hard part #1: the database itself refuses overlapping active bookings."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models.booking import Booking, BookingStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.user import User, UserRole
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_stylists_api import make_stylist

START = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)


async def setup(db_session: AsyncSession) -> tuple[Salon, User, Service, User]:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    service = await make_service(db_session, salon)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    return salon, stylist, service, client


def booking(
    stylist: User,
    service: Service,
    client: User,
    start_offset_min: int,
    minutes: int = 60,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> Booking:
    start = START + timedelta(minutes=start_offset_min)
    return Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        status=status,
    )


async def test_concurrent_bookings_for_one_slot_only_one_succeeds(
    db_engine: AsyncEngine, db_session: AsyncSession
) -> None:
    _, stylist, service, client = await setup(db_session)
    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def attempt() -> bool:
        async with maker() as session:
            session.add(booking(stylist, service, client, 0))
            try:
                await session.commit()
            except IntegrityError:
                return False
            return True

    results = await asyncio.gather(*(attempt() for _ in range(5)))

    assert results.count(True) == 1
    assert results.count(False) == 4


@pytest.mark.parametrize(
    "offset_min, minutes", [(0, 60), (30, 60), (-30, 60), (15, 15)]
)
async def test_overlapping_booking_is_rejected(
    db_session: AsyncSession, offset_min: int, minutes: int
) -> None:
    _, stylist, service, client = await setup(db_session)
    db_session.add(booking(stylist, service, client, 0))
    await db_session.commit()

    db_session.add(booking(stylist, service, client, offset_min, minutes))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_back_to_back_bookings_are_allowed(db_session: AsyncSession) -> None:
    _, stylist, service, client = await setup(db_session)
    db_session.add_all(
        [
            booking(stylist, service, client, 0),  # 10:00-11:00
            booking(stylist, service, client, 60),  # 11:00-12:00
            booking(stylist, service, client, -60),  # 09:00-10:00
        ]
    )

    await db_session.commit()


@pytest.mark.parametrize(
    "status",
    [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW],
)
async def test_inactive_booking_does_not_block_the_slot(
    db_session: AsyncSession, status: BookingStatus
) -> None:
    _, stylist, service, client = await setup(db_session)
    db_session.add(booking(stylist, service, client, 0, status=status))
    await db_session.commit()

    db_session.add(booking(stylist, service, client, 0))
    await db_session.commit()


async def test_pending_booking_blocks_the_slot(db_session: AsyncSession) -> None:
    _, stylist, service, client = await setup(db_session)
    db_session.add(booking(stylist, service, client, 0, status=BookingStatus.PENDING))
    await db_session.commit()

    db_session.add(booking(stylist, service, client, 0))
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_reactivating_into_an_occupied_slot_is_rejected(
    db_session: AsyncSession,
) -> None:
    """Moving a cancelled booking back to confirmed re-checks the overlap."""
    _, stylist, service, client = await setup(db_session)
    old = booking(stylist, service, client, 0, status=BookingStatus.CANCELLED)
    db_session.add_all([old, booking(stylist, service, client, 0)])
    await db_session.commit()

    old.status = BookingStatus.CONFIRMED
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_cancelling_frees_the_slot_for_a_new_booking(
    db_session: AsyncSession,
) -> None:
    _, stylist, service, client = await setup(db_session)
    first = booking(stylist, service, client, 0)
    db_session.add(first)
    await db_session.commit()

    first.status = BookingStatus.CANCELLED
    await db_session.commit()
    db_session.add(booking(stylist, service, client, 0))
    await db_session.commit()


async def test_different_stylists_can_book_the_same_time(
    db_session: AsyncSession,
) -> None:
    salon, stylist, service, client = await setup(db_session)
    other = await make_stylist(db_session, salon)
    db_session.add_all(
        [booking(stylist, service, client, 0), booking(other, service, client, 0)]
    )

    await db_session.commit()
