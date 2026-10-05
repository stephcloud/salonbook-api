import asyncio

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import DUMMY_HASH, hash_password, verify_password
from app.models.user import User, UserRole
from app.schemas.user import UserCreate


def invalid_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email.lower()))
    return result.scalar_one_or_none()


async def register_user(session: AsyncSession, data: UserCreate) -> User:
    email = data.email.lower()
    if await get_user_by_email(session, email):
        raise _email_taken()
    user = User(
        name=data.name,
        email=email,
        password_hash=await asyncio.to_thread(hash_password, data.password),
        role=UserRole(data.role),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _email_taken() from None
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, email: str, password: str) -> User:
    user = await get_user_by_email(session, email)
    if user is None:
        await asyncio.to_thread(verify_password, password, DUMMY_HASH)
        raise invalid_credentials()
    if not await asyncio.to_thread(verify_password, password, user.password_hash):
        raise invalid_credentials()
    return user


def _email_taken() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="email already registered"
    )
