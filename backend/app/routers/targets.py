from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import LEVELS, level_for
from ..crypto import encrypt, is_configured
from ..db import get_db
from ..models import Target, TargetAccess, User
from ..schemas import TargetGrant, TargetIn, TargetOut, TargetPatch
from ..security import current_user

router = APIRouter(prefix="/api/targets", tags=["targets"])

AUTH_TYPES = ("key", "password")
HOST_KEY_POLICIES = ("accept-new", "strict")


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "target"


def _require(target: Target, user: User, *, manage: bool) -> str:
    level = level_for(target, user)
    # A system the caller cannot see must 404, not 403: "you may not touch this"
    # still confirms that a host by that name exists and who might own it.
    if level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
    if manage and level not in ("owner", "manage"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can use this system but not change it. Ask its owner for manage access.",
        )
    return level


def _require_key() -> None:
    if not is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AIOPS_SECRET_KEY is not set on the server, so credentials cannot be stored. "
            "Set it in the server's .env and restart.",
        )


def _out(target: Target, level: str) -> TargetOut:
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
        owner_id=target.owner_id,
        grants=[TargetGrant(user_id=g.user_id, level=g.level) for g in target.grants],
        my_level=level,
        created_at=target.created_at,
    )


def _validate(auth_type: str | None, policy: str | None, grants: list[TargetGrant] | None) -> None:
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
    for grant in grants or []:
        if grant.level not in LEVELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Access level must be one of {', '.join(LEVELS)} (got {grant.level!r})",
            )


async def _apply_grants(
    db: AsyncSession, target: Target, grants: list[TargetGrant] | None, owner: User
) -> None:
    if grants is None:
        return
    for existing in list(target.grants):
        await db.delete(existing)
    target.grants = []
    await db.flush()
    seen: set[int] = set()
    for grant in grants:
        # Granting the owner would create a second, weaker claim on their own
        # system, which the level lookup would never reach anyway.
        if grant.user_id in seen or grant.user_id == (target.owner_id or owner.id):
            continue
        seen.add(grant.user_id)
        db.add(TargetAccess(target_id=target.id, user_id=grant.user_id, level=grant.level))


@router.get("", response_model=list[TargetOut])
async def list_targets(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Only what this user owns or has been granted — nothing else exists to them."""
    rows = await db.scalars(
        select(Target)
        .outerjoin(TargetAccess, TargetAccess.target_id == Target.id)
        .where(or_(Target.owner_id == user.id, TargetAccess.user_id == user.id))
        .order_by(Target.name)
        .distinct()
    )
    out = []
    for target in rows:
        level = level_for(target, user)
        if level is not None:
            out.append(_out(target, level))
    return out


@router.post("", response_model=TargetOut, status_code=status.HTTP_201_CREATED)
async def create_target(
    payload: TargetIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Anyone may store a system; it belongs to them until they share it."""
    _require_key()
    _validate(payload.auth_type, payload.host_key_policy, payload.grants)

    slug = _slugify(payload.name)
    # Names are global because the slug is what an agent types as `ssh <slug>`,
    # and two people's "prod" resolving differently per session would be a trap.
    if await db.scalar(select(Target).where((Target.name == payload.name) | (Target.slug == slug))):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A system with that name already exists. Names are shared across AIOps "
            "because agents connect by name.",
        )

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
        owner_id=user.id,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    await _apply_grants(db, target, payload.grants, user)
    await db.commit()
    await db.refresh(target)
    return _out(target, "owner")


@router.patch("/{target_id}", response_model=TargetOut)
async def update_target(
    target_id: int,
    payload: TargetPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    target = await db.get(Target, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
    level = _require(target, user, manage=True)
    _validate(payload.auth_type, payload.host_key_policy, payload.grants)

    data = payload.model_dump(exclude_unset=True)

    new_owner = data.pop("owner_id", None)
    if new_owner is not None and new_owner != target.owner_id:
        # Handing it on is the owner's decision alone; a manager who could
        # reassign it could take it from under them.
        if level != "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only the owner can hand this system to someone else"
            )
        if await db.get(User, new_owner) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That user does not exist")
        target.owner_id = new_owner

    # Secrets are write-only and only touched when explicitly sent, so an
    # ordinary edit cannot wipe a stored key.
    for field, column in (
        ("private_key", "private_key_enc"),
        ("passphrase", "passphrase_enc"),
        ("password", "password_enc"),
    ):
        if field in data:
            _require_key()
            setattr(target, column, encrypt(data.pop(field)))

    grants = data.pop("grants", None)
    if "name" in data and data["name"]:
        data["slug"] = _slugify(data["name"])
    for key, value in data.items():
        if value is not None:
            setattr(target, key, value)

    await _apply_grants(
        db, target, [TargetGrant(**g) for g in grants] if grants is not None else None, user
    )
    await db.commit()
    await db.refresh(target)
    return _out(target, level_for(target, user) or level)


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_target(
    target_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    target = await db.get(Target, target_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "System not found")
    _require(target, user, manage=True)
    await db.delete(target)
    await db.commit()
