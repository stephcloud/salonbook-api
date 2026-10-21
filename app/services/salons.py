import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.salon import Salon, SalonStatus
from app.models.user import User
from app.schemas.salon import SalonCreate, SalonUpdate


def forbidden() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="salon not found"
    )


def not_accepting_bookings() -> HTTPException:
    """The clear refusal for slots, booking and payment on a non-approved salon."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="salon is not accepting bookings"
    )


async def create_salon(session: AsyncSession, owner: User, data: SalonCreate) -> Salon:
    # Always pending: only an admin approves, whatever the client sends.
    salon = Salon(owner_id=owner.id, status=SalonStatus.PENDING, **data.model_dump())
    session.add(salon)
    await session.commit()
    await session.refresh(salon)
    return salon


async def list_salons(session: AsyncSession, limit: int, offset: int) -> list[Salon]:
    """The public list: approved salons only."""
    result = await session.execute(
        select(Salon)
        .where(Salon.status == SalonStatus.APPROVED)
        .order_by(Salon.created_at, Salon.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


async def list_owned_salons(
    session: AsyncSession, owner: User, limit: int, offset: int
) -> list[Salon]:
    result = await session.execute(
        select(Salon)
        .where(Salon.owner_id == owner.id)
        .order_by(Salon.created_at, Salon.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


async def get_salon(session: AsyncSession, salon_id: uuid.UUID) -> Salon:
    salon = await session.get(Salon, salon_id)
    if salon is None:
        raise _not_found()
    return salon


async def get_public_salon(session: AsyncSession, salon_id: uuid.UUID) -> Salon:
    """A salon as the public sees it: pending and suspended ones are a plain 404."""
    salon = await get_salon(session, salon_id)
    if salon.status != SalonStatus.APPROVED:
        raise _not_found()
    return salon


async def get_visible_salon(
    session: AsyncSession, salon_id: uuid.UUID, viewer: User | None
) -> Salon:
    """A public read that the salon's own owner may make whatever the status.

    Everyone else, signed in or not, gets the same 404 as `get_public_salon`.
    """
    salon = await get_salon(session, salon_id)
    if salon.status != SalonStatus.APPROVED and (
        viewer is None or viewer.id != salon.owner_id
    ):
        raise _not_found()
    return salon


async def get_owned_salon(
    session: AsyncSession, salon_id: uuid.UUID, user: User
) -> Salon:
    """Load a salon for writing: 404 if missing, 403 if `user` doesn't own it."""
    salon = await get_salon(session, salon_id)
    if salon.owner_id != user.id:
        raise forbidden()
    return salon


async def update_salon(
    session: AsyncSession, salon_id: uuid.UUID, user: User, data: SalonUpdate
) -> Salon:
    salon = await get_owned_salon(session, salon_id, user)
    for field, value in data.model_dump(exclude_unset=True).items():
        # An explicit null only means "clear" for the optional image; for the required
        # fields it is ignored, as before.
        if value is not None or field == "image_url":
            setattr(salon, field, value)
    session.add(salon)
    await session.commit()
    await session.refresh(salon)
    return salon
