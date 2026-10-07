"""Thin Paystack HTTP client. Test mode only: the secret key decides, nothing here does.

Every call has explicit timeouts and is made with no database transaction open, so a
slow or failing Paystack can never hold a row lock.
"""

from dataclasses import dataclass

import httpx

from app.core.config import settings

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Statuses that mean Paystack has taken a refund on. `failed` is deliberately absent:
# a failed refund is one we still owe.
ACCEPTED_REFUND_STATUSES = frozenset({"pending", "processing", "processed"})
# From Paystack's refund error list: the transaction was already refunded in full.
FULLY_REVERSED_MESSAGE = "fully reversed"


class PaystackError(Exception):
    """Paystack was unreachable or refused the request. Safe to retry unless noted."""


@dataclass(frozen=True)
class InitializedTransaction:
    authorization_url: str
    access_code: str
    reference: str


@dataclass(frozen=True)
class RefundResult:
    """A refund Paystack has accepted (it may still be settling with the bank)."""

    refund_id: str | None
    status: str  # pending | processing | processed


def _says_fully_reversed(response: httpx.Response) -> bool:
    try:
        message = response.json().get("message", "")
    except (ValueError, AttributeError):
        return False
    return isinstance(message, str) and FULLY_REVERSED_MESSAGE in message.lower()


def _refund_record(item: object, reference: str) -> RefundResult | None:
    """The refund in `item` for transaction `reference`, or None if failed or not ours."""
    if not isinstance(item, dict):
        return None
    status = str(item.get("status", "")).lower()
    if status not in ACCEPTED_REFUND_STATUSES:
        return None
    transaction = item.get("transaction")
    seen = (
        transaction.get("reference") if isinstance(transaction, dict) else transaction
    )
    if isinstance(seen, str) and seen != reference:
        return None
    refund_id = item.get("id")
    return RefundResult(
        refund_id=None if refund_id is None else str(refund_id), status=status
    )


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

    async def create_refund(self, *, reference: str, amount: int) -> RefundResult:
        """Ask Paystack to refund `amount` kobo of the transaction `reference`.

        The docs don't promise idempotency, so a caller must never call this twice for
        one payment without checking `find_refund` first. A reply saying the transaction
        is already fully reversed counts as success. A 200 only means "queued": the
        refund can still fail later (refund.failed).
        """
        payload = {
            "transaction": reference,
            "amount": amount,
            "merchant_note": "SalonBook deposit refund",
        }
        try:
            async with self._http() as http:
                response = await http.post("/refund", json=payload)
        except httpx.HTTPError as exc:
            raise PaystackError(
                f"refund request failed: {type(exc).__name__}"
            ) from None
        if response.status_code >= 400:
            if _says_fully_reversed(response):
                return RefundResult(refund_id=None, status="processed")
            # Status only, never the body: e.g. a balance error needs a human, not a log.
            raise PaystackError(f"refund rejected: HTTP {response.status_code}")
        try:
            body = response.json()
            result = _refund_record(body["data"], reference)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise PaystackError("refund reply was malformed") from None
        if body.get("status") is not True or result is None:
            raise PaystackError("refund was not accepted")
        return result

    async def find_refund(self, *, reference: str) -> RefundResult | None:
        """An accepted refund already on file for this transaction, if any.

        Used before a retry: if an earlier attempt reached Paystack but its answer was
        lost, this finds it, so we never create a second refund.
        """
        try:
            async with self._http() as http:
                response = await http.get("/refund", params={"reference": reference})
            response.raise_for_status()
            for item in response.json()["data"]:
                result = _refund_record(item, reference)
                if result is not None:
                    return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise PaystackError("refund lookup failed") from None
        return None


def get_paystack_client() -> PaystackClient:
    return PaystackClient(settings.PAYSTACK_SECRET_KEY)
