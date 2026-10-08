import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.api.deps import PaginationDep, SessionDep, SessionMakerDep, require_role
from app.models.user import User, UserRole
from app.schemas.booking import BookingCreate, BookingDetailResponse, BookingResponse
from app.schemas.payment import PaymentResponse
from app.services import bookings as booking_service
from app.services import payments as payment_service
from app.services.paystack import PaystackClient, get_paystack_client

router = APIRouter(prefix="/bookings", tags=["bookings"])

Client = Annotated[User, Depends(require_role(UserRole.CLIENT))]
ClientOrOwner = Annotated[User, Depends(require_role(UserRole.CLIENT, UserRole.OWNER))]


@router.post("", response_model=BookingResponse, status_code=status.HTTP_201_CREATED)
async def create_booking(
    data: BookingCreate, session: SessionDep, client: Client
) -> BookingResponse:
    booking = await booking_service.create_booking(session, client, data)
    return BookingResponse.model_validate(booking)


@router.get("", response_model=list[BookingResponse])
async def list_my_bookings(
    session: SessionDep, client: Client, page: PaginationDep
) -> list[BookingResponse]:
    bookings = await booking_service.list_client_bookings(
        session, client, page.limit, page.offset
    )
    return [BookingResponse.model_validate(b) for b in bookings]


@router.get("/{booking_id}", response_model=BookingDetailResponse)
async def get_booking(
    booking_id: uuid.UUID, session: SessionDep, user: ClientOrOwner
) -> BookingDetailResponse:
    booking, payment = await booking_service.get_booking_detail(
        session, booking_id, user
    )
    return BookingDetailResponse(
        **BookingResponse.model_validate(booking).model_dump(),
        payment_status=payment.status if payment else None,
        payment_amount=payment.amount if payment else None,
    )


@router.post("/{booking_id}/pay", response_model=PaymentResponse)
async def pay_booking(
    booking_id: uuid.UUID,
    session: SessionDep,
    client: Client,
    paystack: Annotated[PaystackClient, Depends(get_paystack_client)],
) -> PaymentResponse:
    payment = await payment_service.start_payment(session, paystack, client, booking_id)
    return PaymentResponse.model_validate(payment)


@router.post("/{booking_id}/cancel", response_model=BookingResponse)
async def cancel_booking(
    booking_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    session_maker: SessionMakerDep,
    paystack: Annotated[PaystackClient, Depends(get_paystack_client)],
    user: ClientOrOwner,
) -> BookingResponse:
    booking, refund_ids = await payment_service.cancel_booking_and_list_refunds(
        session, booking_id, user
    )
    # The cancel has committed. Send any queued refund now, outside every lock; if
    # Paystack is down the retry job picks it up.
    for payment_id in refund_ids:
        background_tasks.add_task(
            payment_service.try_refund, session_maker, paystack, payment_id
        )
    return BookingResponse.model_validate(booking)
