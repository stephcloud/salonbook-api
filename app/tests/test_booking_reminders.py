"""Client reminders and the owner's daily summary, with a fake sender and a set clock.

Every test calls `run_due` with an explicit `now`, so nothing depends on the wall clock.
"""

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.user import User
from app.services.email import reminders
from app.services.email.base import EmailMessage, EmailSendError
from app.services.email.brevo import BrevoSender
from app.services.email.notifications import _send_once
from app.services.email.reminders import WINDOWS, run_due
from app.services.email.templates import (
    BookingEmailContext,
    OwnerSummaryContext,
    SummaryEntry,
    owner_daily_summary,
)
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import Scenario, make_scenario

# 06:05 UTC is 07:05 in Lagos (UTC+1): inside the owner-summary window.
MORNING = datetime(2026, 10, 15, 6, 5, tzinfo=UTC)
NOON = datetime(2026, 10, 15, 11, 0, tzinfo=UTC)


async def confirmed(
    db_session: AsyncSession,
    *,
    now: datetime,
    starts_in: timedelta,
    paid_ago: timedelta = timedelta(days=3),
    status: BookingStatus = BookingStatus.CONFIRMED,
    payment_status: PaymentStatus | None = PaymentStatus.PAID,
    client_name: str | None = None,
) -> Scenario:
    scenario = await make_scenario(
        db_session,
        now=now,
        starts_in=starts_in,
        booking_status=status,
        payment_status=payment_status,
    )
    if scenario.payment is not None:
        scenario.payment.paid_at = now - paid_ago
    if client_name is not None:
        scenario.client.name = client_name
    await db_session.commit()
    return scenario


