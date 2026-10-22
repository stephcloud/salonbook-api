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
from app.schemas.stylist import PublicStylistResponse, StylistCreate, StylistUpdate
from app.services.auth import email_taken, get_user_by_email
from app.services.salons import get_owned_salon, get_visible_salon
from app.services.services import FOREIGN_KEY_VIOLATION


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
    session: AsyncSession,
    salon_id: uuid.UUID,
    viewer: User | None,
    limit: int,
    offset: int,
) -> list[PublicStylistResponse]:
    await get_visible_salon(session, salon_id, viewer)
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
    session: AsyncSession, stylist_id: uuid.UUID, owner: User, lock: bool = False
) -> User:
    """With `lock`, the row is read fresh under FOR NO KEY UPDATE, so a stylist deleted
    by a concurrent request is a 404 here, not a failed write later. That mode does not
    block foreign-key checks from new bookings (FOR KEY SHARE).
    """
    stmt = (
        select(User)
        .where(User.id == stylist_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update(key_share=True)
    stylist = (await session.execute(stmt)).scalar_one_or_none()
    if stylist is None or stylist.role != UserRole.STYLIST or stylist.salon_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stylist not found"
        )
    await get_owned_salon(session, stylist.salon_id, owner)
    return stylist


async def update_stylist(
    session: AsyncSession, stylist_id: uuid.UUID, owner: User, data: StylistUpdate
) -> User:
    stylist = await _get_owned_stylist(session, stylist_id, owner, lock=True)
    for field, value in data.model_dump(exclude_unset=True).items():
        # An explicit null only means "clear" for the optional image; a null name is
        # ignored, as in the salon update.
        if value is not None or field == "image_url":
            setattr(stylist, field, value)
    session.add(stylist)
    await session.commit()
    await session.refresh(stylist)
    return stylist


async def delete_stylist(
    session: AsyncSession, stylist_id: uuid.UUID, owner: User
) -> None:
    """Delete a stylist, unless a booking still references them (409).

    Their availability rules and service links go with them (`ON DELETE CASCADE`);
    bookings keep their stylist (`ON DELETE RESTRICT`). The database decides, so a
    booking created at the same moment can't slip past a check.
    """
    stylist = await _get_owned_stylist(session, stylist_id, owner)
    try:
        await session.delete(stylist)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) != FOREIGN_KEY_VIOLATION:
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This stylist has bookings and cannot be deleted.",
        ) from None


async def set_stylist_services(
    session: AsyncSession,
    stylist_id: uuid.UUID,
    owner: User,
    service_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """Replace the set of services a stylist offers (idempotent)."""
    # The lock serializes concurrent replaces for this stylist (delete-then-insert
    # would otherwise collide on the composite primary key).
    stylist = await _get_owned_stylist(session, stylist_id, owner, lock=True)
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
