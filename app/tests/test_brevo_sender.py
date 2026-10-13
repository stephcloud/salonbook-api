"""BrevoSender against httpx.MockTransport: request shape and error mapping. No network."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.core.config import settings
from app.services.email.base import EmailMessage, EmailSendError
from app.services.email.brevo import BrevoSender, get_email_sender

API_KEY = "xkeysib-not-a-real-key"
MESSAGE = EmailMessage(
    to_address="chi@example.com",
    to_name="Chi",
    subject="Hello",
    text="plain",
    html="<p>html</p>",
)


def sender_with(handler: Callable[[httpx.Request], httpx.Response]) -> BrevoSender:
    return BrevoSender(
        API_KEY,
        "no-reply@salonbook.test",
        "SalonBook",
        transport=httpx.MockTransport(handler),
    )


async def test_send_posts_the_message_with_the_api_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"messageId": "<abc>"})

    await sender_with(handler).send(MESSAGE)

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == API_KEY
    assert json.loads(request.content) == {
        "sender": {"email": "no-reply@salonbook.test", "name": "SalonBook"},
        "to": [{"email": "chi@example.com", "name": "Chi"}],
        "subject": "Hello",
        "textContent": "plain",
        "htmlContent": "<p>html</p>",
    }


@pytest.mark.parametrize("status_code", [400, 401, 429, 500])
async def test_an_error_answer_becomes_a_send_error_that_leaks_nothing(
    status_code: int,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A provider that echoes the recipient and the key back in its error body.
        return httpx.Response(
            status_code, json={"message": f"bad chi@example.com {API_KEY}"}
        )

    with pytest.raises(EmailSendError) as caught:
        await sender_with(handler).send(MESSAGE)

    assert str(status_code) in str(caught.value)
    assert "chi@example.com" not in str(caught.value)
    assert API_KEY not in str(caught.value)


async def test_a_timeout_becomes_a_send_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(EmailSendError) as caught:
        await sender_with(handler).send(MESSAGE)

    assert "ReadTimeout" in str(caught.value)


# --- configuration ---


@pytest.mark.parametrize(
    ("api_key", "from_address"),
    [("", ""), ("", "no-reply@salonbook.test"), (API_KEY, ""), ("  ", "  ")],
)
def test_missing_config_gives_no_sender(
    monkeypatch: pytest.MonkeyPatch, api_key: str, from_address: str
) -> None:
    monkeypatch.setattr(settings, "BREVO_API_KEY", api_key)
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", from_address)

    assert get_email_sender() is None


def test_full_config_gives_a_brevo_sender_with_the_default_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "BREVO_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "EMAIL_FROM_ADDRESS", "no-reply@salonbook.test")
    monkeypatch.setattr(settings, "EMAIL_FROM_NAME", "")

    sender = get_email_sender()

    assert isinstance(sender, BrevoSender)
    assert sender._from_name == "SalonBook"
