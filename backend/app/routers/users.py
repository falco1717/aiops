from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import User
from ..schemas import UserCreate, UserOut, UserPasswordReset, UserPatch
from ..security import current_admin, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


async def _admin_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True))) or 0


@router.get("", response_model=list[UserOut])
async def list_users(_: User = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(User).order_by(User.id)))


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    username = payload.username.strip()
    if not username:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Username must not be empty")
    if await db.scalar(select(User).where(User.username == username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "That username is already taken")

    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        is_admin=payload.is_admin,
        must_change_password=payload.must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(
    user_id: int,
    payload: UserPatch,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    data = payload.model_dump(exclude_unset=True)
    # Removing the last admin would leave nobody able to manage users or sign
    # the agent CLIs in.
    if data.get("is_admin") is False and user.is_admin and await _admin_count(db) <= 1:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This is the only admin — promote someone else first"
        )
    if data.get("is_admin") is False and user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot remove your own admin rights")

    for key, value in data.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int,
    payload: UserPasswordReset,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin reset. The target must change it at next sign-in unless told otherwise."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = payload.must_change_password
    await db.commit()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    admin: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot delete your own account")
    if user.is_admin and await _admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot delete the only admin")
    await db.delete(user)
    await db.commit()
