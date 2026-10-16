"""One-off: grant the admin role to an existing account.

    python -m app.jobs.make_admin <email>

Run it yourself against the database you mean to change (set DATABASE_URL for that one
command). There is no API route that does this, and the role is never granted by matching
an email at login, because emails are not verified: anyone could register an address
that is "supposed" to be an admin. So register your own account first, then run this on
that exact email.

It refuses (exit code 1, nothing changed) when the email has no account, or when the
account owns a salon, is a stylist, or has bookings: an admin should be a dedicated
account. Running it on an existing admin is a harmless no-op.

Writes no audit row (the audit log records the acting admin and there is none yet). The
log line, which carries the user id and never the email, is the record.
"""

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_maker, engine
from app.models.booking import Booking
from app.models.salon import Salon
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


class MakeAdminError(Exception):
    """The account can't (or shouldn't) be promoted. The message is safe to print."""


@dataclass
class PromoteResult:
    user_id: uuid.UUID
    changed: bool


async def promote_to_admin(session: AsyncSession, email: str) -> PromoteResult:
    user = (
        await session.execute(
            select(User)
            .where(User.email == email.strip().lower())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise MakeAdminError("no account with that email")
    if user.role == UserRole.ADMIN:
        return PromoteResult(user_id=user.id, changed=False)
    if user.role == UserRole.STYLIST or user.salon_id is not None:
        raise MakeAdminError("that account is a stylist")
    if await session.scalar(select(exists().where(Salon.owner_id == user.id))):
        raise MakeAdminError("that account owns a salon")
    has_bookings = await session.scalar(
        select(
            exists().where(
                (Booking.client_id == user.id) | (Booking.stylist_id == user.id)
            )
        )
    )
    if has_bookings:
        raise MakeAdminError("that account has bookings")
    user.role = UserRole.ADMIN
    session.add(user)
    await session.commit()
    return PromoteResult(user_id=user.id, changed=True)


async def _main(email: str) -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        async with async_session_maker() as session:
            try:
                result = await promote_to_admin(session, email)
            except MakeAdminError as exc:
                await session.rollback()
                logger.error("refused: %s", exc)
                return 1
        if result.changed:
            logger.info("user %s is now an admin", result.user_id)
        else:
            logger.info("user %s was already an admin; nothing changed", result.user_id)
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.jobs.make_admin <email>", file=sys.stderr)
        return 2
    return asyncio.run(_main(args[0]))


if __name__ == "__main__":
    sys.exit(main())
