import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.salon import Salon
from app.models.service import PriceType, Service
from app.models.user import User, UserRole
from app.tests.test_salons_api import make_salon, make_user

FIXED = {
    "name": "Box braids",
    "duration_minutes": 180,
    "price_type": "fixed",
    "price": 2500000,
}
QUOTE = {
    "name": "Custom install",
    "duration_minutes": 240,
    "price_type": "quote",
    "price": None,
}


def services_url(salon: Salon) -> str:
    return f"/api/v1/salons/{salon.id}/services"


async def make_service(
    db_session: AsyncSession, salon: Salon, **overrides: object
) -> Service:
    fields: dict[str, object] = {
        "name": "Wash",
        "duration_minutes": 30,
        "price_type": PriceType.FIXED,
        "price": 100000,
        **overrides,
    }
    service = Service(salon_id=salon.id, **fields)
    db_session.add(service)
    await db_session.commit()
    return service


async def owner_and_salon(
    db_session: AsyncSession,
) -> tuple[User, dict[str, str], Salon]:
    owner, headers = await make_user(db_session, UserRole.OWNER)
    return owner, headers, await make_salon(db_session, owner)


# --- create + price_type rule ---


async def test_create_fixed_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(services_url(salon), json=FIXED, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["price"] == FIXED["price"]
    assert resp.json()["salon_id"] == str(salon.id)


async def test_create_quote_service_with_null_price(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(services_url(salon), json=QUOTE, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["price"] is None
    assert resp.json()["price_type"] == "quote"


async def test_create_quote_service_without_price_field(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    body = {k: v for k, v in QUOTE.items() if k != "price"}
    resp = await db_client.post(services_url(salon), json=body, headers=headers)
    assert resp.status_code == 201


async def test_quote_service_with_price_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(
        services_url(salon), json={**QUOTE, "price": 1000}, headers=headers
    )
    assert resp.status_code == 422


async def test_fixed_service_without_price_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(
        services_url(salon), json={**FIXED, "price": None}, headers=headers
    )
    assert resp.status_code == 422


async def test_service_rejects_non_positive_duration(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(
        services_url(salon), json={**FIXED, "duration_minutes": 0}, headers=headers
    )
    assert resp.status_code == 422


# --- ownership ---


async def test_owner_a_cannot_add_service_to_owner_b_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    _, headers_a = await make_user(db_session)
    resp = await db_client.post(services_url(salon_b), json=FIXED, headers=headers_a)
    assert resp.status_code == 403


async def test_owner_a_cannot_edit_or_delete_owner_b_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    service = await make_service(db_session, salon_b)
    _, headers_a = await make_user(db_session)
    url = f"/api/v1/services/{service.id}"

    patch = await db_client.patch(url, json={"name": "Hijacked"}, headers=headers_a)
    delete = await db_client.delete(url, headers=headers_a)

    assert patch.status_code == 403
    assert delete.status_code == 403
    await db_session.refresh(service)
    assert service.name == "Wash"


async def test_client_cannot_create_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    _, headers = await make_user(db_session, UserRole.CLIENT)
    resp = await db_client.post(services_url(salon), json=FIXED, headers=headers)
    assert resp.status_code == 403


async def test_service_writes_require_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    service = await make_service(db_session, salon)
    url = f"/api/v1/services/{service.id}"
    assert (await db_client.post(services_url(salon), json=FIXED)).status_code == 401
    assert (await db_client.patch(url, json={"name": "X"})).status_code == 401
    assert (await db_client.delete(url)).status_code == 401


async def test_create_service_on_missing_salon_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        f"/api/v1/salons/{uuid.uuid4()}/services", json=FIXED, headers=headers
    )
    assert resp.status_code == 404


# --- list (public) + pagination ---


async def test_list_services_is_public_and_scoped_to_salon(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    _, _, other = await owner_and_salon(db_session)
    await make_service(db_session, salon)
    await make_service(db_session, other)
    resp = await db_client.get(services_url(salon))
    assert resp.status_code == 200
    assert [s["salon_id"] for s in resp.json()] == [str(salon.id)]


async def test_list_services_missing_salon_is_404(db_client: AsyncClient) -> None:
    resp = await db_client.get(f"/api/v1/salons/{uuid.uuid4()}/services")
    assert resp.status_code == 404


async def test_list_services_default_limit_limit_and_cap(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    for i in range(25):
        db_session.add(
            Service(
                salon_id=salon.id,
                name=f"S{i}",
                duration_minutes=30,
                price_type=PriceType.FIXED,
                price=1000,
            )
        )
    await db_session.commit()
    url = services_url(salon)
    assert len((await db_client.get(url)).json()) == 20
    assert (
        len((await db_client.get(url, params={"limit": 5, "offset": 22})).json()) == 3
    )
    assert (await db_client.get(url, params={"limit": 101})).status_code == 422


# --- update ---


async def test_owner_updates_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    service = await make_service(db_session, salon)
    resp = await db_client.patch(
        f"/api/v1/services/{service.id}", json={"duration_minutes": 45}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["duration_minutes"] == 45
    assert resp.json()["price"] == 100000


async def test_patch_to_quote_requires_null_price(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    service = await make_service(db_session, salon)
    url = f"/api/v1/services/{service.id}"

    # Existing fixed price would remain: invalid.
    bad = await db_client.patch(url, json={"price_type": "quote"}, headers=headers)
    assert bad.status_code == 422

    ok = await db_client.patch(
        url, json={"price_type": "quote", "price": None}, headers=headers
    )
    assert ok.status_code == 200
    assert ok.json()["price"] is None


async def test_patch_to_fixed_requires_price(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    service = await make_service(
        db_session, salon, price_type=PriceType.QUOTE, price=None
    )
    url = f"/api/v1/services/{service.id}"

    bad = await db_client.patch(url, json={"price_type": "fixed"}, headers=headers)
    assert bad.status_code == 422

    ok = await db_client.patch(
        url, json={"price_type": "fixed", "price": 5000}, headers=headers
    )
    assert ok.status_code == 200
    assert ok.json()["price"] == 5000


async def test_patch_price_on_quote_service_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    service = await make_service(
        db_session, salon, price_type=PriceType.QUOTE, price=None
    )
    resp = await db_client.patch(
        f"/api/v1/services/{service.id}", json={"price": 5000}, headers=headers
    )
    assert resp.status_code == 422


async def test_patch_missing_service_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.patch(
        f"/api/v1/services/{uuid.uuid4()}", json={"name": "X"}, headers=headers
    )
    assert resp.status_code == 404


# --- delete ---


async def test_owner_deletes_service(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    service = await make_service(db_session, salon)
    resp = await db_client.delete(f"/api/v1/services/{service.id}", headers=headers)
    assert resp.status_code == 204
    assert (await db_client.get(services_url(salon))).json() == []
