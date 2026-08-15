from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import installer, relay
from ..access import LEVELS, node_level_for
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import RelayNode, RelayNodeAccess, Target, User
from ..ratelimit import client_address, throttle
from ..schemas import (
    InstallCommand,
    NodeEnrolIn,
    NodeEnrolOut,
    NodeEnrolmentOut,
    NodeGrant,
    NodeIn,
    NodeOut,
    NodePatch,
)
from ..security import current_admin, current_user

log = logging.getLogger("aiops.relay")

#: Admin-facing management of nodes.
router = APIRouter(prefix="/api/nodes", tags=["nodes"])
#: Node-facing. Authenticated by the node's own credential, never a cookie.
relay_router = APIRouter(prefix="/api/relay", tags=["relay"])

HEARTBEAT_SECONDS = 25

# Close codes the agent distinguishes. 4401 and 4403 are worth retrying — a
# node may simply be waiting to be approved — while 4410 means stop.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_APPROVED = 4403
CLOSE_REVOKED = 4410


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "node"


async def _target_counts(db: AsyncSession) -> dict[int, int]:
    rows = await db.execute(
        select(Target.relay_node_id, func.count(Target.id))
        .where(Target.relay_node_id.isnot(None))
        .group_by(Target.relay_node_id)
    )
    return {node_id: count for node_id, count in rows}


def _out(node: RelayNode, level: str, counts: dict[int, int]) -> NodeOut:
    return NodeOut(
        id=node.id,
        name=node.name,
        slug=node.slug,
        description=node.description,
        status=node.status,
        enrolment_pending=bool(node.enrolment_token_hash),
        enrolment_token_expires_at=node.enrolment_token_expires_at,
        enrolled_at=node.enrolled_at,
        last_seen_at=node.last_seen_at,
        online=relay.hub.is_online(node.id),
        version=node.version,
        reported_hostname=node.reported_hostname,
        networks=list(node.networks or []),
        owner_id=node.owner_id,
        grants=[NodeGrant(user_id=g.user_id, level=g.level) for g in node.grants],
        my_level=level,
        target_count=counts.get(node.id, 0),
        created_at=node.created_at,
    )


def _require(node: RelayNode, user: User, *, manage: bool) -> str:
    level = node_level_for(node, user)
    # Same 404-not-403 rule as stored systems: refusing by name still confirms
    # that a route into some network exists and roughly who has it.
    if level is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    if manage and level not in ("owner", "manage"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You can route through this node but not change it. Ask its owner for manage access.",
        )
    return level


async def _apply_grants(
    db: AsyncSession, node: RelayNode, grants: list[NodeGrant] | None, owner: User
) -> None:
    if grants is None:
        return
    for existing in list(node.grants):
        await db.delete(existing)
    node.grants = []
    await db.flush()
    seen: set[int] = set()
    for grant in grants:
        if grant.level not in LEVELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Access level must be one of {', '.join(LEVELS)} (got {grant.level!r})",
            )
        if grant.user_id in seen or grant.user_id == (node.owner_id or owner.id):
            continue
        seen.add(grant.user_id)
        db.add(RelayNodeAccess(node_id=node.id, user_id=grant.user_id, level=grant.level))


def _issue_token(node: RelayNode) -> str:
    token, node.enrolment_token_hash = relay.mint_enrolment_token(node.id)
    node.enrolment_token_expires_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.relay_enrolment_token_ttl_hours
    )
    return token


def _install_hint(request: Request, node: RelayNode, token: str) -> str:
    """Kept as the Linux one-liner, for anything reading the older field."""
    base = str(request.base_url).rstrip("/")
    return f"sudo ./install.sh --url {base} --token {token} --name {node.slug}"


