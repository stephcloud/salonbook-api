from datetime import datetime, timedelta


def is_refundable(starts_at: datetime, now: datetime, cancellation_hours: int) -> bool:
    """True if a cancellation at `now` earns a full deposit refund.

    Pure: no DB, no clock. The refund applies only when the booking is cancelled
    *more than* `cancellation_hours` before it starts, so exactly at the limit (and
    anything later, including after the start) gets no refund. Both datetimes must
    be timezone-aware.
    """
    return starts_at - now > timedelta(hours=cancellation_hours)
