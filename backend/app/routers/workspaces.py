from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import agent_env, github_creds
from ..access import LEVELS, github_account_level_for, workspace_level_for
from ..config import settings
from ..db import get_db
from ..models import GithubAccount, User, Workspace, WorkspaceAccess
from ..schemas import (
    WorkspaceFromGithubIn,
    WorkspaceGrant,
    WorkspaceIn,
    WorkspaceOut,
    WorkspacePatch,
    WorkspaceStatus,
)
from ..security import current_user

log = logging.getLogger("aiops.workspaces")

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
        github_account_id=ws.github_account_id,
        created_at=ws.created_at,
    )


async def _check_github_account(db: AsyncSession, account_id: int | None, user: User) -> None:
    """Linking a workspace to a GitHub account needs `use` on that account.

    Otherwise anyone able to edit a workspace they manage could point it at a
    GitHub account by guessing an id and borrow somebody else's token.
    """
    if account_id is None:
        return
    account = await db.get(GithubAccount, account_id)
    if account is None or github_account_level_for(account, user) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That GitHub account does not exist, or has not been shared with you",
        )


#: "owner/name", optionally as a full https://github.com/... URL. Anything
#: else — ssh://, git@, another host — is rejected below: this endpoint clones
#: from github.com over https with a token and nowhere else, so accepting an
#: arbitrary URL here would turn it into a general-purpose SSRF-shaped fetch
#: primitive wearing a GitHub-shaped name.
_REPO_SHORTHAND = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
_REPO_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def _parse_github_repo(raw: str) -> tuple[str, str]:
    """`(owner, name)`, or a 400 naming exactly what was rejected and why."""
    text = raw.strip()
    match = _REPO_URL.match(text) or _REPO_SHORTHAND.match(text)
    if match is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{raw!r} is not a github.com repository. Use 'owner/name' or a "
            "https://github.com/owner/name URL — an ssh:// URL, a git@ shorthand, or "
            "any other host is rejected.",
        )
    return match.group(1), match.group(2)


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


@router.post(
    "/from-github", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED
)
async def create_workspace_from_github(
    payload: WorkspaceFromGithubIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clone a github.com repository straight into a new, registered workspace.

    A deliberate, user-initiated action through this endpoint — not something
    an agent decides to do mid-turn, the same distinction the rest of the
    workspaces API draws between registering a directory (here) and an agent
    merely running commands inside one it was already given.

    The clone is owned by the caller, never by anyone else: this is the same
    rule `runner.py` applies to a turn's stored credentials, applied one layer
    earlier — whoever's token pays for the clone is whoever ends up owning what
    it produced.
    """
    account = await db.get(GithubAccount, payload.github_account_id)
    if account is None or github_account_level_for(account, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GitHub account not found")

    owner_name, repo_name = _parse_github_repo(payload.repo)
    clone_url = f"https://github.com/{owner_name}/{repo_name}.git"

    ws_name = (payload.name or repo_name).strip()
    if not ws_name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Workspace name must not be empty")
    if await db.scalar(select(Workspace).where(Workspace.name == ws_name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A workspace with that name already exists")

    # The directory a fresh clone lands in — reusing the same root-confinement
    # and collision check the rest of this router already applies to a
    # hand-typed path, rather than inventing a second rule for this one.
    slug = _slugify_dir(repo_name)
    path = resolve_workspace_path(slug)
    if os.path.exists(path):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{slug!r} already exists under the workspace root. Choose a different "
            "workspace name, or remove that directory first.",
        )

    creds = github_creds.prepare(account)
    if creds is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "That GitHub account's token could not be read — it may need to be re-entered.",
        )
    try:
        proc = await agent_env.spawn(
            ["git", "clone", "--", clone_url, path],
            cwd=settings.workspace_root,
            env=creds.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            agent_env.kill_agent(proc)
            raise HTTPException(
                status.HTTP_504_GATEWAY_TIMEOUT, "Cloning that repository timed out."
            ) from None
    finally:
        creds.cleanup()

    if proc.returncode != 0:
        # A partial clone left on disk would silently register as an empty
        # workspace the next attempt collides with; clear it so the operator
        # can simply try again.
        shutil.rmtree(path, ignore_errors=True)
        detail = out.decode("utf-8", errors="replace").strip()[-2000:]
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"git clone failed: {detail or f'exit code {proc.returncode}'}",
        )

    # Verified rather than assumed: the whole point of the credential helper in
    # github_creds.py is that the token never lands in the repository's own
    # config. `git clone <plain-url>` never writes a credential into
    # `origin.url` on its own, but this is exactly the property a regression
    # here would break silently, so it is checked before the workspace is ever
    # registered.
    origin_cfg = os.path.join(path, ".git", "config")
    if os.path.isfile(origin_cfg):
        with open(origin_cfg, "r", encoding="utf-8", errors="replace") as fh:
            cfg_text = fh.read()
        if "@github.com" in cfg_text or (account.token_enc and _looks_like_token(cfg_text)):
            log.error(
                "github: clone of %s left a credential-looking string in .git/config; "
                "refusing to register the workspace",
                clone_url,
            )
            import shutil as _shutil

            _shutil.rmtree(path, ignore_errors=True)
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "The clone appeared to leave a credential in .git/config, so it was removed "
                "rather than registered. This is a bug — please report it.",
            )

    agent_env.grant_agent_access(path, writable=True)
    ws = Workspace(
        name=ws_name,
        path=path,
        description=payload.description,
        owner_id=user.id,
        github_account_id=account.id,
    )
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    await _apply_grants(db, ws, payload.grants, user)
    await db.commit()
    await db.refresh(ws)
    return _out(ws, "owner")


def _slugify_dir(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return slug or "repo"


def _looks_like_token(text: str) -> bool:
    """A crude, best-effort check for a leaked GitHub token shape.

    Not the primary defence — `github_creds.prepare` not writing the token
    anywhere near the clone is — this is a second, independent check on the
    actual artifact the clone produced, worth having precisely because it does
    not share any code with the mechanism it is checking.
    """
    return bool(re.search(r"gh[pousr]_[A-Za-z0-9]{20,}", text))


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

    if "github_account_id" in data:
        await _check_github_account(db, data["github_account_id"], user)
        # Applied here rather than by the loop below, which skips nulls: null
        # is the meaningful value that unlinks the account.
        ws.github_account_id = data.pop("github_account_id")

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
