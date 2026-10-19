"""Admin moderation: approve, suspend and remove salons, remove accounts.

Every function writes its audit row in the same transaction as the action, so the two
commit or roll back together. Audit rows hold ids only (no emails, names or secrets).
The role check lives on the router; the self-protection rules (an admin can't suspend a
salon they own or delete themselves) live here so tests can call them directly.
"""

import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_audit_log import AdminAction, AdminAuditLog, AuditTargetType
from app.models.booking import ACTIVE_STATUSES, Booking
from app.models.salon import Salon, SalonStatus
from app.models.service import Service
from app.models.user import User, UserRole
from app.schemas.admin import AdminSalonItem, AdminSalonOwner


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"{what} not found"
    )


def _audit(
    admin: User,
    action: AdminAction,
    target_type: AuditTargetType,
    target_id: uuid.UUID,
    reason: str | None = None,
) -> AdminAuditLog:
    return AdminAuditLog(
        admin_id=admin.id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason=reason,
    )


async def list_salons(
    session: AsyncSession,
    limit: int,
    offset: int,
    salon_status: SalonStatus | None = None,
) -> list[AdminSalonItem]:
    """Salons with owner name and booking counts: two queries, whatever the page size.

    The counts are grouped only over the salons on this page, not the whole bookings table.
    """
    stmt = (
        select(Salon, User.name)
        .join(User, User.id == Salon.owner_id)
        .order_by(Salon.created_at, Salon.id)
        .limit(limit)
        .offset(offset)
    )
    if salon_status is not None:
        stmt = stmt.where(Salon.status == salon_status)
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    counts = {
        salon_id: (total, active)
        for salon_id, total, active in await session.execute(
            select(
                Service.salon_id,
                func.count(Booking.id),
                func.count(Booking.id).filter(Booking.status.in_(ACTIVE_STATUSES)),
            )
            .join(Service, Service.id == Booking.service_id)
            .where(Service.salon_id.in_([salon.id for salon, _ in rows]))
            .group_by(Service.salon_id)
        )
    }
    return [
        AdminSalonItem(
            id=salon.id,
            name=salon.name,
            status=salon.status,
            owner=AdminSalonOwner(id=salon.owner_id, name=owner_name),
            booking_count=counts.get(salon.id, (0, 0))[0],
            active_booking_count=counts.get(salon.id, (0, 0))[1],
            created_at=salon.created_at,
        )
        for salon, owner_name in rows
    ]


@dataclass
class StatusOutcome:
    salon: Salon
    changed: bool


async def _lock_salon(session: AsyncSession, salon_id: uuid.UUID) -> Salon:
    salon = (
        await session.execute(
            select(Salon)
            .where(Salon.id == salon_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if salon is None:
        raise _not_found("salon")
    return salon


async def set_salon_status(
    session: AsyncSession,
    admin: User,
    salon_id: uuid.UUID,
    target: SalonStatus,
    reason: str | None = None,
) -> StatusOutcome:
    """Approve or suspend a salon. Repeating the current status changes nothing.

    The row is locked first, so of two concurrent calls only one flips the status; the
    other sees it already set and is a no-op (no audit row, no email).
    """
    salon = await _lock_salon(session, salon_id)
    if target == SalonStatus.SUSPENDED and salon.owner_id == admin.id:
        await session.rollback()
        raise _conflict("you cannot suspend your own salon")
    if salon.status == target:
        await session.rollback()  # release the row lock; this also expires the salon
        await session.refresh(salon)
        return StatusOutcome(salon=salon, changed=False)
    salon.status = target
    session.add(salon)
    if target == SalonStatus.APPROVED:
        action = AdminAction.SALON_APPROVED
        reason = None  # a reason only makes sense for a suspension
    else:
        action = AdminAction.SALON_SUSPENDED
    session.add(_audit(admin, action, AuditTargetType.SALON, salon.id, reason))
    await session.commit()
    await session.refresh(salon)
    return StatusOutcome(salon=salon, changed=True)


async def delete_salon(session: AsyncSession, admin: User, salon_id: uuid.UUID) -> None:
    """Remove a salon that has no bookings and no stylists. Its services go with it."""
    salon = await _lock_salon(session, salon_id)
    has_bookings = await session.scalar(
        select(
            exists().where(
                Booking.service_id == Service.id, Service.salon_id == salon.id
            )
        )
    )
    if has_bookings:
        await session.rollback()
        raise _conflict("salon has bookings")
    if await session.scalar(select(exists().where(User.salon_id == salon.id))):
        await session.rollback()
        raise _conflict("salon still has stylists")
    session.add(
        _audit(admin, AdminAction.SALON_DELETED, AuditTargetType.SALON, salon.id)
    )
    await session.delete(salon)
    try:
        await session.commit()
    except IntegrityError:
        # A booking or stylist slipped in between the checks and the delete.
        await session.rollback()
        raise _conflict("salon has bookings or stylists") from None


async def list_users(
    session: AsyncSession, limit: int, offset: int, role: UserRole | None = None
) -> list[User]:
    stmt = select(User).order_by(User.created_at, User.id).limit(limit).offset(offset)
    if role is not None:
        stmt = stmt.where(User.role == role)
    return list((await session.execute(stmt)).scalars())


async def delete_user(session: AsyncSession, admin: User, user_id: uuid.UUID) -> None:
    """Remove an account with no bookings and no salon. Admins can't be removed here."""
    user = (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise _not_found("user")
    if user.id == admin.id:
        await session.rollback()
        raise _conflict("you cannot delete your own account")
    if user.role == UserRole.ADMIN:
        await session.rollback()
        raise _conflict("admin accounts cannot be deleted")
    owns_salon = await session.scalar(select(exists().where(Salon.owner_id == user.id)))
    if owns_salon or user.salon_id is not None:
        await session.rollback()
        raise _conflict("user has a salon")
    has_bookings = await session.scalar(
        select(
            exists().where(
                (Booking.client_id == user.id) | (Booking.stylist_id == user.id)
            )
        )
    )
    if has_bookings:
        await session.rollback()
        raise _conflict("user has bookings")
    session.add(_audit(admin, AdminAction.USER_DELETED, AuditTargetType.USER, user.id))
    await session.delete(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _conflict("user has bookings or a salon") from None
