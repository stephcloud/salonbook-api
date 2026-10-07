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
    RefundResult,
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


def refund_event_body(event: str, reference: str, status: str = "processed") -> bytes:
    return event_body(
        event,
        {
            "status": status,
            "transaction_reference": reference,
            "refund_reference": "rf_ref",
            "amount": 500000,
            "currency": "NGN",
        },
    )


def event_body(event: str, data: dict[str, Any]) -> bytes:
    return json.dumps({"event": event, "data": data}).encode()


def webhook_headers(raw_body: bytes) -> dict[str, str]:
    return {"x-paystack-signature": sign(raw_body), "content-type": "application/json"}


class FakePaystack(PaystackClient):
    """Records calls instead of making them, and can be scripted to misbehave.

    Initialize: set `fail_initialize`. Refunds: `refund_failures` makes the next N
    create_refund calls fail cleanly; `lose_next_refund_reply` makes the next one
    succeed at "Paystack" but raise to the caller (the answer got lost).
    """

    def __init__(self) -> None:
        super().__init__("sk_test_fake")
        self.initialize_calls: list[dict[str, Any]] = []
        self.fail_initialize = False
        # Runs inside initialize_transaction, e.g. to inspect the database mid-call.
        self.on_initialize: Callable[[str], Awaitable[None]] | None = None

        self.refund_calls: list[dict[str, Any]] = []
        self.find_calls: list[str] = []
        self.refund_failures = 0
        self.lose_next_refund_reply = False
        self.refund_status = "pending"
        # Runs inside create_refund, e.g. to inspect row locks mid-call.
        self.on_refund: Callable[[str], Awaitable[None]] | None = None
        # What "Paystack" holds, keyed by transaction reference.
        self.refunds: dict[str, RefundResult] = {}

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

    async def create_refund(self, *, reference: str, amount: int) -> RefundResult:
        self.refund_calls.append({"reference": reference, "amount": amount})
        if self.on_refund is not None:
            await self.on_refund(reference)
        if self.refund_failures > 0:
            self.refund_failures -= 1
            raise PaystackError("scripted refund failure")
        result = RefundResult(
            refund_id=f"rf_{len(self.refund_calls)}", status=self.refund_status
        )
        self.refunds[reference] = result
        if self.lose_next_refund_reply:
            self.lose_next_refund_reply = False
            raise PaystackError("reply lost after Paystack accepted the refund")
        return result

    async def find_refund(self, *, reference: str) -> RefundResult | None:
        self.find_calls.append(reference)
        return self.refunds.get(reference)
