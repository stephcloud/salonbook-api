import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import PaginationDep, SessionDep, require_role
from app.models.booking import BookingStatus
from app.models.user import User, UserRole
from app.schemas.booking import OwnerBookingItem
from app.schemas.salon import SalonCreate, SalonResponse, SalonUpdate
from app.schemas.service import ServiceCreate, ServiceResponse
from app.services import bookings as booking_service
from app.services import salons as salon_service
from app.services import services as service_service

router = APIRouter(prefix="/salons", tags=["salons"])

Owner = Annotated[User, Depends(require_role(UserRole.OWNER))]


@router.post("", response_model=SalonResponse, status_code=status.HTTP_201_CREATED)
async def create_salon(
    data: SalonCreate, session: SessionDep, owner: Owner
) -> SalonResponse:
    salon = await salon_service.create_salon(session, owner, data)
    return SalonResponse.model_validate(salon)


# Public: salon listings are read-only discovery data.
@router.get("", response_model=list[SalonResponse])
async def list_salons(session: SessionDep, page: PaginationDep) -> list[SalonResponse]:
    salons = await salon_service.list_salons(session, page.limit, page.offset)
    return [SalonResponse.model_validate(s) for s in salons]


# The signed-in owner's own salons. Declared before "/{salon_id}" so "mine" is not
# parsed as an id.
@router.get("/mine", response_model=list[SalonResponse])
async def list_my_salons(
    session: SessionDep, owner: Owner, page: PaginationDep
) -> list[SalonResponse]:
    salons = await salon_service.list_owned_salons(
        session, owner, page.limit, page.offset
    )
    return [SalonResponse.model_validate(s) for s in salons]


# Public.
@router.get("/{salon_id}", response_model=SalonResponse)
async def get_salon(salon_id: uuid.UUID, session: SessionDep) -> SalonResponse:
    salon = await salon_service.get_salon(session, salon_id)
    return SalonResponse.model_validate(salon)


@router.patch("/{salon_id}", response_model=SalonResponse)
async def update_salon(
    salon_id: uuid.UUID, data: SalonUpdate, session: SessionDep, owner: Owner
) -> SalonResponse:
    salon = await salon_service.update_salon(session, salon_id, owner, data)
    return SalonResponse.model_validate(salon)


@router.post(
    "/{salon_id}/services",
    response_model=ServiceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_service(
    salon_id: uuid.UUID, data: ServiceCreate, session: SessionDep, owner: Owner
) -> ServiceResponse:
    service = await service_service.create_service(session, salon_id, owner, data)
    return ServiceResponse.model_validate(service)


# Public.
@router.get("/{salon_id}/services", response_model=list[ServiceResponse])
async def list_services(
    salon_id: uuid.UUID, session: SessionDep, page: PaginationDep
) -> list[ServiceResponse]:
    services = await service_service.list_services(
        session, salon_id, page.limit, page.offset
    )
    return [ServiceResponse.model_validate(s) for s in services]


# Owner of this salon only (checked in the service).
@router.get("/{salon_id}/bookings", response_model=list[OwnerBookingItem])
async def list_salon_bookings(
    salon_id: uuid.UUID,
    session: SessionDep,
    owner: Owner,
    page: PaginationDep,
    booking_status: Annotated[BookingStatus | None, Query(alias="status")] = None,
    stylist_id: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> list[OwnerBookingItem]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="'from' must not be after 'to'",
        )
    return await booking_service.list_salon_bookings(
        session,
        salon_id,
        owner,
        page.limit,
        page.offset,
        booking_status=booking_status,
        stylist_id=stylist_id,
        date_from=date_from,
        date_to=date_to,
    )
