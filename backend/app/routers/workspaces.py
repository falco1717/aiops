from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import agent_env
from ..access import LEVELS, workspace_level_for
from ..config import settings
from ..db import get_db
from ..models import User, Workspace, WorkspaceAccess
from ..schemas import (
    WorkspaceGrant,
    WorkspaceIn,
    WorkspaceOut,
    WorkspacePatch,
    WorkspaceStatus,
)
from ..security import current_user

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


def resolve_workspace_path(raw: str) -> str:
    """Confine every workspace to the configured root.

    Agents run arbitrary commands inside these directories, so the root is the
    only thing standing between a mistyped path and the rest of the filesystem.
    """
    root = os.path.realpath(settings.workspace_root)
    candidate = raw if os.path.isabs(raw) else os.path.join(root, raw)
    resolved = os.path.realpath(candidate)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Workspace path must live under {root} (got {resolved})",
        )
    return resolved


async def _require(
    db: AsyncSession, workspace_id: int, user: User, *, manage: bool
) -> tuple[Workspace, str]:
    """The workspace and this user's level on it, or the right refusal.

    A workspace the caller cannot see must 404, not 403: "you may not touch
    this" still confirms that a project by that name is registered here and
    invites guessing at whose it is. The same rule and the same reason as a
    stored system.
    """
    ws = await db.get(Workspace, workspace_id)
    level = workspace_level_for(ws, user) if ws is not None else None
    if ws is None or level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    if manage and level not in ("owner", "manage"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can work in this workspace but not change it. Ask its owner for "
            "manage access.",
        )
    return ws, level


def _out(ws: Workspace, level: str) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        path=ws.path,
        description=ws.description,
        owner_id=ws.owner_id,
        grants=[WorkspaceGrant(user_id=g.user_id, level=g.level) for g in ws.grants],
        my_level=level,
        created_at=ws.created_at,
    )


def _validate(grants: list[WorkspaceGrant] | None) -> None:
    for grant in grants or []:
        if grant.level not in LEVELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Access level must be one of {', '.join(LEVELS)} (got {grant.level!r})",
            )