def _install_commands(request: Request, node: RelayNode, token: str) -> list[InstallCommand]:
    """One command per platform, spelled the way each installer actually takes it.

    Written out rather than templated from a single string: the three
    installers do not share a flag between them — `--url` against `-Url`
    against an environment variable — and a UI that guesses at that teaches
    people a command that does not work.

    Each note now names the unzipped folder rather than `deploy/relay`, because
    the installer is downloaded beside this command instead of being assumed to
    already be on the machine. It said "run it from deploy/relay" on a fresh
    Windows box that had never seen the repository.
    """
    base = str(request.base_url).rstrip("/")
    return [
        InstallCommand(
            platform="linux",
            label="Linux (systemd)",
            command=f"sudo ./install.sh --url {base} --token {token} --name {node.slug}",
            note=(
                "Download the installer, unzip it, and run this from inside that "
                "folder. Installs a systemd unit and starts it."
            ),
        ),
        InstallCommand(
            platform="windows",
            label="Windows (service)",
            command=f".\\install.ps1 -Url {base} -Token {token}",
            note=(
                "Download the installer, unzip it, and run this from inside that "
                "folder in a PowerShell started as Administrator — creating a "
                "service needs it. Python 3 must be installed first: "
                "winget install --id Python.Python.3.12 --scope machine"
            ),
        ),
        InstallCommand(
            platform="docker",
            label="Docker",
            command=(
                f"AIOPS_RELAY_URL={base} AIOPS_RELAY_TOKEN={token} "
                "docker compose up -d --build"
            ),
            note=(
                "Download the installer, unzip it, and run this from inside that "
                "folder, where the compose file lives."
            ),
        ),
    ]


