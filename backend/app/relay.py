"""The machinery behind relay nodes: who is connected, and how bytes cross.

A node holds one outbound websocket to AIOps — the *control* channel — and
does nothing on it but wait to be asked. When a run wants to reach a host on
the node's network the path is:

    ssh → ProxyCommand helper → 127.0.0.1 forwarder (this file)
        → control channel → the node → TCP to the far host

Each proxied connection gets its own websocket rather than being multiplexed
onto the control channel. That is a deliberate trade: one more dial-out per
connection, in exchange for no framing layer of our own, no stream ids in the
data path, and no head-of-line blocking between an interactive shell and a
file copy sharing a node.

Nothing here ever sees a provider credential or an SSH key. The node is told a
host and a port and copies bytes; the SSH session is end-to-end between the
AIOps container and the far host, so what crosses the node is ciphertext it
holds no key for.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import or_, select

from .access import node_level_for
from .config import settings
from .db import SessionLocal
from .models import RelayNode, RelayNodeAccess
from .security import hash_password, verify_password

log = logging.getLogger("aiops.relay")

#: Bytes moved per read. Matches the SSH channel window closely enough that an
#: interactive session is not chopped into needless websocket frames.
CHUNK = 64 * 1024

#: The one line a ProxyCommand helper speaks before its bytes become the
#: connection. Versioned so an older helper left in a stale run directory is
#: refused rather than misread.
PROTOCOL = "AIOPS-RELAY/1"


class RelayError(Exception):
    """A connection could not be opened. The text reaches the agent's terminal."""


# --- what a node is allowed to reach -----------------------------------
#: No /8, and certainly no 0.0.0.0/0. Sixteen bits is already 65k hosts; the
#: point of the floor is that a slip of the keyboard cannot turn one node into
#: a route to everything the machine can see.
MIN_CIDR_PREFIX = 16
#: A node here serves one LAN. A list this length is a workable number of
#: segments and a hard stop on somebody pasting a routing table in.
MAX_ALLOWED_CIDRS = 10
MAX_ALLOWED_PORTS = 10
#: What an empty port list means. Never "everything".
DEFAULT_ALLOWED_PORTS = (22,)


class SubnetRuleError(ValueError):
    """A CIDR or port list that cannot be accepted. The text reaches the user."""


def normalise_cidrs(values) -> list[str]:
    """Validate the subnets a node may be routed to, or explain the refusal.

    The private-address rule is the anti-open-proxy guard and the reason this
    is not merely a shape check: without it a node — which by design dials out
    from inside somebody's network and copies bytes for whoever asks — becomes
    a way to reach the public internet from an address that is not ours, and
    AIOps becomes the thing that pointed it there.

    IPv4 only, deliberately. Not because the gate could not compare a v6
    address, but because the ssh config generated alongside it is built from
    dotted-quad globs, and a range the gate allows but no generated Host
    pattern matches is a feature that looks configured and does nothing.
    """
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SubnetRuleError("Allowed networks must be a list of CIDRs, such as 198.51.100.0/24.")
    if len(values) > MAX_ALLOWED_CIDRS:
        raise SubnetRuleError(
            f"At most {MAX_ALLOWED_CIDRS} networks per node (got {len(values)})."
        )
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise SubnetRuleError(f"{text!r} is not a network in CIDR form: {exc}") from None
        if network.version != 4:
            raise SubnetRuleError(
                f"{text!r} is IPv6. Only IPv4 subnets can be routed through a node today."
            )
        if not network.is_private:
            raise SubnetRuleError(
                f"{text!r} is not a private network. A relay node may only be pointed at "
                "private address space — routing public addresses through it would make "
                "AIOps an open proxy."
            )
        if network.prefixlen < MIN_CIDR_PREFIX:
            raise SubnetRuleError(
                f"{text!r} is too broad. Use /{MIN_CIDR_PREFIX} or narrower — a node is a "
                "route into one network, not into everything it can see."
            )
        # Stored normalised, so 198.51.100.5/24 is kept as the range it means
        # rather than as an address that reads like a single host.
        listed = str(network)
        if listed not in out:
            out.append(listed)
    return out


