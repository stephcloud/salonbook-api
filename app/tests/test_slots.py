import uuid
from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.availability_rule import AvailabilityRule, RuleKind
from app.models.booking import Booking, BookingStatus
from app.models.salon import Salon
from app.models.service import Service
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole
from app.services.slots import MAX_ADVANCE_DAYS, compute_slots, get_slots
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import make_service, owner_and_salon
from app.tests.test_stylists_api import make_stylist

WAT = timezone(timedelta(hours=1))
MONDAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=WAT)  # well before MONDAY
assert MONDAY.weekday() == 0


def t(hhmm: str) -> time:
    return time.fromisoformat(hhmm)


def at(hhmm: str, day: date = MONDAY) -> datetime:
    return datetime.combine(day, t(hhmm), WAT)


def starts(*hhmm: str) -> list[datetime]:
    return [at(x) for x in hhmm]


def quarter_hours(first: str, last: str) -> list[str]:
    """'09:00'..'10:00' -> ['09:00', '09:15', ..., '10:00'] (inclusive)."""
    cur, stop = at(first), at(last)
    out = []
    while cur <= stop:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=15)
    return out


async def add_rules(
    db_session: AsyncSession, stylist: User, *rules: dict[str, object]
) -> None:
    db_session.add_all(AvailabilityRule(stylist_id=stylist.id, **r) for r in rules)
    await db_session.commit()


def working(start: str, end: str, weekday: int = 0) -> dict[str, object]:
    return {
        "kind": RuleKind.WORKING,
        "weekday": weekday,
        "start_time": t(start),
        "end_time": t(end),
    }


def lunch(start: str, end: str, weekday: int = 0) -> dict[str, object]:
    return {**working(start, end, weekday), "kind": RuleKind.BREAK}


async def setup_stylist(
    db_session: AsyncSession, duration_minutes: int
) -> tuple[Salon, User, Service]:
    """A salon, a stylist offering one service of the given length."""
    _, _, salon = await owner_and_salon(db_session)
    stylist = await make_stylist(db_session, salon)
    service = await make_service(db_session, salon, duration_minutes=duration_minutes)
    db_session.add(StylistService(stylist_id=stylist.id, service_id=service.id))
    await db_session.commit()
    return salon, stylist, service


async def slots_for(
    db_session: AsyncSession,
    stylist: User,
    service: Service,
    day: date = MONDAY,
    now: datetime = NOW,
) -> list[datetime]:
    return await get_slots(db_session, stylist.id, service.id, day, now=now, tz=WAT)


async def book(
    db_session: AsyncSession,
    stylist: User,
    service: Service,
    start: str,
    end: str,
    status: BookingStatus = BookingStatus.CONFIRMED,
) -> None:
    client, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service.id,
            starts_at=at(start),
            ends_at=at(end),
            status=status,
        )
    )
    await db_session.commit()


# --- the four core cases ---


async def test_normal_day(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("09:00", "11:00"))

    slots = await slots_for(db_session, stylist, service)

    # Every 15 min; the last 30-minute start is 10:30 so it ends at closing time.
    assert slots == [at(x) for x in quarter_hours("09:00", "10:30")]


