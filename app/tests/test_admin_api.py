import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.models.admin_audit_log import AdminAction, AdminAuditLog, AuditTargetType
from app.models.salon import Salon, SalonStatus
from app.models.user import User, UserRole
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import make_scenario
from app.tests.test_salons_api import make_salon, make_user
from app.tests.test_stylists_api import make_stylist

ADMIN = "/api/v1/admin"


async def make_admin(db_session: AsyncSession) -> tuple[User, dict[str, str]]:
    return await make_user(db_session, UserRole.ADMIN)


async def audit_rows(db_session: AsyncSession) -> list[AdminAuditLog]:
    result = await db_session.execute(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at)
    )
    return list(result.scalars())


async def reloaded_status(db_session: AsyncSession, salon: Salon) -> SalonStatus:
    status = await db_session.scalar(
        select(Salon.status)
        .where(Salon.id == salon.id)
        .execution_options(populate_existing=True)
    )
    assert status is not None
    return status


# --- every admin route is protected ---


def _admin_routes() -> list[tuple[str, str]]:
    """Every documented /admin route, read from the OpenAPI schema (they are public docs)."""
    found = []
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api/v1/admin"):
            found += [(method.upper(), path) for method in operations]
    return sorted(found)


def test_the_admin_routes_exist() -> None:
    assert len(_admin_routes()) == 6


@pytest.mark.parametrize(("method", "path"), _admin_routes())
async def test_every_admin_route_is_401_without_a_token(
    db_client: AsyncClient, method: str, path: str
) -> None:
    url = path.replace("{salon_id}", str(uuid.uuid4())).replace(
        "{user_id}", str(uuid.uuid4())
    )
    resp = await db_client.request(method, url)
    assert resp.status_code == 401


@pytest.mark.parametrize("role", [UserRole.CLIENT, UserRole.OWNER, UserRole.STYLIST])
@pytest.mark.parametrize(("method", "path"), _admin_routes())
async def test_every_admin_route_is_403_for_other_roles(
    db_client: AsyncClient,
    db_session: AsyncSession,
    method: str,
    path: str,
    role: UserRole,
) -> None:
    _, headers = await make_user(db_session, role)
    url = path.replace("{salon_id}", str(uuid.uuid4())).replace(
        "{user_id}", str(uuid.uuid4())
    )
    resp = await db_client.request(method, url, headers=headers)
    assert resp.status_code == 403


