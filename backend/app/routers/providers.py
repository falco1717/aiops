from __future__ import annotations

import asyncio
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


async def _run(argv: list[str], timeout: float = 10.0) -> tuple[int | None, str]:
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


@router.get("", response_model=list[ProviderOut])
async def list_providers(_: User = Depends(current_user)):
    """Report what each agent CLI can do and whether it is logged in.

    This is the page to check first when runs fail instantly — an un-authenticated
    CLI is by far the most common cause.
    """
    results: list[ProviderOut] = []
    for name, provider in PROVIDERS.items():
        binary = BINARIES[name]
        found = shutil.which(binary) is not None or os.path.isabs(binary)
        version: str | None = None
        authenticated: bool | None = None
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
                code, out = await _run([binary, "auth", "status", "--text"])
            else:
                code, out = await _run([binary, "login", "status"])
            authenticated = code == 0
            detail = out[:2000] or None

        results.append(
            ProviderOut(
                name=name,
                models=provider.models,
                permission_modes=provider.permission_modes,
                binary=binary,
                available=found,
                version=version,
                authenticated=authenticated,
                detail=detail,
            )
        )
    return results
