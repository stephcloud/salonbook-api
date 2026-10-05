import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import PaginationDep, SessionDep, require_role
from app.models.user import User, UserRole
from app.schemas.stylist import (
    PublicStylistResponse,
    StylistCreate,
    StylistResponse,
    StylistServicesResponse,
    StylistServicesUpdate,
)
from app.services import stylists as stylist_service

router = APIRouter(tags=["stylists"])

Owner = Annotated[User, Depends(require_role(UserRole.OWNER))]


@router.post(
    "/salons/{salon_id}/stylists",
    response_model=StylistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_stylist(
    salon_id: uuid.UUID, data: StylistCreate, session: SessionDep, owner: Owner
) -> StylistResponse:
    stylist = await stylist_service.create_stylist(session, salon_id, owner, data)
    return StylistResponse.model_validate(stylist)


# Public: only id, name and service ids (no personal data).
@router.get("/salons/{salon_id}/stylists", response_model=list[PublicStylistResponse])
async def list_stylists(
    salon_id: uuid.UUID, session: SessionDep, page: PaginationDep
) -> list[PublicStylistResponse]:
    return await stylist_service.list_salon_stylists(
        session, salon_id, page.limit, page.offset
    )


@router.put("/stylists/{stylist_id}/services", response_model=StylistServicesResponse)
async def set_stylist_services(
    stylist_id: uuid.UUID,
    data: StylistServicesUpdate,
    session: SessionDep,
    owner: Owner,
) -> StylistServicesResponse:
    service_ids = await stylist_service.set_stylist_services(
        session, stylist_id, owner, data.service_ids
    )
    return StylistServicesResponse(stylist_id=stylist_id, service_ids=service_ids)
