"""Brevo transactional email (https://developers.brevo.com/reference/sendtransacemail)."""

import httpx

from app.core.config import settings
from app.services.email.base import EmailMessage, EmailSender, EmailSendError

BREVO_BASE_URL = "https://api.brevo.com/v3"
BREVO_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_FROM_NAME = "SalonBook"


# 4xx statuses that say something about our account, not about this message: a revoked key
# (401), an exhausted quota (402), a blocked or unverified account (403), and "try again
# later" (408, 429). They pass once the account is fixed, so they are temporary like a 5xx
# and must not burn every due reminder.
TEMPORARY_CLIENT_ERRORS = frozenset({401, 402, 403, 408, 429})


def _is_permanent(status: int) -> bool:
    """A 4xx means Brevo refused this message (bad address, sender, payload) for good."""
    return 400 <= status < 500 and status not in TEMPORARY_CLIENT_ERRORS


class BrevoSender:
    def __init__(
        self,
        api_key: str,
        from_address: str,
        from_name: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._from_address = from_address
        self._from_name = from_name
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BREVO_BASE_URL,
            timeout=BREVO_TIMEOUT,
            transport=self._transport,
            headers={"api-key": self._api_key, "accept": "application/json"},
        )

    async def send(self, message: EmailMessage) -> None:
        payload = {
            "sender": {"email": self._from_address, "name": self._from_name},
            "to": [{"email": message.to_address, "name": message.to_name}],
            "subject": message.subject,
            "textContent": message.text,
            "htmlContent": message.html,
        }
        try:
            async with self._http() as http:
                response = await http.post("/smtp/email", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # The status code only: the body and the request can echo the address.
            status = exc.response.status_code
            raise EmailSendError(
                f"brevo answered {status}", permanent=_is_permanent(status)
            ) from None
        except httpx.HTTPError as exc:
            raise EmailSendError(
                f"brevo request failed: {type(exc).__name__}"
            ) from None


def get_email_sender() -> EmailSender | None:
    """The configured sender, or None when email is not set up (callers skip sending)."""
    api_key = settings.BREVO_API_KEY.get_secret_value().strip()
    from_address = settings.EMAIL_FROM_ADDRESS.strip()
    if not api_key or not from_address:
        return None
    return BrevoSender(
        api_key, from_address, settings.EMAIL_FROM_NAME.strip() or DEFAULT_FROM_NAME
    )
