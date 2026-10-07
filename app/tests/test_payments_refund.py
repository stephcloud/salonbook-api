"""Sending refunds: claim, call Paystack with no lock held, finalize, retry safely."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.payment import Payment, PaymentStatus
from app.services.payments import (
    MAX_REFUND_ATTEMPTS,
    REFUND_RETRY_AFTER,
    process_refund,
)
from app.tests.payment_helpers import (
    Scenario,
    make_scenario,
    reload,
    row_dict,
    until,
)
from app.tests.paystack_fakes import (
    FakePaystack,
    charge_success_body,
    refund_event_body,
    webhook_headers,
)

WEBHOOK = "/api/v1/payments/webhook"
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
AFTER_RETRY_WINDOW = T0 + REFUND_RETRY_AFTER + timedelta(minutes=1)


async def owed(
    db_session: AsyncSession,
    *,
    status: PaymentStatus = PaymentStatus.REFUND_PENDING,
    refund_amount: int | None = None,
) -> Scenario:
    """A payment whose refund is waiting to be sent."""
    scenario = await make_scenario(db_session, payment_status=status)
    assert scenario.payment is not None
    scenario.payment.refund_amount = refund_amount
    await db_session.commit()
    return scenario


async def run(
    session_maker: async_sessionmaker[AsyncSession],
    fake: FakePaystack,
    scenario: Scenario,
    now: datetime = T0,
) -> bool:
    assert scenario.payment is not None
    return await process_refund(session_maker, fake, scenario.payment.id, now=now)


# --- the happy path ---


async def test_a_waiting_refund_is_sent_and_the_payment_marked_refunded(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)

    assert await run(session_maker, fake_paystack, scenario) is True

    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": scenario.amount}
    ]
    assert fake_paystack.find_calls == [scenario.reference]  # looks before it creates
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refunded_at == T0
    assert payment.paystack_refund_id == "rf_1"
    assert payment.refund_attempts == 1


async def test_the_refund_amount_on_the_row_is_what_is_sent(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session, refund_amount=123)

    await run(session_maker, fake_paystack, scenario)

    assert fake_paystack.refund_calls[0]["amount"] == 123


async def test_refunding_twice_sends_one_refund(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)

    assert await run(session_maker, fake_paystack, scenario) is True
    assert (
        await run(session_maker, fake_paystack, scenario, AFTER_RETRY_WINDOW) is False
    )

    assert len(fake_paystack.refund_calls) == 1


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.PENDING,
        PaymentStatus.PAID,
        PaymentStatus.REFUNDED,
        PaymentStatus.FAILED,
    ],
)
async def test_only_refund_pending_payments_are_refunded(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    status: PaymentStatus,
) -> None:
    scenario = await owed(db_session, status=status)
    before = row_dict((await reload(db_session, scenario))[1])

    assert await run(session_maker, fake_paystack, scenario) is False

    assert fake_paystack.refund_calls == []
    assert row_dict((await reload(db_session, scenario))[1]) == before


# --- retries ---


async def test_a_failed_attempt_stays_refund_pending_and_is_retried_after_the_window(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)
    fake_paystack.refund_failures = 1

    assert await run(session_maker, fake_paystack, scenario, T0) is False
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refund_attempts == 1
    assert payment.refund_attempted_at == T0

    # Too soon: another attempt could still be in flight, so nothing is sent.
    assert (
        await run(session_maker, fake_paystack, scenario, T0 + timedelta(minutes=1))
        is False
    )
    assert len(fake_paystack.refund_calls) == 1

    assert await run(session_maker, fake_paystack, scenario, AFTER_RETRY_WINDOW) is True
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refund_attempts == 2
    assert fake_paystack.find_calls == [scenario.reference] * 2  # every attempt looks


async def test_a_lost_reply_is_found_on_retry_and_never_refunded_twice(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    """Paystack took the refund but we never heard back: the retry must not send another."""
    scenario = await owed(db_session)
    fake_paystack.lose_next_refund_reply = True

    assert await run(session_maker, fake_paystack, scenario, T0) is False
    assert len(fake_paystack.refund_calls) == 1

    assert await run(session_maker, fake_paystack, scenario, AFTER_RETRY_WINDOW) is True

    assert len(fake_paystack.refund_calls) == 1  # still just the one
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.paystack_refund_id == "rf_1"


async def test_a_refund_that_ran_out_of_attempts_is_left_alone(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)
    assert scenario.payment is not None
    scenario.payment.refund_attempts = MAX_REFUND_ATTEMPTS
    await db_session.commit()

    assert (
        await run(session_maker, fake_paystack, scenario, AFTER_RETRY_WINDOW) is False
    )

    assert fake_paystack.refund_calls == []
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING


# --- concurrency and locking ---


async def test_two_workers_refunding_at_once_send_one_refund(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)

    release = asyncio.Event()

    async def hold(_: str) -> None:
        await release.wait()  # the first call stays in flight until we say so

    fake_paystack.on_refund = hold

    workers = [
        asyncio.create_task(run(session_maker, fake_paystack, scenario))
        for _ in range(4)
    ]
    # One is inside Paystack; the other three have already found nothing to do.
    await until(
        lambda: (
            len(fake_paystack.refund_calls) == 1 and sum(w.done() for w in workers) == 3
        )
    )
    release.set()
    results = await asyncio.gather(*workers)

    assert sorted(results) == [False, False, False, True]
    assert len(fake_paystack.refund_calls) == 1
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED


async def test_no_row_lock_is_held_while_paystack_is_called(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await owed(db_session)
    seen: list[bool] = []

    async def peek(reference: str) -> None:
        # NOWAIT raises at once if anyone still holds the row, so this proves no lock.
        async with session_maker() as other:
            row = await other.execute(
                select(Payment.id)
                .where(Payment.paystack_reference == reference)
                .with_for_update(nowait=True)
            )
            seen.append(row.scalar_one_or_none() is not None)

    fake_paystack.on_refund = peek

    assert await run(session_maker, fake_paystack, scenario) is True
    assert seen == [True]


# --- refund events from Paystack ---


async def deliver_event(db_client: AsyncClient, raw: bytes) -> None:
    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))
    assert resp.status_code == 200


async def test_refund_processed_event_settles_a_waiting_refund_and_replays_are_no_ops(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await owed(db_session)
    raw = refund_event_body("refund.processed", scenario.reference)

    await deliver_event(db_client, raw)
    _, first = await reload(db_session, scenario)
    assert first.status == PaymentStatus.REFUNDED
    assert first.refunded_at is not None

    await deliver_event(db_client, raw)
    _, second = await reload(db_session, scenario)
    assert row_dict(second) == row_dict(first)


async def test_refund_failed_event_reopens_a_payment_marked_refunded(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await owed(db_session, status=PaymentStatus.REFUNDED)
    assert scenario.payment is not None
    scenario.payment.refunded_at = T0
    await db_session.commit()
    raw = refund_event_body("refund.failed", scenario.reference, status="failed")

    await deliver_event(db_client, raw)

    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING  # we still owe it
    assert payment.refunded_at is None
    assert payment.refund_attempted_at is None  # due for a retry at once


async def test_refund_failed_event_makes_a_waiting_refund_due_at_once_without_a_new_budget(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await owed(db_session)
    assert scenario.payment is not None
    scenario.payment.refund_attempted_at = T0
    scenario.payment.refund_attempts = 7
    await db_session.commit()

    await deliver_event(
        db_client, refund_event_body("refund.failed", scenario.reference)
    )

    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refund_attempted_at is None  # due now
    assert payment.refund_attempts == 7  # a refund that keeps failing still runs out


@pytest.mark.parametrize(
    "raw",
    [
        refund_event_body("refund.processed", "sb_does_not_exist"),
        refund_event_body("refund.pending", "sb_x", status="pending"),
        refund_event_body("refund.processing", "sb_x", status="processing"),
        b'{"event": "refund.processed", "data": {"status": "processed"}}',
        b'{"event": ["refund.processed"], "data": {}}',
    ],
    ids=[
        "unknown-reference",
        "pending-ignored",
        "processing-ignored",
        "no-reference",
        "odd-event",
    ],
)
async def test_unusable_refund_events_are_acknowledged_and_change_nothing(
    db_client: AsyncClient, db_session: AsyncSession, raw: bytes
) -> None:
    scenario = await owed(db_session)
    before = row_dict((await reload(db_session, scenario))[1])

    await deliver_event(db_client, raw)

    assert row_dict((await reload(db_session, scenario))[1]) == before


# --- the webhook starts the refund itself ---


async def test_a_mismatched_charge_is_refunded_by_the_webhook_in_the_background(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session)
    received = scenario.amount - 1000
    raw = charge_success_body(scenario.reference, received)

    await deliver_event(db_client, raw)

    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": received}
    ]
    booking, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert booking.status.value == "pending"  # never confirmed on a wrong amount


async def test_a_confirmed_payment_is_not_refunded(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session)

    await deliver_event(
        db_client, charge_success_body(scenario.reference, scenario.amount)
    )

    assert fake_paystack.refund_calls == []
