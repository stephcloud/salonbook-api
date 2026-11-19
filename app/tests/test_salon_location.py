"""Salon location: owner-only coordinates, public area search and distance sort."""

import logging

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import DropQueryString
from app.models.salon import SalonStatus
from app.models.user import UserRole
from app.tests.test_salons_api import SALON, SALONS, make_salon, make_user

LAGOS = {"latitude": 6.5244, "longitude": 3.3792}


# --- owner write: validation ---


@pytest.mark.parametrize(
    ("body", "bad_field"),
    [
        ({"latitude": 90.1, "longitude": 0}, "latitude"),
        ({"latitude": -90.1, "longitude": 0}, "latitude"),
        ({"latitude": 0, "longitude": 180.1}, "longitude"),
        ({"latitude": 0, "longitude": -180.1}, "longitude"),
        ({"latitude": 6.5}, "longitude"),
        ({"longitude": 3.4}, "latitude"),
        ({"latitude": 6.5, "longitude": None}, "longitude"),
    ],
)
async def test_create_rejects_bad_or_unpaired_coordinates(
    db_client: AsyncClient,
    db_session: AsyncSession,
    body: dict[str, object],
    bad_field: str,
) -> None:
    _, headers = await make_user(db_session)
    resp = await db_client.post(SALONS, json={**SALON, **body}, headers=headers)
    assert resp.status_code == 422
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", bad_field]]


