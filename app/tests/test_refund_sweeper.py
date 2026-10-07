"""The refund retry job: picks up what the webhook and cancel couldn't send."""

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.jobs import retry_refunds
from app.main import app
from app.models.payment import PaymentStatus
from app.services.payments import (
    MAX_REFUND_ATTEMPTS,
    REFUND_RETRY_AFTER,
    list_refunds_due,
)
from app.tests.payment_helpers import Scenario, make_scenario, reload
from app.tests.paystack_fakes import FakePaystack

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - REFUND_RETRY_AFTER - timedelta(minutes=1)


@pytest.fixture(autouse=True)
def _job_uses_the_test_database(
    session_maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retry_refunds, "async_session_maker", session_maker)


async def owed(
    db_session: AsyncSession,
    *,
    attempted_at: datetime | None = None,
    attempts: int = 0,
    status: PaymentStatus = PaymentStatus.REFUND_PENDING,
) -> Scenario:
    scenario = await make_scenario(db_session, payment_status=status)
    assert scenario.payment is not None
    scenario.payment.refund_attempted_at = attempted_at
    scenario.payment.refund_attempts = attempts
    await db_session.commit()
    return scenario


async def status_of(db_session: AsyncSession, scenario: Scenario) -> PaymentStatus:
    return (await reload(db_session, scenario))[1].status


async def test_run_once_sends_every_due_refund_and_skips_the_rest(
    db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    fresh = await owed(db_session)  # never attempted
    stale = await owed(db_session, attempted_at=LONG_AGO, attempts=2)
    in_flight = await owed(
        db_session, attempted_at=NOW - timedelta(minutes=1), attempts=1
    )
    exhausted = await owed(
        db_session, attempted_at=LONG_AGO, attempts=MAX_REFUND_ATTEMPTS
    )
    paid = await owed(db_session, status=PaymentStatus.PAID)

    assert await retry_refunds.run_once(NOW, fake_paystack) == 2

    assert await status_of(db_session, fresh) == PaymentStatus.REFUNDED
    assert await status_of(db_session, stale) == PaymentStatus.REFUNDED
    assert await status_of(db_session, in_flight) == PaymentStatus.REFUND_PENDING
    assert await status_of(db_session, exhausted) == PaymentStatus.REFUND_PENDING
    assert await status_of(db_session, paid) == PaymentStatus.PAID
    sent = {call["reference"] for call in fake_paystack.refund_calls}
    assert sent == {fresh.reference, stale.reference}


async def test_run_once_is_idempotent(
    db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    await owed(db_session)

    assert await retry_refunds.run_once(NOW, fake_paystack) == 1
    assert await retry_refunds.run_once(NOW, fake_paystack) == 0
    assert await retry_refunds.run_once(NOW + timedelta(hours=1), fake_paystack) == 0

    assert len(fake_paystack.refund_calls) == 1


async def test_a_failing_refund_is_counted_out_and_retried_on_a_later_run(
    db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await owed(db_session)
    fake_paystack.refund_failures = 1

    assert await retry_refunds.run_once(NOW, fake_paystack) == 0
    assert await status_of(db_session, scenario) == PaymentStatus.REFUND_PENDING

    later = NOW + REFUND_RETRY_AFTER + timedelta(minutes=1)
    assert await retry_refunds.run_once(later, fake_paystack) == 1
    assert await status_of(db_session, scenario) == PaymentStatus.REFUNDED


async def test_run_once_does_nothing_without_a_paystack_key(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    fake_paystack: FakePaystack,
) -> None:
    """Local without a key: don't burn the attempt budget on calls that can't work."""
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "")
    scenario = await owed(db_session)

    assert await retry_refunds.run_once(NOW) == 0

    payment = (await reload(db_session, scenario))[1]
    assert payment.refund_attempts == 0
    assert fake_paystack.refund_calls == []


async def test_list_refunds_due_is_oldest_first_and_respects_the_limit(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    first = await owed(db_session)
    await owed(db_session)
    await owed(db_session)
    assert first.payment is not None

    due = await list_refunds_due(session_maker, NOW, limit=2)

    assert len(due) == 2
    assert due[0] == first.payment.id


async def test_run_forever_survives_a_failed_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    second_run = asyncio.Event()

    async def flaky(now: datetime | None = None, paystack: object = None) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database hiccup")
        second_run.set()
        return 0

    monkeypatch.setattr(retry_refunds, "run_once", flaky)
    task = asyncio.create_task(retry_refunds.run_forever(interval_seconds=0.01))

    try:
        await asyncio.wait_for(second_run.wait(), timeout=5)  # looped past the error
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert calls >= 2


async def test_app_lifespan_starts_and_stops_the_refund_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_loop() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def idle_expiry() -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(retry_refunds, "run_forever", fake_loop)
    monkeypatch.setattr("app.jobs.expire_pending.run_forever", idle_expiry)

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=2)

    assert cancelled.is_set()