async def test_break_in_the_middle_removes_overlapping_starts(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(
        db_session, stylist, working("09:00", "13:00"), lunch("11:00", "12:00")
    )

    slots = await slots_for(db_session, stylist, service)

    # 10:00-11:00 touches the break (allowed); 10:15 would run into it. Resumes at 12:00.
    assert slots == [at(x) for x in quarter_hours("09:00", "10:00")] + starts("12:00")


async def test_day_off_has_no_slots(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(
        db_session,
        stylist,
        working("09:00", "11:00"),
        {"kind": RuleKind.DAY_OFF, "off_date": MONDAY},
    )

    assert await slots_for(db_session, stylist, service) == []
    # A day off is one date, not every Monday.
    next_monday = MONDAY + timedelta(days=7)
    assert await slots_for(db_session, stylist, service, next_monday) != []


async def test_long_service_does_not_fit_before_a_break(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await setup_stylist(db_session, 180)
    await add_rules(
        db_session, stylist, working("09:00", "17:00"), lunch("11:00", "12:00")
    )

    slots = await slots_for(db_session, stylist, service)

    # Only 2h free before the break, so a 3h service can't start in the morning.
    # After the break the last start that ends by 17:00 is 14:00.
    assert slots == [at(x) for x in quarter_hours("12:00", "14:00")]


# --- variable durations ---


async def test_slots_depend_on_the_service_duration(db_session: AsyncSession) -> None:
    salon, stylist, short = await setup_stylist(db_session, 30)
    long = await make_service(db_session, salon, name="Install", duration_minutes=120)
    db_session.add(StylistService(stylist_id=stylist.id, service_id=long.id))
    await add_rules(db_session, stylist, working("09:00", "11:00"))

    assert len(await slots_for(db_session, stylist, short)) == 7
    assert await slots_for(db_session, stylist, long) == starts("09:00")


async def test_service_longer_than_the_working_window_has_no_slots(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await setup_stylist(db_session, 240)
    await add_rules(db_session, stylist, working("09:00", "12:00"))

    assert await slots_for(db_session, stylist, service) == []


async def test_multiple_working_windows(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(
        db_session, stylist, working("09:00", "10:00"), working("14:00", "15:00")
    )

    assert await slots_for(db_session, stylist, service) == starts("09:00", "14:00")


# --- existing bookings ---


@pytest.mark.parametrize(
    "status",
    [BookingStatus.PENDING, BookingStatus.CONFIRMED],
)
async def test_active_booking_blocks_its_time(
    db_session: AsyncSession, status: BookingStatus
) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "13:00"))
    await book(db_session, stylist, service, "10:00", "11:00", status)

    slots = await slots_for(db_session, stylist, service)

    # 09:00-10:00 and 11:00-12:00 touch the booking but don't overlap it.
    assert slots == starts("09:00") + [at(x) for x in quarter_hours("11:00", "12:00")]


@pytest.mark.parametrize(
    "status",
    [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW],
)
async def test_inactive_booking_does_not_block(
    db_session: AsyncSession, status: BookingStatus
) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "11:00"))
    await book(db_session, stylist, service, "09:00", "10:00", status)

    assert await slots_for(db_session, stylist, service) == [
        at(x) for x in quarter_hours("09:00", "10:00")
    ]


async def test_another_stylists_booking_does_not_block(
    db_session: AsyncSession,
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 60)
    other = await make_stylist(db_session, salon)
    await add_rules(db_session, stylist, working("09:00", "10:00"))
    await book(db_session, other, service, "09:00", "10:00")

    assert await slots_for(db_session, stylist, service) == starts("09:00")


async def test_booking_from_the_previous_evening_spilling_over_midnight(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("00:00", "02:00"))
    client, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service.id,
            starts_at=datetime.combine(MONDAY - timedelta(days=1), t("23:30"), WAT),
            ends_at=at("00:30"),
        )
    )
    await db_session.commit()

    slots = await slots_for(db_session, stylist, service)

    assert slots == [at(x) for x in quarter_hours("00:30", "01:00")]


# --- availability edge cases ---


async def test_no_working_rule_for_that_weekday(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("09:00", "17:00", weekday=1))  # Tue

    assert await slots_for(db_session, stylist, service) == []


async def test_break_on_another_weekday_is_ignored(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(
        db_session,
        stylist,
        working("09:00", "10:00"),
        lunch("09:00", "10:00", weekday=1),
    )

    assert await slots_for(db_session, stylist, service) == starts("09:00")


async def test_past_start_times_are_dropped(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("09:00", "11:00"))

    slots = await slots_for(db_session, stylist, service, now=at("10:07"))

    assert slots == starts("10:15", "10:30")


async def test_past_date_has_no_slots(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("09:00", "11:00"))

    assert await slots_for(db_session, stylist, service, now=at("08:00", MONDAY)) != []
    assert (
        await slots_for(
            db_session, stylist, service, MONDAY, now=at("08:00", MONDAY + timedelta(1))
        )
        == []
    )


async def test_date_too_far_ahead_is_rejected(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    far = NOW.date() + timedelta(days=MAX_ADVANCE_DAYS + 1)

    with pytest.raises(HTTPException) as exc:
        await slots_for(db_session, stylist, service, far)

    assert exc.value.status_code == 422


async def test_start_exactly_at_now_is_dropped(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("09:00", "11:00"))

    slots = await slots_for(db_session, stylist, service, now=at("09:30"))

    assert at("09:30") not in slots  # not strictly after now
    assert slots[0] == at("09:45")


async def test_booking_stored_in_utc_blocks_the_local_time(
    db_session: AsyncSession,
) -> None:
    """Production stores UTC; the salon here is UTC-5, so local 10:00 is 15:00 UTC."""
    est = timezone(timedelta(hours=-5))
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "12:00"))
    client, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service.id,
            starts_at=datetime(2026, 10, 12, 15, 0, tzinfo=UTC),
            ends_at=datetime(2026, 10, 12, 16, 0, tzinfo=UTC),
        )
    )
    await db_session.commit()

    slots = await get_slots(
        db_session,
        stylist.id,
        service.id,
        MONDAY,
        now=datetime(2026, 10, 1, tzinfo=est),
        tz=est,
    )

    # 09:00-10:00 touches the booking; 11:00-12:00 is the only other start that fits.
    assert slots[0].utcoffset() == timedelta(hours=-5)
    assert [s.strftime("%H:%M") for s in slots] == ["09:00", "11:00"]


