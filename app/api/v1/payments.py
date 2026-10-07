from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)

from app.api.deps import SessionDep, SessionMakerDep
from app.services import payments as payment_service
from app.services.paystack import PaystackClient, get_paystack_client

router = APIRouter(prefix="/payments", tags=["payments"])

# Paystack events are a few KB. The body is read before the signature can be checked,
# so it is capped: an anonymous caller must not be able to make us buffer a huge one.
MAX_WEBHOOK_BYTES = 64 * 1024


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="body too large"
    )


async def _read_capped_body(request: Request) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_WEBHOOK_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():  # also stops a body that lies about its size
        body.extend(chunk)
        if len(body) > MAX_WEBHOOK_BYTES:
            raise _too_large()
    return bytes(body)


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
    raw_body = await _read_capped_body(request)
    refund_ids = await payment_service.handle_paystack_event(
        session, raw_body, x_paystack_signature
    )
    # After the commit, outside the request's transaction: Paystack is never called
    # while a lock is held, and a failure here is retried by the sweeper.
    for payment_id in refund_ids:
        background_tasks.add_task(
            payment_service.try_refund, session_maker, paystack, payment_id
        )
    return {"status": "ok"}