async def booking_of(db_session: AsyncSession, scenario: Scenario) -> Booking:
    return (
        await db_session.execute(
            select(Booking)
            .where(Booking.id == scenario.booking.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def owner_of(db_session: AsyncSession, scenario: Scenario) -> User:
    return (
        await db_session.execute(
            select(User)
            .where(User.id == scenario.salon.owner_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def subjects(sender: FakeEmailSender, address: str) -> list[str]:
    return [m.subject for m in sender.to(address)]


class Rejecting:
    """A sender whose provider refuses every message."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        raise self.error


# --- the 24h and 2h windows ---


@pytest.mark.parametrize(
    ("starts_in", "due"),
    [
        (timedelta(hours=24, seconds=1), False),  # reminder moment not reached
        (timedelta(hours=24), True),  # exactly on it
        (timedelta(hours=21, seconds=1), True),  # 2h59m59s late: caught up
        (timedelta(hours=21), False),  # a full tolerance late: dropped
    ],
)
async def test_the_24h_window_edges(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    starts_in: timedelta,
    due: bool,
) -> None:
    now = NOON
    scenario = await confirmed(db_session, now=now, starts_in=starts_in)

    counts = await run_due(session_maker, fake_email, now)

    assert counts.reminder_24h == int(due)
    assert len(fake_email.to(scenario.client.email)) == int(due)
    marker = (await booking_of(db_session, scenario)).reminder_24h_email_sent_at
    assert (marker is not None) == due


@pytest.mark.parametrize(
    ("starts_in", "due"),
    [
        (timedelta(hours=2, seconds=1), False),
        (timedelta(hours=2), True),
        (timedelta(hours=1, minutes=15, seconds=1), True),  # 44m59s late
        (timedelta(hours=1, minutes=15), False),  # 45 minutes late: dropped
    ],
)
async def test_the_2h_window_edges(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    starts_in: timedelta,
    due: bool,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=starts_in)

    counts = await run_due(session_maker, fake_email, NOON)

    assert counts.reminder_2h == int(due)
    marker = (await booking_of(db_session, scenario)).reminder_2h_email_sent_at
    assert (marker is not None) == due
    assert counts.reminder_24h == 0


async def test_one_booking_gets_both_reminders_as_separate_emails(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    await run_due(session_maker, fake_email, NOON)
    await run_due(session_maker, fake_email, NOON + timedelta(hours=22))

    sent = subjects(fake_email, scenario.client.email)
    assert len(sent) == 2
    assert "24 hours" in sent[0]
    assert "2 hours" in sent[1]
    booking = await booking_of(db_session, scenario)
    assert booking.reminder_24h_email_sent_at is not None
    assert booking.reminder_2h_email_sent_at is not None


async def test_the_reminder_is_in_lagos_time_and_has_the_salon_details(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=2))

    await run_due(session_maker, fake_email, NOON)

    (message,) = fake_email.to(scenario.client.email)
    assert "14:00" in message.text  # 13:00 UTC is 14:00 in Lagos
    assert scenario.salon.address in message.text
    assert scenario.salon.phone in message.text


# --- never twice ---


async def test_running_again_sends_nothing_more(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    first = await run_due(session_maker, fake_email, NOON)
    again = await run_due(session_maker, fake_email, NOON + timedelta(minutes=5))

    assert (first.reminder_24h, again.reminder_24h) == (1, 0)
    assert len(fake_email.sent) == 1


async def test_simultaneous_runs_send_once(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    runs = await asyncio.gather(
        *(run_due(session_maker, fake_email, NOON) for _ in range(4))
    )

    assert sum(r.reminder_24h for r in runs) == 1
    assert len(fake_email.sent) == 1


# --- who is skipped ---


@pytest.mark.parametrize(
    "status",
    [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW],
)
async def test_a_booking_that_is_not_confirmed_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    status: BookingStatus,
) -> None:
    await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24), status=status)

    await run_due(session_maker, fake_email, NOON)

    assert fake_email.sent == []


async def test_a_pending_booking_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    await confirmed(
        db_session,
        now=NOON,
        starts_in=timedelta(hours=24),
        status=BookingStatus.PENDING,
        payment_status=PaymentStatus.PENDING,
    )

    await run_due(session_maker, fake_email, NOON)

    assert fake_email.sent == []


async def test_a_booking_that_has_already_started_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    await confirmed(db_session, now=NOON, starts_in=-timedelta(minutes=1))

    await run_due(session_maker, fake_email, NOON)

    assert fake_email.sent == []


async def test_a_booking_confirmed_after_the_24h_moment_gets_no_24h_reminder(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    # Booked and paid 5 minutes ago for an appointment in 1 hour.
    scenario = await confirmed(
        db_session,
        now=NOON,
        starts_in=timedelta(hours=1),
        paid_ago=timedelta(minutes=5),
    )

    await run_due(session_maker, fake_email, NOON)

    assert fake_email.sent == []
    booking = await booking_of(db_session, scenario)
    assert booking.reminder_24h_email_sent_at is None
    assert booking.reminder_2h_email_sent_at is None  # its 2h moment was before payment


async def test_a_booking_made_inside_24h_still_gets_the_2h_reminder_only(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    # Paid 10 hours ago for an appointment in 2 hours: made 12h ahead.
    scenario = await confirmed(
        db_session,
        now=NOON,
        starts_in=timedelta(hours=2),
        paid_ago=timedelta(hours=10),
    )

    counts = await run_due(session_maker, fake_email, NOON)

    assert (counts.reminder_24h, counts.reminder_2h) == (0, 1)
    (message,) = fake_email.to(scenario.client.email)
    assert "2 hours" in message.subject


async def test_a_booking_cancelled_after_it_was_selected_is_not_emailed(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))
    email = WINDOWS[0].email

    ctx = BookingEmailContext(
        salon_name="S",
        salon_address="A",
        salon_phone="P",
        service_name="Svc",
        stylist_name="Sty",
        client_name="C",
        client_email=scenario.client.email,
        owner_name="O",
        owner_email="o@example.com",
        starts_at=scenario.booking.starts_at,
        cancellation_hours=24,
        deposit_amount=None,
    )
    # The cancel lands between the due query and the claim.
    async with session_maker() as session:
        await session.execute(
            update(Booking)
            .where(Booking.id == scenario.booking.id)
            .values(status=BookingStatus.CANCELLED)
        )
        await session.commit()

    sent = await _send_once(
        session_maker,
        fake_email,
        scenario.booking.id,
        email,
        ctx,
        NOON,
    )

    assert sent is False
    assert fake_email.sent == []
    assert (await booking_of(db_session, scenario)).reminder_24h_email_sent_at is None


# --- failures ---


async def test_a_temporary_failure_releases_the_claim_and_the_next_run_retries(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))
    fake_email.fail_next = 1  # a timeout or 5xx: EmailSendError that is not permanent

    first = await run_due(session_maker, fake_email, NOON)

    assert first.reminder_24h == 0
    assert (await booking_of(db_session, scenario)).reminder_24h_email_sent_at is None

    again = await run_due(session_maker, fake_email, NOON + timedelta(minutes=5))

    assert again.reminder_24h == 1
    assert len(fake_email.sent) == 1


async def test_a_permanent_rejection_keeps_the_claim_and_is_not_retried(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))
    sender = Rejecting(EmailSendError("brevo answered 400", permanent=True))

    with caplog.at_level(logging.INFO):
        await run_due(session_maker, sender, NOON)
        await run_due(session_maker, sender, NOON + timedelta(minutes=5))

    assert sender.attempts == 1
    assert (
        await booking_of(db_session, scenario)
    ).reminder_24h_email_sent_at is not None
    assert str(scenario.booking.id) in caplog.text
    assert scenario.client.email not in caplog.text


async def test_an_unexpected_error_from_the_sender_is_contained_and_released(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    counts = await run_due(session_maker, Rejecting(RuntimeError("boom")), NOON)

    assert counts.reminder_24h == 0
    assert (await booking_of(db_session, scenario)).reminder_24h_email_sent_at is None


async def test_one_failing_email_does_not_stop_the_others(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    first = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=23))
    second = await confirmed(
        db_session, now=NOON, starts_in=timedelta(hours=23, minutes=1)
    )
    fake_email.fail_next = 1

    counts = await run_due(session_maker, fake_email, NOON)

    assert counts.reminder_24h == 1
    delivered = {m.to_address for m in fake_email.sent}
    assert delivered in ({first.client.email}, {second.client.email})


async def test_a_run_without_a_configured_sender_sends_and_claims_nothing(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    counts = await run_due(session_maker, None, NOON)

    assert counts == reminders.RunCounts()
    assert (await booking_of(db_session, scenario)).reminder_24h_email_sent_at is None


async def test_each_run_sends_at_most_one_batch_and_the_next_run_finishes(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reminders, "BATCH_SIZE", 2)
    for minutes in range(3):
        await confirmed(
            db_session, now=NOON, starts_in=timedelta(hours=23, minutes=minutes)
        )

    first = await run_due(session_maker, fake_email, NOON)
    second = await run_due(session_maker, fake_email, NOON + timedelta(minutes=5))

    assert (first.reminder_24h, second.reminder_24h) == (2, 1)


# --- the owner's morning summary ---


async def test_the_owner_gets_one_summary_with_todays_bookings(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    # 10:05 Lagos the same day (09:05 UTC).
    scenario = await confirmed(
        db_session,
        now=MORNING,
        starts_in=timedelta(hours=3),
        client_name="Ada\n  Obi",
    )
    owner = await owner_of(db_session, scenario)

    counts = await run_due(session_maker, fake_email, MORNING)

    assert counts.owner_summaries == 1
    (message,) = fake_email.to(owner.email)
    assert "10:05" in message.text
    assert "Ada Obi" in message.text  # whitespace collapsed
    assert scenario.client.email not in message.text + message.html
    assert (await owner_of(db_session, scenario)).daily_summary_sent_on is not None


async def test_the_summary_is_sent_once_a_day(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))
    owner = await owner_of(db_session, scenario)

    runs = await asyncio.gather(
        *(run_due(session_maker, fake_email, MORNING) for _ in range(3))
    )
    await run_due(session_maker, fake_email, MORNING + timedelta(minutes=30))

    assert sum(r.owner_summaries for r in runs) == 1
    assert len(fake_email.to(owner.email)) == 1

    # The next morning is a new day.
    next_morning = MORNING + timedelta(days=1)
    async with session_maker() as session:
        session.add(
            Booking(
                client_id=scenario.client.id,
                stylist_id=scenario.stylist.id,
                service_id=scenario.booking.service_id,
                starts_at=next_morning + timedelta(hours=3),
                ends_at=next_morning + timedelta(hours=4),
                status=BookingStatus.CONFIRMED,
            )
        )
        await session.commit()
    await run_due(session_maker, fake_email, next_morning)
    assert len(fake_email.to(owner.email)) == 2


async def test_an_empty_day_sends_nothing(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    # Confirmed, but tomorrow in Lagos: 00:30 on the 16th is 23:30 UTC on the 15th.
    scenario = await confirmed(
        db_session,
        now=MORNING,
        starts_in=datetime(2026, 10, 15, 23, 30, tzinfo=UTC) - MORNING,
    )
    owner = await owner_of(db_session, scenario)

    counts = await run_due(session_maker, fake_email, MORNING)

    assert counts.owner_summaries == 0
    assert fake_email.to(owner.email) == []
    assert (await owner_of(db_session, scenario)).daily_summary_sent_on is None


async def test_the_last_minute_of_the_lagos_day_is_included(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    # 23:30 Lagos on the 15th is 22:30 UTC.
    scenario = await confirmed(
        db_session,
        now=MORNING,
        starts_in=datetime(2026, 10, 15, 22, 30, tzinfo=UTC) - MORNING,
    )
    owner = await owner_of(db_session, scenario)

    await run_due(session_maker, fake_email, MORNING)

    assert len(fake_email.to(owner.email)) == 1


@pytest.mark.parametrize(
    ("now", "sends"),
    [
        (datetime(2026, 10, 15, 5, 59, tzinfo=UTC), False),  # 06:59 Lagos
        (datetime(2026, 10, 15, 6, 0, tzinfo=UTC), True),  # 07:00 Lagos
        (datetime(2026, 10, 15, 7, 59, tzinfo=UTC), True),  # 08:59 Lagos
        (datetime(2026, 10, 15, 8, 0, tzinfo=UTC), False),  # 09:00 Lagos
    ],
)
async def test_the_summary_only_goes_out_in_the_morning_window(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    now: datetime,
    sends: bool,
) -> None:
    scenario = await confirmed(db_session, now=now, starts_in=timedelta(hours=8))
    owner = await owner_of(db_session, scenario)

    await run_due(session_maker, fake_email, now)

    assert len(fake_email.to(owner.email)) == int(sends)


@pytest.mark.parametrize(
    "status", [BookingStatus.CANCELLED, BookingStatus.PENDING, BookingStatus.COMPLETED]
)
async def test_only_confirmed_bookings_are_listed(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    status: BookingStatus,
) -> None:
    scenario = await confirmed(
        db_session, now=MORNING, starts_in=timedelta(hours=3), status=status
    )
    owner = await owner_of(db_session, scenario)

    await run_due(session_maker, fake_email, MORNING)

    assert fake_email.to(owner.email) == []


async def test_a_booking_that_has_already_started_is_not_listed(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=MORNING, starts_in=-timedelta(minutes=1))
    owner = await owner_of(db_session, scenario)

    await run_due(session_maker, fake_email, MORNING)

    assert fake_email.to(owner.email) == []


async def test_many_owners_each_get_their_own_summary_only(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenarios = [
        await confirmed(
            db_session,
            now=MORNING,
            starts_in=timedelta(hours=3),
            client_name=f"Client Number{i}",
        )
        for i in range(5)
    ]
    # A second booking for the first owner, in the same salon.
    first = scenarios[0]
    async with session_maker() as session:
        session.add(
            Booking(
                client_id=first.client.id,
                stylist_id=first.stylist.id,
                service_id=first.booking.service_id,
                starts_at=MORNING + timedelta(hours=5),
                ends_at=MORNING + timedelta(hours=6),
                status=BookingStatus.CONFIRMED,
            )
        )
        await session.commit()

    counts = await run_due(session_maker, fake_email, MORNING)

    assert counts.owner_summaries == 5
    for i, scenario in enumerate(scenarios):
        owner = await owner_of(db_session, scenario)
        (message,) = fake_email.to(owner.email)
        assert f"Client Number{i}" in message.text
        for j in range(5):
            if j != i:
                assert f"Client Number{j}" not in message.text
    first_owner = await owner_of(db_session, first)
    assert "2 confirmed bookings" in fake_email.to(first_owner.email)[0].text


async def test_the_summary_batch_is_capped_and_the_next_run_finishes(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reminders, "BATCH_SIZE", 2)
    for _ in range(3):
        await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))

    first = await run_due(session_maker, fake_email, MORNING)
    second = await run_due(session_maker, fake_email, MORNING + timedelta(minutes=5))

    assert (first.owner_summaries, second.owner_summaries) == (2, 1)


async def test_a_temporary_failure_releases_the_summary_and_the_next_run_retries(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))
    owner = await owner_of(db_session, scenario)
    fake_email.fail_next = 1

    await run_due(session_maker, fake_email, MORNING)
    assert (await owner_of(db_session, scenario)).daily_summary_sent_on is None

    await run_due(session_maker, fake_email, MORNING + timedelta(minutes=5))

    assert len(fake_email.to(owner.email)) == 1


async def test_a_permanent_rejection_of_the_summary_is_not_retried(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))
    owner = await owner_of(db_session, scenario)
    sender = Rejecting(EmailSendError("brevo answered 400", permanent=True))

    with caplog.at_level(logging.INFO):
        await run_due(session_maker, sender, MORNING)
        await run_due(session_maker, sender, MORNING + timedelta(minutes=5))

    assert sender.attempts == 1
    assert (await owner_of(db_session, scenario)).daily_summary_sent_on is not None
    assert str(owner.id) in caplog.text
    assert owner.email not in caplog.text


# --- Brevo: which statuses are permanent ---


@pytest.mark.parametrize(
    ("status", "permanent"),
    [
        (400, True),
        (401, False),
        (404, True),
        (408, False),
        (429, False),
        (500, False),
        (503, False),
    ],
)
async def test_brevo_statuses_split_into_permanent_and_temporary(
    status: int, permanent: bool
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    sender = BrevoSender("key", "from@example.com", "SalonBook", transport)
    message = EmailMessage("a@example.com", "A", "s", "t", "<p>t</p>")

    with pytest.raises(EmailSendError) as caught:
        await sender.send(message)

    assert caught.value.permanent is permanent


async def test_a_network_error_is_temporary() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    sender = BrevoSender(
        "key", "from@example.com", "SalonBook", httpx.MockTransport(fail)
    )

    with pytest.raises(EmailSendError) as caught:
        await sender.send(EmailMessage("a@example.com", "A", "s", "t", "<p>t</p>"))

    assert caught.value.permanent is False


# --- the owner summary template on its own ---


def _entry(client_name: str = "Ada Obi", hour: int = 9) -> SummaryEntry:
    return SummaryEntry(
        datetime(2026, 10, 15, hour, 0, tzinfo=UTC),
        "Glow Studio",
        "Braids",
        "Tola",
        client_name,
    )


def test_the_summary_escapes_html_and_collapses_whitespace() -> None:
    ctx = OwnerSummaryContext(
        owner_name="Owner\nName",
        owner_email="owner@example.com",
        day=date(2026, 10, 15),
        entries=(_entry("<b>Ada</b>\r\nObi"),),
    )

    message = owner_daily_summary(ctx)

    assert "\n" not in message.subject
    assert "Hi Owner Name," in message.text
    assert "<b>Ada</b>" not in message.html
    assert "&lt;b&gt;Ada&lt;/b&gt; Obi" in message.html


def test_the_summary_says_how_many_more_are_not_listed() -> None:
    ctx = OwnerSummaryContext(
        "Owner", "owner@example.com", date(2026, 10, 15), (_entry(),), more=4
    )

    message = owner_daily_summary(ctx)

    assert "5 confirmed bookings" in message.text
    assert "and 4 more" in message.text


# --- review follow-ups ---


async def test_a_second_run_while_one_is_in_progress_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))

    async with reminders._run_lock:  # a run is "in progress"
        await reminders.run_due_safely(session_maker, fake_email, NOON)
    assert fake_email.sent == []

    await reminders.run_due_safely(session_maker, fake_email, NOON)
    assert len(fake_email.sent) == 1


async def test_a_provider_that_is_down_stops_the_batch_after_a_few_failures(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
) -> None:
    for minutes in range(6):
        await confirmed(
            db_session, now=NOON, starts_in=timedelta(hours=23, minutes=minutes)
        )
    sender = Rejecting(EmailSendError("brevo answered 503"))

    await run_due(session_maker, sender, NOON)

    assert sender.attempts == reminders.MAX_CONSECUTIVE_FAILURES


@pytest.mark.parametrize(
    ("status", "permanent"), [(401, False), (402, False), (403, False), (422, True)]
)
async def test_account_level_brevo_errors_do_not_burn_the_claim(
    status: int, permanent: bool
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status))
    sender = BrevoSender("key", "from@example.com", "SalonBook", transport)

    with pytest.raises(EmailSendError) as caught:
        await sender.send(EmailMessage("a@example.com", "A", "s", "t", "<p>t</p>"))

    assert caught.value.permanent is permanent
