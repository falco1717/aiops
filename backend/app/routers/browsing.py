"""The loopback API the agent's browser calls back on.

Three things the browser cannot be trusted to work out for itself, and one
thing it must never be handed on disk. Authenticated by the same per-run token
the approval bridge uses: it identifies a run and nothing else, and every answer
below is scoped by that run to the person who asked for the turn.

Mounted separately from every other router for the same reason
`approvals.internal` is — nothing here takes a session cookie.

Nothing here is reachable from outside this container either, and that is now
enforced rather than asserted: `require_loopback` (plus the same check as
middleware in `main.py`) refuses anything whose transport peer is not the
loopback, with the 404 this application gives for everything a caller may not
reach. It was an assertion only until the credential endpoint below was found
answering `401 Unknown or expired run token` to the public internet, which
turned a leaked run token — a value that lives in an agent's environment — into
a remotely usable one. The check deliberately does not look at `request.client`;
see `app/loopback.py` for why that attribute is forgeable here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from .. import browsing, ssh_targets
from ..approvals import run_tokens
from ..config import settings
from ..crypto import SecretUnavailable, decrypt
from ..db import SessionLocal
from ..loopback import require_loopback
from ..models import Run, User

log = logging.getLogger("aiops.browser")

internal = APIRouter(
    prefix="/api/internal/browser",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_loopback)],
)

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


@internal.post("/screenshot")
async def screenshot(request: Request):
    """One capture, as bytes, for AIOps to keep with the session.

    Bytes rather than a path, and that is the security decision in this
    endpoint. The run's screenshot directory is group-writable by the agent —
    it has to be, because opening a capture by path is how the agent looks at
    one — so harvesting files out of it would mean the app opening a name the
    agent controls, with symlinks and hardlinks under it to worry about. What
    arrives here instead is the return value of the same masked
    `page.screenshot(...)` call that wrote the agent's own copy: there is no
    filesystem object in between for anything to be substituted for, and the
    mask travels with the bytes by construction.

    Which session it lands in is read off the run token, never off the request.
    The agent's side names only the file, and that name is checked against the
    generated shape and resolved inside this turn's directory before anything is
    opened (see `browsing.keep_shot`).

    A refusal is a 409 and is not an error the agent has to handle — its own
    copy is untouched. The tool result says the operator will not see that one.
    """
    run_id, session_id = _resolve({}, request)
    grant = _grant(run_id)
    name = str(request.headers.get("x-aiops-screenshot") or "")

    # Read with the cap applied as it streams, in the same shape as an upload:
    # checking a declared Content-Type or Content-Length would trust a header,
    # and checking afterwards would mean the memory had already been taken.
    limit = settings.browser_screenshot_max_bytes
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"A screenshot may be at most {limit} bytes.",
            )

    try:
        size = browsing.keep_shot(session_id, run_id, name, bytes(data))
    except browsing.ShotRefused as exc:
        log.info(
            "browser: screenshot %s not kept for %s (session %s) — %s",
            name or "<unnamed>", grant.who, session_id, exc,
        )
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    log.info(
        "browser: screenshot %s (%d bytes) kept with session %s for %s",
        name, size, session_id, grant.who,
    )
    return {"stored": True, "size": size}


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
