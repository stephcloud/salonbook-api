"""Plain booking emails: pure functions from a context to an EmailMessage.

No database, no clock, no network. Times are shown in Africa/Lagos (the API stores UTC).

Refund wording comes only from what the cancel flow actually recorded
(`services/bookings._cancel_booking`): `refund_due`, who cancelled, and the state of the
booking's real payment row. Cancellation emails are only sent for a confirmed (paid)
booking; cancelling an unpaid pending booking sends nothing. The code makes only these
outcomes, so these are the only ones the emails can state:
  - a confirmed booking with `refund_due` true and a payment queued for refund (or already
    refunded): the deposit is being (or has been) refunded;
  - a confirmed booking cancelled by the client with `refund_due` false and its payment
    still `paid`: the salon keeps the deposit under its cancellation window;
  - anything else (for example `refund_due` true but no payment to refund): say nothing
    about money rather than guess.
"""

import html
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.payment import PaymentStatus
from app.services.email.base import EmailMessage


def _lagos_tz() -> timezone | ZoneInfo:
    try:
        return ZoneInfo("Africa/Lagos")
    except ZoneInfoNotFoundError:  # no tz database on the host; Lagos has no DST
        return timezone(timedelta(hours=1))


LAGOS = _lagos_tz()


class RefundOutcome(StrEnum):
    NONE = "none"  # nothing to say about money
    REFUND_PENDING = "refund_pending"  # refund queued or in flight
    REFUNDED = "refunded"  # refund already settled
    KEPT = "kept"  # deposit stays with the salon (late client cancel)


@dataclass(frozen=True)
class BookingEmailContext:
    salon_name: str
    salon_address: str
    salon_phone: str
    service_name: str
    stylist_name: str
    client_name: str
    client_email: str
    owner_name: str
    owner_email: str
    starts_at: datetime
    cancellation_hours: int
    # Kobo, from the booking's real payment row; None if it has none.
    deposit_amount: int | None
    # Cancellation facts; unused by the confirmation emails.
    refund_due: bool | None = None
    payment_status: PaymentStatus | None = None
    cancelled_by_owner: bool = False

    def __post_init__(self) -> None:
        # Names, address and phone are free text typed by users. Collapse every run of
        # whitespace (newlines included) so one cannot add lines to a subject or body.
        for name in (
            "salon_name",
            "salon_address",
            "salon_phone",
            "service_name",
            "stylist_name",
            "client_name",
            "owner_name",
        ):
            object.__setattr__(self, name, " ".join(getattr(self, name).split()))


def refund_outcome(ctx: BookingEmailContext) -> RefundOutcome:
    """What the cancel flow did with the deposit. See the module docstring."""
    if ctx.payment_status is None:
        return RefundOutcome.NONE
    if ctx.refund_due is True:
        if ctx.payment_status == PaymentStatus.REFUND_PENDING:
            return RefundOutcome.REFUND_PENDING
        if ctx.payment_status == PaymentStatus.REFUNDED:
            return RefundOutcome.REFUNDED
        return RefundOutcome.NONE
    if (
        ctx.refund_due is False
        and not ctx.cancelled_by_owner  # an owner's cancel is always refund_due
        and ctx.payment_status == PaymentStatus.PAID
    ):
        return RefundOutcome.KEPT
    return RefundOutcome.NONE


def format_naira(kobo: int) -> str:
    naira, rest = divmod(kobo, 100)
    return f"₦{naira:,}" if rest == 0 else f"₦{naira:,}.{rest:02d}"


def format_when(starts_at: datetime) -> str:
    local = starts_at.astimezone(LAGOS)
    return f"{local:%A} {local.day} {local:%b %Y}, {local:%H:%M}"


def _deposit(ctx: BookingEmailContext) -> str:
    if ctx.deposit_amount is None:
        return "your deposit"
    return f"your deposit of {format_naira(ctx.deposit_amount)}"


def _render(
    ctx: BookingEmailContext,
    *,
    to_address: str,
    to_name: str,
    subject: str,
    intro: str,
    details: list[tuple[str, str]],
    notes: list[str],
) -> EmailMessage:
    greeting = f"Hi {to_name},"
    text_lines = [greeting, "", intro, ""]
    text_lines += [f"{label}: {value}" for label, value in details]
    for note in notes:
        text_lines += ["", note]
    text_lines += ["", "SalonBook"]

    rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#555">{html.escape(label)}</td>'
        f'<td style="padding:2px 0"><strong>{html.escape(value)}</strong></td></tr>'
        for label, value in details
    )
    paragraphs = "".join(f"<p>{html.escape(note)}</p>" for note in notes)
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.5;color:#222;max-width:520px">'
        f"<p>{html.escape(greeting)}</p><p>{html.escape(intro)}</p>"
        f'<table role="presentation" style="border-collapse:collapse">{rows}</table>'
        f"{paragraphs}<p>SalonBook</p></div>"
    )
    return EmailMessage(
        to_address=to_address,
        to_name=to_name,
        subject=subject,
        text="\n".join(text_lines),
        html=html_body,
    )


