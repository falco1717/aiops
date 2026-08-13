from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .db import get_db
from .models import User

ALGORITHM = "HS256"

# Paths a user with a forced password change may still reach.
PASSWORD_CHANGE_EXEMPT = ("/api/auth/",)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.jwt_ttl_hours)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


async def current_user(
    request: Request,
    token: str | None = Cookie(default=None, alias=settings.cookie_name),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Dependency for every authenticated endpoint."""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    user = await db.scalar(select(User).where(User.id == int(payload["sub"])))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")

    # A forced password change is enforced here rather than in the UI alone, so
    # it cannot be skipped by calling the API directly.
    if user.must_change_password and not request.url.path.startswith(PASSWORD_CHANGE_EXEMPT):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You must change your password before using AIOps",
        )
    return user


async def current_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This action requires an administrator account"
        )
    return user


async def user_from_token(token: str | None, db: AsyncSession) -> User | None:
    """Websocket-friendly variant: returns None instead of raising."""
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = await db.scalar(select(User).where(User.id == int(payload["sub"])))
    if user is None or user.must_change_password:
        return None
    return user
