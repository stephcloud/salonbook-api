"""Cancel lapsed pending bookings.

Runs in-process every EXPIRY_INTERVAL_SECONDS (started from the app lifespan), or as
a one-shot: `python -m app.jobs.expire_pending` (e.g. a Render Cron Job).

Housekeeping only: POST /bookings cancels the stylist's lapsed pendings itself, so
correctness never depends on this running. It is idempotent and safe to run in
several workers at once.
"""

import asyncio
import logging
from datetime import datetime

from app.db.session import async_session_maker, engine
from app.services.bookings import expire_pending_bookings

logger = logging.getLogger(__name__)

EXPIRY_INTERVAL_SECONDS = 60


async def run_once(now: datetime | None = None) -> int:
    async with async_session_maker() as session:
        cancelled = await expire_pending_bookings(session, now)
        await session.commit()
    return cancelled


async def run_forever(interval_seconds: float = EXPIRY_INTERVAL_SECONDS) -> None:
    while True:
        try:
            cancelled = await run_once()
            if cancelled:
                logger.info("cancelled %d lapsed pending bookings", cancelled)
        except Exception:  # one failed run must not stop the loop
            logger.exception("pending-booking expiry run failed")
        await asyncio.sleep(interval_seconds)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        logger.info("cancelled %d lapsed pending bookings", await run_once())
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
