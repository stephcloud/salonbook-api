import uuid
from datetime import time
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.availability_rule import AvailabilityRule, RuleKind
from app.models.user import User, UserRole
from app.schemas.availability import AvailabilityRuleInput
from app.services.availability import find_conflict
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import owner_and_salon
from app.tests.test_stylists_api import make_stylist


def working(weekday: int, start: str, end: str) -> dict[str, Any]:
    return {"kind": "working", "weekday": weekday, "start_time": start, "end_time": end}


def lunch(weekday: int, start: str, end: str) -> dict[str, Any]:
    return {"kind": "break", "weekday": weekday, "start_time": start, "end_time": end}


def day_off(off_date: str) -> dict[str, Any]:
    return {"kind": "day_off", "off_date": off_date}


def url(stylist: User) -> str:
    return f"/api/v1/stylists/{stylist.id}/availability"


async def stored(db_session: AsyncSession, stylist: User) -> list[AvailabilityRule]:
    result = await db_session.execute(
        select(AvailabilityRule).where(AvailabilityRule.stylist_id == stylist.id)
    )
    return list(result.scalars())


def rules_of(*raw: dict[str, Any]) -> list[AvailabilityRuleInput]:
    return [AvailabilityRuleInput(**r) for r in raw]


# --- validation: single rule (no DB) ---


