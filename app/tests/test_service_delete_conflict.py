from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import UserRole
from app.tests.test_booking_cancel import add_booking, salon_owner
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import services_url
from app.tests.test_slots import setup_stylist


async def test_deleting_a_service_with_bookings_is_409_and_keeps_it(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 60)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    await add_booking(
        db_session, stylist, service.id, client, datetime.now(UTC) + timedelta(days=3)
    )
    _, headers = await salon_owner(db_session, salon)

    resp = await db_client.delete(f"/api/v1/services/{service.id}", headers=headers)

    assert resp.status_code == 409
    listed = await db_client.get(services_url(salon))
    assert [s["id"] for s in listed.json()] == [str(service.id)]


async def test_deleting_a_service_without_bookings_still_works(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, _, service = await setup_stylist(db_session, 60)
    _, headers = await salon_owner(db_session, salon)

    resp = await db_client.delete(f"/api/v1/services/{service.id}", headers=headers)

    assert resp.status_code == 204
