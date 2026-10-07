from app.services.payments import verify_signature
from app.tests.paystack_fakes import TEST_PAYSTACK_SECRET, sign

BODY = b'{"event":"charge.success","data":{"reference":"sb_1","amount":500000}}'


def test_a_correct_signature_is_valid() -> None:
    assert verify_signature(BODY, sign(BODY), TEST_PAYSTACK_SECRET) is True


def test_a_signature_from_another_key_is_invalid() -> None:
    assert verify_signature(BODY, sign(BODY, "sk_other"), TEST_PAYSTACK_SECRET) is False


def test_a_changed_body_is_invalid() -> None:
    assert verify_signature(BODY + b" ", sign(BODY), TEST_PAYSTACK_SECRET) is False


def test_a_missing_or_empty_signature_is_invalid() -> None:
    assert verify_signature(BODY, None, TEST_PAYSTACK_SECRET) is False
    assert verify_signature(BODY, "", TEST_PAYSTACK_SECRET) is False


def test_an_unset_secret_is_never_valid() -> None:
    assert verify_signature(BODY, sign(BODY, ""), "") is False


def test_signature_is_over_the_raw_bytes_not_the_parsed_json() -> None:
    reformatted = b'{ "event": "charge.success", "data": {"reference": "sb_1", "amount": 500000} }'
    assert verify_signature(reformatted, sign(BODY), TEST_PAYSTACK_SECRET) is False
