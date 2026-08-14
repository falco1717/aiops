from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import encrypt, is_configured
from ..db import get_db
from ..models import Target, TargetAccess, User
from ..schemas import TargetIn, TargetOut, TargetPatch
from ..security import current_user

router = APIRouter(prefix="/api/targets", tags=["targets"])

AUTH_TYPES = ("key", "password")
HOST_KEY_POLICIES = ("accept-new", "strict")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "target"


def _admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only an administrator can manage stored systems"
        )


def _require_key() -> None:
    if not is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AIOPS_SECRET_KEY is not set on the server, so credentials cannot be stored. "
            "Set it in the server's .env and restart.",
        )


def _out(target: Target, user: User) -> TargetOut:
    granted = [g.user_id for g in target.grants]
    return TargetOut(
        id=target.id,
        name=target.name,
        slug=target.slug,
        hostname=target.hostname,
        port=target.port,
        username=target.username,
        description=target.description,
        auth_type=target.auth_type,
        has_private_key=bool(target.private_key_enc),
        has_passphrase=bool(target.passphrase_enc),
        has_password=bool(target.password_enc),
        host_key_policy=target.host_key_policy,
        has_known_host_key=bool(target.known_host_key),
        allowed_user_ids=granted,
        usable_by_me=user.is_admin or not granted or user.id in granted,
        created_at=target.created_at,
    )


def _validate(auth_type: str | None, policy: str | None) -> None:
    if auth_type is not None and auth_type not in AUTH_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"auth_type must be one of {', '.join(AUTH_TYPES)}"
        )
    if policy is not None and policy not in HOST_KEY_POLICIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"host_key_policy must be one of {', '.join(HOST_KEY_POLICIES)}. "
            "Disabling host key checking entirely is not offered.",
        )


async def _apply_grants(db: AsyncSession, target: Target, user_ids: list[int] | None) -> None:
    if user_ids is None:
        return
    for grant in list(target.grants):
        await db.delete(grant)
    target.grants = []
    for user_id in dict.fromkeys(user_ids):
        db.add(TargetAccess(target_id=target.id, user_id=user_id))


@router.get("", response_model=list[TargetOut])
async def list_targets(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Every stored system. Non-admins see them but only use what they are granted."""
    rows = await db.scalars(select(Target).order_by(Target.name))
    return [_out(target, user) for target in rows]


@router.post("", response_model=TargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: TargetIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    _admin(user)
    _require_key()
    _validate(payload.auth_type, payload.host_key_policy)

    slug = _slugify(payload.name)
    if await db.scalar(select(Target).where((Target.name == payload.name) | (Target.slug == slug))):
        raise HTTPException(status.HTTP_409_CONFLICT, "A system with that name already exists")

    target = Target(
        name=payload.name.strip(),
        slug=slug,
        hostname=payload.hostname.strip(),
        port=payload.port,
        username=payload.username.strip(),
        description=payload.description,
        auth_type=payload.auth_type,
        private_key_enc=encrypt(payload.private_key),
        passphrase_enc=encrypt(payload.passphrase),
        password_enc=encrypt(payload.password),
        host_key_policy=payload.host_key_policy,
        known_host_key=payload.known_host_key,
        created_by_id=user.id,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    await _apply_grants(db, target, payload.allowed_user_ids)
    await db.commit()
    await db.refresh(target)
    return _out(target, user)


@router.patch("/{target_id}", response_model=TargetOut)
async def update_target(
    target_id: int,
    payload: TargetPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    _admin(user)
    target = await db.get(Target, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
    _validate(payload.auth_type, payload.host_key_policy)

    data = payload.model_dump(exclude_unset=True)
    # Secrets are write-only and are only touched when explicitly sent, so an
    # ordinary edit (renaming, changing the port) cannot wipe a stored key.
    for field, column in (
        ("private_key", "private_key_enc"),
        ("passphrase", "passphrase_enc"),
        ("password", "password_enc"),
    ):
        if field in data:
            _require_key()
            setattr(target, column, encrypt(data.pop(field)))

    grants = data.pop("allowed_user_ids", None)
    if "name" in data and data["name"]:
        data["slug"] = _slugify(data["name"])
    for key, value in data.items():
        if value is not None:
            setattr(target, key, value)

    await _apply_grants(db, target, grants)
    await db.commit()
    await db.refresh(target)
    return _out(target, user)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    _admin(user)
    target = await db.get(Target, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
    await db.delete(target)
    await db.commit()