def _booking_details(ctx: BookingEmailContext) -> list[tuple[str, str]]:
    return [
        ("Salon", ctx.salon_name),
        ("Service", ctx.service_name),
        ("Stylist", ctx.stylist_name),
        ("When", format_when(ctx.starts_at)),
    ]


def client_confirmation(ctx: BookingEmailContext) -> EmailMessage:
    details = _booking_details(ctx)
    if ctx.deposit_amount is not None:
        details.append(("Deposit paid", format_naira(ctx.deposit_amount)))
    details += [("Address", ctx.salon_address), ("Phone", ctx.salon_phone)]
    return _render(
        ctx,
        to_address=ctx.client_email,
        to_name=ctx.client_name,
        subject=f"Your booking at {ctx.salon_name} is confirmed",
        intro="Your deposit was received and your booking is confirmed.",
        details=details,
        notes=[],
    )


def owner_new_booking(ctx: BookingEmailContext) -> EmailMessage:
    details = [("Client", ctx.client_name), *_booking_details(ctx)[1:]]
    if ctx.deposit_amount is not None:
        details.append(("Deposit paid", format_naira(ctx.deposit_amount)))
    return _render(
        ctx,
        to_address=ctx.owner_email,
        to_name=ctx.owner_name,
        subject=f"New booking at {ctx.salon_name}",
        intro=f"{ctx.client_name} has booked and paid the deposit.",
        details=details,
        notes=[],
    )


def client_cancellation(ctx: BookingEmailContext) -> EmailMessage:
    who = "The salon cancelled" if ctx.cancelled_by_owner else "You cancelled"
    outcome = refund_outcome(ctx)
    notes: list[str] = []
    if outcome == RefundOutcome.REFUND_PENDING:
        notes.append(
            f"{_deposit(ctx).capitalize()} is being refunded to your original "
            "payment method. Your bank may take some time to show it."
        )
    elif outcome == RefundOutcome.REFUNDED:
        notes.append(
            f"{_deposit(ctx).capitalize()} has been refunded to your original "
            "payment method. Your bank may take some time to show it."
        )
    elif outcome == RefundOutcome.KEPT:
        notes.append(
            f"This was cancelled within {ctx.cancellation_hours} hours of the start "
            f"time, so {_deposit(ctx)} is not refunded, as set out in the salon's "
            "cancellation policy."
        )
    return _render(
        ctx,
        to_address=ctx.client_email,
        to_name=ctx.client_name,
        subject=f"Your booking at {ctx.salon_name} was cancelled",
        intro=f"{who} this booking.",
        details=_booking_details(ctx),
        notes=notes,
    )


def owner_cancellation(ctx: BookingEmailContext) -> EmailMessage:
    who = "You cancelled" if ctx.cancelled_by_owner else f"{ctx.client_name} cancelled"
    outcome = refund_outcome(ctx)
    deposit = (
        "The deposit"
        if ctx.deposit_amount is None
        else f"The deposit of {format_naira(ctx.deposit_amount)}"
    )
    notes: list[str] = []
    if outcome == RefundOutcome.REFUND_PENDING:
        notes.append(f"{deposit} is being refunded to the client.")
    elif outcome == RefundOutcome.REFUNDED:
        notes.append(f"{deposit} has been refunded to the client.")
    elif outcome == RefundOutcome.KEPT:
        notes.append(
            f"{deposit} is kept: the booking was cancelled within "
            f"{ctx.cancellation_hours} hours of the start time."
        )
    return _render(
        ctx,
        to_address=ctx.owner_email,
        to_name=ctx.owner_name,
        subject=f"Booking cancelled at {ctx.salon_name}",
        intro=f"{who} a booking. The slot is free again.",
        details=[("Client", ctx.client_name), *_booking_details(ctx)[1:]],
        notes=notes,
    )


def _reminder(ctx: BookingEmailContext, *, when: str) -> EmailMessage:
    return _render(
        ctx,
        to_address=ctx.client_email,
        to_name=ctx.client_name,
        subject=f"Reminder: your appointment at {ctx.salon_name} is {when}",
        intro=f"This is a reminder that your appointment is {when}.",
        details=[
            *_booking_details(ctx),
            ("Address", ctx.salon_address),
            ("Phone", ctx.salon_phone),
        ],
        notes=[],
    )


def client_reminder_24h(ctx: BookingEmailContext) -> EmailMessage:
    return _reminder(ctx, when="in about 24 hours")


def client_reminder_2h(ctx: BookingEmailContext) -> EmailMessage:
    return _reminder(ctx, when="in about 2 hours")


