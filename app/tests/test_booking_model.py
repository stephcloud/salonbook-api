import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.booking import Booking, BookingStatus
from app.models.user import UserRole
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_stylists_api import make_stylist


async def make_booking(db_session: AsyncSession) -> Booking:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    service = await make_service(db_session, salon)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    start = datetime(2026, 10, 12, 10, 0, tzinfo=UTC)
    booking = Booking(
        client_id=client.id,
        stylist_id=stylist.id,
        service_id=service.id,
        starts_at=start,
        ends_at=start + timedelta(hours=1),
        status=BookingStatus.PENDING,
    )
    db_session.add(booking)
    await db_session.commit()
    await db_session.refresh(booking)
    return booking


async def test_updated_at_is_set_on_insert(
    db_session: AsyncSession,
) -> None:
    booking = await make_booking(db_session)

    assert booking.updated_at is not None
    assert abs(booking.updated_at - booking.created_at) < timedelta(seconds=5)


async def test_status_change_through_the_orm_bumps_updated_at(
    db_session: AsyncSession,
) -> None:
    booking = await make_booking(db_session)
    created, before = booking.created_at, booking.updated_at
    await asyncio.sleep(0.05)

    booking.status = BookingStatus.CONFIRMED
    await db_session.commit()
    await db_session.refresh(booking)

    assert booking.updated_at > before
    assert booking.created_at == created  # never touched after insert


async def test_bulk_update_statement_bumps_updated_at(
    db_session: AsyncSession,
) -> None:
    booking = await make_booking(db_session)
    before = booking.updated_at
    await asyncio.sleep(0.05)

    await db_session.execute(
        update(Booking)
        .where(Booking.id == booking.id)
        .values(status=BookingStatus.CANCELLED)
    )
    await db_session.commit()
    await db_session.refresh(booking)

    assert booking.status == BookingStatus.CANCELLED
    assert booking.updated_at > before
