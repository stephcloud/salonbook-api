import asyncio
import uuid
from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.booking import BookingStatus
from app.models.payment import Payment, PaymentStatus
from app.models.salon import Salon
from app.models.user import UserRole
from app.services.bookings import PENDING_EXPIRY_MINUTES
from app.tests.payment_helpers import all_payments, make_scenario
from app.tests.paystack_fakes import FakePaystack
from app.tests.test_salons_api import make_user


def pay_url(booking_id: uuid.UUID) -> str:
    return f"/api/v1/bookings/{booking_id}/pay"


# --- success ---


async def test_client_gets_a_checkout_for_their_pending_booking(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["amount"] == scenario.salon.deposit_amount
    assert body["currency"] == "NGN"
    assert body["authorization_url"].startswith("https://checkout.paystack.test/")
    assert "access_code" not in body  # only what the client needs
    (call,) = fake_paystack.initialize_calls
    assert (
        call["amount"] == scenario.salon.deposit_amount
    )  # from the salon, not the request
    assert call["email"] == scenario.client.email
    assert call["reference"] == body["paystack_reference"]


async def test_the_amount_cannot_be_chosen_by_the_client(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)

    await db_client.post(
        pay_url(scenario.booking.id), json={"amount": 1}, headers=scenario.headers
    )

    assert fake_paystack.initialize_calls[0]["amount"] == scenario.salon.deposit_amount


async def test_payment_row_is_committed_before_paystack_is_called(
    db_client: AsyncClient,
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_paystack: FakePaystack,
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    seen: list[PaymentStatus | None] = []

    async def peek(reference: str) -> None:
        # A different connection, so only committed data is visible to it.
        async with session_maker() as other:
            payment = (
                await other.execute(
                    select(Payment).where(Payment.paystack_reference == reference)
                )
            ).scalar_one_or_none()
            seen.append(payment.status if payment else None)

    fake_paystack.on_initialize = peek

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 200
    assert seen == [PaymentStatus.PENDING]


async def test_calling_pay_again_returns_the_same_checkout(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    first = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)
    second = await db_client.post(
        pay_url(scenario.booking.id), headers=scenario.headers
    )

    assert second.status_code == 200
    assert second.json()["authorization_url"] == first.json()["authorization_url"]
    assert len(fake_paystack.initialize_calls) == 1
    assert len(await all_payments(db_session)) == 1


async def test_a_double_click_creates_one_live_payment(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)

    async def slow(_: str) -> None:
        await asyncio.sleep(0.2)  # keep the first request in flight

    fake_paystack.on_initialize = slow

    responses = await asyncio.gather(
        *(
            db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)
            for _ in range(3)
        )
    )

    codes = sorted(r.status_code for r in responses)
    assert codes[0] == 200 and set(codes) <= {200, 409}
    live = [
        p for p in await all_payments(db_session) if p.status != PaymentStatus.FAILED
    ]
    assert len(live) == 1
    assert len(fake_paystack.initialize_calls) == 1


# --- Paystack failure ---


async def test_paystack_failure_marks_the_row_failed_and_a_retry_works(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    fake_paystack.fail_initialize = True

    failed = await db_client.post(
        pay_url(scenario.booking.id), headers=scenario.headers
    )

    assert failed.status_code == 502
    (row,) = await all_payments(db_session)
    assert row.status == PaymentStatus.FAILED

    fake_paystack.fail_initialize = False
    retried = await db_client.post(
        pay_url(scenario.booking.id), headers=scenario.headers
    )

    assert retried.status_code == 200
    assert (
        retried.json()["paystack_reference"] != row.paystack_reference
    )  # fresh reference
    statuses = sorted(p.status for p in await all_payments(db_session))
    assert statuses == [PaymentStatus.FAILED, PaymentStatus.PENDING]


# --- ownership and roles ---


async def test_user_b_cannot_pay_for_user_as_booking(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    _, other_headers = await make_user(db_session, UserRole.CLIENT)

    resp = await db_client.post(pay_url(scenario.booking.id), headers=other_headers)

    assert resp.status_code == 403
    assert await all_payments(db_session) == []
    assert fake_paystack.initialize_calls == []


async def test_only_clients_can_pay(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    for role in (UserRole.OWNER, UserRole.STYLIST):
        _, headers = await make_user(db_session, role)
        resp = await db_client.post(pay_url(scenario.booking.id), headers=headers)
        assert resp.status_code == 403
    assert fake_paystack.initialize_calls == []


async def test_pay_requires_auth(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=None)
    resp = await db_client.post(pay_url(scenario.booking.id))
    assert resp.status_code == 401


async def test_unknown_booking_is_404(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    _, headers = await make_user(db_session, UserRole.CLIENT)
    resp = await db_client.post(pay_url(uuid.uuid4()), headers=headers)
    assert resp.status_code == 404


# --- bookings that can't be paid for ---


async def test_a_lapsed_pending_booking_cannot_be_paid_for(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(
        db_session,
        payment_status=None,
        age=timedelta(minutes=PENDING_EXPIRY_MINUTES, seconds=1),
    )

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 409
    assert await all_payments(db_session) == []
    assert fake_paystack.initialize_calls == []


async def test_confirmed_and_cancelled_bookings_cannot_be_paid_for(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    for status in (BookingStatus.CONFIRMED, BookingStatus.CANCELLED):
        scenario = await make_scenario(
            db_session, payment_status=None, booking_status=status
        )
        resp = await db_client.post(
            pay_url(scenario.booking.id), headers=scenario.headers
        )
        assert resp.status_code == 409
    assert fake_paystack.initialize_calls == []


async def test_an_already_paid_booking_cannot_be_paid_twice(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    scenario = await make_scenario(db_session, payment_status=PaymentStatus.PAID)

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 409
    assert fake_paystack.initialize_calls == []


async def test_a_payment_waiting_for_refund_does_not_block_a_new_checkout(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    """E.g. a mismatched charge was queued for refund; the booking is still open."""
    scenario = await make_scenario(
        db_session, payment_status=PaymentStatus.REFUND_PENDING
    )

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 200
    assert len(fake_paystack.initialize_calls) == 1
    statuses = sorted(p.status for p in await all_payments(db_session))
    assert statuses == [PaymentStatus.PENDING, PaymentStatus.REFUND_PENDING]


async def test_a_salon_with_no_deposit_is_a_409_backstop(
    db_client: AsyncClient, db_session: AsyncSession, fake_paystack: FakePaystack
) -> None:
    """The API rejects a 0 deposit, but a row from before that rule could still hold one."""
    scenario = await make_scenario(db_session, payment_status=None)
    salon = await db_session.get(Salon, scenario.salon.id)
    assert salon is not None
    salon.deposit_amount = 0
    await db_session.commit()

    resp = await db_client.post(pay_url(scenario.booking.id), headers=scenario.headers)

    assert resp.status_code == 409
    assert await all_payments(db_session) == []
    assert fake_paystack.initialize_calls == []
