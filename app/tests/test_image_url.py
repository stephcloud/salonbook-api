import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.tests.test_salons_api import SALON, SALONS, make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_stylists_api import STYLIST, make_stylist, stylists_url

URL = "https://cdn.example.com/salons/glow.jpg"


# --- salons ---


async def test_create_salon_with_valid_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS, json={**SALON, "image_url": URL}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["image_url"] == URL


async def test_create_salon_without_image_url_is_fine(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(SALONS, json=SALON, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["image_url"] is None


@pytest.mark.parametrize(
    "bad",
    [
        "http://cdn.example.com/a.jpg",
        "ftp://cdn.example.com/a.jpg",
        "javascript:alert(1)",
        "https://cdn.example.com/a b.jpg",
        "https://",
        "",
        "https://cdn.example.com/" + "a" * 500,
    ],
)
async def test_create_salon_rejects_bad_image_url(
    db_client: AsyncClient, db_session: AsyncSession, bad: str
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(
        SALONS, json={**SALON, "image_url": bad}, headers=headers
    )
    assert resp.status_code == 422


async def test_image_url_of_exactly_500_characters_is_accepted(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    url = "https://x.co/" + "a" * (500 - len("https://x.co/"))
    assert len(url) == 500
    resp = await db_client.post(
        SALONS, json={**SALON, "image_url": url}, headers=headers
    )
    assert resp.status_code == 201


async def test_update_rejects_http_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"image_url": "http://x.co/a.jpg"}, headers=headers
    )
    assert resp.status_code == 422


async def test_update_sets_keeps_and_clears_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    path = f"{SALONS}/{salon.id}"

    set_ = await db_client.patch(path, json={"image_url": URL}, headers=headers)
    assert set_.json()["image_url"] == URL

    # A PATCH that doesn't mention the image leaves it alone.
    other = await db_client.patch(path, json={"name": "New name"}, headers=headers)
    assert other.json()["image_url"] == URL

    cleared = await db_client.patch(path, json={"image_url": None}, headers=headers)
    assert cleared.status_code == 200
    assert cleared.json()["image_url"] is None
    assert (await db_client.get(path)).json()["image_url"] is None


async def test_null_name_in_update_is_still_ignored(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"name": None}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == salon.name


async def test_another_owner_cannot_set_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    _, other_headers = await make_user(db_session)
    resp = await db_client.patch(
        f"{SALONS}/{salon.id}", json={"image_url": URL}, headers=other_headers
    )
    assert resp.status_code == 403
    assert (await db_client.get(f"{SALONS}/{salon.id}")).json()["image_url"] is None


# --- stylists ---


async def test_owner_creates_stylist_with_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(
        stylists_url(salon), json={**STYLIST, "image_url": URL}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["image_url"] == URL


async def test_create_stylist_without_image_url_is_fine(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    resp = await db_client.post(stylists_url(salon), json=STYLIST, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["image_url"] is None


async def test_create_stylist_rejects_http_and_too_long_image_url(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    for bad in ("http://x.co/a.jpg", "https://x.co/" + "a" * 500):
        resp = await db_client.post(
            stylists_url(salon), json={**STYLIST, "image_url": bad}, headers=headers
        )
        assert resp.status_code == 422


async def test_another_owner_cannot_create_stylist_with_image(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    _, other_headers = await make_user(db_session)
    resp = await db_client.post(
        stylists_url(salon), json={**STYLIST, "image_url": URL}, headers=other_headers
    )
    assert resp.status_code == 403


async def test_public_stylist_list_returns_image_url_and_no_email(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    stylist.image_url = URL
    db_session.add(stylist)
    await make_service(db_session, salon)
    await db_session.commit()

    resp = await db_client.get(stylists_url(salon))  # no token: public

    assert resp.status_code == 200
    [item] = resp.json()
    assert set(item) == {"id", "name", "service_ids", "image_url"}
    assert item["image_url"] == URL
    assert stylist.email not in resp.text
