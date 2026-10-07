"""Send refunds that are still waiting.

Runs in-process every REFUND_INTERVAL_SECONDS (started from the app lifespan), or as a
one-shot: `python -m app.jobs.retry_refunds` (e.g. a Render Cron Job).

The webhook and cancel start a refund right after they commit; this picks up whatever
that missed (Paystack down, a crash, a lost reply). `process_refund` claims each row
with a stamp, so this is safe next to those, and in several workers at once.
"""

import asyncio
import logging
from datetime import datetime

from app.core.config import settings
from app.db.session import async_session_maker, engine
from app.services.payments import list_refunds_due, try_refund
from app.services.paystack import PaystackClient, get_paystack_client

logger = logging.getLogger(__name__)

REFUND_INTERVAL_SECONDS = 60
BATCH_SIZE = 20


async def run_once(
    now: datetime | None = None, paystack: PaystackClient | None = None
) -> int:
    """Try every due refund once; return how many were sent."""
    if paystack is None:
        if not settings.PAYSTACK_SECRET_KEY.strip():
            # Local without a key: don't burn attempts on calls that can't work.
            return 0
        paystack = get_paystack_client()
    sent = 0
    for payment_id in await list_refunds_due(async_session_maker, now, BATCH_SIZE):
        if await try_refund(async_session_maker, paystack, payment_id, now):
            sent += 1
    return sent


async def run_forever(interval_seconds: float = REFUND_INTERVAL_SECONDS) -> None:
    while True:
        try:
            sent = await run_once()
            if sent:
                logger.info("sent %d waiting refunds", sent)
        except Exception:  # one failed run must not stop the loop
            logger.exception("refund retry run failed")
        await asyncio.sleep(interval_seconds)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        logger.info("sent %d waiting refunds", await run_once())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
