"""Endpoints for the external scheduler, not for users.

The app may be asleep (Render's free tier), so an in-process loop cannot send reminders on
time. cron-job.org calls this every few minutes instead; the call wakes the app. It is
authenticated by a shared secret header, not a JWT, and is hidden from the OpenAPI docs.
"""

import hmac
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status

from app.api.deps import SessionMakerDep
from app.core.config import settings
from app.schemas.internal import AcceptedResponse
from app.services.email.base import EmailSender
from app.services.email.brevo import get_email_sender
from app.services.email.reminders import run_due_safely

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def require_cron_secret(
    x_cron_secret: Annotated[str | None, Header()] = None,
) -> None:
    secret = settings.CRON_SECRET.get_secret_value().strip()
    if not secret.strip():
        # Never accept an empty secret: without one configured the endpoint is off.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not configured"
        )
    if x_cron_secret is None or not hmac.compare_digest(
        x_cron_secret.encode(), secret.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )


@router.post(
    "/reminders/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    dependencies=[Depends(require_cron_secret)],
)
async def run_reminders(
    background_tasks: BackgroundTasks,
    session_maker: SessionMakerDep,
    email_sender: Annotated[EmailSender | None, Depends(get_email_sender)],
) -> AcceptedResponse:
    """Send the reminders and owner summaries that are due. Answers at once.

    The work runs after the response, claims every send first (so overlapping calls and
    repeated calls are safe), and logs instead of raising.
    """
    background_tasks.add_task(run_due_safely, session_maker, email_sender)
    return AcceptedResponse()
