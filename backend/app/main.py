from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .approvals import reap_pending_approvals
from .config import settings
from .db import SessionLocal, init_db
from .models import Run, Session, User
from .migrate import run_migrations
from .routers import (
    accounts,
    approvals,
    auth,
    presets,
    providers,
    runs,
    schedules,
    sessions,
    targets,
    usage,
    users,
    workspaces,
    ws,
)
from .runner import runner
from .scheduler import backfill_next_runs, scheduler_loop
from .security import hash_password

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
)
log = logging.getLogger("aiops")

STATIC_DIR = Path(__file__).parent / "static"


async def bootstrap_admin() -> None:
    async with SessionLocal() as db:
        if await db.scalar(select(func.count(User.id))):
            return
        password = settings.admin_password or secrets.token_urlsafe(18)
        db.add(
            User(
                username=settings.admin_username,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        await db.commit()
        if settings.admin_password:
            log.info("Created admin user %r from AIOPS_ADMIN_PASSWORD", settings.admin_username)
        else:
            log.warning(
                "No AIOPS_ADMIN_PASSWORD set. Created user %r with generated password: %s",
                settings.admin_username,
                password,
            )
            log.warning("Save it now — it is not stored anywhere else and won't be shown again.")


async def reap_orphaned_runs() -> None:
    """A restart kills every agent subprocess; their rows must not stay 'running'."""
    async with SessionLocal() as db:
        orphans = list(
            await db.scalars(select(Run).where(Run.status.in_(("queued", "running"))))
        )
        for run in orphans:
            run.status = "failed"
            run.error = "Interrupted by an AIOps restart"
            run.finished_at = datetime.now(timezone.utc)
        stale = list(await db.scalars(select(Session).where(Session.status == "running")))
        for sess in stale:
            sess.status = "idle"
        if orphans or stale:
            await db.commit()
            log.info("Reaped %d interrupted run(s) after restart", len(orphans))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await run_migrations()
    await bootstrap_admin()
    await reap_orphaned_runs()
    await reap_pending_approvals()
    await backfill_next_runs()

    task: asyncio.Task | None = None
    if settings.scheduler_enabled:
        task = asyncio.create_task(scheduler_loop(), name="scheduler")

    os.makedirs(settings.workspace_root, exist_ok=True)
    os.makedirs(settings.accounts_root, exist_ok=True)
    os.makedirs(settings.attachments_root, exist_ok=True)
    log.info("AIOps ready. Workspace root: %s", settings.workspace_root)
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await runner.shutdown()


app = FastAPI(title="AIOps", version="1.0.0", lifespan=lifespan)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

for module in (
    auth,
    users,
    accounts,
    providers,
    usage,
    workspaces,
    presets,
    sessions,
    runs,
    schedules,
    approvals,
    targets,
    ws,
):
    app.include_router(module.router)

# Token-authenticated callback used by the in-container approval bridge.
app.include_router(approvals.internal)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


# --- single-page app --------------------------------------------------
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    _STATIC_ROOT = STATIC_DIR.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:
            # `full_path` is attacker-controlled and arrives percent-decoded, so
            # it can contain `..`. Resolve it and require the result to stay
            # inside the static root, or this route serves arbitrary files to
            # anyone — it is deliberately reachable before login.
            candidate = (STATIC_DIR / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(_STATIC_ROOT):
                return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
