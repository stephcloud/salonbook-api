import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import SessionDep, require_role
from app.models.user import User, UserRole
from app.schemas.booking import BookingCreate, BookingResponse
from app.services import bookings as booking_service

router = APIRouter(prefix="/bookings", tags=["bookings"])

Client = Annotated[User, Depends(require_role(UserRole.CLIENT))]
ClientOrOwner = Annotated[User, Depends(require_role(UserRole.CLIENT, UserRole.OWNER))]


@router.post("", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_booking(
    data: BookingCreate, session: SessionDep, client: Client
) -> BookingResponse:
    booking = await booking_service.create_booking(session, client, data)
    return BookingResponse.model_validate(booking)


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: uuid.UUID, session: SessionDep, user: ClientOrOwner
) -> BookingResponse:
    booking = await booking_service.cancel_booking(session, booking_id, user)
    return BookingResponse.model_validate(booking)
