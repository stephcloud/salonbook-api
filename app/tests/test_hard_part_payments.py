"""Hard part #3: idempotent payments. (a) replays change state once; (c) forged calls write nothing.

(b), a late charge.success refunds and never revives, joins these with the refund flow.
"""

import asyncio
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
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
