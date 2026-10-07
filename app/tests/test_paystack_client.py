"""PaystackClient against httpx.MockTransport: request shape and error mapping."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.services.paystack import (
    InitializedTransaction,
    PaystackClient,
    PaystackError,
    RefundResult,
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


# --- refunds ---

REFUND_OK = {
    "status": True,
    "message": "Refund has been queued for processing",
    "data": {
        "transaction": {
            "id": 1004723697,
            "reference": REFERENCE,
            "amount": 500000,
            "currency": "NGN",
        },
        "id": 3018284,
        "amount": 500000,
        "currency": "NGN",
        "status": "pending",
    },
}


async def test_create_refund_sends_the_documented_request_and_parses_the_reply() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REFUND_OK)

    result = await client_with(handler).create_refund(
        reference=REFERENCE, amount=500000
    )

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://api.paystack.co/refund"
    assert request.headers["authorization"] == "Bearer sk_test_secret"
    body = json.loads(request.content)
    assert body["transaction"] == REFERENCE
    assert body["amount"] == 500000  # kobo, from our database
    assert result == RefundResult(refund_id="3018284", status="pending")


@pytest.mark.parametrize("status", ["pending", "processing", "processed"])
async def test_every_accepted_refund_status_is_success(status: str) -> None:
    payload = {**REFUND_OK, "data": {**REFUND_OK["data"], "status": status}}
    result = await client_with(
        lambda _: httpx.Response(200, json=payload)
    ).create_refund(reference=REFERENCE, amount=1)
    assert result.status == status


async def test_a_fully_reversed_transaction_counts_as_already_refunded() -> None:
    response = httpx.Response(
        400, json={"status": False, "message": "Transaction has been fully reversed"}
    )

    result = await client_with(lambda _: response).create_refund(
        reference=REFERENCE, amount=500000
    )

    assert result == RefundResult(refund_id=None, status="processed")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            400,
            json={
                "message": "Insufficient balance to fulfill this refund. Please top up."
            },
        ),
        httpx.Response(400, json={"message": "Cannot refund less than USD 1"}),
        httpx.Response(404, json={"message": "Transaction not found"}),
        httpx.Response(500, text="oops"),
        httpx.Response(
            200, json={**REFUND_OK, "data": {**REFUND_OK["data"], "status": "failed"}}
        ),
        httpx.Response(200, json={**REFUND_OK, "status": False}),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"status": True}),
        httpx.Response(
            200,
            json={
                **REFUND_OK,
                "data": {**REFUND_OK["data"], "transaction": {"reference": "sb_other"}},
            },
        ),
    ],
    ids=[
        "insufficient-balance",
        "below-minimum",
        "not-found",
        "500",
        "failed-status",
        "status-false",
        "not-json",
        "missing-data",
        "other-transaction",
    ],
)
async def test_create_refund_failures_become_paystack_error(
    response: httpx.Response,
) -> None:
    with pytest.raises(PaystackError) as excinfo:
        await client_with(lambda _: response).create_refund(
            reference=REFERENCE, amount=1
        )
    assert "balance" not in str(excinfo.value).lower()  # the body is never echoed


async def test_create_refund_timeout_becomes_paystack_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PaystackError):
        await client_with(handler).create_refund(reference=REFERENCE, amount=1)


async def test_find_refund_asks_for_this_transaction_and_returns_an_accepted_one() -> (
    None
):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": [
                    {
                        "id": 1,
                        "status": "failed",
                        "transaction": {"reference": REFERENCE},
                    },
                    {
                        "id": 2,
                        "status": "processing",
                        "transaction": {"reference": "sb_x"},
                    },
                    {
                        "id": 3,
                        "status": "processing",
                        "transaction": {"reference": REFERENCE},
                    },
                ],
            },
        )

    result = await client_with(handler).find_refund(reference=REFERENCE)

    assert seen[0].method == "GET"
    assert seen[0].url.params["reference"] == REFERENCE
    assert result == RefundResult(refund_id="3", status="processing")


async def test_find_refund_is_none_when_there_is_no_usable_refund() -> None:
    only_failed = {"data": [{"id": 1, "status": "failed", "transaction": REFERENCE}]}
    for payload in ({"data": []}, only_failed):
        client = client_with(lambda _, p=payload: httpx.Response(200, json=p))
        assert await client.find_refund(reference=REFERENCE) is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="oops"),
        httpx.Response(200, text="nope"),
        httpx.Response(200, json={}),
    ],
)
async def test_find_refund_failures_become_paystack_error(
    response: httpx.Response,
) -> None:
    with pytest.raises(PaystackError):
        await client_with(lambda _: response).find_refund(reference=REFERENCE)
