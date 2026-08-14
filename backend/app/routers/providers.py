from __future__ import annotations

import asyncio
import json
import os
import shutil

from fastapi import APIRouter, Depends

from ..config import settings
from ..models import User
from ..providers import PROVIDERS
from ..schemas import ProviderOut
from ..security import current_user

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
                # Surface *how* it is authenticated: a subscription login means
                # usage counts against the plan, not pay-as-you-go API billing.
                bits = [b for b in (data.get("email"), data.get("orgName")) if b]
                plan = data.get("subscriptionType")
                if plan:
                    bits.append(f"{plan} subscription")
                elif data.get("authMethod") == "apiKey":
                    bits.append("API key (metered)")
                account = " · ".join(bits) or data.get("authMethod")
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
        efforts=provider.efforts,
        efforts_by_model=provider.efforts_by_model,
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
