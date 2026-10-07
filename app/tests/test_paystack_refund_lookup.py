"""PaystackClient refund lookups: strict matching, and states we don't know."""

import httpx
import pytest

from app.services.paystack import RefundResult
from app.tests.test_paystack_client import REFERENCE, REFUND_OK, client_with


@pytest.mark.parametrize("transaction", [12345, None], ids=["bare-id", "missing"])
async def test_find_refund_ignores_a_record_it_cannot_tie_to_the_transaction(
    transaction: object,
) -> None:
    """If Paystack ignored our filter, another transaction's refund must not count as ours."""
    payload = {"data": [{"id": 9, "status": "processed", "transaction": transaction}]}
    client = client_with(lambda _: httpx.Response(200, json=payload))

    assert await client.find_refund(reference=REFERENCE) is None


async def test_a_refund_in_a_state_we_do_not_know_is_returned_not_ignored() -> None:
    """Treating it as absent would create a second refund on top of it."""
    listed = {
        "data": [
            {
                "id": 5,
                "status": "needs-attention",
                "transaction": {"reference": REFERENCE},
            }
        ]
    }
    created = {
        **REFUND_OK,
        "data": {**REFUND_OK["data"], "status": "needs-attention"},
    }

    found = await client_with(lambda _: httpx.Response(200, json=listed)).find_refund(
        reference=REFERENCE
    )
    made = await client_with(lambda _: httpx.Response(200, json=created)).create_refund(
        reference=REFERENCE, amount=1
    )

    assert found == RefundResult(refund_id="5", status="needs-attention")
    assert made.status == "needs-attention"
