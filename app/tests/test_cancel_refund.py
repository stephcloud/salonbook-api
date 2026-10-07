"""Cancelling a booking applies the refund decision to its deposit exactly once."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.jobs import retry_refunds
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.user import User, UserRole
from app.services import bookings as booking_service
from app.tests.payment_helpers import Scenario, make_scenario, reload, until
from app.tests.paystack_fakes import FakePaystack, charge_success_body, webhook_headers
from app.tests.test_salons_api import make_user

WEBHOOK = "/api/v1/payments/webhook"
PAID_AND_CONFIRMED: dict[str, Any] = {
    "booking_status": BookingStatus.CONFIRMED,
    "payment_status": PaymentStatus.PAID,
}


def cancel_url(scenario: Scenario) -> str:
    return f"/api/v1/bookings/{scenario.booking.id}/cancel"


async def owner_headers(db_session: AsyncSession, scenario: Scenario) -> dict[str, str]:
    owner = await db_session.get(User, scenario.salon.owner_id)
    assert owner is not None
    return {"Authorization": f"Bearer {create_access_token(owner.id)}"}


# --- the refund decision reaches the deposit ---


async def test_cancelling_well_before_the_limit_refunds_the_deposit(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await db_client.post(cancel_url(scenario), headers=scenario.headers)

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUNDED
    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": scenario.amount}
    ]


async def test_the_service_queues_the_refund_in_the_same_commit_and_never_calls_paystack(
    db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    await booking_service.cancel_booking(
        db_session, scenario.booking.id, scenario.client
    )

    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUND_PENDING  # decided with the cancel
    assert payment.refund_amount == scenario.amount
    assert fake_paystack.refund_calls == []  # sending is a separate, later step


async def test_a_late_client_cancel_keeps_the_deposit(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )

    resp = await db_client.post(cancel_url(scenario), headers=scenario.headers)

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is False
    assert payment.status == PaymentStatus.PAID  # the salon keeps it
    assert fake_paystack.refund_calls == []


async def test_the_salon_owner_cancelling_inside_the_window_still_refunds(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )

    resp = await db_client.post(
        cancel_url(scenario), headers=await owner_headers(db_session, scenario)
    )

    assert resp.status_code == 200
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert len(fake_paystack.refund_calls) == 1


async def test_cancelling_a_confirmed_booking_with_no_payment_row_still_works(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(
        db_session, booking_status=BookingStatus.CONFIRMED, payment_status=None
    )

    resp = await db_client.post(cancel_url(scenario), headers=scenario.headers)

    assert resp.status_code == 200
    assert resp.json()["refund_due"] is True
    assert fake_paystack.refund_calls == []


# --- exactly once ---


async def test_cancelling_twice_refunds_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    first = await db_client.post(cancel_url(scenario), headers=scenario.headers)
    second = await db_client.post(cancel_url(scenario), headers=scenario.headers)

    assert (first.status_code, second.status_code) == (200, 200)
    assert len(fake_paystack.refund_calls) == 1
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED


async def test_simultaneous_cancels_refund_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    release = asyncio.Event()

    async def hold(_: str) -> None:
        await release.wait()  # one refund stays in flight until we say so

    fake_paystack.on_refund = hold

    requests = [
        asyncio.create_task(
            db_client.post(cancel_url(scenario), headers=scenario.headers)
        )
        for _ in range(4)
    ]
    # One request is inside Paystack; the other three have already returned.
    await until(
        lambda: (
            len(fake_paystack.refund_calls) == 1
            and sum(r.done() for r in requests) == 3
        )
    )
    release.set()
    responses = await asyncio.gather(*requests)

    assert [r.status_code for r in responses] == [200] * 4
    assert len(fake_paystack.refund_calls) == 1
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED


# --- cancel and payment racing in either order ---


async def test_cancelling_before_the_payment_lands_refunds_it_when_it_does(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    """Client cancels with a checkout open, then pays anyway: refunded, never confirmed."""
    scenario = await make_scenario(db_session)  # pending booking, pending payment

    cancel = await db_client.post(cancel_url(scenario), headers=scenario.headers)
    assert cancel.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.refund_due is False  # nothing had been paid yet
    assert payment.status == PaymentStatus.PENDING
    assert fake_paystack.refund_calls == []

    raw = charge_success_body(scenario.reference, scenario.amount)
    hook = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert hook.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED  # not revived
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUNDED
    assert len(fake_paystack.refund_calls) == 1


# --- failure and access ---


async def test_cancel_succeeds_even_when_paystack_is_down_and_the_retry_job_finishes_it(
    db_client: AsyncClient,
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    fake_paystack.refund_failures = 1

    resp = await db_client.post(cancel_url(scenario), headers=scenario.headers)

    assert resp.status_code == 200  # the cancel itself is not held hostage by Paystack
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUND_PENDING

    monkeypatch.setattr(retry_refunds, "async_session_maker", session_maker)
    later = datetime.now(UTC) + timedelta(minutes=10)
    assert await retry_refunds.run_once(later, fake_paystack) == 1

    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert len(fake_paystack.refund_calls) == 2  # one failed, one sent


async def test_another_client_cannot_cancel_or_trigger_a_refund(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    _, other_headers = await make_user(db_session, UserRole.CLIENT)

    resp = await db_client.post(cancel_url(scenario), headers=other_headers)

    assert resp.status_code == 403
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.PAID
    assert fake_paystack.refund_calls == []


async def test_cancelling_an_unknown_booking_is_404_and_refunds_nothing(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await db_client.post(
        f"/api/v1/bookings/{uuid.uuid4()}/cancel", headers=scenario.headers
    )

    assert resp.status_code == 404
    assert fake_paystack.refund_calls == []


# --- locking ---


async def test_cancel_does_not_block_a_payment_taking_its_foreign_key_lock(
    db_client: AsyncClient,
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel locks the booking FOR NO KEY UPDATE, so a row referencing it (a payment
    insert) can still take FOR KEY SHARE while the cancel is in flight. A plain FOR
    UPDATE would make that lock attempt fail."""
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    booking_id = scenario.booking.id
    real = booking_service._queue_deposit_refund
    seen: list[bool] = []

    async def peek_then_queue(session: AsyncSession, booking_id: uuid.UUID) -> None:
        # Runs inside the cancel's transaction, booking lock held and uncommitted.
        async with session_maker() as referencing:
            await referencing.execute(
                select(Booking.id)
                .where(Booking.id == booking_id)
                .with_for_update(
                    read=True, key_share=True, nowait=True
                )  # FOR KEY SHARE
            )
            seen.append(True)
        await real(session, booking_id)

    monkeypatch.setattr(booking_service, "_queue_deposit_refund", peek_then_queue)

    resp = await db_client.post(
        f"/api/v1/bookings/{booking_id}/cancel", headers=scenario.headers
    )

    assert resp.status_code == 200
    assert seen == [True]
