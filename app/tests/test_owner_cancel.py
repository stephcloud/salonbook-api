"""A salon owner cancelling a booking at their salon, through POST /bookings/{id}/cancel.

The owner's cancel always refunds the deposit in full, even inside the cancellation
window, and the client is told the salon cancelled. Emails go out once, never break the
cancel, and no email is ever sent for a booking that is already cancelled.
"""

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import httpx
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import create_access_token
from app.main import app
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.models.user import User, UserRole
from app.services.bookings import cancel_booking_with_outcome
from app.services.email.base import EmailMessage
from app.services.email.brevo import get_email_sender
from app.services.email.notifications import notify_booking_confirmed
from app.services.email.reminders import run_due
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import Scenario, make_scenario, reload, row_dict
from app.tests.paystack_fakes import FakePaystack
from app.tests.test_booking_reminders import MORNING, NOON, confirmed
from app.tests.test_salons_api import make_user
from app.tests.test_services_api import owner_and_salon

PAID_AND_CONFIRMED: dict[str, Any] = {
    "booking_status": BookingStatus.CONFIRMED,
    "payment_status": PaymentStatus.PAID,
}


async def owner_of(db_session: AsyncSession, scenario: Scenario) -> User:
    owner = await db_session.get(User, scenario.salon.owner_id)
    assert owner is not None
    return owner


async def owner_headers(db_session: AsyncSession, scenario: Scenario) -> dict[str, str]:
    owner = await owner_of(db_session, scenario)
    return {"Authorization": f"Bearer {create_access_token(owner.id)}"}


async def owner_cancel(
    db_client: AsyncClient, db_session: AsyncSession, scenario: Scenario
) -> httpx.Response:
    return await db_client.post(
        f"/api/v1/bookings/{scenario.booking.id}/cancel",
        headers=await owner_headers(db_session, scenario),
    )


async def cancel_as(
    db_client: AsyncClient, scenario: Scenario, headers: dict[str, str]
) -> httpx.Response:
    return await db_client.post(
        f"/api/v1/bookings/{scenario.booking.id}/cancel", headers=headers
    )


# --- who may cancel ---


