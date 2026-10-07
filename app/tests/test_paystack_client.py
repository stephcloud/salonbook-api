"""PaystackClient against httpx.MockTransport: request shape and error mapping."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.services.paystack import (
    InitializedTransaction,
    PaystackClient,
    PaystackError,
)

REFERENCE = "sb_abc123"
OK_BODY = {
    "status": True,
    "message": "Authorization URL created",
    "data": {
        "authorization_url": "https://checkout.paystack.com/xyz",
        "access_code": "ac_xyz",
        "reference": REFERENCE,
    },
}


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> PaystackClient:
    return PaystackClient("sk_test_secret", transport=httpx.MockTransport(handler))


async def initialize(client: PaystackClient) -> InitializedTransaction:
    return await client.initialize_transaction(
        email="a@example.com", amount=500000, currency="NGN", reference=REFERENCE
    )


async def test_initialize_sends_the_expected_request_and_parses_the_reply() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=OK_BODY)

    started = await initialize(client_with(handler))

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.paystack.co/transaction/initialize"
    assert request.headers["authorization"] == "Bearer sk_test_secret"
    assert json.loads(request.content) == {
        "email": "a@example.com",
        "amount": 500000,
        "currency": "NGN",
        "reference": REFERENCE,
    }
    assert started.authorization_url == "https://checkout.paystack.com/xyz"
    assert started.access_code == "ac_xyz"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"status": False, "message": "Duplicate Reference"}),
        httpx.Response(401, json={"status": False, "message": "Invalid key"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={**OK_BODY, "status": False}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"status": True}),  # no data
        httpx.Response(200, json={"status": True, "data": {"reference": REFERENCE}}),
        httpx.Response(
            200, json={**OK_BODY, "data": {**OK_BODY["data"], "reference": "sb_other"}}
        ),
    ],
    ids=[
        "400",
        "401",
        "500",
        "status-false",
        "not-json",
        "missing-data",
        "missing-fields",
        "reference-mismatch",
    ],
)
async def test_bad_replies_become_paystack_error(response: httpx.Response) -> None:
    with pytest.raises(PaystackError):
        await initialize(client_with(lambda _: response))


async def test_a_timeout_becomes_paystack_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PaystackError):
        await initialize(client_with(handler))


async def test_error_text_never_contains_the_response_body_or_the_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "secret-detail sk_test_secret"})

    with pytest.raises(PaystackError) as excinfo:
        await initialize(client_with(handler))

    assert "secret-detail" not in str(excinfo.value)
    assert "sk_test_secret" not in str(excinfo.value)