def normalise_ports(values) -> list[int]:
    """Validate the ports a subnet route may use."""
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise SubnetRuleError("Allowed ports must be a list of numbers, such as [22].")
    if len(values) > MAX_ALLOWED_PORTS:
        raise SubnetRuleError(f"At most {MAX_ALLOWED_PORTS} ports per node (got {len(values)}).")
    out: list[int] = []
    for value in values:
        try:
            port = int(str(value).strip())
        except (TypeError, ValueError):
            raise SubnetRuleError(f"{value!r} is not a port number.") from None
        if not 1 <= port <= 65535:
            raise SubnetRuleError(f"{port} is not a port number — ports run from 1 to 65535.")
        if port not in out:
            out.append(port)
    return out


def subnet_rules(node: RelayNode) -> list[tuple[str, ipaddress.IPv4Network, tuple[int, ...]]]:
    """One node's subnet reach, as the gate compares it.

    Whatever is in the column is re-validated on the way out rather than
    trusted: the rows outlive the code that wrote them, and a rule that no
    longer passes today's checks must stop working rather than keep its
    grandfathered reach.
    """
    try:
        cidrs = normalise_cidrs(node.allowed_cidrs)
        ports = normalise_ports(node.allowed_ports)
    except SubnetRuleError as exc:
        log.warning("relay node %r has an unusable subnet rule, ignoring it: %s", node.slug, exc)
        return []
    if not cidrs:
        return []
    allowed = tuple(ports) or DEFAULT_ALLOWED_PORTS
    return [(node.slug, ipaddress.ip_network(c), allowed) for c in cidrs]


# --- credentials -------------------------------------------------------
def mint_credential(node_id: int) -> tuple[str, str]:
    """A node's long-lived secret, and the hash to store.

    The id travels with the secret so a reconnecting node can be looked up in
    one query without the secret ever being used as an index. Only the hash is
    kept, so a database copy does not let anyone impersonate a node.
    """
    secret = secrets.token_urlsafe(32)
    return f"{node_id}.{secret}", hash_password(secret)


def mint_enrolment_token(node_id: int) -> tuple[str, str]:
    """The one-time token an installer is given. Same shape, different purpose."""
    return mint_credential(node_id)


def split_credential(raw: str) -> tuple[int, str] | None:
    node_id, _, secret = (raw or "").partition(".")
    if not node_id.isdigit() or not secret:
        return None
    return int(node_id), secret


async def node_for_credential(db, raw: str) -> RelayNode | None:
    """The node this credential belongs to, or None. Never says which half failed."""
    parts = split_credential(raw)
    if parts is None:
        return None
    node_id, secret = parts
    node = await db.get(RelayNode, node_id)
    if node is None or not node.credential_hash:
        return None
    if not verify_password(secret, node.credential_hash):
        return None
    return node


# --- run-scoped permission to use a node -------------------------------
class RelayGrant:
    """Everything one run may ask the forwarder for, and who is asking.

    Two kinds of reach, both fixed when the run's ssh config is written:

    * `routes` — exact (node, host, port) triples, one per stored system the
      requester may reach. A hostname is allowed here because an operator
      typed it and it names one machine.
    * `subnets` — (node, network, ports) rules, from nodes the requester may
      route through that have been given an explicit CIDR. These match by
      address only; see `allows` for why a name is never resolved against one.

    `who` is carried so the stream log says which person and which turn a
    connection belongs to, rather than only that some run opened one.
    """

    def __init__(
        self,
        routes: set[tuple[str, str, int]],
        subnets: list[tuple[str, ipaddress.IPv4Network, tuple[int, ...]]] | None = None,
        who: str = "",
    ) -> None:
        self.routes = set(routes)
        self.subnets = list(subnets or [])
        self.who = who