async def test_booking_starting_today_and_ending_after_midnight(
    db_session: AsyncSession,
) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    await add_rules(db_session, stylist, working("22:00", "23:59"))
    client, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service.id,
            starts_at=at("23:30"),
            ends_at=at("00:30", MONDAY + timedelta(days=1)),
        )
    )
    await db_session.commit()

    slots = await slots_for(db_session, stylist, service)

    # 23:00-23:30 touches the booking; 23:15 would overlap it.
    assert slots == [at(x) for x in quarter_hours("22:00", "23:00")]


async def test_today_is_the_salon_date_not_the_utc_date(
    db_session: AsyncSession,
) -> None:
    """23:30 UTC on Oct 12 is already Oct 13 at +01:00, so the 365-day limit moves."""
    _, stylist, service = await setup_stylist(db_session, 30)
    now = datetime(2026, 10, 12, 23, 30, tzinfo=UTC)
    local_today = date(2026, 10, 13)

    ok = local_today + timedelta(days=MAX_ADVANCE_DAYS)
    assert await slots_for(db_session, stylist, service, ok, now=now) == []
    with pytest.raises(HTTPException) as exc:
        await slots_for(db_session, stylist, service, ok + timedelta(days=1), now=now)
    assert exc.value.status_code == 422


async def test_last_allowed_date_works(db_session: AsyncSession) -> None:
    _, stylist, service = await setup_stylist(db_session, 30)
    last = NOW.date() + timedelta(days=MAX_ADVANCE_DAYS)
    await add_rules(db_session, stylist, working("09:00", "10:00", last.weekday()))

    assert await slots_for(db_session, stylist, service, last) != []


async def test_another_stylists_day_off_does_not_apply(
    db_session: AsyncSession,
) -> None:
    salon, stylist, service = await setup_stylist(db_session, 30)
    other = await make_stylist(db_session, salon)
    await add_rules(db_session, stylist, working("09:00", "10:00"))
    await add_rules(db_session, other, {"kind": RuleKind.DAY_OFF, "off_date": MONDAY})

    assert await slots_for(db_session, stylist, service) != []


# --- lookups ---


async def test_service_not_offered_by_stylist_is_404(db_session: AsyncSession) -> None:
    salon, stylist, _ = await setup_stylist(db_session, 30)
    unlinked = await make_service(db_session, salon, name="Other")

    with pytest.raises(HTTPException) as exc:
        await slots_for(db_session, stylist, unlinked)

    assert exc.value.status_code == 404


async def test_unknown_stylist_and_non_stylist_are_404(
    db_session: AsyncSession,
) -> None:
    _, _, service = await setup_stylist(db_session, 30)
    client, _ = await make_user(db_session, UserRole.CLIENT)

    for bad_id in (uuid.uuid4(), client.id):
        with pytest.raises(HTTPException) as exc:
            await get_slots(db_session, bad_id, service.id, MONDAY, now=NOW, tz=WAT)
        assert exc.value.status_code == 404


async def test_stylist_and_service_404s_are_indistinguishable(
    db_session: AsyncSession,
) -> None:
    salon, stylist, _ = await setup_stylist(db_session, 30)
    unlinked = await make_service(db_session, salon, name="Other")

    with pytest.raises(HTTPException) as bad_service:
        await slots_for(db_session, stylist, unlinked)
    with pytest.raises(HTTPException) as bad_stylist:
        await get_slots(db_session, uuid.uuid4(), unlinked.id, MONDAY, now=NOW, tz=WAT)

    assert bad_service.value.detail == bad_stylist.value.detail == "not found"


# --- pure function ---


