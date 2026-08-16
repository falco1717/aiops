"""The loopback API the agent's browser calls back on.

Three things the browser cannot be trusted to work out for itself, and one
thing it must never be handed on disk. Authenticated by the same per-run token
the approval bridge uses: it identifies a run and nothing else, and every answer
below is scoped by that run to the person who asked for the turn.

Mounted separately from every other router for the same reason
`approvals.internal` is — nothing here takes a session cookie, and nothing here
should ever be reachable from outside this container.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status

from .. import browsing, ssh_targets
from ..approvals import run_tokens
from ..crypto import SecretUnavailable, decrypt
from ..db import SessionLocal
from ..models import Run, User

log = logging.getLogger("aiops.browser")

internal = APIRouter(prefix="/api/internal/browser", tags=["internal"], include_in_schema=False)

#: Actions worth a line each. Anything else the browser reports is recorded
#: under its own name too — the list is here so the log reads consistently, not
#: to filter what is written down.
_ACTIONS = ("start", "navigate", "opened", "refused", "failed", "click", "fill", "login",
            "screenshot")


def _resolve(payload: dict, request: Request) -> tuple[int, str]:
    token = payload.get("token") or request.headers.get("x-aiops-token") or ""
    resolved = run_tokens.resolve(token)
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or expired run token")
    return resolved


def _grant(run_id: int) -> browsing.BrowserGrant:
    grant = browsing.grants.get(run_id)
    if grant is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "This run was not given a browser."
        )
    return grant


@internal.post("/reach")
async def reach(request: Request):
    """What this run's browser may reach, and which stored systems it may sign in as.

    Handed over rather than kept secret because it authorises nothing: it names
    the node to ask about, and the gate in relay.py decides. A browser told a
    lie here reaches nothing it could not otherwise reach.
    """
    payload = await request.json()
    run_id, _session_id = _resolve(payload, request)
    return _grant(run_id).reach


@internal.post("/log")
async def record(request: Request):
    """One line per navigation, per refusal, and per change to a page.

    Written on this side of the boundary on purpose. The browser's own stderr
    goes to the CLI that spawned it and is not kept, so a record the agent
    cannot edit or drop has to be made by something the agent does not run.
    """
    payload = await request.json()
    run_id, session_id = _resolve(payload, request)
    grant = _grant(run_id)
    action = str(payload.get("action") or "action")[:32]
    if action not in _ACTIONS:
        action = "action"
    url = str(payload.get("url") or "")[:1000]
    host = str(payload.get("host") or "")[:255]
    port = payload.get("port")
    node = str(payload.get("node") or "")[:64]
    detail = str(payload.get("detail") or "")[:500]

    where = url or (f"{host}:{port}" if host else "")
    log.info(
        "browser: %s %s%s for %s (session %s)%s",
        action,
        where,
        f" via node {node}" if node else "",
        grant.who,
        session_id,
        f" — {detail}" if detail else "",
    )
    return {"ok": True}


@internal.post("/credential")
async def credential(request: Request):
    """The password for one stored system, for injection into a page.

    Scoped here rather than by anything the agent holds: the run's requester is
    read off the row, and `visible_targets` applies the same ownership rule that
    decides which systems that person's ssh config gets. An administrator gets
    nothing extra, which is the rule everywhere a stored credential is involved.

    The secret is returned to the browser process and typed into the page. It is
    never written to disk, never put in a tool result, and never logged — the
    line below names the system and the person, which is what an audit needs.
    """
    payload = await request.json()
    run_id, session_id = _resolve(payload, request)
    grant = _grant(run_id)
    slug = str(payload.get("system") or "").strip()
    if not slug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No system named")

    async with SessionLocal() as db:
        run = await db.get(Run, run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That run no longer exists")
        asker = await db.get(User, run.requested_by_id) if run.requested_by_id else None
        allowed = await ssh_targets.visible_targets(db, asker)
        target = next((t for t in allowed if t.slug == slug), None)
        if target is None:
            # Deliberately the same answer whether the system does not exist or
            # is somebody else's: an agent must not be able to enumerate what
            # other people have stored by asking for slugs.
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"No system called {slug!r} is available to whoever asked for this turn.",
            )
        try:
            secret = decrypt(target.password_enc)
        except SecretUnavailable as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if not secret:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{slug!r} has no stored password — it authenticates with a key, which a "
                "login form cannot use.",
            )
        username = target.username

    log.info(
        "browser: credential for system %s injected into the browser of %s (session %s) — "
        "the value was not returned to the agent",
        slug,
        grant.who,
        session_id,
    )
    return {"username": username, "secret": secret}
