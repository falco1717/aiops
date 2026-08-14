from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import agent_env
from ..config import settings
from ..db import get_db
from ..models import User, Workspace
from ..schemas import WorkspaceIn, WorkspaceOut, WorkspaceStatus
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


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(_: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.scalars(select(Workspace).order_by(Workspace.name))
    return list(rows)


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    payload: WorkspaceIn,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    path = resolve_workspace_path(payload.path)
    if await db.scalar(select(Workspace).where(Workspace.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A workspace with that name already exists")
    os.makedirs(path, exist_ok=True)
    # A workspace the app just created belongs to the app, and an agent runs as
    # somebody else: without this it would be handed a directory it cannot write
    # to. Recursive because registering a directory that already has a checkout
    # in it is the common case, not the exception.
    agent_env.grant_agent_access(path, writable=True)
    ws = Workspace(name=payload.name, path=path, description=payload.description)
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    return ws


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: int,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Unregisters the workspace. The directory on disk is left untouched."""
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    await db.delete(ws)
    await db.commit()


@router.get("/{workspace_id}/status", response_model=WorkspaceStatus)
async def workspace_status(
    workspace_id: int,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
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
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
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
