from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import LEVELS, github_account_level_for
from ..approvals import run_tokens
from ..crypto import SecretUnavailable, decrypt, encrypt, is_configured
from ..db import SessionLocal, get_db
from ..loopback import require_loopback
from ..models import GithubAccount, GithubAccountAccess, Run, Session, User, Workspace
from ..schemas import GithubAccountGrant, GithubAccountIn, GithubAccountOut, GithubAccountPatch
from ..security import current_user

log = logging.getLogger("aiops.github")

router = APIRouter(prefix="/api/github-accounts", tags=["github"])

#: The loopback endpoint the pull-request MCP bridge calls to fetch a decrypted
#: token. Mounted separately, like `approvals.internal` and `browsing.internal`,
#: because it takes a run token instead of a session cookie and must be refused
#: to anything that did not arrive over loopback.
internal = APIRouter(
    prefix="/api/internal/github",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_loopback)],
)


def _require(account: GithubAccount, user: User, *, manage: bool) -> str:
    level = github_account_level_for(account, user)
    # An account the caller may not see must 404, not 403: "you may not touch
    # this" still confirms that an account by this id exists and invites
    # guessing at whose it is — the same rule as a stored system.
    if level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GitHub account not found")
    if manage and level not in ("owner", "manage"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can use this GitHub account but not change it. Ask its owner for "
            "manage access.",
        )
    return level


def _require_key() -> None:
    if not is_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "AIOPS_SECRET_KEY is not set on the server, so credentials cannot be stored. "
            "Set it in the server's .env and restart.",
        )


def _out(account: GithubAccount, level: str) -> GithubAccountOut:
    return GithubAccountOut(
        id=account.id,
        label=account.label,
        has_token=bool(account.token_enc),
        owner_id=account.owner_id,
        grants=[GithubAccountGrant(user_id=g.user_id, level=g.level) for g in account.grants],
        my_level=level,
        created_at=account.created_at,
    )


def _validate(grants: list[GithubAccountGrant] | None) -> None:
    for grant in grants or []:
        if grant.level not in LEVELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Access level must be one of {', '.join(LEVELS)} (got {grant.level!r})",
            )


async def _apply_grants(
    db: AsyncSession,
    account: GithubAccount,
    grants: list[GithubAccountGrant] | None,
    owner: User,
) -> None:
    if grants is None:
        return
    for existing in list(account.grants):
        await db.delete(existing)
    account.grants = []
    await db.flush()
    seen: set[int] = set()
    for grant in grants:
        if grant.user_id in seen or grant.user_id == (account.owner_id or owner.id):
            continue
        seen.add(grant.user_id)
        db.add(
            GithubAccountAccess(
                github_account_id=account.id, user_id=grant.user_id, level=grant.level
            )
        )


@router.get("", response_model=list[GithubAccountOut])
async def list_accounts(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Only what this user owns or has been granted — nothing else exists to them."""
    rows = await db.scalars(
        select(GithubAccount)
        .where(
            or_(
                GithubAccount.owner_id == user.id,
                GithubAccount.id.in_(
                    select(GithubAccountAccess.github_account_id).where(
                        GithubAccountAccess.user_id == user.id
                    )
                ),
            )
        )
        .order_by(GithubAccount.label)
    )
    out = []
    for account in rows:
        level = github_account_level_for(account, user)
        if level is not None:
            out.append(_out(account, level))
    return out


@router.post("", response_model=GithubAccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: GithubAccountIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Anyone may add a GitHub account; it belongs to them until they share it."""
    _require_key()
    _validate(payload.grants)
    account = GithubAccount(
        label=payload.label.strip(),
        token_enc=encrypt(payload.token),
        owner_id=user.id,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    await _apply_grants(db, account, payload.grants, user)
    await db.commit()
    await db.refresh(account)
    return _out(account, "owner")


@router.patch("/{account_id}", response_model=GithubAccountOut)
async def update_account(
    account_id: int,
    payload: GithubAccountPatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await db.get(GithubAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GitHub account not found")
    level = _require(account, user, manage=True)
    _validate(payload.grants)

    data = payload.model_dump(exclude_unset=True)

    new_owner = data.pop("owner_id", None)
    if new_owner is not None and new_owner != account.owner_id:
        if level != "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only the owner can hand this GitHub account to someone else",
            )
        if await db.get(User, new_owner) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That user does not exist")
        account.owner_id = new_owner

    if "token" in data:
        _require_key()
        account.token_enc = encrypt(data.pop("token"))

    grants = data.pop("grants", None)
    for key, value in data.items():
        if value is not None:
            setattr(account, key, value)

    await _apply_grants(
        db,
        account,
        [GithubAccountGrant(**g) for g in grants] if grants is not None else None,
        user,
    )
    await db.commit()
    await db.refresh(account)
    return _out(account, github_account_level_for(account, user) or level)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    account = await db.get(GithubAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "GitHub account not found")
    _require(account, user, manage=True)
    # A workspace pointed at this account keeps working — it just falls back
    # to no GitHub credential — instead of failing to delete the account.
    await db.execute(
        Workspace.__table__.update()
        .where(Workspace.github_account_id == account.id)
        .values(github_account_id=None)
    )
    await db.delete(account)
    await db.commit()


# --- the loopback endpoint the pull-request bridge calls ----------------
@internal.post("/credential")
async def credential(request: Request):
    """The token for one run's linked GitHub account, for the PR-creation bridge.

    Authenticated by the same per-run token the approval and browser bridges
    use (`AIOPS_APPROVAL_TOKEN`), which this endpoint resolves back to a run
    and, through it, to the workspace the run's session points at and the
    person who actually asked for the turn. An administrator gets nothing
    extra: this applies `github_account_level_for` to that person, exactly as
    every other reader of a GitHub account does.

    Returned to the bridge process and used there to call the GitHub API
    directly; it is never handed to the model and never logged.
    """
    payload = await request.json()
    token = payload.get("token") or request.headers.get("x-aiops-token") or ""
    resolved = run_tokens.resolve(token)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or expired run token")
    run_id, _session_id = resolved

    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That run no longer exists")
        sess = await db.get(Session, run.session_id)
        workspace = sess.workspace if sess is not None else None
        if workspace is None or workspace.github_account_id is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "This session's workspace has no GitHub account linked to it. Link one on "
                "the Workspaces page before opening a pull request.",
            )
        asker = await db.get(User, run.requested_by_id) if run.requested_by_id else None
        account = await db.get(GithubAccount, workspace.github_account_id)
        if account is None or github_account_level_for(account, asker) is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"{asker.username if asker else 'Whoever sent this turn'} does not have "
                "access to the GitHub account linked to this workspace.",
            )
        try:
            secret = decrypt(account.token_enc)
        except SecretUnavailable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not secret:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "That GitHub account has no stored token.",
            )
        label = account.label

    log.info(
        "github: token for account %s handed to the pull-request bridge for run %s "
        "— the value was not returned to the agent",
        label,
        run_id,
    )
    return {"token": secret}
