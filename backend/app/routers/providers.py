from __future__ import annotations

import asyncio
import json
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth_flows import login_manager
from ..config import settings
from ..models import User
from ..providers import PROVIDERS
from ..schemas import LoginCodeIn, LoginFlowOut, ProviderOut
from ..security import current_admin, current_user

router = APIRouter(prefix="/api/providers", tags=["providers"])

BINARIES = {"claude": settings.claude_bin, "codex": settings.codex_bin}
LABELS = {"claude": "Claude Code", "codex": "OpenAI Codex"}


async def _run(argv: list[str], timeout: float = 15.0) -> tuple[int | None, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None, "not installed"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None, "timed out"
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


def _require_known(name: str) -> None:
    if name not in PROVIDERS:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Unknown provider {name!r}. Known: {', '.join(PROVIDERS)}",
        )


async def _describe(name: str) -> ProviderOut:
    provider = PROVIDERS[name]
    binary = BINARIES[name]
    found = shutil.which(binary) is not None or os.path.isabs(binary)
    version: str | None = None
    authenticated: bool | None = None
    account: str | None = None
    detail: str | None = None

    if found:
        code, out = await _run([binary, "--version"])
        if code is None:
            found = False
            detail = out
        else:
            version = out.splitlines()[0] if out else None

    if found:
        if name == "claude":
            # `claude auth status` emits JSON, so read it properly rather than
            # scraping the human-readable form.
            code, out = await _run([binary, "auth", "status"])
            try:
                data = json.loads(out)
                authenticated = bool(data.get("loggedIn"))
                account = data.get("account") or data.get("authMethod")
                detail = json.dumps(data, indent=2)
            except (json.JSONDecodeError, AttributeError):
                authenticated = code == 0
                detail = out[:2000] or None
        else:
            code, out = await _run([binary, "login", "status"])
            authenticated = code == 0 and "not logged in" not in out.lower()
            account = out.splitlines()[0][:120] if out and authenticated else None
            detail = out[:2000] or None

    return ProviderOut(
        name=name,
        label=LABELS.get(name, name),
        models=provider.models,
        permission_modes=provider.permission_modes,
        binary=binary,
        available=found,
        version=version,
        authenticated=authenticated,
        account=account,
        detail=detail,
    )


@router.get("", response_model=list[ProviderOut])
async def list_providers(_: User = Depends(current_user)):
    """What each agent CLI can do, and whether it is signed in.

    Check this first when runs fail instantly — an un-authenticated CLI is by
    far the most common cause.
    """
    return [await _describe(name) for name in PROVIDERS]


# --- sign-in flows -----------------------------------------------------
# The operator authenticates on the provider's own site. AIOps only relays the
# verification URL, the device code, and (for Claude) the short-lived
# authorization code pasted back — never an account password.


@router.post("/{name}/login", response_model=LoginFlowOut)
async def start_login(name: str, _: User = Depends(current_admin)):
    _require_known(name)
    flow = await login_manager.start(name)
    if flow.status == "failed" and flow.verification_url is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, flow.message or "Could not start sign-in")
    return flow.public()


@router.get("/{name}/login", response_model=LoginFlowOut)
async def login_status(name: str, _: User = Depends(current_admin)):
    _require_known(name)
    flow = login_manager.get(name)
    if flow is None:
        return LoginFlowOut(provider=name, status="idle")
    return flow.public()


@router.post("/{name}/login/code", response_model=LoginFlowOut)
async def submit_login_code(name: str, payload: LoginCodeIn, _: User = Depends(current_admin)):
    """Claude prints an authorize URL then blocks on stdin for the code."""
    _require_known(name)
    try:
        flow = await login_manager.submit_code(name, payload.code)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return flow.public()


@router.delete("/{name}/login", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_login(name: str, _: User = Depends(current_admin)):
    _require_known(name)
    await login_manager.cancel(name)


@router.post("/{name}/logout")
async def provider_logout(name: str, _: User = Depends(current_admin)):
    _require_known(name)
    ok, message = await login_manager.logout(name)
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message or "Logout failed")
    return {"status": "signed out", "detail": message}