# --- management --------------------------------------------------------
@router.get("", response_model=list[NodeOut])
async def list_nodes(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Only nodes this user owns or was granted — the stored-systems rule."""
    rows = await db.scalars(
        select(RelayNode)
        .outerjoin(RelayNodeAccess, RelayNodeAccess.node_id == RelayNode.id)
        .where(or_(RelayNode.owner_id == user.id, RelayNodeAccess.user_id == user.id))
        .order_by(RelayNode.name)
        .distinct()
    )
    counts = await _target_counts(db)
    out = []
    for node in rows:
        level = node_level_for(node, user)
        if level is not None:
            out.append(_out(node, level, counts))
    return out


@router.get("/pending", response_model=list[NodeOut])
async def list_pending(_: User = Depends(current_admin), db: AsyncSession = Depends(get_db)):
    """Nodes waiting to be approved.

    Separate from the list above, and admin-only, because approving a node is
    an administrator's job while *using* one is not something administering
    AIOps confers. An admin sees enough here to make that call — what the node
    says it is and what it can reach — and no right to send traffic through it.
    """
    rows = await db.scalars(
        select(RelayNode).where(RelayNode.status == "pending").order_by(RelayNode.created_at)
    )
    counts = await _target_counts(db)
    return [_out(node, "", counts) for node in rows]


@router.get("/installer/{platform}")
async def download_installer(platform: str, _: User = Depends(current_user)):
    """The installer files for one platform, as a zip.

    Behind `current_user` rather than open: there is nothing secret in here —
    it is the same source anyone with the repository already has — but an
    unauthenticated endpoint on an AIOps is a thing to justify, and this one
    cannot be. It is not admin-only either, because whoever registers a node is
    the person who then has to install it.

    The enrolment token is never written into the bundle; see installer.py for
    why. It goes in the command, which is why the command is shown beside the
    download rather than instead of it.
    """
    try:
        payload = installer.build_bundle(platform)
    except KeyError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No installer for {platform!r}. Choose one of: "
            + ", ".join(sorted(installer.BUNDLES)),
        )
    except FileNotFoundError as exc:  # a deployment built without deploy/relay
        log.error("relay installer bundle unavailable: %s", exc)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "The relay installer files are missing from this AIOps deployment.",
        )
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{installer.bundle_name(platform)}"',
            # Identical for every caller and every node, so it may be cached —
            # which is only true because the token is not in it.
            "Cache-Control": "private, max-age=300",
        },
    )


@router.post("", response_model=NodeEnrolmentOut, status_code=status.HTTP_201_CREATED)
async def create_node(
    payload: NodeIn,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a node and mint the one-time token that installs it.

    The token is in this response and nowhere else — only its hash is stored —
    so losing it means issuing another rather than reading the old one back.
    """
    name = payload.name.strip()
    slug = _slugify(name)
    if await db.scalar(select(RelayNode).where((RelayNode.name == name) | (RelayNode.slug == slug))):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A relay node with that name already exists. Names are shared across AIOps "
            "because a stored system points at one by name.",
        )

    node = RelayNode(
        name=name,
        slug=slug,
        description=payload.description,
        status="pending",
        networks=[],
        created_by_id=user.id,
        owner_id=user.id,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    token = _issue_token(node)
    await _apply_grants(db, node, payload.grants, user)
    await db.commit()
    await db.refresh(node)
    log.info("relay node %r registered by %r, awaiting enrolment", node.slug, user.username)
    return NodeEnrolmentOut(
        node=_out(node, "owner", await _target_counts(db)),
        enrolment_token=token,
        expires_at=node.enrolment_token_expires_at,
        install_hint=_install_hint(request, node, token),
        install=_install_commands(request, node, token),
    )


@router.post("/{node_id}/token", response_model=NodeEnrolmentOut)
async def reissue_token(
    node_id: int,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a fresh enrolment token, invalidating any outstanding one."""
    node = await db.get(RelayNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    level = _require(node, user, manage=True)
    if node.status == "revoked":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This node is revoked. Delete it and register a new one rather than "
            "re-enrolling the machine that was withdrawn.",
        )
    token = _issue_token(node)
    # Re-enrolling replaces the credential, and whatever an administrator
    # vouched for previously may not be the machine that turns up next, so the
    # node goes back to pending and has to be approved again.
    node.status = "pending"
    node.credential_hash = None
    await db.commit()
    await db.refresh(node)
    await _disconnect(node.id, CLOSE_REVOKED, "re-enrolment")
    return NodeEnrolmentOut(
        node=_out(node, level, await _target_counts(db)),
        enrolment_token=token,
        expires_at=node.enrolment_token_expires_at,
        install_hint=_install_hint(request, node, token),
        install=_install_commands(request, node, token),
    )


@router.patch("/{node_id}", response_model=NodeOut)
async def update_node(
    node_id: int,
    payload: NodePatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    node = await db.get(RelayNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    level = _require(node, user, manage=True)
    data = payload.model_dump(exclude_unset=True)

    new_owner = data.pop("owner_id", None)
    if new_owner is not None and new_owner != node.owner_id:
        if level != "owner":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "Only the owner can hand this node to someone else"
            )
        if await db.get(User, new_owner) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "That user does not exist")
        node.owner_id = new_owner

    grants = data.pop("grants", None)
    if data.get("name"):
        # The slug is what a stored system's ProxyCommand names, so it moves
        # with the name rather than being edited separately.
        node.name = data["name"].strip()
        node.slug = _slugify(node.name)
    if "description" in data:
        node.description = data["description"]

    await _apply_grants(
        db, node, [NodeGrant(**g) for g in grants] if grants is not None else None, user
    )
    await db.commit()
    await db.refresh(node)
    return _out(node, node_level_for(node, user) or level, await _target_counts(db))


@router.post("/{node_id}/approve", response_model=NodeOut)
async def approve_node(
    node_id: int, admin: User = Depends(current_admin), db: AsyncSession = Depends(get_db)
):
    """Let a node carry traffic. Administrator only, by design."""
    node = await db.get(RelayNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    if node.status == "revoked":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A revoked node cannot be approved again. Register a new one.",
        )
    if not node.credential_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This node has not enrolled yet, so there is nothing to approve. Run the "
            "installer on the machine first.",
        )
    node.status = "approved"
    await db.commit()
    await db.refresh(node)
    log.warning("relay node %r approved by %r", node.slug, admin.username)
    return _out(node, node_level_for(node, admin) or "", await _target_counts(db))


@router.post("/{node_id}/revoke", response_model=NodeOut)
async def revoke_node(
    node_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    """Withdraw a node, now.

    Its credential stops working, its live connection is closed, and anything
    routed through it fails from here on. Available to an administrator or to
    whoever manages the node — taking a route out of service must not require
    finding the one person who can do it.
    """
    node = await db.get(RelayNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    if not user.is_admin:
        _require(node, user, manage=True)
    node.status = "revoked"
    # The credential hash is kept, deliberately. It authenticates but no longer
    # authorises anything, and that distinction is what lets the machine be told
    # it was withdrawn rather than that it is unrecognised — the difference
    # between an agent that stops and one that reconnects every minute forever.
    node.enrolment_token_hash = None
    node.enrolment_token_expires_at = None
    await db.commit()
    await db.refresh(node)
    await _disconnect(node.id, CLOSE_REVOKED, "revoked")
    log.warning("relay node %r revoked by %r", node.slug, user.username)
    return _out(node, node_level_for(node, user) or "", await _target_counts(db))


@router.delete("/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    node_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    node = await db.get(RelayNode, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Relay node not found")
    if not user.is_admin:
        _require(node, user, manage=True)
    # Refused rather than quietly unbinding: a system that was reachable only
    # through this node would silently become one that tries to dial a private
    # address from the AIOps server and fail for reasons nobody would connect
    # to this delete.
    bound = list(await db.scalars(select(Target.name).where(Target.relay_node_id == node_id)))
    if bound:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "These systems still route through this node: "
            + ", ".join(sorted(bound)[:10])
            + ". Point them elsewhere first.",
        )
    await _disconnect(node.id, CLOSE_REVOKED, "deleted")
    await db.delete(node)
    await db.commit()


async def _disconnect(node_id: int, code: int, reason: str) -> None:
    """Close a node's live control channel, if it has one."""
    conn = relay.hub.connection(node_id)
    if conn is None:
        return
    relay.hub.abandon(node_id)
    with contextlib.suppress(Exception):
        await conn.websocket.close(code=code, reason=reason)


# --- node-facing -------------------------------------------------------
@relay_router.post("/enroll", response_model=NodeEnrolOut)
async def enroll(
    payload: NodeEnrolIn, request: Request, db: AsyncSession = Depends(get_db)
):
    """Trade a one-time enrolment token for the node's long-lived credential.

    Unauthenticated in the cookie sense — the machine being installed has no
    user session — so the token is the whole of the proof, and it is spent
    here: the hash is cleared before the response is written, and a replay of
    the same token finds nothing to match. Enrolling does not make the node
    usable; that still needs an administrator.
    """
    address = client_address(request)
    wait = throttle.retry_after("relay-enrolment", address)
    if wait:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many enrolment attempts. Try again in {wait}s.",
        )

    parts = relay.split_credential(payload.token)
    node = await db.get(RelayNode, parts[0]) if parts else None
    ok = (
        node is not None
        and node.enrolment_token_hash is not None
        and node.status != "revoked"
        and relay.verify_password(parts[1], node.enrolment_token_hash)
    )
    if ok and node.enrolment_token_expires_at is not None:
        expires = node.enrolment_token_expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        ok = expires > datetime.now(timezone.utc)
    if not ok or node is None:
        throttle.record_failure("relay-enrolment", address)
        # One message for every failure mode. A token that is merely expired
        # must not be distinguishable from one that was never valid.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "That enrolment token is not valid. Tokens are single-use and expire; "
            "ask AIOps for a new one.",
        )

    credential, node.credential_hash = relay.mint_credential(node.id)
    node.enrolment_token_hash = None
    node.enrolment_token_expires_at = None
    node.enrolled_at = datetime.now(timezone.utc)
    node.status = "pending" if node.status != "approved" else node.status
    if payload.version:
        node.version = payload.version[:64]
    if payload.hostname:
        node.reported_hostname = payload.hostname[:255]
    if payload.networks:
        node.networks = [str(n)[:64] for n in payload.networks[:32]]
    await db.commit()
    throttle.record_success("relay-enrolment", address)
    log.warning(
        "relay node %r enrolled from %s as %r",
        node.slug,
        address,
        node.reported_hostname,
    )
    return NodeEnrolOut(
        node_id=node.id,
        slug=node.slug,
        name=node.name,
        status=node.status,
        credential=credential,
        message=(
            "Enrolled. This node stays pending and carries no traffic until an AIOps "
            "administrator approves it."
        )
        if node.status != "approved"
        else "Enrolled and already approved.",
    )


