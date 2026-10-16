"""Tell a salon owner that an admin approved or suspended their salon.

Runs as a background task after the admin request has committed and answered, in its own
session. It never raises: any failure is logged and dropped, so the admin action is never
affected. There is no sent-marker: the status change itself is the claim. Only the request
that actually flipped the status schedules this, so a repeated approve or suspend (or two
concurrent ones) sends nothing extra. A crash between commit and send loses the email.

Logs carry the salon id only: never an address, a name, a body, or the API key.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.salon import Salon, SalonStatus
from app.models.user import User
from app.services.email import templates
from app.services.email.base import EmailSender
from app.services.email.templates import SalonStatusEmailContext

logger = logging.getLogger(__name__)

SessionFactory = async_sessionmaker[AsyncSession]


async def notify_salon_status_changed(
    session_maker: SessionFactory,
    sender: EmailSender | None,
    salon_id: uuid.UUID,
    new_status: SalonStatus,
) -> None:
    try:
        if sender is None:
            logger.info("salon status email skipped: email is not configured")
            return
        if new_status == SalonStatus.APPROVED:
            build = templates.owner_salon_approved
        elif new_status == SalonStatus.SUSPENDED:
            build = templates.owner_salon_suspended
        else:
            return
        async with session_maker() as session:
            row = (
                await session.execute(
                    select(Salon, User)
                    .join(User, User.id == Salon.owner_id)
                    .where(Salon.id == salon_id)
                )
            ).one_or_none()
        # Skip if the salon is gone or has already moved on to another status.
        if row is None or row[0].status != new_status:
            logger.info("salon status email skipped: salon changed; salon=%s", salon_id)
            return
        salon, owner = row
        await sender.send(
            build(
                SalonStatusEmailContext(
                    owner_name=owner.name,
                    owner_email=owner.email,
                    salon_name=salon.name,
                )
            )
        )
    except Exception as exc:  # noqa: BLE001 - never break the caller's work
        logger.warning(
            "salon status email failed: %s; salon=%s", type(exc).__name__, salon_id
        )
