"""Hard part #3: idempotent payments, and money that arrives too late.

(a) a replayed webhook changes state once; (b) a late charge.success is refunded exactly
once and never revives the booking; (c) a forged call is a 401 that writes nothing.
"""

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.user import UserRole
from app.services.bookings import PENDING_EXPIRY_MINUTES, expire_pending_bookings
from app.tests.payment_helpers import (
    Scenario,
    all_payments,
    make_scenario,
    reload,
    row_dict,
)
from app.tests.paystack_fakes import (
    FakePaystack,
    charge_success_body,
    sign,
    webhook_headers,
)
from app.tests.test_salons_api import make_user

WEBHOOK = "/api/v1/payments/webhook"


async def snapshot(
    db_session: AsyncSession, scenario: Scenario
) -> tuple[dict[str, Any], dict[str, Any]]:
    booking, payment = await reload(db_session, scenario)
    return row_dict(booking), row_dict(payment)


# --- (a) the same webhook delivered more than once changes state once ---


async def test_replayed_webhook_changes_state_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session)
    raw = charge_success_body(scenario.reference, scenario.amount)

    first = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))
    assert first.status_code == 200
    booking, payment = await snapshot(db_session, scenario)
    assert booking["status"] == BookingStatus.CONFIRMED
    assert payment["status"] == PaymentStatus.PAID
    assert payment["paid_at"] is not None

    for _ in range(3):  # Paystack retries; so do flaky networks
        again = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))
        assert again.status_code == 200

    # Every column, including updated_at, is exactly as the first delivery left it.
    assert await snapshot(db_session, scenario) == (booking, payment)
    assert len(await all_payments(db_session)) == 1
    assert fake_paystack.initialize_calls == []


async def test_simultaneous_duplicate_webhooks_confirm_once(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Two copies in flight at once: the second must see `paid`, not `pending`.

    If it didn't, it would find the booking already confirmed and treat the payment as
    late, wrongly moving a good deposit to refund_pending.
    """
    scenario = await make_scenario(db_session)
    raw = charge_success_body(scenario.reference, scenario.amount)
    headers = webhook_headers(raw)

    responses = await asyncio.gather(
        *(db_client.post(WEBHOOK, content=raw, headers=headers) for _ in range(5))
    )

    assert [r.status_code for r in responses] == [200] * 5
    booking, payment = await snapshot(db_session, scenario)
    assert booking["status"] == BookingStatus.CONFIRMED
    assert booking["refund_due"] is None
    assert payment["status"] == PaymentStatus.PAID
    assert payment["refund_amount"] is None
    # A further replay still writes nothing.
    await db_client.post(WEBHOOK, content=raw, headers=headers)
    assert await snapshot(db_session, scenario) == (booking, payment)


# --- (c) a bad signature is rejected before anything is read or written ---


def _tampered_case(scenario: Scenario) -> tuple[bytes, dict[str, str]]:
    """A valid signature for the real body, attached to a body that claims more."""
    real = charge_success_body(scenario.reference, scenario.amount)
    forged = charge_success_body(scenario.reference, scenario.amount * 10)
    return forged, {"x-paystack-signature": sign(real)}


def _wrong_key_case(scenario: Scenario) -> tuple[bytes, dict[str, str]]:
    raw = charge_success_body(scenario.reference, scenario.amount)
    return raw, {"x-paystack-signature": sign(raw, secret="sk_test_someone_elses")}


def _missing_header_case(scenario: Scenario) -> tuple[bytes, dict[str, str]]:
    return charge_success_body(scenario.reference, scenario.amount), {}


def _garbage_case(scenario: Scenario) -> tuple[bytes, dict[str, str]]:
    raw = charge_success_body(scenario.reference, scenario.amount)
    return raw, {"x-paystack-signature": "not-a-signature"}


def _empty_signature_case(scenario: Scenario) -> tuple[bytes, dict[str, str]]:
    raw = charge_success_body(scenario.reference, scenario.amount)
    return raw, {"x-paystack-signature": ""}


@pytest.mark.parametrize(
    "build",
    [
        _tampered_case,
        _wrong_key_case,
        _missing_header_case,
        _garbage_case,
        _empty_signature_case,
    ],
)
async def test_bad_signature_is_401_and_writes_nothing(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_paystack: FakePaystack,
    build: Any,
) -> None:
    scenario = await make_scenario(db_session)
    before = await snapshot(db_session, scenario)
    raw, headers = build(scenario)

    resp = await db_client.post(WEBHOOK, content=raw, headers=headers)

    assert resp.status_code == 401
    assert scenario.reference not in resp.text  # nothing from the body is echoed back
    assert await snapshot(db_session, scenario) == before
    assert len(await all_payments(db_session)) == 1
    assert fake_paystack.initialize_calls == []


async def test_empty_configured_secret_rejects_even_a_matching_signature(
    db_client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a deployment with no secret set must not accept anything."""
    monkeypatch.setattr(settings, "PAYSTACK_SECRET_KEY", "")
    scenario = await make_scenario(db_session)
    before = await snapshot(db_session, scenario)
    raw = charge_success_body(scenario.reference, scenario.amount)
    # The signature an attacker could compute knowing the key is empty.
    headers = {"x-paystack-signature": sign(raw, secret="")}

    resp = await db_client.post(WEBHOOK, content=raw, headers=headers)

    assert resp.status_code == 401
    assert await snapshot(db_session, scenario) == before


# --- (b) money that arrives too late is refunded once and never revives the booking ---

LAPSED_AGE = timedelta(minutes=PENDING_EXPIRY_MINUTES + 5)
LATE_CASES = [
    "cancelled-by-client",
    "cancelled-by-expiry-job",
    "lapsed-but-job-not-run",
]


async def late_scenario(db_session: AsyncSession, how: str) -> Scenario:
    """A booking that can no longer take the client's payment, in each way that happens."""
    if how == "cancelled-by-client":
        return await make_scenario(db_session, booking_status=BookingStatus.CANCELLED)
    scenario = await make_scenario(db_session, age=LAPSED_AGE)
    if how == "cancelled-by-expiry-job":
        await expire_pending_bookings(db_session)
        await db_session.commit()
    else:
        assert how == "lapsed-but-job-not-run"  # still `pending` in the table
    return scenario


@pytest.mark.parametrize("how", LATE_CASES)
async def test_late_charge_success_is_refunded_once_and_never_confirms(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_paystack: FakePaystack,
    how: str,
) -> None:
    scenario = await late_scenario(db_session, how)
    raw = charge_success_body(scenario.reference, scenario.amount)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED  # never revived, never confirmed
    assert booking.refund_due is True
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refunded_at is not None
    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": scenario.amount}  # in full
    ]

    # Paystack delivers it again: nothing changes and no second refund is sent.
    first = await snapshot(db_session, scenario)
    again = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))
    assert again.status_code == 200
    assert await snapshot(db_session, scenario) == first
    assert len(fake_paystack.refund_calls) == 1


