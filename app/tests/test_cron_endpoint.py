"""POST /api/v1/internal/reminders/run: the secret header, a fast 202, and safe repeats."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings
from app.models.booking import BookingStatus
from app.models.payment import PaymentStatus
from app.tests.email_fakes import FakeEmailSender
from app.tests.payment_helpers import Scenario, make_scenario

URL = "/api/v1/internal/reminders/run"
SECRET = "cron-test-secret-0123456789-abcdefghij"  # a throwaway, test-only value


@pytest.fixture(autouse=True)
def cron_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CRON_SECRET", SecretStr(SECRET))


async def due_booking(db_session: AsyncSession) -> Scenario:
    """Confirmed and paid long ago, starting in 23h59m: its 24h reminder is due now."""
    now = datetime.now(UTC)
    scenario = await make_scenario(
        db_session,
        now=now,
        starts_in=timedelta(hours=23, minutes=59),
        booking_status=BookingStatus.CONFIRMED,
        payment_status=PaymentStatus.PAID,
    )
    assert scenario.payment is not None
    scenario.payment.paid_at = now - timedelta(days=3)
    await db_session.commit()
    return scenario


async def test_the_right_secret_is_accepted_and_the_reminders_go_out(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await due_booking(db_session)

    resp = await db_client.post(URL, headers={"X-Cron-Secret": SECRET})

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted"}
    assert len(fake_email.to(scenario.client.email)) == 1


async def test_calling_it_again_or_at_once_sends_each_reminder_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await due_booking(db_session)
    headers = {"X-Cron-Secret": SECRET}

    responses = await asyncio.gather(
        *(db_client.post(URL, headers=headers) for _ in range(3))
    )
    await db_client.post(URL, headers=headers)

    assert [r.status_code for r in responses] == [202, 202, 202]
    assert len(fake_email.to(scenario.client.email)) == 1


async def test_no_header_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    await due_booking(db_session)

    resp = await db_client.post(URL)

    assert resp.status_code == 401
    assert fake_email.sent == []


async def test_a_wrong_secret_is_rejected(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    await due_booking(db_session)

    resp = await db_client.post(URL, headers={"X-Cron-Secret": SECRET + "x"})

    assert resp.status_code == 401
    assert fake_email.sent == []


async def test_a_signed_in_users_token_does_not_open_it(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    scenario = await due_booking(db_session)

    resp = await db_client.post(URL, headers=scenario.headers)

    assert resp.status_code == 401
    assert fake_email.sent == []


@pytest.mark.parametrize("sent_header", [None, "", "anything"])
async def test_with_no_secret_configured_it_is_off_whatever_is_sent(
    db_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    sent_header: str | None,
) -> None:
    monkeypatch.setattr(settings, "CRON_SECRET", SecretStr(""))
    headers = {} if sent_header is None else {"X-Cron-Secret": sent_header}

    resp = await db_client.post(URL, headers=headers)

    assert resp.status_code == 503


async def test_a_crash_in_the_work_does_not_break_the_call_or_the_app(
    db_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.email.reminders.run_due", boom)

    resp = await db_client.post(URL, headers={"X-Cron-Secret": SECRET})

    assert resp.status_code == 202
    assert (await db_client.get("/health")).status_code == 200


async def test_it_is_not_listed_in_the_public_docs(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")

    assert URL not in resp.json()["paths"]


@pytest.mark.parametrize("environment", ["production", "local"])
def test_a_secret_that_is_set_but_too_short_is_refused(environment: str) -> None:
    with pytest.raises(ValidationError, match="only 5 characters.*at least 32"):
        make_settings(environment, "short")


@pytest.mark.parametrize("environment", ["production", "local"])
@pytest.mark.parametrize("secret", ["", "   ", "y" * 32, "y" * 64])
def test_an_empty_or_long_enough_secret_is_accepted(
    environment: str, secret: str
) -> None:
    assert make_settings(environment, secret)


def test_the_error_never_contains_the_secret() -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings("local", "hunter2")

    assert "hunter2" not in str(caught.value)


def make_settings(environment: str, secret: str) -> Settings:
    return Settings(
        ENVIRONMENT=environment,
        SECRET_KEY="x" * 40,
        PAYSTACK_SECRET_KEY="sk_test_x",
        CRON_SECRET=SecretStr(secret),
        _env_file=None,
    )
