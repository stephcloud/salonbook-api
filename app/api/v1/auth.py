from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.core.security import create_access_token
from app.schemas.user import Token, UserCreate, UserLogin, UserResponse
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED
)
async def register(data: UserCreate, session: SessionDep) -> UserResponse:
    user = await auth_service.register_user(session, data)
    return UserResponse.model_validate(user)


@router.post("/login", response_model=Token)
async def login(data: UserLogin, session: SessionDep) -> Token:
    user = await auth_service.authenticate_user(session, data.email, data.password)
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserResponse)
async def me(current_user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(current_user)