async def test_create_accepts_edge_values_and_no_location(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers = await make_user(db_session)
    edge = {"latitude": -90, "longitude": 180}
    body = (
        await db_client.post(SALONS, json={**SALON, **edge}, headers=headers)
    ).json()
    assert (body["latitude"], body["longitude"]) == (-90, 180)
    body = (await db_client.post(SALONS, json=SALON, headers=headers)).json()
    assert (body["latitude"], body["longitude"]) == (None, None)


async def test_owner_sets_updates_and_clears_location(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    url = f"{SALONS}/{salon.id}"

    resp = await db_client.patch(url, json=LAGOS, headers=headers)
    assert resp.status_code == 200
    assert (resp.json()["latitude"], resp.json()["longitude"]) == (6.5244, 3.3792)

    # A PATCH without location keeps it.
    resp = await db_client.patch(url, json={"name": "New"}, headers=headers)
    assert resp.json()["latitude"] == 6.5244

    mine = (await db_client.get(f"{SALONS}/mine", headers=headers)).json()
    assert (mine[0]["latitude"], mine[0]["longitude"]) == (6.5244, 3.3792)

    resp = await db_client.patch(
        url, json={"latitude": None, "longitude": None}, headers=headers
    )
    assert (resp.json()["latitude"], resp.json()["longitude"]) == (None, None)


@pytest.mark.parametrize(
    ("body", "bad_field"),
    [
        ({"latitude": None}, "longitude"),
        ({"longitude": 3.4}, "latitude"),
        ({"latitude": None, "longitude": 3.4}, "latitude"),
        ({"latitude": 91, "longitude": 3.4}, "latitude"),
    ],
)
async def test_update_rejects_half_a_location(
    db_client: AsyncClient,
    db_session: AsyncSession,
    body: dict[str, object],
    bad_field: str,
) -> None:
    owner, headers = await make_user(db_session)
    salon = await make_salon(db_session, owner, **LAGOS)
    resp = await db_client.patch(f"{SALONS}/{salon.id}", json=body, headers=headers)
    assert resp.status_code == 422
    assert [e["loc"] for e in resp.json()["detail"]] == [["body", bad_field]]


async def test_only_the_owner_can_set_location(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    url = f"{SALONS}/{salon.id}"

    _, other_owner = await make_user(db_session)
    assert (
        await db_client.patch(url, json=LAGOS, headers=other_owner)
    ).status_code == 403
    for role in (UserRole.CLIENT, UserRole.STYLIST, UserRole.ADMIN):
        _, headers = await make_user(db_session, role)
        assert (
            await db_client.patch(url, json=LAGOS, headers=headers)
        ).status_code == 403
    assert (await db_client.patch(url, json=LAGOS)).status_code == 401

    await db_session.refresh(salon)
    assert (salon.latitude, salon.longitude) == (None, None)


# --- public responses never expose raw coordinates ---


async def test_public_reads_never_include_coordinates(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, **LAGOS)
    responses = [
        (await db_client.get(f"{SALONS}/{salon.id}")).json(),
        *(await db_client.get(SALONS)).json(),
        *(await db_client.get(SALONS, params={"lat": 6.6, "lng": 3.4})).json(),
    ]
    for body in responses:
        assert "latitude" not in body and "longitude" not in body


# --- area search ---


async def test_area_matches_address_case_insensitive_and_trimmed(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    lekki = await make_salon(db_session, owner, address="12 Admiralty Way, Lekki")
    await make_salon(db_session, owner, address="5 Allen Avenue, Ikeja")
    await make_salon(
        db_session, owner, address="1 Lekki Road", status=SalonStatus.PENDING
    )

    resp = await db_client.get(SALONS, params={"area": "  LEKKI "})
    assert [s["id"] for s in resp.json()] == [str(lekki.id)]
    # LIKE wildcards are matched literally.
    assert (await db_client.get(SALONS, params={"area": "%"})).json() == []


async def test_area_match_is_found_on_page_two(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    yaba = []
    for i in range(6):
        await make_salon(db_session, owner, address=f"{i} Herbert Macaulay, Ikeja")
        yaba.append(await make_salon(db_session, owner, address=f"{i} Sabo, Yaba"))

    page1 = (await db_client.get(SALONS, params={"area": "yaba", "limit": 4})).json()
    page2 = (
        await db_client.get(SALONS, params={"area": "yaba", "limit": 4, "offset": 4})
    ).json()
    assert [s["id"] for s in page1 + page2] == [str(s.id) for s in yaba]


# --- distance ---


async def test_sorted_by_distance_with_location_less_salons_last(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    no_location = await make_salon(db_session, owner, name="Nowhere")
    abuja = await make_salon(db_session, owner, latitude=9.0765, longitude=7.3986)
    ikeja = await make_salon(db_session, owner, latitude=6.6018, longitude=3.3515)
    lekki = await make_salon(db_session, owner, latitude=6.4698, longitude=3.5852)

    params: dict[str, float | int] = {"lat": 6.5244, "lng": 3.3792}
    body = (await db_client.get(SALONS, params=params)).json()
    assert [s["id"] for s in body] == [
        str(s.id) for s in (ikeja, lekki, abuja, no_location)
    ]
    assert [s["distance_km"] for s in body[:3]] == [9.1, 23.6, 525.9]
    assert body[3]["distance_km"] is None

    # Pages are slices of the same ordering.
    page2 = (
        await db_client.get(SALONS, params={**params, "limit": 2, "offset": 2})
    ).json()
    assert [s["id"] for s in page2] == [str(abuja.id), str(no_location.id)]


async def test_distance_sort_without_nulls_and_with_area(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    far = await make_salon(db_session, owner, address="Lekki", latitude=7, longitude=4)
    near = await make_salon(db_session, owner, address="Lekki", latitude=6, longitude=3)
    await make_salon(db_session, owner, address="Ikeja", latitude=6, longitude=3)

    params = {"lat": 6, "lng": 3, "area": "lekki"}
    body = (await db_client.get(SALONS, params=params)).json()
    assert [s["id"] for s in body] == [str(near.id), str(far.id)]
    assert body[0]["distance_km"] == 0.0
    assert all(s["distance_km"] is not None for s in body)


async def test_antipodal_point_does_not_error(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    await make_salon(db_session, owner, latitude=0, longitude=180)
    resp = await db_client.get(SALONS, params={"lat": 0, "lng": 0})
    assert resp.status_code == 200
    assert resp.json()[0]["distance_km"] == 20015.1


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 6.5},
        {"lng": 3.4},
        {"lat": 90.1, "lng": 0},
        {"lat": 0, "lng": -180.1},
        {"lat": "nan", "lng": 0},
    ],
)
async def test_list_rejects_bad_or_unpaired_lat_lng(
    db_client: AsyncClient, params: dict[str, object]
) -> None:
    assert (await db_client.get(SALONS, params=params)).status_code == 422


# --- old behaviour without the new params ---


async def test_list_without_new_params_is_unchanged(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await make_user(db_session)
    first = await make_salon(db_session, owner, latitude=9, longitude=7)
    second = await make_salon(db_session, owner)
    third = await make_salon(db_session, owner, latitude=6.5, longitude=3.4)

    body = (await db_client.get(SALONS)).json()
    assert [s["id"] for s in body] == [str(s.id) for s in (first, second, third)]
    assert set(body[0]) == {
        "id",
        "owner_id",
        "name",
        "address",
        "phone",
        "image_url",
        "cancellation_hours",
        "deposit_amount",
        "created_at",
    }
    # An empty or blank area is no filter.
    assert len((await db_client.get(SALONS, params={"area": "  "})).json()) == 3


# --- logging ---


def test_access_log_drops_query_string() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/v1/salons?lat=6.52&lng=3.37", "1.1", 200),
        None,
    )
    assert DropQueryString().filter(record)
    assert "6.52" not in record.getMessage()
    assert "/api/v1/salons" in record.getMessage()
