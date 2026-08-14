from __future__ import annotations

import asyncio
import json
import os
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_flows import login_manager
from .. import agent_env
from ..config import settings
from ..db import get_db
from ..models import AccountAccess, ProviderAccount, User
from ..providers import PROVIDERS
from ..schemas import AccountIn, AccountOut, AccountPatch, LoginCodeIn, LoginFlowOut
from ..security import current_admin, current_user

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

BINARIES = {"claude": settings.claude_bin, "codex": settings.codex_bin}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "account"


async def _get(db: AsyncSession, account_id: int) -> ProviderAccount:
    account = await db.get(ProviderAccount, account_id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


def _may_use(account: ProviderAccount, user: User) -> bool:
    """Admins may use anything; otherwise an ungranted account is open to all."""
    if user.is_admin or not account.grants:
        return True
    return any(g.user_id == user.id for g in account.grants)


async def _status(account: ProviderAccount) -> tuple[bool | None, str | None]:
    """Ask the CLI whether this account's credential directory is signed in."""
    binary = BINARIES.get(account.provider)
    if not binary or not os.path.isdir(account.config_dir):
        return (None if not binary else False), None
    argv = (
        [binary, "auth", "status"]
        if account.provider == "claude"
        else [binary, "login", "status"]
    )
    env = {**account.env(), "NO_COLOR": "1"}
    try:
        proc = await agent_env.spawn(
            argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except FileNotFoundError:
        return False, None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        # Left running, it would hold a credential directory's lock files and
        # the next status check would time out too.
        agent_env.kill_agent(proc)
        return False, None
    text = out.decode("utf-8", "replace").strip()
    if account.provider == "claude":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return proc.returncode == 0, text[:300]
        bits = [b for b in (data.get("email"), data.get("orgName")) if b]
        if data.get("subscriptionType"):
            bits.append(f"{data['subscriptionType']} subscription")
        return bool(data.get("loggedIn")), " · ".join(bits) or None
    ok = proc.returncode == 0 and "not logged in" not in text.lower()
    return ok, (text.splitlines()[0][:200] if ok and text else None)


async def _out(account: ProviderAccount, user: User) -> AccountOut:
    signed_in, detail = await _status(account)
    return AccountOut(
        id=account.id,
        name=account.name,
        provider=account.provider,
        slug=account.slug,
        description=account.description,
        is_default=account.is_default,
        fallback_account_id=account.fallback_account_id,
        limited_until=account.limited_until,
        limit_status=account.limit_status,
        limit_window=account.limit_window,
        limit_resets_at=account.limit_resets_at,
        config_dir=account.config_dir,
        signed_in=signed_in,
        account_detail=detail,
        allowed_user_ids=[g.user_id for g in account.grants],
        usable_by_me=_may_use(account, user),
    )


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    rows = list(
        await db.scalars(
            select(ProviderAccount).order_by(ProviderAccount.provider, ProviderAccount.name)
        )
    )
    # Each _out shells out to the CLI to read sign-in state. Serially that is
    # one 15s timeout per account in the worst case, on a page that polls.
    return list(await asyncio.gather(*(_out(a, user) for a in rows)))


@router.post("", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: AccountIn, user: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    if payload.provider not in PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown provider {payload.provider!r}")
    if await db.scalar(select(ProviderAccount).where(ProviderAccount.name == payload.name)):
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that name already exists")

    slug = _slugify(payload.name)
    if await db.scalar(select(ProviderAccount).where(ProviderAccount.slug == slug)):
        slug = f"{slug}-{os.urandom(2).hex()}"

    account = ProviderAccount(
        name=payload.name,
        provider=payload.provider,
        slug=slug,
        description=payload.description,
        is_default=payload.is_default,
        config_dir=os.path.join(settings.accounts_root, f"{payload.provider}-{slug}"),
    )
    # Created up front so the CLI has somewhere to write; Codex refuses to start
    # if CODEX_HOME points at a path that does not exist.
    os.makedirs(account.config_dir, exist_ok=True)
    agent_env.grant_agent_access(account.config_dir, writable=True)
    db.add(account)
    if payload.is_default:
        await _clear_other_defaults(db, payload.provider, exclude_id=None)
    await db.commit()
    await db.refresh(account)
    return await _out(account, user)


@router.patch("/{account_id}", response_model=AccountOut)
async def patch_account(
    account_id: int,
    payload: AccountPatch,
    user: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    account = await _get(db, account_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("fallback_account_id") == account.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "An account cannot fall back to itself")
    if data.get("fallback_account_id"):
        target = await db.get(ProviderAccount, data["fallback_account_id"])
        if target is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Fallback account not found")
        if target.provider != account.provider:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Failover only works between accounts of the same provider",
            )

    allowed = data.pop("allowed_user_ids", None)
    for key, value in data.items():
        setattr(account, key, value)
    if data.get("is_default"):
        await _clear_other_defaults(db, account.provider, exclude_id=account.id)

    if allowed is not None:
        account.grants.clear()
        await db.flush()
        for uid in dict.fromkeys(allowed):
            if await db.get(User, uid) is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unknown user id {uid}")
            account.grants.append(AccountAccess(account_id=account.id, user_id=uid))

    await db.commit()
    await db.refresh(account)
    return await _out(account, user)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    """Removes the account. Credentials on disk are left alone."""
    account = await _get(db, account_id)
    await db.delete(account)
    await db.commit()


# --- per-account sign-in ----------------------------------------------
def _key(account: ProviderAccount) -> str:
    return f"account:{account.id}"


@router.post("/{account_id}/login", response_model=LoginFlowOut)
async def start_login(
    account_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    account = await _get(db, account_id)
    os.makedirs(account.config_dir, exist_ok=True)
    agent_env.grant_agent_access(account.config_dir, writable=True)
    flow = await login_manager.start(account.provider, _key(account), account.env())
    if flow.status == "failed" and flow.verification_url is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, flow.message or "Could not start sign-in")
    return flow.public()


@router.get("/{account_id}/login", response_model=LoginFlowOut)
async def login_status(
    account_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    account = await _get(db, account_id)
    flow = login_manager.get(_key(account))
    if flow is None:
        return LoginFlowOut(provider=account.provider, status="idle")
    return flow.public()


@router.post("/{account_id}/login/code", response_model=LoginFlowOut)
async def submit_code(
    account_id: int,
    payload: LoginCodeIn,
    _: User = Depends(current_admin),
    db: AsyncSession = Depends(get_db),
):
    account = await _get(db, account_id)
    try:
        flow = await login_manager.submit_code(_key(account), payload.code)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return flow.public()


@router.delete("/{account_id}/login", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_login(
    account_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    account = await _get(db, account_id)
    await login_manager.cancel(_key(account))


@router.post("/{account_id}/logout")
async def logout(
    account_id: int, _: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    account = await _get(db, account_id)
    ok, message = await login_manager.logout(account.provider, _key(account), account.env())
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message or "Logout failed")
    return {"status": "signed out", "detail": message}


@router.post("/{account_id}/clear-limit", response_model=AccountOut)
async def clear_limit(
    account_id: int, user: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    """Forget a recorded usage limit so this account is eligible again."""
    account = await _get(db, account_id)
    account.limited_until = None
    # Clear the reported window too, otherwise the UI keeps showing the old
    # "limit hit" banner until some later run happens to overwrite it.
    account.limit_status = None
    account.limit_window = None
    account.limit_resets_at = None
    await db.commit()
    await db.refresh(account)
    return await _out(account, user)


async def _clear_other_defaults(db: AsyncSession, provider: str, exclude_id: int | None) -> None:
    rows = await db.scalars(
        select(ProviderAccount).where(
            ProviderAccount.provider == provider, ProviderAccount.is_default.is_(True)
        )
    )
    for other in rows:
        if other.id != exclude_id:
            other.is_default = False
