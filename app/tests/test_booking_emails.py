"""Booking emails through the real endpoints, with a fake sender (Brevo is never called)."""

import asyncio
import logging
import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.main import app
from app.models.booking import Booking, BookingStatus
from app.models.payment import PaymentStatus
from app.models.user import User
from app.services.bookings import cancel_booking_with_outcome
from app.services.email.base import EmailMessage
from app.services.email.brevo import BrevoSender, get_email_sender
from app.services.email.notifications import (
    notify_booking_cancelled,
    notify_booking_confirmed,
)
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import Scenario, make_scenario, reload
from app.tests.paystack_fakes import FakePaystack, charge_success_body, webhook_headers

WEBHOOK = "/api/v1/payments/webhook"
PAID_AND_CONFIRMED: dict[str, Any] = {
    "booking_status": BookingStatus.CONFIRMED,
    "payment_status": PaymentStatus.PAID,
}
EMAIL_ADDRESS = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


async def owner_of(db_session: AsyncSession, scenario: Scenario) -> User:
    owner = await db_session.get(User, scenario.salon.owner_id)
    assert owner is not None
    return owner


async def owner_headers(db_session: AsyncSession, scenario: Scenario) -> dict[str, str]:
    owner = await owner_of(db_session, scenario)
    return {"Authorization": f"Bearer {create_access_token(owner.id)}"}


async def pay(
    db_client: AsyncClient, scenario: Scenario, amount: int | None = None
) -> httpx.Response:
    raw = charge_success_body(scenario.reference, amount or scenario.amount)
    return await db_client.post(WEBHOOK, content=raw, headers=webhook_headers(raw))


async def cancel(
    db_client: AsyncClient, scenario: Scenario, headers: dict[str, str] | None = None
) -> httpx.Response:
    return await db_client.post(
        f"/api/v1/bookings/{scenario.booking.id}/cancel",
        headers=headers or scenario.headers,
    )


async def stored(db_session: AsyncSession, scenario: Scenario) -> Booking:
    booking, _ = (
        await reload(db_session, scenario) if scenario.payment else (None, None)
    )
    assert booking is not None
    return booking


def only(messages: list[EmailMessage]) -> EmailMessage:
    assert len(messages) == 1, [m.subject for m in messages]
    return messages[0]


# --- confirmation (the Paystack webhook) ---


async def test_a_paid_booking_emails_the_client_and_the_owner(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)
    owner = await owner_of(db_session, scenario)

    resp = await pay(db_client, scenario)

    assert resp.status_code == 200
    assert len(fake_email.sent) == 2
    client_msg = only(fake_email.to(scenario.client.email))
    owner_msg = only(fake_email.to(owner.email))
    assert "confirmed" in client_msg.subject
    assert "New booking" in owner_msg.subject
    booking = await stored(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert booking.confirmation_email_sent_at is not None
    assert booking.owner_confirmation_email_sent_at is not None
    assert booking.cancellation_email_sent_at is None


async def test_the_same_webhook_twice_sends_the_emails_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)

    first = await pay(db_client, scenario)
    again = await pay(db_client, scenario)

    assert first.status_code == again.status_code == 200
    assert len(fake_email.sent) == 2  # one to the client, one to the owner, not four


async def test_simultaneous_duplicate_webhooks_send_the_emails_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)

    responses = await asyncio.gather(*(pay(db_client, scenario) for _ in range(3)))

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(fake_email.sent) == 2


async def test_a_mismatched_charge_sends_no_confirmation(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)

    resp = await pay(db_client, scenario, amount=scenario.amount + 1)

    assert resp.status_code == 200
    assert (await stored(db_session, scenario)).status == BookingStatus.PENDING
    assert fake_email.sent == []


async def test_a_late_payment_sends_no_confirmation(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, age=timedelta(minutes=20))

    resp = await pay(db_client, scenario)

    assert resp.status_code == 200
    assert (await stored(db_session, scenario)).status == BookingStatus.CANCELLED
    assert fake_email.sent == []  # expiry-type cancellations send nothing in Phase 1


async def test_an_unsigned_webhook_sends_nothing(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)
    raw = charge_success_body(scenario.reference, scenario.amount)

    resp = await db_client.post(
        WEBHOOK, content=raw, headers={"x-paystack-signature": "no"}
    )

    assert resp.status_code == 401
    assert fake_email.sent == []


# --- cancellation: the wording follows what the cancel flow did ---