def test_compute_slots_touching_edges_is_allowed() -> None:
    slots = compute_slots(
        MONDAY,
        60,
        working=[(t("09:00"), t("12:00"))],
        breaks=[(t("10:00"), t("11:00"))],
        bookings=[],
        tz=WAT,
        now=NOW,
    )
    assert at("09:00") in slots  # ends exactly when the break starts
    assert at("11:00") in slots  # starts exactly when the break ends
    assert at("09:15") not in slots
    assert at("10:45") not in slots


# --- GET /stylists/{id}/slots ---


def upcoming(weekday: int) -> date:
    """A date 7-13 days from now with the given weekday (always in the future)."""
    base = datetime.now(UTC).date() + timedelta(days=7)
    return base + timedelta(days=(weekday - base.weekday()) % 7)


async def test_slots_endpoint_returns_start_times(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SALON_UTC_OFFSET_MINUTES", 60)  # the +01:00 below
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "11:00"))
    _, headers = await make_user(db_session, UserRole.CLIENT)
    day = upcoming(0)

    resp = await db_client.get(
        f"/api/v1/stylists/{stylist.id}/slots",
        params={"service_id": str(service.id), "date": day.isoformat()},
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["stylist_id"] == str(stylist.id)
    assert body["service_id"] == str(service.id)
    assert body["slots"] == [
        f"{day.isoformat()}T{x}:00+01:00" for x in quarter_hours("09:00", "10:00")
    ]


async def test_slots_endpoint_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, stylist, service = await setup_stylist(db_session, 60)

    resp = await db_client.get(
        f"/api/v1/stylists/{stylist.id}/slots",
        params={"service_id": str(service.id), "date": upcoming(0).isoformat()},
    )

    assert resp.status_code == 401


async def test_slots_endpoint_service_not_offered_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    salon, stylist, _ = await setup_stylist(db_session, 60)
    unlinked = await make_service(db_session, salon, name="Other")
    _, headers = await make_user(db_session, UserRole.CLIENT)

    resp = await db_client.get(
        f"/api/v1/stylists/{stylist.id}/slots",
        params={"service_id": str(unlinked.id), "date": upcoming(0).isoformat()},
        headers=headers,
    )

    assert resp.status_code == 404


@pytest.mark.parametrize(
    "params",
    [
        {"date": "2026-10-12"},  # missing service_id
        {"service_id": "not-a-uuid", "date": "2026-10-12"},
        {"service_id": str(uuid.uuid4()), "date": "next-monday"},
        {"service_id": str(uuid.uuid4())},  # missing date
    ],
)
async def test_slots_endpoint_rejects_bad_query(
    db_client: AsyncClient, db_session: AsyncSession, params: dict[str, str]
) -> None:
    _, stylist, _ = await setup_stylist(db_session, 60)
    _, headers = await make_user(db_session, UserRole.CLIENT)

    resp = await db_client.get(
        f"/api/v1/stylists/{stylist.id}/slots", params=params, headers=headers
    )

    assert resp.status_code == 422


@pytest.mark.parametrize("role", [UserRole.OWNER, UserRole.CLIENT])
async def test_slots_endpoint_non_stylist_id_is_404(
    db_client: AsyncClient, db_session: AsyncSession, role: UserRole
) -> None:
    _, _, service = await setup_stylist(db_session, 60)
    other, headers = await make_user(db_session, role)

    resp = await db_client.get(
        f"/api/v1/stylists/{other.id}/slots",
        params={"service_id": str(service.id), "date": upcoming(0).isoformat()},
        headers=headers,
    )

    assert resp.status_code == 404


async def test_slots_endpoint_any_client_sees_only_times(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Open to any signed-in user, and exposes no booking, client or payment data."""
    _, stylist, service = await setup_stylist(db_session, 60)
    await add_rules(db_session, stylist, working("09:00", "11:00"))
    day = upcoming(0)
    client, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=client.id,
            stylist_id=stylist.id,
            service_id=service.id,
            starts_at=datetime.combine(day, t("09:00"), WAT),
            ends_at=datetime.combine(day, t("10:00"), WAT),
        )
    )
    await db_session.commit()
    _, stranger_headers = await make_user(db_session, UserRole.CLIENT)

    resp = await db_client.get(
        f"/api/v1/stylists/{stylist.id}/slots",
        params={"service_id": str(service.id), "date": day.isoformat()},
        headers=stranger_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"stylist_id", "service_id", "slots"}
    assert str(client.id) not in resp.text
