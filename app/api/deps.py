import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import decode_access_token
from app.db.session import get_session, get_session_maker
from app.models.user import User, UserRole
from app.services.auth import invalid_credentials

bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SessionMakerDep = Annotated[
    async_sessionmaker[AsyncSession], Depends(get_session_maker)
]


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None:
        raise invalid_credentials()
    user_id: uuid.UUID | None = decode_access_token(credentials.credentials)
    if user_id is None:
        raise invalid_credentials()
    user = await session.get(User, user_id)
    if user is None:
        raise invalid_credentials()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    allowed = set(roles)

    async def role_checker(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="forbidden"
            )
        return user

    return role_checker


@dataclass
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> Pagination:
    return Pagination(limit=limit, offset=offset)


PaginationDep = Annotated[Pagination, Depends(get_pagination)]
