import uuid
from collections.abc import Sequence

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.availability_rule import AvailabilityRule, RuleKind
from app.models.user import User, UserRole
from app.schemas.availability import AvailabilityRuleInput
from app.services.salons import forbidden, get_owned_salon


def find_conflict(rules: Sequence[AvailabilityRuleInput]) -> str | None:
    """Return a message for the first overlap, or None if the rules are consistent.

    Working windows may not overlap each other on a weekday, breaks may not overlap
    each other, and a date can be a day off only once. Back-to-back ranges (one ends
    exactly when the next starts) are fine.
    """
    timed = [r for r in rules if r.kind != RuleKind.DAY_OFF]
    for i, a in enumerate(timed):
        for b in timed[i + 1 :]:
            if a.kind != b.kind or a.weekday != b.weekday:
                continue
            assert a.start_time and a.end_time and b.start_time and b.end_time
            if a.start_time < b.end_time and b.start_time < a.end_time:
                return f"overlapping {a.kind.value} rules on weekday {a.weekday}"
    dates = [r.off_date for r in rules if r.kind == RuleKind.DAY_OFF]
    if len(dates) != len(set(dates)):
        return "duplicate day_off date"
    return None


async def _get_editable_stylist(
    session: AsyncSession, stylist_id: uuid.UUID, user: User
) -> User:
    """The stylist themself or the owner of their salon may edit; others get 403."""
    stylist = await session.get(User, stylist_id)
    if stylist is None or stylist.role != UserRole.STYLIST or stylist.salon_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stylist not found"
        )
    if user.id == stylist.id:
        return stylist
    if user.role != UserRole.OWNER:
        raise forbidden()
    await get_owned_salon(session, stylist.salon_id, user)
    return stylist


async def _list_rules(
    session: AsyncSession, stylist_id: uuid.UUID
) -> list[AvailabilityRule]:
    result = await session.execute(
        select(AvailabilityRule)
        .where(AvailabilityRule.stylist_id == stylist_id)
        .order_by(
            AvailabilityRule.kind,
            AvailabilityRule.weekday,
            AvailabilityRule.start_time,
            AvailabilityRule.off_date,
            AvailabilityRule.id,
        )
    )
    return list(result.scalars())


async def get_availability(
    session: AsyncSession, stylist_id: uuid.UUID, user: User
) -> list[AvailabilityRule]:
    await _get_editable_stylist(session, stylist_id, user)
    return await _list_rules(session, stylist_id)


async def set_availability(
    session: AsyncSession,
    stylist_id: uuid.UUID,
    user: User,
    rules: Sequence[AvailabilityRuleInput],
) -> list[AvailabilityRule]:
    """Replace all of a stylist's rules (idempotent)."""
    await _get_editable_stylist(session, stylist_id, user)
    if (conflict := find_conflict(rules)) is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=conflict
        )
    # Serialize concurrent replaces for this stylist.
    await session.execute(
        select(User.id).where(User.id == stylist_id).with_for_update()
    )
    await session.execute(
        delete(AvailabilityRule).where(AvailabilityRule.stylist_id == stylist_id)
    )
    session.add_all(
        AvailabilityRule(stylist_id=stylist_id, **r.model_dump()) for r in rules
    )
    await session.commit()
    return await _list_rules(session, stylist_id)
