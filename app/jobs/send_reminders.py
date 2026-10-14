"""Send the reminders and owner summaries that are due.

The app calls `run_due` from POST /api/v1/internal/reminders/run, which an external
scheduler hits every few minutes. As a one-shot: `python -m app.jobs.send_reminders`
(e.g. a Render Cron Job, if the service moves to a paid plan). Idempotent and safe to run
next to the endpoint or in several workers at once: every send is claimed first.
"""

import asyncio
import logging

from app.db.session import async_session_maker, engine
from app.services.email.brevo import get_email_sender
from app.services.email.reminders import run_due


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        await run_due(async_session_maker, get_email_sender())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
