import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.service import Service
from app.models.user import User
from app.schemas.service import ServiceCreate, ServiceUpdate, check_price_matches_type
from app.services.salons import get_owned_salon, get_salon

FOREIGN_KEY_VIOLATION = "23503"


async def create_service(
    session: AsyncSession, salon_id: uuid.UUID, user: User, data: ServiceCreate
) -> Service:
    await get_owned_salon(session, salon_id, user)
    service = Service(salon_id=salon_id, **data.model_dump())
    session.add(service)
    await session.commit()
    await session.refresh(service)
    return service


async def list_services(
    session: AsyncSession, salon_id: uuid.UUID, limit: int, offset: int
) -> list[Service]:
    await get_salon(session, salon_id)
    result = await session.execute(
        select(Service)
        .where(Service.salon_id == salon_id)
        .order_by(Service.created_at, Service.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


async def _get_owned_service(
    session: AsyncSession, service_id: uuid.UUID, user: User
) -> Service:
    service = await session.get(Service, service_id)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="service not found"
        )
    await get_owned_salon(session, service.salon_id, user)
    return service


async def update_service(
    session: AsyncSession, service_id: uuid.UUID, user: User, data: ServiceUpdate
) -> Service:
    service = await _get_owned_service(session, service_id, user)
    changes = data.model_dump(exclude_unset=True)
    # An explicit `price: null` is meaningful (switching to quote); other nulls are not.
    changes = {k: v for k, v in changes.items() if v is not None or k == "price"}

    price_type = changes.get("price_type", service.price_type)
    price = changes.get("price", service.price)
    try:
        check_price_matches_type(price_type, price)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from None

    for field, value in changes.items():
        setattr(service, field, value)
    session.add(service)
    await session.commit()
    await session.refresh(service)
    return service


async def delete_service(
    session: AsyncSession, service_id: uuid.UUID, user: User
) -> None:
    """Delete a service, unless a booking still references it (409).

    Bookings keep their service (`ON DELETE RESTRICT`) so history stays intact. The
    database decides, so a booking created at the same moment can't slip past a check.
    """
    service = await _get_owned_service(session, service_id, user)
    try:
        await session.delete(service)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) != FOREIGN_KEY_VIOLATION:
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This service has bookings and cannot be deleted.",
        ) from None
