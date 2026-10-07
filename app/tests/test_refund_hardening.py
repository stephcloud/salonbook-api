"""Edge cases found in review: refund events mid-attempt, crashes, backoff, webhook limits."""

import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.payments import MAX_WEBHOOK_BYTES
from app.jobs import retry_refunds
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.services import payments as payment_service
from app.services.payments import (
    MAX_REFUND_ATTEMPTS,
    REFUND_FAST_ATTEMPTS,
    REFUND_RETRY_AFTER,
    REFUND_SLOW_RETRY_AFTER,
    handle_paystack_event,
    list_refunds_due,
    process_refund,
    try_refund,
)
from app.services.paystack import RefundResult
from app.tests.payment_helpers import Scenario, make_scenario, reload
from app.tests.paystack_fakes import (
    FakePaystack,
    charge_success_body,
    event_body,
    refund_event_body,
    sign,
    webhook_headers,
)

WEBHOOK = "/api/v1/payments/webhook"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
PAID_AND_CONFIRMED: dict[str, Any] = {
    "booking_status": BookingStatus.CONFIRMED,
    "payment_status": PaymentStatus.PAID,
}


async def owed(
    db_session: AsyncSession,
    *,
    attempted_at: datetime | None = None,
    attempts: int = 0,
) -> Scenario:
    """A payment whose refund is waiting to be sent."""
    scenario = await make_scenario(
        db_session, payment_status=PaymentStatus.REFUND_PENDING
    )
    assert scenario.payment is not None
    scenario.payment.refund_attempted_at = attempted_at
    scenario.payment.refund_attempts = attempts
    await db_session.commit()
    return scenario


async def run(
    session_maker: async_sessionmaker[AsyncSession],
    fake: FakePaystack,
    scenario: Scenario,
    now: datetime = NOW,
) -> bool:
    assert scenario.payment is not None
    return await process_refund(session_maker, fake, scenario.payment.id, now=now)


async def payment_of(db_session: AsyncSession, scenario: Scenario) -> Any:
    return (await reload(db_session, scenario))[1]


async def explode(_: str) -> None:
    raise RuntimeError("something unexpected")


# --- refund.failed / refund.processed landing while an attempt is in flight ---


