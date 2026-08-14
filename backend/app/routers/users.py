from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..models import (
    RelayNode,
    RelayNodeAccess,
    Session,
    SessionShare,
    Target,
    TargetAccess,
    TeamMember,
    User,
)
from ..schemas import UserCreate, UserOut, UserPasswordReset, UserPatch, UserSummary
from ..security import current_admin, current_user, hash_password

router = APIRouter(prefix="/api/users", tags=["users"])


async def _admin_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True))) or 0


@router.get("", response_model=list[UserOut])
async def list_users(_: User = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    return list(await db.scalars(select(User).order_by(User.id)))


@router.get("/directory", response_model=list[UserSummary])
async def directory(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Names to share things with, for anyone signed in.

    Sharing is not an admin action — whoever stores a system decides who else
    may reach it — so it cannot depend on the admin-only listing above. This
    exposes usernames and nothing else: no roles, no timestamps, no password
    state.
    """
    rows = await db.scalars(select(User).order_by(User.username))
    return [UserSummary(id=u.id, username=u.username) for u in rows]


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
    await _hand_on_systems(db, user)
    await _release_sessions(db, user)
    await db.delete(user)
    await db.commit()


async def _release_sessions(db: AsyncSession, leaving: User) -> None:
    """Take a departing user out of everything that grants session access.

    The foreign keys say as much, but SQLite does not enforce ON DELETE and
    reuses integer ids, so a leftover share is a grant lying in wait for whoever
    is created next. Their own sessions are left ownerless rather than handed
    on: administrators can still reach them, which is exactly why they keep
    visibility of every session.
    """
    await db.execute(delete(SessionShare).where(SessionShare.user_id == leaving.id))
    await db.execute(delete(TeamMember).where(TeamMember.user_id == leaving.id))
    await db.execute(
        update(Session).where(Session.owner_id == leaving.id).values(owner_id=None)
    )


async def _hand_on_systems(db: AsyncSession, leaving: User) -> None:
    """Pass this user's stored systems and relay nodes to someone who can manage them.

    Admins get no implicit access to a stored credential, so a system left
    without an owner would be reachable by nobody and removable by nobody. The
    delete is refused rather than orphaning one — and refusing is also what
    stops deleting a user becoming a way to inherit their credentials.
    """
    # Grants this user held on other people's systems and nodes. The foreign
    # keys say cascade, and Postgres honours them, but SQLite enforces neither
    # that nor unique ids over time — so a leftover row is access lying in wait
    # for whoever is created next, and access to a stored credential or a route
    # into a network is the worst thing to inherit by accident.
    await db.execute(delete(TargetAccess).where(TargetAccess.user_id == leaving.id))
    await db.execute(delete(RelayNodeAccess).where(RelayNodeAccess.user_id == leaving.id))
    # Relay nodes are owned and shared on exactly the same terms, so a node
    # whose owner leaves strands a route into a network the same way.
    owned = list(
        await db.scalars(select(Target).where(Target.owner_id == leaving.id))
    ) + list(await db.scalars(select(RelayNode).where(RelayNode.owner_id == leaving.id)))
    stranded: list[str] = []
    for target in owned:
        heir = next((g for g in target.grants if g.level == "manage"), None)
        if heir is None:
            stranded.append(target.name)
            continue
        target.owner_id = heir.user_id
        await db.delete(heir)
    # Nothing above is committed if this fires, so the refusal leaves the user
    # and every grant exactly as they were.
    if stranded:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{leaving.username} owns {len(stranded)} system(s) or relay node(s) nobody "
            f"else can manage: "
            f"{', '.join(sorted(stranded)[:5])}"
            + (" …" if len(stranded) > 5 else "")
            + ". Give someone manage access, or delete them, before removing this user.",
        )
