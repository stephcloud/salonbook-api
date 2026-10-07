from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request

from app.api.deps import SessionDep, SessionMakerDep
from app.services import payments as payment_service
from app.services.paystack import PaystackClient, get_paystack_client

router = APIRouter(prefix="/payments", tags=["payments"])


# Public on purpose: Paystack calls this, not a signed-in user. The x-paystack-signature
# check on the raw body is the authentication.
@router.post("/webhook")
async def paystack_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    session_maker: SessionMakerDep,
    paystack: Annotated[PaystackClient, Depends(get_paystack_client)],
    x_paystack_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    raw_body = await request.body()
    refund_ids = await payment_service.handle_paystack_event(
        session, raw_body, x_paystack_signature
    )
    # After the commit, outside the request's transaction: Paystack is never called
    # while a lock is held, and a failure here is retried by the sweeper.
    for payment_id in refund_ids:
        background_tasks.add_task(
            payment_service.process_refund, session_maker, paystack, payment_id
        )
    return {"status": "ok"}