async def test_a_refund_that_fails_mid_attempt_is_not_recorded_as_done(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    """refund.failed arrives while we are still talking to Paystack. Finalize must not
    then write `refunded`, or the customer would be told they were paid back when not."""
    scenario = await owed(db_session)

    async def fail_during_the_call(reference: str) -> None:
        raw = refund_event_body("refund.failed", reference, status="failed")
        async with session_maker() as session:
            await handle_paystack_event(session, raw, sign(raw), now=NOW)

    fake_paystack.on_refund = fail_during_the_call

    assert await run(session_maker, fake_paystack, scenario) is False

    payment = await payment_of(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refunded_at is None
    assert payment.paystack_refund_id is None
    assert payment.refund_attempted_at is None  # due again at once

    # Paystack no longer lists the failed refund as a live one: the retry sends a new one.
    fake_paystack.on_refund = None
    fake_paystack.refunds.clear()
    assert await run(session_maker, fake_paystack, scenario) is True
    assert len(fake_paystack.refund_calls) == 2
    assert (await payment_of(db_session, scenario)).status == PaymentStatus.REFUNDED


async def test_when_refund_processed_wins_the_race_the_refund_id_is_still_recorded(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)

    async def processed_during_the_call(reference: str) -> None:
        raw = refund_event_body("refund.processed", reference)
        async with session_maker() as session:
            await handle_paystack_event(session, raw, sign(raw), now=NOW)

    fake_paystack.on_refund = processed_during_the_call

    # The webhook settled it, not this call, so this call reports False.
    assert await run(session_maker, fake_paystack, scenario) is False

    payment = await payment_of(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.paystack_refund_id == "rf_1"  # still kept for the audit trail


async def test_a_refund_in_a_state_we_do_not_know_is_neither_done_nor_created_again(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = await owed(db_session)
    fake_paystack.refunds[scenario.reference] = RefundResult("rf_x", "needs-attention")

    assert await run(session_maker, fake_paystack, scenario) is False

    assert fake_paystack.refund_calls == []  # one already exists: never a second
    assert (await payment_of(db_session, scenario)).status == (
        PaymentStatus.REFUND_PENDING
    )
    assert "needs review" in caplog.text


# --- a crash never escapes ---


async def test_try_refund_logs_a_crash_and_leaves_the_refund_waiting(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = await owed(db_session)
    assert scenario.payment is not None
    fake_paystack.on_refund = explode

    sent = await try_refund(session_maker, fake_paystack, scenario.payment.id, now=NOW)

    assert sent is False
    assert "crashed" in caplog.text
    assert (await payment_of(db_session, scenario)).status == (
        PaymentStatus.REFUND_PENDING
    )


async def test_a_crashing_background_refund_does_not_break_the_webhook_response(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session)
    fake_paystack.on_refund = explode
    raw = charge_success_body(scenario.reference, scenario.amount - 1000)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200  # Paystack must not retry a webhook we handled
    assert (await payment_of(db_session, scenario)).status == (
        PaymentStatus.REFUND_PENDING
    )  # the retry job will finish it


async def test_cancel_still_succeeds_when_listing_the_queued_refunds_fails(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_paystack: FakePaystack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    async def boom(*args: Any, **kwargs: Any) -> list[uuid.UUID]:
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(payment_service, "refunds_waiting_for_booking", boom)

    resp = await db_client.post(
        f"/api/v1/bookings/{scenario.booking.id}/cancel", headers=scenario.headers
    )

    assert resp.status_code == 200  # the cancel committed: not a 500
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUND_PENDING  # the retry job sends it
    assert fake_paystack.refund_calls == []


async def test_a_crashing_background_refund_does_not_fail_the_cancel(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    fake_paystack.on_refund = explode

    resp = await db_client.post(
        f"/api/v1/bookings/{scenario.booking.id}/cancel", headers=scenario.headers
    )

    assert resp.status_code == 200
    assert (await payment_of(db_session, scenario)).status == (
        PaymentStatus.REFUND_PENDING
    )


# --- backoff: a long outage must not use up the budget ---


async def test_retries_are_quick_at_first_and_hourly_after(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    quick = await owed(
        db_session,
        attempted_at=NOW - REFUND_RETRY_AFTER - timedelta(minutes=1),
        attempts=REFUND_FAST_ATTEMPTS - 1,
    )
    slow_too_soon = await owed(
        db_session,
        attempted_at=NOW - timedelta(minutes=30),
        attempts=REFUND_FAST_ATTEMPTS,
    )
    slow_due = await owed(
        db_session,
        attempted_at=NOW - REFUND_SLOW_RETRY_AFTER - timedelta(minutes=1),
        attempts=REFUND_FAST_ATTEMPTS,
    )
    assert quick.payment is not None and slow_due.payment is not None
    assert slow_too_soon.payment is not None

    due = await list_refunds_due(session_maker, NOW)

    assert set(due) == {quick.payment.id, slow_due.payment.id}


def test_the_attempt_budget_covers_a_day_or_more_of_trouble() -> None:
    slow_attempts = MAX_REFUND_ATTEMPTS - REFUND_FAST_ATTEMPTS
    assert REFUND_SLOW_RETRY_AFTER * slow_attempts >= timedelta(days=1)


async def test_the_final_failed_attempt_is_logged_as_giving_up_and_the_row_is_left(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(retry_refunds, "async_session_maker", session_maker)
    scenario = await owed(
        db_session,
        attempted_at=NOW - timedelta(hours=2),
        attempts=MAX_REFUND_ATTEMPTS - 1,
    )
    fake_paystack.refund_failures = 1

    assert await retry_refunds.run_once(NOW, fake_paystack) == 0

    gave_up = [r for r in caplog.records if "gave up" in r.getMessage()]
    assert gave_up and gave_up[0].levelno == logging.ERROR
    payment = await payment_of(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refund_attempts == MAX_REFUND_ATTEMPTS
    # Exhausted: it is not picked up again until someone resets the counter.
    assert await retry_refunds.run_once(NOW + timedelta(days=3), fake_paystack) == 0
    assert len(fake_paystack.refund_calls) == 1


async def test_failures_after_the_quick_phase_are_logged_at_error_level(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(retry_refunds, "async_session_maker", session_maker)
    await owed(
        db_session,
        attempted_at=NOW - REFUND_SLOW_RETRY_AFTER - timedelta(minutes=1),
        attempts=REFUND_FAST_ATTEMPTS,
    )
    fake_paystack.refund_failures = 1

    await retry_refunds.run_once(NOW, fake_paystack)

    failures = [r for r in caplog.records if "not sent" in r.getMessage()]
    assert failures and failures[0].levelno == logging.ERROR


async def test_one_crashing_refund_does_not_stop_the_rest_of_the_batch(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retry_refunds, "async_session_maker", session_maker)
    first = await owed(db_session)
    second = await owed(db_session)

    async def explode_for_the_first(reference: str) -> None:
        if reference == first.reference:
            raise RuntimeError("something unexpected")

    fake_paystack.on_refund = explode_for_the_first

    assert await retry_refunds.run_once(NOW, fake_paystack) == 1

    assert (await payment_of(db_session, first)).status == PaymentStatus.REFUND_PENDING
    assert (await payment_of(db_session, second)).status == PaymentStatus.REFUNDED


# --- the public webhook caps what it will read ---


async def test_a_declared_oversized_body_is_413_and_changes_nothing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)
    before = await payment_of(db_session, scenario)
    status_before = before.status
    raw = b"x" * (MAX_WEBHOOK_BYTES + 1)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 413
    assert (await payment_of(db_session, scenario)).status == status_before
    booking, _ = await reload(db_session, scenario)
    assert booking.status == BookingStatus.PENDING


async def test_a_chunked_body_that_grows_past_the_cap_is_413(
    db_client: AsyncClient,
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):  # no Content-Length to check up front
            yield b"x" * (MAX_WEBHOOK_BYTES // 2 + 1)

    resp = await db_client.post(
        WEBHOOK, content=chunks(), headers={"x-paystack-signature": "irrelevant"}
    )

    assert resp.status_code == 413


async def test_a_body_just_under_the_cap_is_still_handled(
    db_client: AsyncClient,
) -> None:
    raw = event_body("transfer.success", {"pad": "x" * (MAX_WEBHOOK_BYTES - 200)})
    assert len(raw) < MAX_WEBHOOK_BYTES

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
