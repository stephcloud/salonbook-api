"""The make_admin job is the only way to get the admin role."""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.make_admin import MakeAdminError, promote_to_admin
from app.models.user import UserRole
from app.tests.payment_helpers import make_scenario
from app.tests.test_salons_api import SALON, SALONS, make_salon, make_user
from app.tests.test_stylists_api import make_stylist

REGISTER = "/api/v1/auth/register"


async def test_promotes_an_existing_account(db_session: AsyncSession) -> None:
    user, _ = await make_user(db_session, UserRole.CLIENT)

    result = await promote_to_admin(db_session, user.email)

    assert result.changed is True and result.user_id == user.id
    await db_session.refresh(user)
    assert user.role == UserRole.ADMIN


async def test_the_email_is_matched_ignoring_case_and_spaces(
    db_session: AsyncSession,
) -> None:
    user, _ = await make_user(db_session, UserRole.CLIENT)
    result = await promote_to_admin(db_session, f"  {user.email.upper()} ")
    assert result.user_id == user.id


async def test_refuses_an_email_with_no_account(db_session: AsyncSession) -> None:
    with pytest.raises(MakeAdminError, match="no account"):
        await promote_to_admin(db_session, "nobody@example.com")


async def test_running_it_again_is_a_no_op(db_session: AsyncSession) -> None:
    user, _ = await make_user(db_session, UserRole.CLIENT)
    await promote_to_admin(db_session, user.email)

    again = await promote_to_admin(db_session, user.email)

    assert again.changed is False


async def test_refuses_an_owner_with_a_salon(db_session: AsyncSession) -> None:
    owner, _ = await make_user(db_session)
    await make_salon(db_session, owner)
    with pytest.raises(MakeAdminError, match="owns a salon"):
        await promote_to_admin(db_session, owner.email)
    await db_session.refresh(owner)
    assert owner.role == UserRole.OWNER


async def test_refuses_a_stylist(db_session: AsyncSession) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    stylist = await make_stylist(db_session, salon)
    with pytest.raises(MakeAdminError, match="stylist"):
        await promote_to_admin(db_session, stylist.email)


async def test_refuses_a_client_with_bookings(db_session: AsyncSession) -> None:
    scenario = await make_scenario(db_session)
    with pytest.raises(MakeAdminError, match="bookings"):
        await promote_to_admin(db_session, scenario.client.email)
    await db_session.refresh(scenario.client)
    assert scenario.client.role == UserRole.CLIENT


# --- no API path to admin ---


async def test_registering_as_admin_is_rejected(db_client: AsyncClient) -> None:
    resp = await db_client.post(
        REGISTER,
        json={
            "name": "Eve",
            "email": "eve@example.com",
            "password": "correct-horse-battery",
            "role": "admin",
        },
    )
    assert resp.status_code == 422


async def test_a_client_gets_no_role_by_sending_one_to_other_endpoints(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS,
        json={**SALON, "role": "admin", "owner_id": str(owner.id)},
        headers=headers,
    )
    assert resp.status_code == 422
    await db_session.refresh(owner)
    assert owner.role == UserRole.OWNER
