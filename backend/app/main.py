from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from . import agent_env
from .approvals import reap_pending_approvals
from .config import settings
from .credentials import credential_loop
from .db import SessionLocal, init_db
from .loopback import LoopbackOnlyMiddleware, build_asgi
from .models import Run, Session, User
from .migrate import run_migrations
from .relay import hub as relay_hub
from .routers import (
    accounts,
    approvals,
    auth,
    browsing,
    nodes,
    presets,
    providers,
    runs,
    schedules,
    sessions,
    targets,
    teams,
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
            run.status = "failed" if run.status == "running" else "cancelled"
            # A queue does not survive a restart — the runner's in-process
            # dispatch state goes with it — so say which of the two happened.
            # Calling a message that was never picked up "failed" puts the
            # session into an error state over a turn that never ran.
            run.error = (
                "Interrupted by an AIOps restart"
                if run.status == "failed"
                else "AIOps restarted before this queued turn was picked up"
            )
            run.finished_at = datetime.now(timezone.utc)
        stale = list(await db.scalars(select(Session).where(Session.status == "running")))
        for sess in stale:
            sess.status = "idle"
        if orphans or stale:
            await db.commit()
            log.info("Reaped %d interrupted run(s) after restart", len(orphans))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    runner.start()
    await init_db()
    await run_migrations()
    await bootstrap_admin()
    await reap_orphaned_runs()
    await reap_pending_approvals()
    await backfill_next_runs()
    # The loopback listener a run's ProxyCommand hands its bytes to. Started
    # unconditionally: it costs one socket, and a relay node that connects
    # before anyone configures anything must have somewhere to be used from.
    await relay_hub.start_forwarder()

    tasks: list[asyncio.Task] = []
    if settings.scheduler_enabled:
        tasks.append(asyncio.create_task(scheduler_loop(), name="scheduler"))
    if settings.credential_watch_enabled:
        # Started after the migrations that add the columns it writes.
        tasks.append(asyncio.create_task(credential_loop(), name="credentials"))

    os.makedirs(settings.workspace_root, exist_ok=True)
    os.makedirs(settings.accounts_root, exist_ok=True)
    os.makedirs(settings.attachments_root, exist_ok=True)
    # Agents run as their own user, so everything they legitimately need has to
    # be group-reachable. These are volumes and bind mounts written before that
    # was true, so it is settled at every boot rather than at image build.
    agent_env.share_startup_paths()
    log.info("Agent processes run as: %s", agent_env.probe_identity())
    if settings.browser_enabled:
        log.info("The agent's browser runs as: %s", agent_env.probe_browser_identity())
    log.info("AIOps ready. Workspace root: %s", settings.workspace_root)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await runner.shutdown()
        await relay_hub.stop_forwarder()


app = FastAPI(title="AIOps", version="1.0.0", lifespan=lifespan)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Added last, which in Starlette means outermost: an `/api/internal` request
# from off-box is refused before CORS, before routing, and before any of this
# application's own code sees it. See loopback.py for why the transport peer,
# and never a header, is what decides.
app.add_middleware(LoopbackOnlyMiddleware)

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
    nodes,
    teams,
    ws,
):
    app.include_router(module.router)

# Token-authenticated callback used by the in-container approval bridge. Both
# of these are refused to anything that did not arrive over loopback — the run
# token identifies a run, it is not a network boundary.
app.include_router(approvals.internal)
# And by the browser bridge, for the three things it is not allowed to decide
# for itself: where it may go, what it must write down, and a credential it is
# given to type but never to keep.
app.include_router(browsing.internal)
# Node-facing relay endpoints. Authenticated by a node's own credential rather
# than a session cookie, so they are mounted separately from the router above.
app.include_router(nodes.relay_router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": app.version}


# --- single-page app --------------------------------------------------
#
# Two cache rules, and a deploy was invisible to an already-open browser for
# want of them. The build fingerprints the bundle, so the only file that says
# *which* bundle to load is index.html — and it went out with no Cache-Control
# and no Expires at all. Given neither, a browser is free to invent a freshness
# lifetime (RFC 9111 §4.2.2 — commonly a tenth of the age since Last-Modified)
# and does, so the shell came back off disk without asking and kept naming
# yesterday's bundle. Three shipped features looked broken for a day.

#: The shell, and the files beside it a build does not fingerprint (mark.svg,
#: logo.svg). `no-cache` is not `no-store`: the copy is still kept, it just has
#: to be revalidated, and the usual answer is a bodyless 304.
REVALIDATE_CACHE_CONTROL = "no-cache"
#: Content-addressed output under /assets. A new build cannot change one of
#: these — it emits a different name — so a year is correct, and `immutable`
#: stops the browser revalidating them on an ordinary reload as well.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"


class CachedStaticFiles(StaticFiles):
    """`StaticFiles` that says how long its responses may be reused.

    Starlette already emits `ETag` and `Last-Modified` and already answers a
    conditional request with a 304; the one thing it leaves out is the header
    that decides whether the browser asks at all. Setting it after the parent
    has chosen between 200 and 304 puts it on both — a 304 that dropped it
    would re-arm heuristic freshness on the very response meant to end it.
    """

    def __init__(self, *args, cache_control: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cache_control = cache_control

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = self.cache_control
        return response


def mount_spa(app: FastAPI, static_dir: Path) -> None:
    """Serve the built frontend out of `static_dir`, with the rules above."""
    assets = CachedStaticFiles(
        directory=static_dir / "assets", cache_control=IMMUTABLE_CACHE_CONTROL
    )
    shell = CachedStaticFiles(
        directory=static_dir, cache_control=REVALIDATE_CACHE_CONTROL
    )
    app.mount("/assets", assets, name="assets")

    static_root = static_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(request: Request, full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if full_path:
            # `full_path` is attacker-controlled and arrives percent-decoded, so
            # it can contain `..`. Resolve it and require the result to stay
            # inside the static root, or this route serves arbitrary files to
            # anyone — it is deliberately reachable before login.
            candidate = (static_dir / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(static_root):
                return await shell.get_response(full_path, request.scope)
        # Every client-side route — /sessions/<id> and the rest — lands here,
        # so the deep link has to carry exactly the headers `/` does.
        return await shell.get_response("index.html", request.scope)


if STATIC_DIR.is_dir():
    mount_spa(app, STATIC_DIR)


#: What the container actually serves — `uvicorn app.main:asgi`, not `:app`.
#:
#: The proxy-header handling that used to be a uvicorn command-line flag is
#: applied here instead, with one layer above it that records the untouched
#: transport peer first. That ordering is the only way the loopback gate can
#: see who really connected, because `--proxy-headers` would otherwise rewrite
#: it from a header the caller controls. `app` itself stays a plain FastAPI
#: instance so tests can drive it directly.
asgi = build_asgi(app)