async def test_the_owner_of_the_salon_cancels_and_the_deposit_is_refunded_in_full(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    fake_paystack: FakePaystack,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["refund_due"] is True
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refund_amount == payment.amount
    assert fake_paystack.refund_calls == [
        {"reference": scenario.reference, "amount": payment.amount}
    ]


async def test_the_owner_cancel_refunds_in_full_even_inside_the_window(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    # Starts in 2 hours; the salon's window is 24, so a client's cancel would not refund.
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )
    assert scenario.salon.cancellation_hours == 24

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 200
    assert resp.json()["refund_due"] is True
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.REFUNDED
    assert payment.refund_amount == payment.amount
    assert [c["amount"] for c in fake_paystack.refund_calls] == [payment.amount]


async def test_the_owner_of_another_salon_gets_403_and_nothing_changes(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    _, other_owner_headers, _ = await owner_and_salon(db_session)
    before_booking, before_payment = await reload(db_session, scenario)
    before = (row_dict(before_booking), row_dict(before_payment))

    resp = await cancel_as(db_client, scenario, other_owner_headers)

    assert resp.status_code == 403
    booking, payment = await reload(db_session, scenario)
    assert (row_dict(booking), row_dict(payment)) == before
    assert fake_email.sent == []


async def test_a_client_cannot_cancel_someone_elses_booking(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    _, stranger = await make_user(db_session, UserRole.CLIENT)

    resp = await cancel_as(db_client, scenario, stranger)

    assert resp.status_code == 403
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.PAID
    assert fake_email.sent == []


# --- pending versus confirmed ---


async def test_an_owner_cancelling_a_pending_booking_refunds_nothing_and_emails_nothing(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session)  # pending, deposit not paid

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["refund_due"] is False
    _, payment = await reload(db_session, scenario)
    assert payment.status == PaymentStatus.PENDING  # nothing was paid, nothing moved
    assert fake_email.sent == []


async def test_an_owner_cancelling_a_confirmed_booking_refunds_and_emails_both(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    owner = await owner_of(db_session, scenario)

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 200
    assert len(fake_email.to(scenario.client.email)) == 1
    assert len(fake_email.to(owner.email)) == 1


# --- the emails ---


async def test_the_client_is_told_the_salon_cancelled_and_the_deposit_is_refunded(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=timedelta(hours=2), **PAID_AND_CONFIRMED
    )

    await owner_cancel(db_client, db_session, scenario)

    (message,) = fake_email.to(scenario.client.email)
    assert "The salon cancelled this booking" in message.text
    assert "refunded" in message.text
    assert "not refunded" not in message.text


async def test_the_owners_email_has_the_client_name_and_never_the_client_email(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    owner = await owner_of(db_session, scenario)

    await owner_cancel(db_client, db_session, scenario)

    (message,) = fake_email.to(owner.email)
    assert "You cancelled a booking" in message.text
    assert scenario.client.name in message.text
    assert scenario.client.email not in message.text + message.html


async def test_cancelling_an_already_cancelled_booking_is_a_200_that_changes_nothing(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    await owner_cancel(db_client, db_session, scenario)
    booking, payment = await reload(db_session, scenario)
    before = (row_dict(booking), row_dict(payment))
    sent = len(fake_email.sent)

    again = await owner_cancel(db_client, db_session, scenario)

    assert again.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert (row_dict(booking), row_dict(payment)) == before
    assert len(fake_email.sent) == sent == 2


async def test_a_double_click_sends_one_set_of_emails(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    first = await owner_cancel(db_client, db_session, scenario)
    second = await owner_cancel(db_client, db_session, scenario)

    assert first.status_code == second.status_code == 200
    assert len(fake_email.sent) == 2


async def test_parallel_cancels_send_one_set_of_emails_and_refund_once(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    fake_paystack: FakePaystack,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    headers = await owner_headers(db_session, scenario)

    responses = await asyncio.gather(
        *(cancel_as(db_client, scenario, headers) for _ in range(3))
    )

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(fake_email.sent) == 2
    booking, payment = await reload(db_session, scenario)
    assert booking.cancellation_email_sent_at is not None
    assert booking.owner_cancellation_email_sent_at is not None
    assert payment.status == PaymentStatus.REFUNDED
    assert len(fake_paystack.refund_calls) == 1  # Paystack was asked exactly once


async def test_a_crashing_email_sender_does_not_break_the_owner_cancel(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    class Exploding:
        async def send(self, message: EmailMessage) -> None:
            raise RuntimeError("boom")

    app.dependency_overrides[get_email_sender] = lambda: Exploding()
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 200
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CANCELLED
    assert payment.status == PaymentStatus.REFUNDED
    assert booking.cancellation_email_sent_at is None  # released, can be sent later


# --- nothing is sent for a cancelled booking ---


async def test_confirmation_emails_are_skipped_for_a_cancelled_booking(
    db_client: AsyncClient,
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    await owner_cancel(db_client, db_session, scenario)
    fake_email.sent.clear()

    await notify_booking_confirmed(session_maker, fake_email, scenario.booking.id)

    assert fake_email.sent == []
    booking, _ = await reload(db_session, scenario)
    assert booking.owner_confirmation_email_sent_at is None
    assert booking.confirmation_email_sent_at is None


async def test_reminders_skip_a_booking_the_owner_cancelled(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    cancelled = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))
    control = await confirmed(db_session, now=NOON, starts_in=timedelta(hours=24))
    owner = await owner_of(db_session, cancelled)
    await cancel_booking_with_outcome(db_session, cancelled.booking.id, owner, NOON)

    counts = await run_due(session_maker, fake_email, NOON)

    # The control is due and is reminded; the cancelled booking is not.
    assert counts.reminder_24h == 1
    assert fake_email.to(cancelled.client.email) == []
    assert len(fake_email.to(control.client.email)) == 1


async def test_the_owner_summary_skips_a_booking_the_owner_cancelled(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    cancelled = await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))
    control = await confirmed(db_session, now=MORNING, starts_in=timedelta(hours=3))
    owner = await owner_of(db_session, cancelled)
    control_owner = await owner_of(db_session, control)
    await cancel_booking_with_outcome(db_session, cancelled.booking.id, owner, MORNING)

    counts = await run_due(session_maker, fake_email, MORNING)

    # The control's owner gets a summary; the owner who cancelled has nothing to hear.
    assert counts.owner_summaries == 1
    assert fake_email.to(owner.email) == []
    assert len(fake_email.to(control_owner.email)) == 1


# --- refusals on the route ---


async def test_cancel_without_a_token_is_401(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await db_client.post(f"/api/v1/bookings/{scenario.booking.id}/cancel")

    assert resp.status_code == 401


async def test_a_stylist_cannot_cancel_a_booking(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)
    headers = {"Authorization": f"Bearer {create_access_token(scenario.stylist.id)}"}

    resp = await cancel_as(db_client, scenario, headers)

    assert resp.status_code == 403
    booking, _ = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert fake_email.sent == []


async def test_the_owner_cancelling_an_unknown_booking_is_404(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    scenario = await make_scenario(db_session, **PAID_AND_CONFIRMED)

    resp = await db_client.post(
        f"/api/v1/bookings/{uuid.uuid4()}/cancel",
        headers=await owner_headers(db_session, scenario),
    )

    assert resp.status_code == 404


async def test_the_owner_cannot_cancel_a_booking_that_has_started(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session, starts_in=-timedelta(minutes=5), **PAID_AND_CONFIRMED
    )

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 409
    booking, payment = await reload(db_session, scenario)
    assert booking.status == BookingStatus.CONFIRMED
    assert payment.status == PaymentStatus.PAID
    assert fake_email.sent == []


async def test_the_owner_cannot_cancel_a_completed_booking(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await make_scenario(
        db_session,
        booking_status=BookingStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
    )

    resp = await owner_cancel(db_client, db_session, scenario)

    assert resp.status_code == 409
    booking, _ = await reload(db_session, scenario)
    assert booking.status == BookingStatus.COMPLETED
    assert fake_email.sent == []