async def _refuse(websocket: WebSocket, code: int, reason: str) -> None:
    """Turn a node away, having first told it why.

    The socket is accepted and then closed rather than refused outright, which
    is the opposite of what the browser-facing feed in ws.py does. The reason is
    the client: an ASGI server turns a close-before-accept into a bare HTTP 403,
    and a node that cannot tell "not approved yet" from "revoked" either gives
    up while an administrator is still deciding, or retries forever after being
    withdrawn.
    """
    await websocket.accept()
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "denied", "code": code, "reason": reason})
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


async def _authenticate(websocket: WebSocket) -> RelayNode | None:
    """Check the node's credential on this connection.

    Run for the control channel *and* for every stream, so authentication is a
    property of each connection rather than something established once at
    enrolment and trusted thereafter.
    """
    header = websocket.headers.get("authorization", "")
    raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
    async with SessionLocal() as db:
        node = await relay.node_for_credential(db, raw)
        if node is None:
            return None
        # Detached deliberately: the caller holds it past this session, and only
        # reads the few fields it needs.
        db.expunge(node)
        return node


@relay_router.websocket("/connect")
async def node_control(websocket: WebSocket):
    """A node's persistent outbound connection.

    It carries no data — only "open a connection to host:port" in one direction
    and heartbeats in both. The bytes travel on their own sockets.
    """
    node = await _authenticate(websocket)
    if node is None:
        await _refuse(websocket, CLOSE_UNAUTHENTICATED, "Unknown node credential")
        return
    if node.status == "revoked":
        await _refuse(websocket, CLOSE_REVOKED, "This node has been revoked")
        return
    if node.status != "approved":
        await _refuse(websocket, CLOSE_NOT_APPROVED, "This node is waiting to be approved")
        return

    await websocket.accept()
    conn = relay.NodeConnection(node.id, node.slug, websocket)
    displaced = relay.hub.register(conn)
    if displaced is not None:
        relay.hub.abandon(node.id)
        with contextlib.suppress(Exception):
            await displaced.websocket.close(code=1012, reason="Replaced by a newer connection")
    await conn.send({"type": "hello", "node": node.slug, "heartbeat": HEARTBEAT_SECONDS})
    log.info("relay node %r connected", node.slug)

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(), timeout=HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                # The heartbeat doubles as the revocation check: a node that
                # was withdrawn while connected is dropped within one interval
                # rather than at the end of whatever it is carrying.
                if not await _still_approved(node.id):
                    await websocket.close(code=CLOSE_REVOKED, reason="This node has been revoked")
                    return
                await conn.send({"type": "ping"})
                continue

            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "ready":
                await relay.mark_seen(
                    node.id,
                    version=message.get("version"),
                    hostname=message.get("hostname"),
                    networks=message.get("networks"),
                )
            elif kind == "pong":
                await relay.mark_seen(node.id)
            elif kind == "open.failed":
                relay.hub.fail_stream(
                    str(message.get("stream")),
                    node.id,
                    str(message.get("error") or "the relay node could not reach that host"),
                )
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("relay control channel for %r closed unexpectedly", node.slug, exc_info=True)
    finally:
        relay.hub.unregister(conn)
        relay.hub.abandon(node.id)
        log.info("relay node %r disconnected", node.slug)