@dataclass(frozen=True)
class SummaryEntry:
    starts_at: datetime
    salon_name: str
    service_name: str
    stylist_name: str
    client_name: str  # the name only: the owner's summary never carries a client email

    def __post_init__(self) -> None:
        for name in ("salon_name", "service_name", "stylist_name", "client_name"):
            object.__setattr__(self, name, " ".join(getattr(self, name).split()))


@dataclass(frozen=True)
class OwnerSummaryContext:
    owner_name: str
    owner_email: str
    day: date  # the Lagos day the summary is for
    entries: tuple[SummaryEntry, ...]  # in start order
    more: int = 0  # bookings beyond `entries`, not listed

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_name", " ".join(self.owner_name.split()))


def owner_daily_summary(ctx: OwnerSummaryContext) -> EmailMessage:
    count = len(ctx.entries) + ctx.more
    plural = "booking" if count == 1 else "bookings"
    day = f"{ctx.day:%A} {ctx.day.day} {ctx.day:%b %Y}"
    greeting = f"Hi {ctx.owner_name},"
    intro = f"You have {count} confirmed {plural} still to come today, {day}."

    def line(e: SummaryEntry) -> str:
        time = e.starts_at.astimezone(LAGOS).strftime("%H:%M")
        return (
            f"{time} - {e.service_name} with {e.stylist_name} "
            f"for {e.client_name} ({e.salon_name})"
        )

    text_lines = [greeting, "", intro, ""] + [line(e) for e in ctx.entries]
    if ctx.more:
        text_lines.append(f"...and {ctx.more} more.")
    text_lines += ["", "SalonBook"]

    cell = "padding:2px 12px 2px 0"
    rows = "".join(
        f'<tr><td style="{cell}"><strong>'
        f"{e.starts_at.astimezone(LAGOS):%H:%M}</strong></td>"
        f'<td style="{cell}">{html.escape(e.service_name)}</td>'
        f'<td style="{cell}">{html.escape(e.stylist_name)}</td>'
        f'<td style="{cell}">{html.escape(e.client_name)}</td>'
        f'<td style="{cell};color:#555">{html.escape(e.salon_name)}</td></tr>'
        for e in ctx.entries
    )
    more = f"<p>...and {ctx.more} more.</p>" if ctx.more else ""
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.5;color:#222;max-width:640px">'
        f"<p>{html.escape(greeting)}</p><p>{html.escape(intro)}</p>"
        f'<table role="presentation" style="border-collapse:collapse">{rows}</table>'
        f"{more}<p>SalonBook</p></div>"
    )
    return EmailMessage(
        to_address=ctx.owner_email,
        to_name=ctx.owner_name,
        subject=f"Your bookings today: {count} on {ctx.day.day} {ctx.day:%b}",
        text="\n".join(text_lines),
        html=html_body,
    )


@dataclass(frozen=True)
class SalonStatusEmailContext:
    owner_name: str
    owner_email: str
    salon_name: str

    def __post_init__(self) -> None:
        # Free text typed by users: no newlines in a subject or body.
        for name in ("owner_name", "salon_name"):
            object.__setattr__(self, name, " ".join(getattr(self, name).split()))


def _salon_status_email(
    ctx: SalonStatusEmailContext, *, subject: str, notes: list[str]
) -> EmailMessage:
    greeting = f"Hi {ctx.owner_name},"
    text = "\n".join([greeting, "", *notes, "", "SalonBook"])
    paragraphs = "".join(f"<p>{html.escape(n)}</p>" for n in notes)
    html_body = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.5;color:#222;max-width:520px">'
        f"<p>{html.escape(greeting)}</p>{paragraphs}<p>SalonBook</p></div>"
    )
    return EmailMessage(
        to_address=ctx.owner_email,
        to_name=ctx.owner_name,
        subject=subject,
        text=text,
        html=html_body,
    )


def owner_salon_approved(ctx: SalonStatusEmailContext) -> EmailMessage:
    return _salon_status_email(
        ctx,
        subject=f"{ctx.salon_name} is approved",
        notes=[
            (
                f"Good news: {ctx.salon_name} has been approved. It now appears on "
                "SalonBook and clients can book it."
            )
        ],
    )


def owner_salon_suspended(ctx: SalonStatusEmailContext) -> EmailMessage:
    # The admin's reason stays in the audit log; it is never put in an email.
    return _salon_status_email(
        ctx,
        subject=f"{ctx.salon_name} has been suspended",
        notes=[
            (
                f"{ctx.salon_name} has been suspended. It no longer appears on "
                "SalonBook and clients cannot make new bookings."
            ),
            (
                "Bookings that already exist are not affected. Please contact "
                "SalonBook support if you have questions."
            ),
        ],
    )
