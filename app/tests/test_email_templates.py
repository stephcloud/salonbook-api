"""Booking email wording: pure functions, no database."""

from datetime import UTC, datetime

import pytest

from app.models.payment import PaymentStatus
from app.services.email import templates
from app.services.email.templates import (
    BookingEmailContext,
    RefundOutcome,
    format_naira,
    format_when,
    refund_outcome,
)

# 23:30 UTC on Wed 14 Oct is 00:30 on Thu 15 Oct in Lagos (UTC+1).
STARTS_AT = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)


def ctx(**overrides: object) -> BookingEmailContext:
    fields: dict[str, object] = {
        "salon_name": "Glow Studio",
        "salon_address": "1 Marina, Lagos",
        "salon_phone": "08012345678",
        "service_name": "Braids",
        "stylist_name": "Ada",
        "client_name": "Chi",
        "client_email": "chi@example.com",
        "owner_name": "Bola",
        "owner_email": "bola@example.com",
        "starts_at": STARTS_AT,
        "cancellation_hours": 24,
        "deposit_amount": 500000,
        "refund_due": None,
        "payment_status": None,
        "was_confirmed": False,
        "cancelled_by_owner": False,
    }
    fields.update(overrides)
    return BookingEmailContext(**fields)  # type: ignore[arg-type]


# --- formatting ---


def test_times_are_shown_in_lagos_not_utc() -> None:
    assert format_when(STARTS_AT) == "Thursday 15 Oct 2026, 00:30"


@pytest.mark.parametrize(
    ("kobo", "shown"),
    [(500000, "₦5,000"), (123456, "₦1,234.56"), (100, "₦1"), (150000000, "₦1,500,000")],
)
def test_kobo_are_shown_as_naira(kobo: int, shown: str) -> None:
    assert format_naira(kobo) == shown


# --- confirmation emails ---


def test_client_confirmation_has_the_booking_and_the_deposit() -> None:
    msg = templates.client_confirmation(ctx())

    assert msg.to_address == "chi@example.com"
    assert "Glow Studio" in msg.subject
    for part in ("Braids", "Ada", "Thursday 15 Oct 2026, 00:30", "₦5,000", "Marina"):
        assert part in msg.text
        assert part in msg.html


def test_owner_new_booking_names_the_client_but_never_their_email() -> None:
    msg = templates.owner_new_booking(ctx())

    assert msg.to_address == "bola@example.com"
    assert "Chi" in msg.text
    assert "chi@example.com" not in msg.text
    assert "chi@example.com" not in msg.html


def test_names_are_html_escaped() -> None:
    msg = templates.client_confirmation(ctx(salon_name="<b>Glow</b> & Co"))

    assert "<b>Glow</b>" not in msg.html
    assert "&lt;b&gt;Glow&lt;/b&gt; &amp; Co" in msg.html


# --- what the cancel flow did with the deposit ---


@pytest.mark.parametrize(
    ("facts", "outcome"),
    [
        # a pending booking paid nothing: never a money sentence
        ({"was_confirmed": False, "refund_due": False}, RefundOutcome.NONE),
        (
            {"was_confirmed": False, "refund_due": True, "payment_status": "refunded"},
            RefundOutcome.NONE,
        ),
        # confirmed, refund_due true, refund queued or settled
        (
            {
                "was_confirmed": True,
                "refund_due": True,
                "payment_status": "refund_pending",
            },
            RefundOutcome.REFUND_PENDING,
        ),
        (
            {"was_confirmed": True, "refund_due": True, "payment_status": "refunded"},
            RefundOutcome.REFUNDED,
        ),
        # refund_due true but no payment to refund: do not claim a refund
        ({"was_confirmed": True, "refund_due": True}, RefundOutcome.NONE),
        # refund_due true but the payment was never moved: do not claim a refund
        (
            {"was_confirmed": True, "refund_due": True, "payment_status": "paid"},
            RefundOutcome.NONE,
        ),
        # late client cancel: the salon keeps the paid deposit
        (
            {"was_confirmed": True, "refund_due": False, "payment_status": "paid"},
            RefundOutcome.KEPT,
        ),
        # an owner's cancel is always refund_due, so false + owner is not a known outcome
        (
            {
                "was_confirmed": True,
                "refund_due": False,
                "payment_status": "paid",
                "cancelled_by_owner": True,
            },
            RefundOutcome.NONE,
        ),
        # no decision recorded
        (
            {"was_confirmed": True, "refund_due": None, "payment_status": "paid"},
            RefundOutcome.NONE,
        ),
    ],
)
def test_refund_outcome_only_states_what_the_cancel_flow_produced(
    facts: dict[str, object], outcome: RefundOutcome
) -> None:
    facts = {
        k: PaymentStatus(v) if k == "payment_status" else v for k, v in facts.items()
    }
    assert refund_outcome(ctx(**facts)) == outcome


def test_client_is_told_a_queued_refund_is_being_refunded() -> None:
    msg = templates.client_cancellation(
        ctx(
            was_confirmed=True,
            refund_due=True,
            payment_status=PaymentStatus.REFUND_PENDING,
        )
    )

    assert "You cancelled this booking" in msg.text
    assert "Your deposit of ₦5,000 is being refunded" in msg.text
    assert "not refunded" not in msg.text


def test_client_is_told_a_settled_refund_has_been_refunded() -> None:
    msg = templates.client_cancellation(
        ctx(was_confirmed=True, refund_due=True, payment_status=PaymentStatus.REFUNDED)
    )

    assert "Your deposit of ₦5,000 has been refunded" in msg.text


def test_client_is_told_a_late_cancel_keeps_the_deposit_with_the_salons_window() -> (
    None
):
    msg = templates.client_cancellation(
        ctx(
            was_confirmed=True,
            refund_due=False,
            payment_status=PaymentStatus.PAID,
            cancellation_hours=48,
        )
    )

    assert "within 48 hours" in msg.text
    assert "your deposit of ₦5,000 is not refunded" in msg.text
    assert "is being refunded" not in msg.text


def test_owner_cancellation_says_the_salon_cancelled_and_the_deposit_goes_back() -> (
    None
):
    msg = templates.client_cancellation(
        ctx(
            was_confirmed=True,
            refund_due=True,
            payment_status=PaymentStatus.REFUND_PENDING,
            cancelled_by_owner=True,
        )
    )

    assert "The salon cancelled this booking" in msg.text
    assert "is being refunded" in msg.text


def test_a_cancelled_pending_booking_says_nothing_about_money() -> None:
    msg = templates.client_cancellation(ctx(was_confirmed=False, refund_due=False))

    assert "refund" not in msg.text.lower()
    assert "deposit" not in msg.text.lower()


def test_owner_cancellation_wording_follows_the_same_outcome() -> None:
    refunded = templates.owner_cancellation(
        ctx(
            was_confirmed=True,
            refund_due=True,
            payment_status=PaymentStatus.REFUND_PENDING,
        )
    )
    kept = templates.owner_cancellation(
        ctx(was_confirmed=True, refund_due=False, payment_status=PaymentStatus.PAID)
    )
    by_owner = templates.owner_cancellation(
        ctx(
            was_confirmed=True,
            refund_due=True,
            payment_status=PaymentStatus.REFUND_PENDING,
            cancelled_by_owner=True,
        )
    )

    assert refunded.to_address == "bola@example.com"
    assert "Chi cancelled a booking" in refunded.text
    assert "The deposit of ₦5,000 is being refunded to the client" in refunded.text
    assert "is kept" in kept.text
    assert "You cancelled a booking" in by_owner.text