class RelayTokens:
    """What a run is allowed to ask the forwarder for.

    A token carries the reach materialised for that run — the systems its
    requester may already use, plus any subnets the nodes they may route
    through have been opened to — so the agent cannot point the helper at an
    arbitrary address. Tokens live in memory only and die with the run.

    The reach is a snapshot taken when the run started, deliberately: narrowing
    a node's subnets takes effect for the next turn rather than mid-connection,
    the same as removing a stored system does. Revoking the *node* is the thing
    that stops traffic immediately, and it still does — it closes the control
    channel, so nothing can be opened through it whatever a token says.
    """

    def __init__(self) -> None:
        self._grants: dict[str, RelayGrant] = {}

    def issue(
        self,
        routes: set[tuple[str, str, int]],
        subnets: list[tuple[str, ipaddress.IPv4Network, tuple[int, ...]]] | None = None,
        who: str = "",
    ) -> str:
        token = secrets.token_urlsafe(32)
        self._grants[token] = RelayGrant(routes, subnets, who)
        return token

    def allows(self, token: str, node_slug: str, host: str, port: int) -> bool:
        """The authorisation check for one stream. Everything else defers to this.

        A subnet rule matches an *address*, never a name. The helper is handed
        ssh's `%h` verbatim, so a name would have to be resolved to be compared
        — and the resolver that would do it is the AIOps container's, which
        knows nothing about the far network and everything about ours. A name
        that happened to resolve to an in-range address here would authorise a
        connection the node then makes to a completely different machine. So
        names are only ever reachable as the exact triples an operator stored.
        """
        grant = self._grants.get(token)
        if grant is None:
            return False
        if (node_slug, host, port) in grant.routes:
            return True
        if not grant.subnets:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(
            slug == node_slug and port in ports and address in network
            for slug, network, ports in grant.subnets
        )

    def describe(self, token: str) -> str:
        """Who this token belongs to, for the log line. Never the token itself."""
        grant = self._grants.get(token)
        return grant.who if grant is not None else "unknown requester"

    def revoke(self, token: str | None) -> None:
        if token:
            self._grants.pop(token, None)


tokens = RelayTokens()