async def test_a_token_for_a_deleted_user_is_401(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    await db_session.delete(admin)
    await db_session.commit()
    assert (await db_client.get(f"{ADMIN}/salons", headers=headers)).status_code == 401


# --- GET /admin/salons ---


async def test_admin_lists_salons_with_status_owner_name_and_counts(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    scenario = await make_scenario(db_session)  # one pending booking
    owner = await db_session.get(User, scenario.salon.owner_id)
    assert owner is not None
    resp = await db_client.get(f"{ADMIN}/salons", headers=headers)
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["id"] == str(scenario.salon.id)
    assert item["status"] == "approved"
    assert item["owner"] == {"id": str(owner.id), "name": owner.name}
    assert item["booking_count"] == 1
    assert item["active_booking_count"] == 1
    assert owner.email not in resp.text
    assert "password" not in resp.text


async def test_admin_salon_list_filters_by_status_and_paginates(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    pending = [
        await make_salon(db_session, owner, status=SalonStatus.PENDING, name=f"P{i}")
        for i in range(3)
    ]
    await make_salon(db_session, owner, status=SalonStatus.SUSPENDED)
    resp = await db_client.get(
        f"{ADMIN}/salons", params={"status": "pending"}, headers=headers
    )
    assert {s["id"] for s in resp.json()} == {str(s.id) for s in pending}
    first = await db_client.get(f"{ADMIN}/salons", params={"limit": 2}, headers=headers)
    rest = await db_client.get(
        f"{ADMIN}/salons", params={"limit": 2, "offset": 2}, headers=headers
    )
    assert len(first.json()) == 2 and len(rest.json()) == 2
    bad = await db_client.get(
        f"{ADMIN}/salons", params={"status": "nope"}, headers=headers
    )
    assert bad.status_code == 422


# --- approve / suspend ---


async def test_admin_approves_a_pending_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {
        "id": str(salon.id),
        "name": salon.name,
        "status": "approved",
    }
    assert await reloaded_status(db_session, salon) == SalonStatus.APPROVED
    (row,) = await audit_rows(db_session)
    assert (row.admin_id, row.action, row.target_type, row.target_id) == (
        admin.id,
        AdminAction.SALON_APPROVED,
        AuditTargetType.SALON,
        salon.id,
    )
    assert row.reason is None


async def test_approving_twice_changes_nothing_the_second_time(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    first = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)
    again = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)
    assert first.status_code == again.status_code == 200
    assert len(await audit_rows(db_session)) == 1
    assert len(fake_email.sent) == 1


async def test_admin_suspends_a_salon_and_the_reason_goes_to_the_audit_log(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.post(
        f"{ADMIN}/salons/{salon.id}/suspend",
        json={"reason": "  fake address  "},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"
    assert await reloaded_status(db_session, salon) == SalonStatus.SUSPENDED
    (row,) = await audit_rows(db_session)
    assert row.admin_id == admin.id
    assert row.action == AdminAction.SALON_SUSPENDED
    assert row.reason == "fake address"


async def test_suspend_works_without_a_body(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/suspend", headers=headers)
    assert resp.status_code == 200
    (row,) = await audit_rows(db_session)
    assert row.reason is None


async def test_suspend_rejects_a_long_reason_and_unknown_fields(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    url = f"{ADMIN}/salons/{salon.id}/suspend"
    too_long = await db_client.post(url, json={"reason": "x" * 501}, headers=headers)
    extra = await db_client.post(url, json={"status": "approved"}, headers=headers)
    assert too_long.status_code == extra.status_code == 422
    assert await audit_rows(db_session) == []


async def test_a_suspended_salon_can_be_approved_again(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.SUSPENDED)
    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


@pytest.mark.parametrize("action", ["approve", "suspend"])
async def test_status_change_on_an_unknown_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession, action: str
) -> None:
    _, headers = await make_admin(db_session)
    resp = await db_client.post(
        f"{ADMIN}/salons/{uuid.uuid4()}/{action}", headers=headers
    )
    assert resp.status_code == 404


async def test_admin_cannot_suspend_a_salon_they_own(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    salon = await make_salon(db_session, admin)  # only possible by hand in the DB
    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/suspend", headers=headers)
    assert resp.status_code == 409
    assert await reloaded_status(db_session, salon) == SalonStatus.APPROVED
    assert await audit_rows(db_session) == []


async def test_the_status_cannot_be_set_through_the_owner_api(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    resp = await db_client.patch(
        f"/api/v1/salons/{salon.id}", json={"status": "approved"}, headers=headers
    )
    assert resp.status_code == 422
    assert await reloaded_status(db_session, salon) == SalonStatus.PENDING


# --- DELETE /admin/salons/{id} ---


async def test_admin_deletes_an_empty_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    resp = await db_client.delete(f"{ADMIN}/salons/{salon.id}", headers=headers)
    assert resp.status_code == 204
    assert (
        await db_session.scalar(select(func.count()).where(Salon.id == salon.id))
    ) == 0
    (row,) = await audit_rows(db_session)
    assert (row.admin_id, row.action, row.target_type, row.target_id) == (
        admin.id,
        AdminAction.SALON_DELETED,
        AuditTargetType.SALON,
        salon.id,
    )


async def test_deleting_an_unknown_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    resp = await db_client.delete(f"{ADMIN}/salons/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_a_salon_with_bookings_cannot_be_deleted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    scenario = await make_scenario(db_session)
    resp = await db_client.delete(
        f"{ADMIN}/salons/{scenario.salon.id}", headers=headers
    )
    assert resp.status_code == 409
    assert "bookings" in resp.json()["detail"]
    assert await audit_rows(db_session) == []


async def test_a_salon_with_stylists_cannot_be_deleted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    await make_stylist(db_session, salon)
    resp = await db_client.delete(f"{ADMIN}/salons/{salon.id}", headers=headers)
    assert resp.status_code == 409
    assert "stylists" in resp.json()["detail"]
    assert await audit_rows(db_session) == []


# --- GET /admin/users ---


async def test_admin_lists_users_without_password_data(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    client_user, _ = await make_user(db_session, UserRole.CLIENT)
    resp = await db_client.get(f"{ADMIN}/users", headers=headers)
    assert resp.status_code == 200
    by_id = {u["id"]: u for u in resp.json()}
    assert set(by_id) == {str(admin.id), str(client_user.id)}
    assert set(by_id[str(client_user.id)]) == {
        "id",
        "name",
        "email",
        "role",
        "created_at",
    }
    assert by_id[str(admin.id)]["role"] == "admin"
    assert "password" not in resp.text and "$argon2" not in resp.text


async def test_admin_user_list_filters_by_role_and_paginates(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    for _ in range(3):
        await make_user(db_session, UserRole.CLIENT)
    clients = await db_client.get(
        f"{ADMIN}/users", params={"role": "client"}, headers=headers
    )
    assert len(clients.json()) == 3
    assert {u["role"] for u in clients.json()} == {"client"}
    page = await db_client.get(
        f"{ADMIN}/users", params={"limit": 2, "offset": 2}, headers=headers
    )
    assert len(page.json()) == 2  # 4 users in all
    bad = await db_client.get(f"{ADMIN}/users", params={"limit": 0}, headers=headers)
    assert bad.status_code == 422


# --- DELETE /admin/users/{id} ---


async def test_admin_deletes_a_user_with_no_bookings_and_no_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    victim, _ = await make_user(db_session, UserRole.CLIENT)
    resp = await db_client.delete(f"{ADMIN}/users/{victim.id}", headers=headers)
    assert resp.status_code == 204
    assert (
        await db_session.scalar(select(func.count()).where(User.id == victim.id))
    ) == 0
    (row,) = await audit_rows(db_session)
    assert (row.admin_id, row.action, row.target_type, row.target_id) == (
        admin.id,
        AdminAction.USER_DELETED,
        AuditTargetType.USER,
        victim.id,
    )
    assert victim.email not in repr(row.__dict__)


async def test_a_deleted_users_token_stops_working(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    victim, victim_headers = await make_user(db_session, UserRole.CLIENT)
    await db_client.delete(f"{ADMIN}/users/{victim.id}", headers=headers)
    me = await db_client.get("/api/v1/auth/me", headers=victim_headers)
    assert me.status_code == 401


async def test_deleting_an_unknown_user_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    resp = await db_client.delete(f"{ADMIN}/users/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


async def test_admin_cannot_delete_themselves(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, headers = await make_admin(db_session)
    resp = await db_client.delete(f"{ADMIN}/users/{admin.id}", headers=headers)
    assert resp.status_code == 409
    assert await db_session.get(User, admin.id) is not None
    assert await audit_rows(db_session) == []


async def test_admin_cannot_delete_another_admin(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    other, _ = await make_admin(db_session)
    resp = await db_client.delete(f"{ADMIN}/users/{other.id}", headers=headers)
    assert resp.status_code == 409


async def test_a_user_who_owns_a_salon_cannot_be_deleted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    await make_salon(db_session, owner)
    resp = await db_client.delete(f"{ADMIN}/users/{owner.id}", headers=headers)
    assert resp.status_code == 409
    assert await audit_rows(db_session) == []


async def test_a_stylist_attached_to_a_salon_cannot_be_deleted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    stylist = await make_stylist(db_session, salon)
    resp = await db_client.delete(f"{ADMIN}/users/{stylist.id}", headers=headers)
    assert resp.status_code == 409


async def test_a_client_with_bookings_cannot_be_deleted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_admin(db_session)
    scenario = await make_scenario(db_session)
    resp = await db_client.delete(
        f"{ADMIN}/users/{scenario.client.id}", headers=headers
    )
    assert resp.status_code == 409
    assert "bookings" in resp.json()["detail"]
    assert await audit_rows(db_session) == []
