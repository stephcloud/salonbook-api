from typing import Annotated

from fastapi import APIRouter, Header, Request

from app.api.deps import SessionDep
from app.services import payments as payment_service

router = APIRouter(prefix="/payments", tags=["payments"])


# Public on purpose: Paystack calls this, not a signed-in user. The x-paystack-signature
# check on the raw body is the authentication.
@router.post("/webhook")
async def paystack_webhook(
    request: Request,
    session: SessionDep,
    x_paystack_signature: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    raw_body = await request.body()
    await payment_service.handle_paystack_event(session, raw_body, x_paystack_signature)
    return {"status": "ok"}
