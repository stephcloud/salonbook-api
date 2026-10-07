import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.services.bookings import PENDING_EXPIRY_MINUTES, expire_pending_bookings
from app.services.payments import handle_paystack_event
from app.tests.payment_helpers import (
    Scenario,
    all_payments,
    make_scenario,
    reload,
    row_dict,
)
from app.tests.paystack_fakes import (
    charge_success_body,
    event_body,
    sign,
    webhook_headers,
)

WEBHOOK = "/api/v1/payments/webhook"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
WINDOW = timedelta(minutes=PENDING_EXPIRY_MINUTES)


async def deliver(
    session_maker: async_sessionmaker[AsyncSession],
    scenario: Scenario,
    now: datetime,
    *,
    amount: int | None = None,
    currency: str = "NGN",
) -> None:
    """Run the webhook service in its own session, at a fixed clock."""
    raw = charge_success_body(
        scenario.reference, amount or scenario.amount, currency=currency
    )
    async with session_maker() as session:
        await handle_paystack_event(session, raw, sign(raw), now=now)


# --- the 15-minute boundary: the same predicate the expiry job uses ---


@pytest.mark.parametrize(
    ("age", "confirmed"),
    [
        (WINDOW - timedelta(seconds=1), True),  # just inside
        (WINDOW, True),  # exactly 15:00 still holds, as in expire_pending_bookings
        (WINDOW + timedelta(seconds=1), False),  # just past: late
    ],
    ids=["just-inside", "exactly-at-limit", "just-past"],
)
async def test_payment_is_confirmed_inside_the_window_and_late_outside_it(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    age: timedelta,
    confirmed: bool,
) -> None:
    scenario = await make_scenario(db_session, age=age, now=NOW)

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    if confirmed:
        assert booking.status == BookingStatus.CONFIRMED
        assert payment.status == PaymentStatus.PAID
        assert booking.refund_due is None
    else:
        assert booking.status == BookingStatus.CANCELLED  # never revived
        assert booking.refund_due is True
        assert payment.status == PaymentStatus.REFUND_PENDING
        assert payment.refund_amount == payment.amount


async def test_the_job_and_the_webhook_agree_on_the_boundary(
    db_session: AsyncSession,
) -> None:
    """A booking the expiry job would leave alone is one the webhook confirms."""
    at_limit = await make_scenario(db_session, age=WINDOW, now=NOW)
    past_limit = await make_scenario(
        db_session, age=WINDOW + timedelta(seconds=1), now=NOW
    )

    assert await expire_pending_bookings(db_session, NOW) == 1
    await db_session.commit()

    at_booking, _ = await reload(db_session, at_limit)
    past_booking, _ = await reload(db_session, past_limit)
    assert at_booking.status == BookingStatus.PENDING
    assert past_booking.status == BookingStatus.CANCELLED


# --- confirming ---


async def test_charge_success_confirms_booking_and_marks_payment_paid(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)
    raw = charge_success_body(scenario.reference, scenario.amount)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.PAID
    assert payment.paid_at is not None


async def test_the_amount_in_the_event_never_overrides_the_stored_amount(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)
    raw = charge_success_body(scenario.reference, scenario.amount + 1)

    await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    _, payment = await reload(db_session, scenario)
    assert payment.amount == scenario.amount  # unchanged
    assert payment.received_amount == scenario.amount + 1


# --- mismatches: money arrived, but not what we asked for ---


@pytest.mark.parametrize(
    ("amount_delta", "currency"),
    [(-1, "NGN"), (1, "NGN"), (0, "GHS")],
    ids=["underpaid", "overpaid", "wrong-currency"],
)
async def test_a_mismatched_charge_is_not_confirmed_and_is_queued_for_refund(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    amount_delta: int,
    currency: str,
) -> None:
    scenario = await make_scenario(db_session, now=NOW)
    received = scenario.amount + amount_delta

    await deliver(session_maker, scenario, NOW, amount=received, currency=currency)

    booking, payment = await reload(db_session, scenario)
    assert (
        booking.status == BookingStatus.PENDING
    )  # not confirmed; the job will expire it
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.amount == scenario.amount
    assert payment.received_amount == received
    assert payment.refund_amount == received


# --- late payments: cancelled, expired, or pending-but-past-the-window ---