@pytest.mark.parametrize("how", LATE_CASES)
async def test_late_charge_success_leaves_the_slot_free(
    db_client: AsyncClient, db_session: AsyncSession, how: str
) -> None:
    """If the late payment had revived the booking, the next booking would hit the
    exclusion constraint. It doesn't, because the slot was never taken back."""
    scenario = await late_scenario(db_session, how)
    raw = charge_success_body(scenario.reference, scenario.amount)
    await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    other, _ = await make_user(db_session, UserRole.CLIENT)
    db_session.add(
        Booking(
            client_id=other.id,
            stylist_id=scenario.booking.stylist_id,
            service_id=scenario.booking.service_id,
            starts_at=scenario.booking.starts_at,
            ends_at=scenario.booking.ends_at,
            status=BookingStatus.CONFIRMED,
        )
    )
    await db_session.commit()  # would raise if the slot were still held


async def test_late_payment_after_the_slot_was_rebooked_is_refunded_not_an_error(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    """The case that matters: someone else already holds the slot. Reviving the booking
    would collide with them (a 500, retried forever); refunding must just work."""
    scenario = await late_scenario(db_session, "cancelled-by-expiry-job")
    rival, _ = await make_user(db_session, UserRole.CLIENT)
    rebooked = Booking(
        client_id=rival.id,
        stylist_id=scenario.booking.stylist_id,
        service_id=scenario.booking.service_id,
        starts_at=scenario.booking.starts_at,
        ends_at=scenario.booking.ends_at,
        status=BookingStatus.CONFIRMED,
    )
    db_session.add(rebooked)
    await db_session.commit()
    rebooked_before = row_dict(rebooked)
    raw = charge_success_body(scenario.reference, scenario.amount)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUNDED
    assert len(fake_paystack.refund_calls) == 1
    await db_session.refresh(rebooked)
    assert row_dict(rebooked) == rebooked_before  # the rival's booking is untouched


@pytest.mark.parametrize(
    ("amount_delta", "currency"),
    [(-1000, "NGN"), (1000, "NGN"), (0, "GHS")],
    ids=["underpaid", "overpaid", "wrong-currency"],
)
async def test_a_mismatched_charge_is_refunded_for_what_was_received_and_never_confirms(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_paystack: FakePaystack,
    amount_delta: int,
    currency: str,
) -> None:
    scenario = await make_scenario(db_session)
    received = scenario.amount + amount_delta
    raw = charge_success_body(scenario.reference, received, currency=currency)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.PENDING  # never confirmed on a wrong payment
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.received_amount == received
    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": received}
    ]
    await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))
    assert len(fake_paystack.refund_calls) == 1  # a replay sends nothing more


async def test_a_refund_paystack_cannot_send_yet_is_left_waiting_not_lost(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await late_scenario(db_session, "cancelled-by-client")
    fake_paystack.refund_failures = 1
    raw = charge_success_body(scenario.reference, scenario.amount)

    resp = await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))

    assert resp.status_code == 200  # Paystack retrying this webhook is not needed
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUND_PENDING  # the retry job will send it
    assert payment.refund_attempts == 1
