import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, Response, status

from app.api.deps import PaginationDep, SessionDep, SessionMakerDep, require_role
from app.models.salon import SalonStatus
from app.models.user import User, UserRole
from app.schemas.admin import (
    AdminSalonItem,
    AdminSalonStatusResponse,
    AdminUserItem,
    SuspendRequest,
)
from app.services import admin as admin_service
from app.services.email.base import EmailSender
from app.services.email.brevo import get_email_sender
from app.services.email.salon_status import notify_salon_status_changed

# Admin only, on every route. The router-level dependency means a route added here later
# can't be left open by forgetting the per-route one.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_role(UserRole.ADMIN))],
)

Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]


def _email_owner(
    background_tasks: BackgroundTasks,
    session_maker: SessionMakerDep,
    email_sender: EmailSender | None,
    outcome: admin_service.StatusOutcome,
) -> None:
    # Only the call that actually flipped the status emails; a repeat sends nothing.
    if outcome.changed:
        background_tasks.add_task(
            notify_salon_status_changed,
            session_maker,
            email_sender,
            outcome.salon.id,
            outcome.salon.status,
        )


@router.get("/salons", response_model=list[AdminSalonItem])
async def list_salons(
    session: SessionDep,
    admin: Admin,
    page: PaginationDep,
    salon_status: Annotated[SalonStatus | None, Query(alias="status")] = None,
) -> list[AdminSalonItem]:
    return await admin_service.list_salons(
        session, page.limit, page.offset, salon_status
    )


@router.post("/salons/{salon_id}/approve", response_model=AdminSalonStatusResponse)
async def approve_salon(
    salon_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    session_maker: SessionMakerDep,
    email_sender: Annotated[EmailSender | None, Depends(get_email_sender)],
    admin: Admin,
) -> AdminSalonStatusResponse:
    outcome = await admin_service.set_salon_status(
        session, admin, salon_id, SalonStatus.APPROVED
    )
    _email_owner(background_tasks, session_maker, email_sender, outcome)
    return AdminSalonStatusResponse.model_validate(outcome.salon)


@router.post("/salons/{salon_id}/suspend", response_model=AdminSalonStatusResponse)
async def suspend_salon(
    salon_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    session: SessionDep,
    session_maker: SessionMakerDep,
    email_sender: Annotated[EmailSender | None, Depends(get_email_sender)],
    admin: Admin,
    data: Annotated[SuspendRequest | None, Body()] = None,
) -> AdminSalonStatusResponse:
    reason = data.reason if data is not None else None
    outcome = await admin_service.set_salon_status(
        session, admin, salon_id, SalonStatus.SUSPENDED, reason
    )
    _email_owner(background_tasks, session_maker, email_sender, outcome)
    return AdminSalonStatusResponse.model_validate(outcome.salon)


@router.delete("/salons/{salon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_salon(
    salon_id: uuid.UUID, session: SessionDep, admin: Admin
) -> Response:
    await admin_service.delete_salon(session, admin, salon_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", response_model=list[AdminUserItem])
async def list_users(
    session: SessionDep,
    admin: Admin,
    page: PaginationDep,
    role: Annotated[UserRole | None, Query()] = None,
) -> list[AdminUserItem]:
    users = await admin_service.list_users(session, page.limit, page.offset, role)
    return [AdminUserItem.model_validate(u) for u in users]


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID, session: SessionDep, admin: Admin
) -> Response:
    await admin_service.delete_user(session, admin, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