async def _apply_grants(
    db: AsyncSession, ws: Workspace, grants: list[WorkspaceGrant] | None, owner: User
) -> None:
    if grants is None:
        return
    for existing in list(ws.grants):
        await db.delete(existing)
    ws.grants = []
    await db.flush()
    seen: set[int] = set()
    for grant in grants:
        # Granting the owner would create a second, weaker claim on their own
        # workspace, which the level lookup would never reach anyway.
        if grant.user_id in seen or grant.user_id == (ws.owner_id or owner.id):
            continue
        seen.add(grant.user_id)
        db.add(WorkspaceAccess(workspace_id=ws.id, user_id=grant.user_id, level=grant.level))


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Only what this user owns or has been granted — nothing else exists to them.

    This is also what makes the session form honest: it offers exactly the
    workspaces you may point a session at, because it is built from this list.
    """
    rows = await db.scalars(
        select(Workspace)
        .where(
            or_(
                Workspace.owner_id == user.id,
                Workspace.id.in_(
                    select(WorkspaceAccess.workspace_id).where(
                        WorkspaceAccess.user_id == user.id
                    )
                ),
            )
        )
        .order_by(Workspace.name)
    )
    out = []
    for ws in rows:
        level = workspace_level_for(ws, user)
        if level is not None:
            out.append(_out(ws, level))
    return out


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Anyone may register a workspace; it belongs to them until they share it."""
    _validate(payload.grants)
    path = resolve_workspace_path(payload.path)
    # Names stay globally unique even though a workspace is now private. Two
    # people's "aiops-src" pointing at different checkouts would make every
    # conversation about which one a session runs in ambiguous, and the name is
    # what the session form shows.
    if await db.scalar(select(Workspace).where(Workspace.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A workspace with that name already exists")
    os.makedirs(path, exist_ok=True)
    # A workspace the app just created belongs to the app, and an agent runs as
    # somebody else: without this it would be handed a directory it cannot write
    # to. Recursive because registering a directory that already has a checkout
    # in it is the common case, not the exception.
    agent_env.grant_agent_access(path, writable=True)
    ws = Workspace(
        name=payload.name,
        path=path,
        description=payload.description,
        owner_id=user.id,
    )
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    await _apply_grants(db, ws, payload.grants, user)
    await db.commit()
    await db.refresh(ws)
    return _out(ws, "owner")


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: int,
    payload: WorkspacePatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    ws, level = await _require(db, workspace_id, user, manage=True)
    _validate(payload.grants)
    data = payload.model_dump(exclude_unset=True)

    new_owner = data.pop("owner_id", None)
    if new_owner is not None and new_owner != ws.owner_id:
        # Handing it on is the owner's decision alone; a manager who could
        # reassign it could take it from under them.
        if level != "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the owner can hand this workspace to someone else",
            )
        if await db.get(User, new_owner) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That user does not exist")
        ws.owner_id = new_owner

    grants = data.pop("grants", None)
    if data.get("name") and data["name"] != ws.name:
        clash = await db.scalar(select(Workspace).where(Workspace.name == data["name"]))
        if clash is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A workspace with that name already exists"
            )
    for key, value in data.items():
        if value is not None:
            setattr(ws, key, value)

    await _apply_grants(
        db, ws, [WorkspaceGrant(**g) for g in grants] if grants is not None else None, user
    )
    await db.commit()
    await db.refresh(ws)
    return _out(ws, workspace_level_for(ws, user) or level)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unregisters the workspace. The directory on disk is left untouched."""
    ws, _level = await _require(db, workspace_id, user, manage=True)
    await db.delete(ws)
    await db.commit()


@router.get("/{workspace_id}/status", response_model=WorkspaceStatus)
async def workspace_status(
    workspace_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    ws, _level = await _require(db, workspace_id, user, manage=False)
    if not os.path.isdir(ws.path):
        return WorkspaceStatus(exists=False, is_git=False, error="Directory does not exist")
    if not os.path.isdir(os.path.join(ws.path, ".git")):
        return WorkspaceStatus(exists=True, is_git=False)

    branch = await _git(ws.path, "rev-parse", "--abbrev-ref", "HEAD")
    head = await _git(ws.path, "rev-parse", "--short", "HEAD")
    porcelain = await _git(ws.path, "status", "--porcelain")
    return WorkspaceStatus(
        exists=True,
        is_git=True,
        branch=branch or None,
        head=head or None,
        dirty_files=[ln for ln in (porcelain or "").splitlines() if ln][:200],
    )


@router.get("/{workspace_id}/diff")
async def workspace_diff(
    workspace_id: int,
    staged: bool = False,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """What the agent changed and has not committed.

    Behind the same visibility check as everything else: a diff is the contents
    of somebody's checkout, which is the whole reason a workspace is owned.
    """
    ws, _level = await _require(db, workspace_id, user, manage=False)
    args = ["diff", "--stat=200", "--patch"]
    if staged:
        args.insert(1, "--cached")
    return {"diff": await _git(ws.path, *args, limit=400_000)}


async def _git(cwd: str, *args: str, limit: int = 100_000) -> str:
    """Read git state out of a working tree an agent has been editing.

    Run on the agent's side of the boundary like everything else, and for a
    sharper reason than consistency: a repository's own config can name
    programs for git to run (pagers, hooks, fsmonitor), and this repository is
    one an agent can write to. Reading it as the application's user would hand
    an agent code execution there for the price of an edit to .git/config.
    """
    try:
        proc = await agent_env.spawn(
            ["git", *args],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        agent_env.kill_agent(proc)
        return ""
    text = out.decode("utf-8", errors="replace").strip()
    return text if len(text) <= limit else text[:limit] + "\n… [truncated]"
