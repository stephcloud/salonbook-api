"""Test doubles and signing helpers for Paystack. No network, no database."""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.paystack import (
    InitializedTransaction,
    PaystackClient,
    PaystackError,
)

# A non-empty secret for every test. Only the empty-secret test overrides it.
TEST_PAYSTACK_SECRET = "sk_test_not_a_real_key"


def sign(raw_body: bytes, secret: str = TEST_PAYSTACK_SECRET) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()


def charge_success_body(reference: str, amount: int, currency: str = "NGN") -> bytes:
    return json.dumps(
        {
            "event": "charge.success",
            "data": {
                "reference": reference,
                "amount": amount,
                "currency": currency,
                "status": "success",
            },
        }
    ).encode()


def event_body(event: str, data: dict[str, Any]) -> bytes:
    return json.dumps({"event": event, "data": data}).encode()


def webhook_headers(raw_body: bytes) -> dict[str, str]:
    return {"x-paystack-signature": sign(raw_body), "content-type": "application/json"}


class FakePaystack(PaystackClient):
    """Records calls instead of making them. Script a failure with `fail_initialize`."""

    def __init__(self) -> None:
        super().__init__("sk_test_fake")
        self.initialize_calls: list[dict[str, Any]] = []
        self.fail_initialize = False
        # Runs inside initialize_transaction, e.g. to inspect the database mid-call.
        self.on_initialize: Callable[[str], Awaitable[None]] | None = None

    async def initialize_transaction(
        self, *, email: str, amount: int, currency: str, reference: str
    ) -> InitializedTransaction:
        self.initialize_calls.append(
            {
                "email": email,
                "amount": amount,
                "currency": currency,
                "reference": reference,
            }
        )
        if self.on_initialize is not None:
            await self.on_initialize(reference)
        if self.fail_initialize:
            raise PaystackError("scripted failure")
        return InitializedTransaction(
            authorization_url=f"https://checkout.paystack.test/{reference}",
            access_code=f"ac_{reference}",
            reference=reference,
        )
