import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import (
    CurrentUser,
    PaginationDep,
    SessionDep,
    get_current_user,
    require_role,
)
from app.models.availability_rule import AvailabilityRule
from app.models.user import User, UserRole
from app.schemas.availability import (
    AvailabilityResponse,
    AvailabilityRuleResponse,
    AvailabilityUpdate,
    SlotsResponse,
)
from app.schemas.stylist import (
    PublicStylistResponse,
    StylistCreate,
    StylistResponse,
    StylistServicesResponse,
    StylistServicesUpdate,
)
from app.services import availability as availability_service
from app.services import slots as slot_service
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


def _availability_response(
    stylist_id: uuid.UUID, rules: list[AvailabilityRule]
) -> AvailabilityResponse:
    return AvailabilityResponse(
        stylist_id=stylist_id,
        rules=[AvailabilityRuleResponse.model_validate(r) for r in rules],
    )


# The stylist themself or their salon's owner (checked in the service).
@router.get("/stylists/{stylist_id}/availability", response_model=AvailabilityResponse)
async def get_availability(
    stylist_id: uuid.UUID, session: SessionDep, user: CurrentUser
) -> AvailabilityResponse:
    rules = await availability_service.get_availability(session, stylist_id, user)
    return _availability_response(stylist_id, rules)


@router.put("/stylists/{stylist_id}/availability", response_model=AvailabilityResponse)
async def set_availability(
    stylist_id: uuid.UUID,
    data: AvailabilityUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> AvailabilityResponse:
    rules = await availability_service.set_availability(
        session, stylist_id, user, data.rules
    )
    return _availability_response(stylist_id, rules)


# Any signed-in user: it only reveals free start times, not who booked them.
@router.get(
    "/stylists/{stylist_id}/slots",
    response_model=SlotsResponse,
    dependencies=[Depends(get_current_user)],
)
async def get_slots(
    stylist_id: uuid.UUID,
    session: SessionDep,
    service_id: Annotated[uuid.UUID, Query()],
    day: Annotated[date, Query(alias="date")],
) -> SlotsResponse:
    slots = await slot_service.get_slots(session, stylist_id, service_id, day)
    return SlotsResponse(stylist_id=stylist_id, service_id=service_id, slots=slots)
