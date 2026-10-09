import asyncio
import uuid

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.service import Service
from app.models.stylist_service import StylistService
from app.models.user import User, UserRole
from app.schemas.stylist import PublicStylistResponse, StylistCreate
from app.services.auth import email_taken, get_user_by_email
from app.services.salons import get_owned_salon, get_salon


async def create_stylist(
    session: AsyncSession, salon_id: uuid.UUID, owner: User, data: StylistCreate
) -> User:
    await get_owned_salon(session, salon_id, owner)
    email = data.email.lower()
    if await get_user_by_email(session, email):
        raise email_taken()
    stylist = User(
        name=data.name,
        email=email,
        password_hash=await asyncio.to_thread(hash_password, data.password),
        role=UserRole.STYLIST,
        salon_id=salon_id,
        image_url=data.image_url,
    )
    session.add(stylist)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise email_taken() from None
    await session.refresh(stylist)
    return stylist


async def list_salon_stylists(
    session: AsyncSession, salon_id: uuid.UUID, limit: int, offset: int
) -> list[PublicStylistResponse]:
    await get_salon(session, salon_id)
    result = await session.execute(
        select(User)
        .where(User.salon_id == salon_id, User.role == UserRole.STYLIST)
        .order_by(User.created_at, User.id)
        .limit(limit)
        .offset(offset)
    )
    stylists = list(result.scalars())
    services_by_stylist: dict[uuid.UUID, list[uuid.UUID]] = {s.id: [] for s in stylists}
    if stylists:
        rows = await session.execute(
            select(StylistService.stylist_id, StylistService.service_id)
            .where(StylistService.stylist_id.in_(services_by_stylist))
            .order_by(StylistService.service_id)
        )
        for stylist_id, service_id in rows:
            services_by_stylist[stylist_id].append(service_id)
    return [
        PublicStylistResponse(
            id=s.id,
            name=s.name,
            image_url=s.image_url,
            service_ids=services_by_stylist[s.id],
        )
        for s in stylists
    ]


async def _get_owned_stylist(
    session: AsyncSession, stylist_id: uuid.UUID, owner: User
) -> User:
    stylist = await session.get(User, stylist_id)
    if stylist is None or stylist.role != UserRole.STYLIST or stylist.salon_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stylist not found"
        )
    await get_owned_salon(session, stylist.salon_id, owner)
    return stylist


async def set_stylist_services(
    session: AsyncSession,
    stylist_id: uuid.UUID,
    owner: User,
    service_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Replace the set of services a stylist offers (idempotent)."""
    stylist = await _get_owned_stylist(session, stylist_id, owner)
    # Serialize concurrent replaces for this stylist (delete-then-insert would
    # otherwise collide on the composite primary key).
    await session.execute(
        select(User.id).where(User.id == stylist_id).with_for_update()
    )
    wanted = set(service_ids)
    if wanted:
        result = await session.execute(
            select(Service.id).where(
                Service.id.in_(wanted), Service.salon_id == stylist.salon_id
            )
        )
        if set(result.scalars()) != wanted:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="all services must belong to the stylist's salon",
            )
    await session.execute(
        delete(StylistService).where(StylistService.stylist_id == stylist_id)
    )
    session.add_all(
        StylistService(stylist_id=stylist_id, service_id=sid) for sid in wanted
    )
    await session.commit()
    return sorted(wanted)