async def test_charge_success_on_a_booking_the_client_cancelled_never_revives_it(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(
        db_session, now=NOW, booking_status=BookingStatus.CANCELLED
    )
    scenario.booking.refund_due = False
    await db_session.commit()

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refund_amount == payment.amount


async def test_charge_success_after_the_expiry_job_cancelled_the_booking(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(db_session, age=timedelta(minutes=30), now=NOW)
    await expire_pending_bookings(db_session, NOW)
    await db_session.commit()
    booking, _ = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is None  # the job records no decision

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUND_PENDING


async def test_charge_success_on_a_lapsed_booking_with_a_failed_row_cancels_it(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(
        db_session,
        age=WINDOW + timedelta(seconds=1),
        now=NOW,
        payment_status=PaymentStatus.FAILED,
    )

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUND_PENDING


async def test_stale_event_on_a_failed_row_is_refunded_and_leaves_a_valid_booking_alone(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """The client may be paying through a newer payment: a stale event mustn't cost them the slot."""
    scenario = await make_scenario(
        db_session, now=NOW, payment_status=PaymentStatus.FAILED
    )
    before, _ = await reload(db_session, scenario)
    before_row = row_dict(before)

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    assert row_dict(booking) == before_row  # still pending, untouched
    assert payment.status == PaymentStatus.REFUND_PENDING
    assert payment.refund_amount == payment.amount


async def test_newer_payment_still_confirms_after_a_stale_event_on_the_failed_one(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(
        db_session, now=NOW, payment_status=PaymentStatus.FAILED
    )
    newer = Payment(
        booking_id=scenario.booking.id,
        paystack_reference="sb_newer",
        amount=scenario.amount,
        currency="NGN",
        status=PaymentStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )
    db_session.add(newer)
    await db_session.commit()

    await deliver(session_maker, scenario, NOW)  # stale event, failed row
    raw = charge_success_body("sb_newer", scenario.amount)
    async with session_maker() as session:
        await handle_paystack_event(session, raw, sign(raw), now=NOW)

    booking, _ = await reload(db_session, scenario)
    await db_session.refresh(newer)
    assert booking.status == BookingStatus.CONFIRMED
    assert newer.status == PaymentStatus.PAID


# --- replays of the non-confirm paths write nothing either ---


async def test_replayed_late_payment_event_changes_nothing(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(
        db_session, now=NOW, booking_status=BookingStatus.CANCELLED
    )
    await deliver(session_maker, scenario, NOW)
    first = tuple(row_dict(r) for r in await reload(db_session, scenario))

    await deliver(session_maker, scenario, NOW + timedelta(minutes=5))

    assert tuple(row_dict(r) for r in await reload(db_session, scenario)) == first


async def test_replayed_mismatch_event_changes_nothing(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    scenario = await make_scenario(db_session, now=NOW)
    wrong = scenario.amount - 1
    await deliver(session_maker, scenario, NOW, amount=wrong)
    first = tuple(row_dict(r) for r in await reload(db_session, scenario))

    # The replay even carries a different amount: the first outcome stands.
    await deliver(session_maker, scenario, NOW + timedelta(minutes=5), amount=wrong + 5)

    assert tuple(row_dict(r) for r in await reload(db_session, scenario)) == first


async def test_a_late_payment_never_touches_a_booking_another_payment_confirmed(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A stale event for a failed row must not cancel a booking that was paid properly."""
    scenario = await make_scenario(
        db_session,
        now=NOW,
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.FAILED,
    )
    before, _ = await reload(db_session, scenario)
    before_row = row_dict(before)

    await deliver(session_maker, scenario, NOW)

    booking, payment = await reload(db_session, scenario)
    assert row_dict(booking) == before_row
    assert payment.status == PaymentStatus.REFUND_PENDING


# --- things we acknowledge but ignore ---


async def test_unknown_reference_is_acknowledged_and_writes_nothing(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)
    before = [row_dict(p) for p in await all_payments(db_session)]
    raw = charge_success_body("sb_does_not_exist", 500000)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    assert [row_dict(p) for p in await all_payments(db_session)] == before
    booking, _ = await reload(db_session, scenario)
    assert booking.status == BookingStatus.PENDING


@pytest.mark.parametrize(
    "event", ["transfer.success", "customer.identification.failed"]
)
async def test_unhandled_event_types_are_acknowledged_and_ignored(
    db_client: AsyncClient, db_session: AsyncSession, event: str
) -> None:
    scenario = await make_scenario(db_session)
    raw = event_body(
        event, {"reference": scenario.reference, "amount": scenario.amount}
    )

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.PENDING
    assert payment.status == PaymentStatus.PENDING


async def test_a_signed_charge_success_missing_fields_is_acknowledged_and_ignored(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session)
    raw = event_body("charge.success", {"reference": scenario.reference})  # no amount

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.PENDING


async def test_a_signed_body_that_is_not_json_is_400(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    raw = b"definitely not json"

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 400


async def test_service_raises_401_before_touching_the_session() -> None:
    class ExplodingSession:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(
                f"session.{name} used before the signature was checked"
            )

    raw = json.dumps({"event": "charge.success"}).encode()
    with pytest.raises(HTTPException) as excinfo:
        await handle_paystack_event(ExplodingSession(), raw, "bad")  # type: ignore[arg-type]
    assert excinfo.value.status_code == 401
