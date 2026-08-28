from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import SetupLock, User
from ..schemas import SetupIn, SetupStatusOut, UserOut
from ..security import create_token, hash_password

router = APIRouter(prefix="/api/setup", tags=["setup"])

#: Both routes below run before anyone can possibly be signed in — that is the
#: whole point of them — so, unlike every other router in this file, neither
#: depends on current_user or current_admin. GET only ever reveals whether a
#: user exists anywhere, which is already inferable from whether /login works,
#: and POST refuses itself the instant a user exists (see below).


async def _needs_setup(db: AsyncSession) -> bool:
    return not await db.scalar(select(func.count(User.id)))


@router.get("/status", response_model=SetupStatusOut)
async def setup_status(db: AsyncSession = Depends(get_db)):
    return SetupStatusOut(needs_setup=await _needs_setup(db))


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def complete_setup(
    payload: SetupIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Create the first admin account, exactly once.

    A plain "no users exist yet, so create one" is a check-then-act race: two
    submissions in flight at the same moment can both pass the check before
    either has committed. The check below is only a fast, friendly rejection
    for the ordinary case (an instance that already has users, which is most
    of the time this endpoint is ever called at all); the thing that actually
    makes concurrent submissions safe is the `SetupLock` insert that follows
    it. That row has a fixed primary key, so of any number of transactions
    racing to insert it, the database guarantees exactly one succeeds — the
    others fail on the spot with an integrity error, before they ever get to
    add a User row. See models.SetupLock for the full reasoning.
    """
    if not await _needs_setup(db):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")

    username = payload.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username must not be empty")

    db.add(SetupLock(id=1))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup has already been completed")

    if await db.scalar(select(User.id).where(User.username == username)):
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken")

    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        is_admin=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # Log the new admin in directly, the same way /api/auth/login does, rather
    # than sending them back to a login screen to retype what they just typed.
    response.set_cookie(
        settings.cookie_name,
        create_token(user),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_ttl_hours * 3600,
        path="/",
    )
    return user