async def test_early_client_cancel_emails_both_and_says_the_deposit_was_refunded(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    owner = await owner_of(db_session, scenario)

    resp = await cancel(db_client, scenario)

    assert resp.status_code == 200
    assert resp.json()["refund_due"] is True
    client_msg = only(fake_email.to(scenario.client.email))
    owner_msg = only(fake_email.to(owner.email))
    # The refund task runs before the email task and the fake Paystack accepts it.
    assert "You cancelled this booking" in client_msg.text
    assert "has been refunded" in client_msg.text
    assert "has been refunded to the client" in owner_msg.text
    booking = await stored(db_session, scenario)
    assert booking.cancellation_email_sent_at is not None
    assert booking.owner_cancellation_email_sent_at is not None


async def test_a_refund_still_in_flight_is_worded_as_being_refunded(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    fake_paystack: FakePaystack,
) -> None:
    fake_paystack.refund_failures = 1  # Paystack is down: the refund stays queued
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    await cancel(db_client, scenario)

    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUND_PENDING
    text = only(fake_email.to(scenario.client.email)).text
    assert "is being refunded" in text
    assert "has been refunded" not in text


async def test_a_late_client_cancel_says_the_deposit_is_kept(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )
    owner = await owner_of(db_session, scenario)

    resp = await cancel(db_client, scenario)

    assert resp.json()["refund_due"] is False
    client_text = only(fake_email.to(scenario.client.email)).text
    assert "within 24 hours" in client_text
    assert "is not refunded" in client_text
    assert "refunded to" not in client_text
    assert "is kept" in only(fake_email.to(owner.email)).text


@pytest.mark.parametrize(
    ("before_start", "refunded"),
    [
        (timedelta(hours=24, seconds=1), True),
        (timedelta(hours=24), False),  # exactly at the limit is not refunded
    ],
    ids=["just-outside-the-window", "exactly-at-the-limit"],
)
async def test_the_email_states_the_refund_boundary_the_cancel_recorded(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
    before_start: timedelta,
    refunded: bool,
) -> None:
    """The cancel is driven at a fixed clock; the wording follows its recorded decision."""
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    outcome = await cancel_booking_with_outcome(
        db_session,
        scenario.booking.id,
        scenario.client,
        now=scenario.booking.starts_at - before_start,
    )
    assert outcome.booking.refund_due is refunded

    await notify_booking_cancelled(
        session_maker,
        fake_email,
        scenario.booking.id,
        cancelled_by_owner=outcome.cancelled_by_owner,
    )

    text = only(fake_email.to(scenario.client.email)).text
    assert ("is being refunded" in text) is refunded
    assert ("is not refunded" in text) is (not refunded)


async def test_the_owner_cancelling_inside_the_window_refunds_and_says_so(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )
    owner = await owner_of(db_session, scenario)

    resp = await cancel(
        db_client, scenario, headers=await owner_headers(db_session, scenario)
    )

    assert resp.json()["refund_due"] is True
    client_text = only(fake_email.to(scenario.client.email)).text
    assert "The salon cancelled this booking" in client_text
    assert "refunded" in client_text
    assert "not refunded" not in client_text
    assert "You cancelled a booking" in only(fake_email.to(owner.email)).text


async def test_a_confirmed_booking_with_no_payment_row_claims_no_refund(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    """refund_due is true here, but there is no deposit to send back: say nothing."""
    scenario = await make_scenario(
        db_session, booking_status=BookingStatus.CONFIRMED, payment_status=None
    )

    resp = await cancel(db_client, scenario)

    assert resp.json()["refund_due"] is True
    text = only(fake_email.to(scenario.client.email)).text
    assert "refund" not in text.lower()
    assert "deposit" not in text.lower()


@pytest.mark.parametrize("by_owner", [False, True], ids=["by-client", "by-owner"])
async def test_cancelling_an_unpaid_pending_booking_sends_no_email(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    by_owner: bool,
) -> None:
    scenario = await make_scenario(db_session)  # pending, checkout open, nothing paid
    headers = (
        await owner_headers(db_session, scenario) if by_owner else scenario.headers
    )

    resp = await cancel(db_client, scenario, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert fake_email.sent == []
    booking = await stored(db_session, scenario)
    assert booking.cancellation_email_sent_at is None
    assert booking.owner_cancellation_email_sent_at is None


async def test_cancelling_twice_sends_one_set_of_emails(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    first = await cancel(db_client, scenario)
    again = await cancel(db_client, scenario)

    assert first.status_code == again.status_code == 200
    assert len(fake_email.sent) == 2


async def test_simultaneous_double_cancel_sends_one_set_of_emails(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    responses = await asyncio.gather(*(cancel(db_client, scenario) for _ in range(3)))

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(fake_email.sent) == 2


async def test_someone_elses_cancel_is_refused_and_sends_nothing(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    from app.models.user import UserRole
    from app.tests.test_salons_api import make_user

    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    _, stranger = await make_user(db_session, UserRole.CLIENT)

    resp = await cancel(db_client, scenario, headers=stranger)

    assert resp.status_code == 403
    assert fake_email.sent == []


# --- a failing email never breaks the booking ---


async def test_a_failing_sender_does_not_break_the_webhook(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    fake_email.fail_next = 10
    scenario = await make_scenario(db_session)

    resp = await pay(db_client, scenario)

    assert resp.status_code == 200  # Paystack must still be told it worked
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.PAID
    assert fake_email.attempts == 2
    # The claim is given back, so the email can still go out later.
    assert booking.confirmation_email_sent_at is None
    assert booking.owner_confirmation_email_sent_at is None


async def test_a_failed_email_can_be_sent_later_and_then_only_once(
    db_client: AsyncClient,
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    fake_email.fail_next = 2
    scenario = await make_scenario(db_session)
    await pay(db_client, scenario)
    assert fake_email.sent == []

    await notify_booking_confirmed(session_maker, fake_email, scenario.booking.id)
    await notify_booking_confirmed(session_maker, fake_email, scenario.booking.id)

    assert len(fake_email.sent) == 2


async def test_an_unexpected_sender_crash_does_not_break_the_cancel(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    class Exploding:
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("boom")

    app.dependency_overrides[get_email_sender] = lambda: Exploding()
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await cancel(db_client, scenario)

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUNDED  # the refund was not disturbed
    assert booking.cancellation_email_sent_at is None


async def test_one_email_failing_does_not_stop_the_other(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    fake_email.fail_next = 1  # the first send (the client's) fails
    scenario = await make_scenario(db_session)
    owner = await owner_of(db_session, scenario)

    await pay(db_client, scenario)

    assert [m.to_address for m in fake_email.sent] == [owner.email]


# --- no email configuration ---


async def test_missing_email_config_skips_sending_and_still_works(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app.dependency_overrides[get_email_sender] = lambda: None  # what unset config gives
    caplog.set_level(logging.INFO)
    paid = await make_scenario(db_session)
    to_cancel = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    assert (await pay(db_client, paid)).status_code == 200
    assert (await cancel(db_client, to_cancel)).status_code == 200

    assert fake_email.sent == []
    assert (await stored(db_session, paid)).status == BookingStatus.CONFIRMED
    assert (await stored(db_session, paid)).confirmation_email_sent_at is None
    assert "email is not configured" in caplog.text


# --- the claim itself ---


async def test_sending_an_email_does_not_touch_updated_at(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    before = (await stored(db_session, scenario)).updated_at

    await notify_booking_confirmed(session_maker, fake_email, scenario.booking.id)

    after = await stored(db_session, scenario)
    assert after.confirmation_email_sent_at is not None
    assert after.updated_at == before


async def test_a_confirmation_for_a_booking_cancelled_meanwhile_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await make_scenario(
        db_session,
        booking_status=BookingStatus.CANCELLED,
        payment_status=PaymentStatus.PAID,
    )

    await notify_booking_confirmed(session_maker, fake_email, scenario.booking.id)

    assert fake_email.sent == []


async def test_a_cancellation_notice_for_a_booking_that_is_not_cancelled_is_skipped(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    await notify_booking_cancelled(
        session_maker,
        fake_email,
        scenario.booking.id,
        cancelled_by_owner=False,
    )

    assert fake_email.sent == []


# --- logs ---


async def test_no_log_line_contains_an_email_address_or_the_api_key(
    db_client: AsyncClient,
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Brevo rejects every send and echoes the recipient and key back in its error body.
    Whatever the code logs while handling that must carry neither."""
    api_key = "xkeysib-log-test-key"

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        return httpx.Response(400, json={"message": f"rejected {body} {api_key}"})

    brevo = BrevoSender(
        api_key,
        "no-reply@salonbook.test",
        "SalonBook",
        transport=httpx.MockTransport(handler),
    )
    app.dependency_overrides[get_email_sender] = lambda: brevo
    caplog.set_level(logging.DEBUG)
    paid = await make_scenario(db_session)
    to_cancel = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    owner = await owner_of(db_session, paid)

    await pay(db_client, paid)
    await cancel(db_client, to_cancel)

    # Not a vacuous pass: the sends really failed and were logged.
    assert any("not sent" in r.getMessage() for r in caplog.records)
    assert not EMAIL_ADDRESS.search(caplog.text), EMAIL_ADDRESS.findall(caplog.text)
    for secret in (paid.client.email, owner.email, "no-reply@salonbook.test", api_key):
        assert secret not in caplog.text