@relay_router.websocket("/stream")
async def node_stream(websocket: WebSocket, stream: str = ""):
    """One proxied connection, dialled back by the node.

    The node opens the far socket first and only then dials this, so its
    arrival is what tells the waiting forwarder that the host answered.
    """
    node = await _authenticate(websocket)
    if node is None:
        await _refuse(websocket, CLOSE_UNAUTHENTICATED, "Unknown node credential")
        return
    if node.status != "approved":
        await _refuse(websocket, CLOSE_NOT_APPROVED, "This node is not approved")
        return

    pending = relay.hub.claim_stream(stream, node.id)
    if pending is None:
        # Either a stale id, or a node answering for a connection it was not
        # asked to make. Neither gets a socket.
        await _refuse(websocket, 1008, "No such pending connection")
        return

    await websocket.accept()
    if not pending.opened.done():
        pending.opened.set_result(True)
    try:
        # The forwarder handed its socket over before resolving `opened`; it is
        # parked on `finished`, which pump() sets.
        await relay.pump(pending, websocket)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.debug("relay stream for %r ended unexpectedly", node.slug, exc_info=True)
    finally:
        pending.finished.set()
        relay.hub.release(node.id)


async def _still_approved(node_id: int) -> bool:
    async with SessionLocal() as db:
        node = await db.get(RelayNode, node_id)
        return node is not None and node.status == "approved"
