"""Thin Paystack HTTP client. Test mode only: the secret key decides, nothing here does.

Every call has explicit timeouts and is made with no database transaction open, so a
slow or failing Paystack can never hold a row lock.
"""

from dataclasses import dataclass

import httpx

from app.core.config import settings

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PaystackError(Exception):
    """Paystack was unreachable or refused the request. Safe to retry unless noted."""


@dataclass(frozen=True)
class InitializedTransaction:
    authorization_url: str
    access_code: str
    reference: str


class PaystackClient:
    def __init__(
        self, secret_key: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._secret_key = secret_key
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=PAYSTACK_BASE_URL,
            timeout=PAYSTACK_TIMEOUT,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._secret_key}"},
        )

    async def initialize_transaction(
        self, *, email: str, amount: int, currency: str, reference: str
    ) -> InitializedTransaction:
        """Start a transaction. `amount` is in kobo and must come from our database."""
        payload = {
            "email": email,
            "amount": amount,
            "currency": currency,
            "reference": reference,
        }
        try:
            async with self._http() as http:
                response = await http.post("/transaction/initialize", json=payload)
            response.raise_for_status()
            body = response.json()
            data = body["data"]
            if body.get("status") is not True:
                raise PaystackError("Paystack rejected the transaction")
            if data["reference"] != reference:
                raise PaystackError("Paystack answered for a different reference")
            return InitializedTransaction(
                authorization_url=data["authorization_url"],
                access_code=data["access_code"],
                reference=data["reference"],
            )
        except PaystackError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Never include the response body or headers: they can echo request data.
            raise PaystackError(f"initialize failed: {type(exc).__name__}") from None


def get_paystack_client() -> PaystackClient:
    return PaystackClient(settings.PAYSTACK_SECRET_KEY)