@pytest.mark.parametrize(
    "bad",
    [
        working(0, "17:00", "09:00"),  # end before start
        working(0, "09:00", "09:00"),  # zero length
        lunch(0, "13:00", "12:00"),
        working(7, "09:00", "17:00"),  # weekday out of range
        working(-1, "09:00", "17:00"),
        {"kind": "working", "start_time": "09:00", "end_time": "17:00"},  # no weekday
        {"kind": "working", "weekday": 0, "start_time": "09:00"},  # no end
        {"kind": "day_off"},  # no date
        {**day_off("2026-12-25"), "weekday": 3},  # day_off with weekday
        {**working(0, "09:00", "17:00"), "off_date": "2026-12-25"},
        {**working(0, "09:00", "17:00"), "kind": "holiday"},  # unknown kind
        {**working(0, "09:00", "17:00"), "stylist_id": str(uuid.uuid4())},  # extra
    ],
)
def test_rule_input_rejects_invalid_shapes(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AvailabilityRuleInput(**bad)


def test_rule_input_accepts_valid_shapes() -> None:
    rules_of(
        working(0, "09:00", "17:00"),
        lunch(0, "12:00", "13:00"),
        day_off("2026-12-25"),
    )


# --- validation: overlap rules (no DB) ---


def test_overlapping_working_windows_conflict() -> None:
    conflict = find_conflict(
        rules_of(working(1, "09:00", "13:00"), working(1, "12:00", "17:00"))
    )
    assert conflict is not None and "working" in conflict


def test_contained_and_identical_windows_conflict() -> None:
    assert find_conflict(
        rules_of(working(1, "09:00", "17:00"), working(1, "10:00", "11:00"))
    )
    assert find_conflict(
        rules_of(working(1, "09:00", "17:00"), working(1, "09:00", "17:00"))
    )


def test_back_to_back_windows_are_allowed() -> None:
    assert (
        find_conflict(
            rules_of(working(1, "09:00", "12:00"), working(1, "12:00", "17:00"))
        )
        is None
    )


def test_same_hours_on_different_weekdays_are_allowed() -> None:
    assert (
        find_conflict(
            rules_of(working(1, "09:00", "17:00"), working(2, "09:00", "17:00"))
        )
        is None
    )


def test_overlapping_breaks_conflict_but_break_may_sit_inside_working() -> None:
    assert find_conflict(
        rules_of(lunch(1, "12:00", "13:00"), lunch(1, "12:30", "13:30"))
    )
    assert (
        find_conflict(
            rules_of(working(1, "09:00", "17:00"), lunch(1, "12:00", "13:00"))
        )
        is None
    )


def test_duplicate_day_off_conflicts() -> None:
    assert find_conflict(rules_of(day_off("2026-12-25"), day_off("2026-12-25")))
    assert find_conflict(rules_of(day_off("2026-12-25"), day_off("2026-12-26"))) is None


# --- PUT /stylists/{id}/availability ---


async def test_owner_sets_availability_idempotent_and_replacing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    body = {
        "rules": [
            working(0, "09:00", "12:00"),
            working(0, "13:00", "17:00"),
            lunch(0, "10:30", "10:45"),
            day_off("2026-12-25"),
        ]
    }

    first = await db_client.put(url(stylist), json=body, headers=headers)
    again = await db_client.put(url(stylist), json=body, headers=headers)
    assert first.status_code == again.status_code == 200
    assert len(first.json()["rules"]) == 4
    assert len(await stored(db_session, stylist)) == 4

    replaced = await db_client.put(
        url(stylist), json={"rules": [working(2, "08:00", "16:00")]}, headers=headers
    )
    assert replaced.status_code == 200
    assert [r["weekday"] for r in replaced.json()["rules"]] == [2]
    assert len(await stored(db_session, stylist)) == 1

    cleared = await db_client.put(url(stylist), json={"rules": []}, headers=headers)
    assert cleared.status_code == 200
    assert await stored(db_session, stylist) == []


async def test_stylist_edits_own_availability(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    headers = {"Authorization": f"Bearer {create_access_token(stylist.id)}"}
    resp = await db_client.put(
        url(stylist), json={"rules": [working(4, "10:00", "18:00")]}, headers=headers
    )
    assert resp.status_code == 200
    got = await db_client.get(url(stylist), headers=headers)
    assert got.status_code == 200
    assert got.json()["rules"][0]["start_time"] == "10:00:00"


async def test_overlap_rejected_and_existing_rules_kept(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    await db_client.put(
        url(stylist), json={"rules": [working(0, "09:00", "17:00")]}, headers=headers
    )

    resp = await db_client.put(
        url(stylist),
        json={"rules": [working(1, "09:00", "13:00"), working(1, "12:00", "17:00")]},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "overlapping" in resp.json()["detail"]
    rows = await stored(db_session, stylist)
    assert [r.weekday for r in rows] == [0]


async def test_end_before_start_rejected_via_api(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, headers, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    resp = await db_client.put(
        url(stylist), json={"rules": [working(0, "17:00", "09:00")]}, headers=headers
    )
    assert resp.status_code == 422
    assert await stored(db_session, stylist) == []


async def test_other_stylist_cannot_edit(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    victim = await make_stylist(db_session, salon)
    other = await make_stylist(db_session, salon)
    headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    body = {"rules": [working(0, "09:00", "17:00")]}
    put = await db_client.put(url(victim), json=body, headers=headers)
    get = await db_client.get(url(victim), headers=headers)
    assert put.status_code == get.status_code == 403
    assert await stored(db_session, victim) == []


async def test_owner_a_cannot_edit_owner_b_stylist(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon_b = await owner_and_salon(db_session)
    stylist_b = await make_stylist(db_session, salon_b)
    _, headers_a = await make_user(db_session, UserRole.OWNER)
    body = {"rules": [working(0, "09:00", "17:00")]}
    put = await db_client.put(url(stylist_b), json=body, headers=headers_a)
    get = await db_client.get(url(stylist_b), headers=headers_a)
    assert put.status_code == get.status_code == 403
    assert await stored(db_session, stylist_b) == []


async def test_client_forbidden_and_anonymous_unauthorized(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    _, client_headers = await make_user(db_session, UserRole.CLIENT)
    body = {"rules": []}
    assert (
        await db_client.put(url(stylist), json=body, headers=client_headers)
    ).status_code == 403
    assert (await db_client.put(url(stylist), json=body)).status_code == 401
    assert (await db_client.get(url(stylist))).status_code == 401


async def test_unknown_or_non_stylist_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, headers, _ = await owner_and_salon(db_session)
    body = {"rules": []}
    missing = await db_client.put(
        f"/api/v1/stylists/{uuid.uuid4()}/availability", json=body, headers=headers
    )
    not_stylist = await db_client.put(url(owner), json=body, headers=headers)
    assert missing.status_code == 404
    assert not_stylist.status_code == 404


# --- DB constraints (backstop behind the API validation) ---


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": RuleKind.WORKING, "weekday": 0, "start_time": None, "end_time": None},
        {"kind": RuleKind.DAY_OFF, "weekday": 1},
        {"kind": RuleKind.WORKING, "weekday": 9},
    ],
)
async def test_db_rejects_malformed_rule(
    db_session: AsyncSession, fields: dict[str, Any]
) -> None:
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    base: dict[str, Any] = {"start_time": time(9), "end_time": time(17)}
    db_session.add(AvailabilityRule(stylist_id=stylist.id, **{**base, **fields}))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
