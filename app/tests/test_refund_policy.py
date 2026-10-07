from datetime import UTC, datetime, timedelta

from app.services.refund_policy import is_refundable

STARTS_AT = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)


def test_cancelling_well_before_the_limit_is_refundable() -> None:
    now = STARTS_AT - timedelta(days=5)
    assert is_refundable(STARTS_AT, now, 24) is True


def test_cancelling_well_after_the_limit_is_not_refundable() -> None:
    now = STARTS_AT - timedelta(hours=2)
    assert is_refundable(STARTS_AT, now, 24) is False


def test_cancelling_exactly_at_the_limit_is_not_refundable() -> None:
    # "More than" cancellation_hours before start: the boundary itself gets no refund.
    now = STARTS_AT - timedelta(hours=24)
    assert is_refundable(STARTS_AT, now, 24) is False


def test_one_second_before_the_limit_is_refundable() -> None:
    now = STARTS_AT - timedelta(hours=24, seconds=1)
    assert is_refundable(STARTS_AT, now, 24) is True


def test_one_second_inside_the_limit_is_not_refundable() -> None:
    now = STARTS_AT - timedelta(hours=24) + timedelta(seconds=1)
    assert is_refundable(STARTS_AT, now, 24) is False


def test_cancelling_after_the_start_is_not_refundable() -> None:
    now = STARTS_AT + timedelta(minutes=5)
    assert is_refundable(STARTS_AT, now, 24) is False


def test_limit_is_read_from_the_salon_setting() -> None:
    now = STARTS_AT - timedelta(hours=10)
    assert is_refundable(STARTS_AT, now, 8) is True  # 10h out, 8h policy
    assert is_refundable(STARTS_AT, now, 12) is False  # 10h out, 12h policy
    assert is_refundable(STARTS_AT, now, 48) is False


def test_zero_hour_policy_refunds_anything_before_the_start() -> None:
    assert is_refundable(STARTS_AT, STARTS_AT - timedelta(seconds=1), 0) is True
    assert is_refundable(STARTS_AT, STARTS_AT, 0) is False
