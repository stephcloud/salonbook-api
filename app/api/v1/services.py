import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import SessionDep, require_role
from app.models.user import User, UserRole
from app.schemas.service import ServiceResponse, ServiceUpdate
from app.services import services as service_service

router = APIRouter(prefix="/services", tags=["services"])

Owner = Annotated[User, Depends(require_role(UserRole.OWNER))]


@router.patch("/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: uuid.UUID, data: ServiceUpdate, session: SessionDep, owner: Owner
) -> ServiceResponse:
    service = await service_service.update_service(session, service_id, owner, data)
    return ServiceResponse.model_validate(service)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: uuid.UUID, session: SessionDep, owner: Owner
) -> Response:
    await service_service.delete_service(session, service_id, owner)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