# --- live connections --------------------------------------------------
class RelayStream:
    """One proxied TCP connection, from the forwarder's side.

    It exists from the moment the forwarder asks a node to open a connection
    until the pumping in the stream endpoint finishes. Three signals, in order:
    `opened` tells the forwarder the far host answered, `go` tells the stream
    endpoint the forwarder has finished with the helper's socket and pumping may
    start, and `finished` tells the forwarder the endpoint is done with it.

    `go` exists because the first thing an SSH server sends is its banner,
    unprompted. Without it those bytes race the forwarder's own status line and
    arrive in front of it, and the helper reads a version string where it
    expected "OK".
    """

    def __init__(
        self,
        node_id: int,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self.node_id = node_id
        self.host = host
        self.port = port
        self.reader = reader
        self.writer = writer
        loop = asyncio.get_running_loop()
        self.opened: asyncio.Future[bool] = loop.create_future()
        self.go = asyncio.Event()
        self.finished = asyncio.Event()


class NodeConnection:
    """One node's control channel, as seen from the server."""

    def __init__(self, node_id: int, slug: str, websocket) -> None:
        self.node_id = node_id
        self.slug = slug
        self.websocket = websocket
        self.connected_at = datetime.now(timezone.utc)
        self.streams = 0
        self._send_lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        # Starlette does not serialise concurrent sends on one socket, and two
        # runs opening connections at the same moment would interleave frames.
        async with self._send_lock:
            await self.websocket.send_json(message)


class RelayHub:
    """Every connected node, and every connection in flight through one."""

    def __init__(self) -> None:
        self._nodes: dict[int, NodeConnection] = {}
        self._by_slug: dict[str, int] = {}
        self._streams: dict[str, RelayStream] = {}
        self._forwarder: asyncio.Server | None = None
        #: The port the forwarder actually bound. Read when a run's ssh config is
        #: written, so an ephemeral port works and nothing has to agree in advance.
        self.forwarder_port: int | None = None

    # -- control channel ------------------------------------------------
    def register(self, conn: NodeConnection) -> NodeConnection | None:
        """Adopt a node's connection, returning whichever one it displaces.

        A node that lost its network without the server noticing reconnects
        while the dead socket is still registered. The newest connection wins —
        it is the one demonstrably alive — and the caller closes the old one.
        """
        previous = self._nodes.get(conn.node_id)
        self._nodes[conn.node_id] = conn
        self._by_slug[conn.slug] = conn.node_id
        return previous

    def unregister(self, conn: NodeConnection) -> None:
        if self._nodes.get(conn.node_id) is conn:
            self._nodes.pop(conn.node_id, None)
            self._by_slug.pop(conn.slug, None)

    def connection(self, node_id: int) -> NodeConnection | None:
        return self._nodes.get(node_id)

    def is_online(self, node_id: int) -> bool:
        return node_id in self._nodes

    def online_ids(self) -> set[int]:
        return set(self._nodes)

    # -- opening a connection through a node ----------------------------
    async def open(
        self,
        slug: str,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> RelayStream:
        """Ask a node to connect to host:port and hand the socket back here."""
        node_id = self._by_slug.get(slug)
        conn = self._nodes.get(node_id) if node_id is not None else None
        if conn is None:
            raise RelayError(
                f"relay node {slug!r} is not connected, so this host cannot be reached"
            )
        if conn.streams >= settings.relay_max_streams_per_node:
            raise RelayError(
                f"relay node {slug!r} already has {conn.streams} connections open"
            )

        stream_id = secrets.token_urlsafe(18)
        stream = RelayStream(conn.node_id, host, port, reader, writer)
        self._streams[stream_id] = stream
        conn.streams += 1
        try:
            await conn.send(
                {"type": "open", "stream": stream_id, "host": host, "port": port}
            )
            await asyncio.wait_for(stream.opened, timeout=settings.relay_connect_timeout_seconds)
        except asyncio.TimeoutError:
            self.drop_stream(stream_id)
            raise RelayError(
                f"relay node {slug!r} did not open a connection to {host}:{port} within "
                f"{settings.relay_connect_timeout_seconds}s"
            ) from None
        except Exception:
            self.drop_stream(stream_id)
            raise
        return stream

    def claim_stream(self, stream_id: str, node_id: int) -> RelayStream | None:
        """Take the pending stream a node is dialling back for.

        Bound to the node that was asked: a second node holding a valid
        credential must not be able to answer for the first one.
        """
        stream = self._streams.get(stream_id)
        if stream is None or stream.node_id != node_id:
            return None
        return self._streams.pop(stream_id)

    def fail_stream(self, stream_id: str, node_id: int, error: str) -> None:
        stream = self._streams.get(stream_id)
        if stream is None or stream.node_id != node_id:
            return
        self._streams.pop(stream_id, None)
        self.release(node_id)
        if not stream.opened.done():
            stream.opened.set_exception(RelayError(error))

    def drop_stream(self, stream_id: str) -> None:
        stream = self._streams.pop(stream_id, None)
        if stream is not None:
            self.release(stream.node_id)

    def release(self, node_id: int) -> None:
        conn = self._nodes.get(node_id)
        if conn is not None and conn.streams > 0:
            conn.streams -= 1

    def abandon(self, node_id: int) -> None:
        """Fail everything waiting on a node whose control channel just died."""
        for stream_id, stream in list(self._streams.items()):
            if stream.node_id != node_id:
                continue
            self._streams.pop(stream_id, None)
            if not stream.opened.done():
                stream.opened.set_exception(
                    RelayError("the relay node disconnected before the connection was opened")
                )

    # -- the local forwarder --------------------------------------------
    async def start_forwarder(self) -> None:
        """Listen on the loopback for ProxyCommand helpers.

        Bound to 127.0.0.1 because the only thing entitled to speak this
        protocol is a process inside this container — the same reasoning as the
        approval bridge's callback. A failure to bind is logged rather than
        fatal: it disables relay routing, and every other part of AIOps works.
        """
        if self._forwarder is not None:
            return
        try:
            self._forwarder = await asyncio.start_server(
                self._serve, settings.relay_forwarder_host, settings.relay_forwarder_port
            )
        except OSError as exc:
            log.error(
                "relay forwarder could not listen on %s:%s (%s). Systems routed through a "
                "relay node will fail to connect; everything else is unaffected.",
                settings.relay_forwarder_host,
                settings.relay_forwarder_port,
                exc,
            )
            return
        self.forwarder_port = self._forwarder.sockets[0].getsockname()[1]
        log.info(
            "relay forwarder listening on %s:%s",
            settings.relay_forwarder_host,
            self.forwarder_port,
        )

    async def stop_forwarder(self) -> None:
        if self._forwarder is None:
            return
        self._forwarder.close()
        with contextlib.suppress(Exception):
            await self._forwarder.wait_closed()
        self._forwarder = None
        self.forwarder_port = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream: RelayStream | None = None
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
            except asyncio.TimeoutError:
                return
            parts = line.decode("utf-8", errors="replace").strip().split(" ")
            if len(parts) != 5 or parts[0] != PROTOCOL:
                writer.write(b"ERR malformed request\n")
                await writer.drain()
                return
            _, token, slug, host, port_text = parts
            try:
                port = int(port_text)
            except ValueError:
                writer.write(b"ERR bad port\n")
                await writer.drain()
                return

            # THE GATE. The token names exactly the reach this run was set up
            # with — the stored systems its requester may use, and any subnet
            # their nodes were explicitly opened to. Anything else is refused
            # here, before a node is contacted.
            #
            # This is the only place that decides, on purpose. The ssh config
            # written for a run is a convenience for the agent and is not a
            # boundary: its Host patterns are globs, which cannot express a
            # CIDR exactly, and nothing stops an agent from running the
            # ProxyCommand helper itself with any address it likes. Both paths
            # arrive here.
            who = tokens.describe(token)
            if not tokens.allows(token, slug, host, port):
                writer.write(b"ERR this run is not permitted to reach that host\n")
                await writer.drain()
                log.warning(
                    "relay: refused %s:%s via node %s for %s — outside this run's reach",
                    host,
                    port,
                    slug,
                    who,
                )
                return

            try:
                stream = await self.open(slug, host, port, reader, writer)
            except RelayError as exc:
                writer.write(f"ERR {exc}\n".encode())
                await writer.drain()
                return

            writer.write(b"OK\n")
            await writer.drain()
            # Only now may the far host's first bytes be delivered.
            stream.go.set()
            log.info("relay: %s:%s opened via node %s for %s", host, port, slug, who)
            await stream.finished.wait()
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001 - one helper must not take the listener down
            log.exception("relay forwarder connection failed")
        finally:
            if stream is not None:
                stream.finished.set()
            with contextlib.suppress(Exception):
                writer.close()


hub = RelayHub()


# --- pumping one stream ------------------------------------------------
async def pump(stream: RelayStream, websocket) -> None:
    """Copy bytes between the helper's socket and the node's websocket.

    Either side closing ends both: an SSH connection has no useful half-open
    state, and leaving one direction running would hold a node stream slot for
    a session that is already over.
    """
    reader, writer = stream.reader, stream.writer
    try:
        # The forwarder may have given up while the node was dialling back.
        await asyncio.wait_for(stream.go.wait(), timeout=settings.relay_connect_timeout_seconds)
    except asyncio.TimeoutError:
        return

    async def to_node() -> None:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                return
            await websocket.send_bytes(data)

    async def to_helper() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                if text is None:
                    return
                data = text.encode()
            writer.write(data)
            await writer.drain()

    first = asyncio.create_task(to_node())
    second = asyncio.create_task(to_helper())
    try:
        await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (first, second):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            writer.close()
        stream.finished.set()


# --- bookkeeping -------------------------------------------------------
async def mark_seen(node_id: int, *, version=None, hostname=None, networks=None) -> None:
    """Record that a node checked in, and whatever it says about itself."""
    async with SessionLocal() as db:
        node = await db.get(RelayNode, node_id)
        if node is None:
            return
        node.last_seen_at = datetime.now(timezone.utc)
        if version:
            node.version = str(version)[:64]
        if hostname:
            node.reported_hostname = str(hostname)[:255]
        if networks is not None and isinstance(networks, list):
            node.networks = [str(n)[:64] for n in networks[:32]]
        await db.commit()


async def nodes_for_targets(db, targets) -> dict[int, RelayNode]:
    """The relay nodes the given systems are bound to, keyed by id."""
    wanted = {t.relay_node_id for t in targets if getattr(t, "relay_node_id", None)}
    if not wanted:
        return {}
    rows = await db.scalars(select(RelayNode).where(RelayNode.id.in_(wanted)))
    return {node.id: node for node in rows}


async def subnet_nodes_for(db, user) -> list[RelayNode]:
    """Nodes this user may route a whole subnet through.

    Scoped to the person, exactly like `visible_targets`: a node is a way into
    somebody's network, so a turn gets the subnets of whoever *asked for the
    turn* — not the session's owner, and not everything on the instance. An
    administrator gets nothing here they were not given; see access.py, where
    that asymmetry is spelled out and is deliberate.

    A node with no CIDRs set is not returned at all, which is every node until
    someone deliberately opens one. Only approved nodes: a pending or revoked
    one carries no traffic anyway, and materialising routes for it would put
    hosts in the agent's briefing that can never answer.
    """
    if user is None:
        return []
    rows = await db.scalars(
        select(RelayNode)
        .where(
            RelayNode.status == "approved",
            or_(
                RelayNode.owner_id == user.id,
                RelayNode.id.in_(
                    select(RelayNodeAccess.node_id).where(RelayNodeAccess.user_id == user.id)
                ),
            ),
        )
        .order_by(RelayNode.name)
    )
    # The query narrows; `node_level_for` decides. Two rules that agree today,
    # and only one of them is the one the rest of AIOps asks.
    return [node for node in rows if node_level_for(node, user) is not None and subnet_rules(node)]
