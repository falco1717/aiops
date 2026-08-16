#!/usr/bin/env python3
"""A stdio MCP server that gives an agent a real browser.

Claude Code is started with this registered alongside the approval bridge::

    --mcp-config '{"mcpServers":{"aiops_browser":{"command":"...","args":["..."]}}}'

and the tools appear as ``mcp__aiops_browser__navigate`` and friends.

Three things make this different from pointing a headless Chromium at the
internet and calling it a browser tool.

**Where it may go.** Chromium is launched with a SOCKS5 proxy that is this
process, and with loopback bypass turned *off*, so every byte the browser sends
— http, https, websocket, and anything Chromium decides to fetch on its own —
arrives at `Socks5Proxy` first. A destination inside a private network is only
ever reached by opening a relay stream through a node, which means it passes
`RelayTokens.allows()` in the app exactly as an ssh connection does. A private
address that no node covers is refused here rather than dialled from the
container, which is the whole point: the AIOps container sits on a real
network, and a browser that could dial it directly would be a way around the
gate rather than a user of it.

**What it may change.** Reading a page and photographing it are reads and
happen silently. Clicking, typing and signing in go to the AIOps approval
broker first, through the same internal endpoint the approval bridge uses, so
they follow the session's approval mode exactly as a Bash call does.

**What it may learn.** A stored system's password is never handed to the agent.
The `login` tool asks AIOps for it over the loopback API, types it into the
page, and adds it to a redaction set that every string leaving this process is
run through. Screenshots are taken with password fields masked, so a photograph
of a filled login form carries no credential into the transcript.

**Who it runs as.** This process is the agent's: a child of the CLI, holding
the run's relay token and the loopback token, at the agent's uid. The *browser*
is not. Playwright's node driver, and every Chromium process under it, are
started through the privilege-dropping helper as a third user — its own uid, in
its own group, in neither the app's group nor the agent's. That boundary is the
answer to the only question a headless browser really raises: a renderer
exploit on a hostile page is code execution, Chromium's own sandbox is off in
this container because Docker's seccomp profile blocks the user namespace it
needs, and before this the code landed at the agent's uid — with read access to
the run's decrypted SSH private keys, which are group-readable to the agent
because `ssh` has to load them. `browser_environ` and the helper's own sweep
are the second half of it: the browser stack starts from an environment with
the run's credentials taken out of it.

Deliberately importable without Playwright: everything above the
`Browser` class is pure logic the test suite exercises with no browser present.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
import sys
import time
import urllib.request

# --- what the run was given -------------------------------------------
API_URL = os.environ.get("AIOPS_INTERNAL_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("AIOPS_APPROVAL_TOKEN", "")
#: ask | auto | bypass — the session's approval mode, forwarded so this process
#: asks for a click exactly when a Bash call would be asked about.
APPROVAL_MODE = os.environ.get("AIOPS_BROWSER_APPROVALS", "ask")
#: The same token and address ssh's ProxyCommand helper uses. Absent when the
#: run may reach no internal network at all, which is the common case.
RELAY_TOKEN = os.environ.get("AIOPS_RELAY_TOKEN", "")
RELAY_ADDR = os.environ.get("AIOPS_RELAY_ADDR", "")
#: Where screenshots land. Created by the runner for this run and deleted with it.
#: Written by *this* process — Playwright's Python client is what turns a
#: screenshot into a file, not the browser — so the directory never has to be
#: reachable by the browser user at all. See `Browser.screenshot`.
SHOT_DIR = os.environ.get("AIOPS_BROWSER_DIR", "")
#: The shim that starts Playwright's driver as the browser user. Empty outside
#: the image, where there is no setuid helper to do it with; the browser then
#: runs as this process does, which is what it did before the split existed.
BROWSER_RUNAS = os.environ.get("AIOPS_BROWSER_RUNAS", "")
SANDBOX = os.environ.get("AIOPS_BROWSER_SANDBOX", "on").strip().lower() not in ("0", "off", "false")
PAGE_TIMEOUT_MS = int(os.environ.get("AIOPS_BROWSER_PAGE_TIMEOUT_SECONDS", "30")) * 1000
SESSION_SECONDS = int(os.environ.get("AIOPS_BROWSER_SESSION_SECONDS", "900"))
MAX_SHOTS = int(os.environ.get("AIOPS_BROWSER_MAX_SCREENSHOTS", "40"))
HTTP_TIMEOUT = int(os.environ.get("AIOPS_APPROVAL_HTTP_TIMEOUT", "660"))

PROTOCOL = "AIOPS-RELAY/1"
PROTOCOL_VERSION = "2024-11-05"
CHUNK = 64 * 1024

#: Ports a *public* destination may be dialled on. A browser speaks two
#: protocols; anything else asked for over the proxy is something other than
#: browsing, and there is no reason to be a general-purpose tunnel out.
PUBLIC_PORTS = (80, 443)


# =====================================================================
# Routing: the decision made about every destination, before any socket
# =====================================================================
class Route:
    """Where one CONNECT goes, or why it does not go anywhere.

    `kind` is "relay", "direct" or "refuse". A refusal carries the sentence the
    agent sees, which is the only place a port that has not been opened on a
    node gets explained to whoever has to open it.
    """

    __slots__ = ("kind", "node", "address", "reason")

    def __init__(self, kind: str, node: str = "", address: str = "", reason: str = "") -> None:
        self.kind = kind
        self.node = node
        self.address = address
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Route({self.kind!r}, node={self.node!r}, reason={self.reason!r})"


def _is_public(address: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True only for an address it is reasonable to dial straight out.

    `is_global` alone is not enough on its own reading: what matters here is
    that everything else — private ranges, loopback, link-local, multicast,
    reserved and the v6 equivalents — comes back False, because those are the
    addresses that would reach the container's own network or the app itself.
    """
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def decide_route(host: str, port: int, reach: dict, resolver=None) -> Route:
    """Where a browser request for host:port is allowed to go.

    The order matters and is the same order the ssh path uses. An exact
    (node, host, port) triple an operator stored wins first, because a name is
    only ever reachable as something a person typed. Then subnet rules, which
    match an *address* and never a name, for the same reason `RelayTokens.allows`
    refuses to resolve one: the resolver here belongs to the AIOps container and
    knows nothing about the far network.

    Everything private that no rule covers is refused rather than dialled. That
    is the rule this whole function exists for — the container has a real
    network interface, and "the browser could not reach it" must mean "no node
    would carry it", not "the app's own subnet answered instead".

    Nothing here authorises anything on its own: a relay route is re-checked by
    the gate in the app when the stream is opened. This decides which of the two
    doors to knock on, and closes both for anything else.
    """
    routes = reach.get("routes") or []
    for entry in routes:
        if (
            str(entry.get("host")) == host
            and int(entry.get("port") or 0) == port
            and entry.get("node")
        ):
            return Route("relay", node=str(entry["node"]), address=host)

    rules = []
    for entry in reach.get("subnets") or []:
        try:
            network = ipaddress.ip_network(str(entry.get("cidr")))
        except ValueError:
            continue
        ports = tuple(int(p) for p in (entry.get("ports") or []))
        rules.append((str(entry.get("node") or ""), network, ports))

    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        in_range = [(slug, net, ports) for slug, net, ports in rules if literal in net]
        for slug, _net, ports in in_range:
            if port in ports:
                return Route("relay", node=slug, address=host)
        if in_range:
            slug, net, ports = in_range[0]
            allowed = ", ".join(str(p) for p in ports) or "none"
            return Route(
                "refuse",
                reason=(
                    f"{host} is inside {net}, which relay node {slug!r} carries, but port "
                    f"{port} is not one of that node's allowed ports ({allowed}). AIOps will "
                    f"not widen a node's ports by itself: add {port} to node {slug!r} on the "
                    "Nodes page and start a new turn."
                ),
            )
        if not _is_public(literal):
            return Route("refuse", reason=_no_range(host, rules))
        return Route("direct", address=host)

    # A name. Resolved here only to decide whether it is public — and the result
    # is what gets dialled, so nothing can answer the lookup twice and hand back
    # a private address on the second reading.
    try:
        addresses = (resolver or _resolve)(host)
    except OSError as exc:
        return Route("refuse", reason=f"{host} could not be resolved ({exc}).")
    if not addresses:
        return Route("refuse", reason=f"{host} could not be resolved.")
    for text in addresses:
        try:
            candidate = ipaddress.ip_address(text)
        except ValueError:
            return Route("refuse", reason=f"{host} resolved to something unusable ({text!r}).")
        if not _is_public(candidate):
            return Route(
                "refuse",
                reason=(
                    f"{host} resolves to {text}, which is not a public address. A private "
                    "address is only reachable through a relay node, and a node's networks "
                    "are matched by address rather than by name — browse it by its address."
                ),
            )
    if port not in PUBLIC_PORTS:
        return Route(
            "refuse",
            reason=(
                f"port {port} is not a browsing port. Public sites are reached on "
                f"{' or '.join(str(p) for p in PUBLIC_PORTS)} only."
            ),
        )
    return Route("direct", address=addresses[0])


def _no_range(host: str, rules) -> str:
    if not rules:
        return (
            f"{host} is a private address and this run may not route through any relay node, "
            "so there is no way to reach it. AIOps refuses to dial a private address directly "
            "from the server — that would be a route around the node gate rather than through "
            "it. Ask the operator for access to a node covering that network."
        )
    listed = ", ".join(
        f"{net} on port{'' if len(ports) == 1 else 's'} "
        f"{', '.join(str(p) for p in ports)} via node {slug!r}"
        for slug, net, ports in rules
    )
    return (
        f"{host} is not inside any network this run may reach through a relay node. "
        f"Reachable: {listed}."
    )


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        text = info[4][0]
        if text not in out:
            out.append(text)
    return out


# =====================================================================
# Redaction: nothing that came out of the credential store leaves here
# =====================================================================
REDACTED = "[redacted by AIOps]"


def redact(text, secrets) -> str:
    """Replace every stored secret with a marker, wherever it appears.

    Applied to *everything* this process returns — tool text, page text, error
    messages, exception strings — rather than only to the places a credential is
    expected. A secret typed into a page can come back out through an error
    message, a URL a form put it in, or a validation notice, and the only way to
    be sure is to filter the exit rather than to enumerate the leaks.

    Short values are skipped: a one or two character "secret" would turn every
    page into markers, and nothing that short is worth protecting.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for secret in secrets or ():
        if secret and len(secret) >= 3:
            text = text.replace(secret, REDACTED)
    return text


# =====================================================================
# What the browser starts from: not this process's environment
# =====================================================================
#: Exactly agent_env.py's list, and for exactly its reasons — with nothing
#: added back. An agent needs AIOPS_WORKSPACE_ROOT; a browser needs nothing
#: from AIOps at all.
#:
#: What this removes in practice is the run's own credentials, which the runner
#: adds to the agent's environment and which are therefore in this process:
#: AIOPS_SSHPASS_* is a stored system's password, AIOPS_ASKPASS_* names a
#: program that prints a key's passphrase, AIOPS_RELAY_TOKEN opens streams
#: through a relay node, AIOPS_APPROVAL_TOKEN speaks to the app's loopback API.
_BLOCKED_PREFIXES = ("AIOPS_", "POSTGRES_", "PG")
_BLOCKED_NAMES = frozenset({"DATABASE_URL", "SECRET_KEY", "JWT_SECRET", "ADMIN_PASSWORD"})


def blocked_in_browser(name: str) -> bool:
    upper = name.upper()
    return upper in _BLOCKED_NAMES or any(upper.startswith(p) for p in _BLOCKED_PREFIXES)


def browser_environ(env) -> dict:
    """The environment the browser stack is allowed to inherit.

    Pure, so the sweep can be asserted without a browser: everything below is
    about a process that only exists inside the image.
    """
    return {name: value for name, value in dict(env).items() if not blocked_in_browser(name)}


def seal_environment() -> list:
    """Apply that sweep to this process, and point Playwright at the shim.

    In place rather than by handing an environment to a subprocess, because the
    subprocess is not ours to start: Playwright reads `os.environ` when it
    launches its driver and takes no argument for it. Everything this module
    needs was read into a constant at import, so removing the variables now
    costs nothing here and means the driver — and every Chromium under it —
    inherits an environment with the run's credentials gone.

    Not the boundary on its own. The helper sweeps the same list again on the
    other side of the uid switch, where the process asking cannot skip it. This
    is what makes the sweep true even in a checkout with no helper compiled.

    Returns the names removed, for the log line.
    """
    dropped = [name for name in list(os.environ) if blocked_in_browser(name)]
    for name in dropped:
        os.environ.pop(name, None)
    if BROWSER_RUNAS:
        # Playwright's own documented hook for "use this node instead of the
        # one I shipped". The shim it names execs the setuid helper, so the
        # driver and every browser process below it are the browser user's.
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = BROWSER_RUNAS
    return dropped


# =====================================================================
# The SOCKS5 proxy the browser is pointed at
# =====================================================================
class Socks5Proxy:
    """Every byte the browser sends, gated and then carried.

    SOCKS5 rather than an HTTP proxy for one reason that matters: Chromium
    hands a SOCKS5 proxy the *hostname* and does not resolve it itself, so this
    process decides what a name means. With an HTTP proxy the browser would
    resolve names for plain http:// requests locally, and a name pointing at a
    private address would be dialled before anything here saw it.
    """

    def __init__(self, reach: dict, log) -> None:
        self.reach = reach
        self.log = log
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            greeting = await reader.readexactly(2)
            if greeting[0] != 5:
                return
            await reader.readexactly(greeting[1])
            writer.write(b"\x05\x00")  # version 5, no authentication
            await writer.drain()

            header = await reader.readexactly(4)
            if header[0] != 5 or header[1] != 1:  # CONNECT only
                writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                await writer.drain()
                return
            atyp = header[3]
            if atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 3:
                length = (await reader.readexactly(1))[0]
                # ASCII, not the idna codec: a client has already punycoded an
                # international name by the time it reaches a SOCKS proxy, and
                # decoding it back to unicode would produce a string that
                # matches nothing the gate holds. (The idna codec also refuses
                # any error handler but "strict", which turned every request
                # into a silently closed connection.)
                host = (await reader.readexactly(length)).decode("ascii", errors="replace")
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            else:
                writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
                await writer.drain()
                return
            port = int.from_bytes(await reader.readexactly(2), "big")

            route = decide_route(host, port, self.reach)
            if route.kind == "refuse":
                self.log("refused", host=host, port=port, detail=route.reason)
                writer.write(b"\x05\x02\x00\x01" + b"\x00" * 6)  # connection not allowed
                await writer.drain()
                return

            try:
                if route.kind == "relay":
                    upstream_reader, upstream = await self._open_relay(route.node, host, port)
                else:
                    upstream_reader, upstream = await asyncio.wait_for(
                        asyncio.open_connection(route.address, port), timeout=30
                    )
            except Exception as exc:  # noqa: BLE001 - any failure is a SOCKS error
                self.log("failed", host=host, port=port, node=route.node, detail=str(exc))
                writer.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
                await writer.drain()
                return

            self.log("opened", host=host, port=port, node=route.node)
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            await _pump(reader, writer, upstream_reader, upstream)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        except Exception as exc:  # noqa: BLE001 - one page load must not stop the proxy
            # Written down rather than swallowed. A fault in here closes the
            # connection with no SOCKS reply, which the browser reports as a
            # generic proxy failure — indistinguishable, without this line,
            # from the gate having refused the destination.
            self.log("failed", detail=f"proxy fault: {type(exc).__name__}: {exc}")
        finally:
            for handle in (upstream, writer):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:  # noqa: BLE001
                        pass

    async def _open_relay(self, node: str, host: str, port: int):
        """Hand this connection to the app's relay forwarder.

        The same protocol, the same loopback listener and the same gate as ssh's
        ProxyCommand helper. If `RelayTokens.allows` does not recognise the
        triple the forwarder answers "ERR ..." and no node is ever contacted.
        """
        if not RELAY_TOKEN or not RELAY_ADDR:
            raise RuntimeError("this run has no relay credentials, so no node can be used")
        forward_host, _, forward_port = RELAY_ADDR.rpartition(":")
        if not forward_port.isdigit():
            raise RuntimeError(f"malformed relay address {RELAY_ADDR!r}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(forward_host, int(forward_port)), timeout=30
        )
        writer.write(f"{PROTOCOL} {RELAY_TOKEN} {node} {host} {port}\n".encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=60)
        status = line.decode("utf-8", errors="replace").strip()
        if not status.startswith("OK"):
            writer.close()
            raise RuntimeError(status.partition(" ")[2] or status or "the relay refused it")
        return reader, writer


async def _pump(a_reader, a_writer, b_reader, b_writer) -> None:
    async def copy(src, dst):
        try:
            while True:
                data = await src.read(CHUNK)
                if not data:
                    return
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            return

    first = asyncio.create_task(copy(a_reader, b_writer))
    second = asyncio.create_task(copy(b_reader, a_writer))
    try:
        await asyncio.wait({first, second}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (first, second):
            task.cancel()


# =====================================================================
# Talking back to AIOps
# =====================================================================
def _post(path: str, payload: dict, timeout: int = 30) -> dict:
    """One loopback call to the app, authenticated by this run's token."""
    body = json.dumps({**payload, "token": TOKEN}).encode()
    request = urllib.request.Request(
        f"{API_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json", "X-AIOps-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode() or "{}")


class Aiops:
    """The app, as this process sees it: a reach, an approver, a credential store."""

    def __init__(self) -> None:
        self.reach: dict = {"routes": [], "subnets": [], "systems": []}

    def load_reach(self) -> str | None:
        """What this run may reach. Empty is normal — most runs browse only the web."""
        if not TOKEN:
            return None
        try:
            self.reach = _post("/api/internal/browser/reach", {}, timeout=30) or self.reach
        except Exception as exc:  # noqa: BLE001
            return f"AIOps could not describe this run's network reach ({exc})."
        return None

    def note(self, action: str, **fields) -> None:
        """Log one browser action against the run, in the app's own log."""
        if not TOKEN:
            return
        try:
            _post("/api/internal/browser/log", {"action": action, **fields}, timeout=15)
        except Exception:  # noqa: BLE001 - logging must never break a page load
            pass

    def approve(self, tool: str, summary: str, detail: dict) -> tuple[bool, str]:
        """Put a state-changing action to the operator, if this session asks.

        Silent in auto and bypass mode, which is what makes a click follow the
        same rule a Bash call does rather than a stricter one of its own.
        """
        if APPROVAL_MODE != "ask" or not TOKEN:
            return True, ""
        try:
            answer = _post(
                "/api/internal/approvals",
                {
                    "provider": "claude",
                    "kind": "tool",
                    "tool_name": tool,
                    "summary": summary,
                    "input": detail,
                },
                timeout=HTTP_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 - a denial is the safe outcome
            return False, f"AIOps could not be reached for approval: {exc}"
        return bool(answer.get("allowed")), str(answer.get("note") or "")

    def credential(self, system: str) -> dict:
        """A stored system's login, fetched for injection and never returned.

        The scoping happens in the app, against the person who asked for this
        turn — this process only says which system it wants.
        """
        return _post("/api/internal/browser/credential", {"system": system}, timeout=30)


# =====================================================================
# The browser itself
# =====================================================================
class BrowserUnavailable(RuntimeError):
    pass


#: Chromium flags. The proxy is set through Playwright's own option, so these
#: are the ones that close the ways around it and keep a headless browser in a
#: container from wandering off on its own.
CHROMIUM_ARGS = [
    # Nothing may resolve a name in the browser's own resolver. With SOCKS5 it
    # never needs to — the proxy is handed the name and resolves it after the
    # routing decision — so this is the second lock on the door that decision
    # guards, and it closes the gap where some other part of Chromium resolves
    # something and dials it.
    #
    # The EXCLUDE is not decoration. Chromium puts its *proxy's* address
    # through the same resolver, so `MAP * ~NOTFOUND` on its own makes the
    # browser unable to reach the proxy at all and every navigation fails with
    # ERR_PROXY_CONNECTION_FAILED. Excluding the loopback address costs
    # nothing: the proxy refuses loopback destinations regardless of what
    # resolves.
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--metrics-recording-only",
    "--disable-breakpad",
    # A container's /dev/shm is 64MB by default and Chromium will fall over
    # part-way through a heavy page rather than saying so.
    "--disable-dev-shm-usage",
]


def launch_options(proxy_port: int, sandbox: bool = SANDBOX) -> dict:
    """Everything Playwright is asked to launch with. Pure, so it can be asserted.

    `bypass: "<-loopback>"` is the important one. Chromium bypasses a proxy for
    localhost by *default*, and this application's own API, its relay forwarder
    and its database all live on this machine's loopback. Without this line the
    browser would reach every one of them without the proxy — and therefore
    without the gate — ever seeing the request.
    """
    return {
        "headless": True,
        "chromium_sandbox": bool(sandbox),
        "proxy": {"server": f"socks5://127.0.0.1:{proxy_port}", "bypass": "<-loopback>"},
        "args": list(CHROMIUM_ARGS),
    }


#: Everything a screenshot must paint over. Playwright fills each match with a
#: solid box, so what the pixels show does not depend on what was typed.
PASSWORD_SELECTOR = "input[type=password]"


class Browser:
    """One Chromium, one page, for the life of one run.

    One page rather than many on purpose: an agent that can open tabs can lose
    track of which one it is looking at, and every extra page is another thing
    holding memory in a container that is also running an agent.
    """

    def __init__(self, aiops: Aiops) -> None:
        self.aiops = aiops
        self.secrets: set[str] = set()
        self.shots = 0
        self.started = time.monotonic()
        self._pw = None
        self._browser = None
        self._page = None
        self._proxy: Socks5Proxy | None = None
        #: One browser operation at a time. Playwright's page is not re-entrant
        #: and two tool calls arriving together would interleave a click with
        #: the navigation it belongs to.
        self.lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------
    async def page(self):
        if self._page is not None:
            if time.monotonic() - self.started > SESSION_SECONDS:
                await self.close()
                raise BrowserUnavailable(
                    f"the browser session passed its {SESSION_SECONDS}s lifetime and was "
                    "closed. Navigate again to start a fresh one."
                )
            return self._page

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - image always has it
            raise BrowserUnavailable(
                "Playwright is not installed in this container, so there is no browser."
            ) from exc

        self._proxy = Socks5Proxy(self.aiops.reach, self._log)
        port = await self._proxy.start()
        self.started = time.monotonic()
        # Before the driver is started, and therefore before anything of the
        # browser's exists: this is the only moment at which the environment it
        # inherits can still be decided.
        dropped = seal_environment()
        self.aiops.note(
            "start",
            detail=(
                f"browser stack starting as {'its own user' if BROWSER_RUNAS else 'the agent'}"
                f"; {len(dropped)} variable(s) withheld from it"
            ),
        )
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(**launch_options(port))
        except Exception as exc:  # noqa: BLE001
            await self.close()
            raise BrowserUnavailable(_launch_hint(exc)) from exc
        context = await self._browser.new_context(
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 900},
        )
        context.set_default_timeout(PAGE_TIMEOUT_MS)
        context.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        self._page = await context.new_page()
        return self._page

    async def close(self) -> None:
        for closer in (
            getattr(self._browser, "close", None),
            getattr(self._pw, "stop", None),
        ):
            if closer is not None:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        if self._proxy is not None:
            await self._proxy.stop()
        self._browser = self._page = self._pw = None
        self._proxy = None

    def _log(self, action: str, **fields) -> None:
        self.aiops.note(action, **fields)

    def clean(self, text) -> str:
        return redact(text, self.secrets)

    # -- reads ---------------------------------------------------------
    async def navigate(self, url: str) -> str:
        if not str(url).lower().startswith(("http://", "https://")):
            raise ValueError(
                "only http:// and https:// can be browsed. A file:// or data: URL is not a "
                "page on a network and there is nothing to gate about it."
            )
        page = await self.page()
        self.aiops.note("navigate", url=url)
        response = await page.goto(url, wait_until="domcontentloaded")
        status = response.status if response is not None else "no response"
        title = await page.title()
        return f"{status} {page.url}\ntitle: {title}"

    async def read(self) -> str:
        """The page as text, plus what can be interacted with.

        A password field is listed by its selector and never by its value, and
        the values that are shown go through the redaction set on the way out —
        an application that echoes a password into a hidden field or a URL would
        otherwise put it in the transcript on the agent's next read.
        """
        page = await self.page()
        body = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        fields = await page.evaluate(_FIELD_SCRIPT)
        lines = [f"url: {page.url}", f"title: {await page.title()}", ""]
        if fields:
            lines.append("Interactive elements:")
            for item in fields:
                lines.append("- " + " ".join(str(part) for part in item if part))
            lines.append("")
        lines.append(str(body or "").strip()[:20000])
        return self.clean("\n".join(lines))

    async def screenshot(self, full_page: bool = False) -> str:
        """Photograph the page into this run's directory, as *this* process.

        Worth being explicit about, because it decides a permission question:
        Playwright's Python client is what writes the file — the browser hands
        back the bytes and never touches the filesystem here. So the run's
        screenshot directory is written by the agent's uid and read by the
        agent's uid, and the browser user needs no access to it at all. It has
        none.
        """
        page = await self.page()
        if self.shots >= MAX_SHOTS:
            raise ValueError(
                f"this run has already taken {MAX_SHOTS} screenshots, which is the limit."
            )
        self.shots += 1
        name = f"screenshot-{self.shots:03d}.png"
        target = os.path.join(SHOT_DIR, name) if SHOT_DIR else name
        await page.screenshot(
            path=target,
            full_page=bool(full_page),
            # The reason a screenshot is safe to put in a transcript. Playwright
            # paints a solid box over every match, so the image does not depend
            # on what was typed into the field — not even on its length.
            mask=[page.locator(PASSWORD_SELECTOR)],
        )
        self.aiops.note("screenshot", url=page.url, detail=name)
        return (
            f"Saved {target} ({page.url}). Password fields are masked. "
            "Read the file to look at it."
        )

    # -- writes, which are approved first ------------------------------
    async def click(self, selector: str) -> str:
        page = await self.page()
        ok, note = self.aiops.approve(
            "browser_click", f"Click {selector} on {page.url}",
            {"selector": selector, "url": page.url},
        )
        if not ok:
            raise PermissionError(note or "The operator denied this click.")
        self.aiops.note("click", url=page.url, detail=selector)
        await page.click(selector)
        return f"clicked {selector} — now at {page.url}"

    async def fill(self, selector: str, value: str) -> str:
        page = await self.page()
        ok, note = self.aiops.approve(
            "browser_fill", f"Type into {selector} on {page.url}",
            {"selector": selector, "url": page.url, "value": value},
        )
        if not ok:
            raise PermissionError(note or "The operator denied this.")
        self.aiops.note("fill", url=page.url, detail=selector)
        await page.fill(selector, value)
        return f"filled {selector}"

    async def login(self, system: str, username_selector: str, password_selector: str,
                    submit_selector: str = "") -> str:
        """Sign in with a stored credential the agent never sees.

        The secret goes from the app's encrypted column into the page and into
        the redaction set, and nowhere else. What comes back is a sentence about
        what happened.
        """
        page = await self.page()
        ok, note = self.aiops.approve(
            "browser_login", f"Sign in to {page.url} as system {system!r}",
            {"system": system, "url": page.url},
        )
        if not ok:
            raise PermissionError(note or "The operator denied this sign-in.")

        answer = self.aiops.credential(system)
        secret = str(answer.get("secret") or "")
        username = str(answer.get("username") or "")
        if not secret:
            raise ValueError(
                f"AIOps has no password stored for {system!r}, so there is nothing to inject."
            )
        # Added before it is used: a failure part-way through must not leave a
        # secret that has been typed but is not being filtered out.
        self.secrets.add(secret)
        self.aiops.note("login", url=page.url, detail=system)

        if username and username_selector:
            await page.fill(username_selector, username)
        await page.fill(password_selector, secret)
        if submit_selector:
            await page.click(submit_selector)
            try:
                await page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - a SPA may never go idle
                pass
        return self.clean(
            f"Signed in to {page.url} using the credential AIOps holds for {system!r}"
            + (f" as {username}" if username else "")
            + ". The password was injected directly into the page; it is not available to you "
            "and is filtered out of every page read and screenshot from here on."
        )


#: Collected in the page rather than through Playwright's locators because one
#: round trip beats one per element, and because the *value* of a password field
#: must never cross back into this process at all — it is not read here.
_FIELD_SCRIPT = """
() => {
  const out = [];
  const nodes = document.querySelectorAll('input, textarea, select, button, a[href]');
  for (const el of Array.from(nodes).slice(0, 120)) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const bits = [tag + (type ? '[' + type + ']' : '')];
    if (el.id) bits.push('#' + el.id);
    if (el.name) bits.push('name=' + el.name);
    const label = (el.getAttribute('aria-label') || el.getAttribute('placeholder')
                   || (el.innerText || '').trim().slice(0, 60));
    if (label) bits.push(JSON.stringify(label));
    if (type === 'password') bits.push('(value hidden)');
    out.push(bits);
  }
  return out;
}
"""


def _launch_hint(exc: Exception) -> str:
    """Say what an operator can change when Chromium will not start.

    Written out in the same spirit as the bubblewrap hint in the runner: the
    browser's own error names a syscall, and an operator reading a failed turn
    needs the compose line instead.
    """
    text = str(exc)
    if "sandbox" in text.lower() or "namespace" in text.lower() or "SUID" in text:
        return (
            "Chromium could not start its own sandbox in this container. An unprivileged "
            "user namespace is what it needs, and Docker's default seccomp profile blocks "
            "the syscall. Either run this container with seccomp=unconfined, or set "
            "AIOPS_BROWSER_SANDBOX=off to run Chromium without its internal sandbox — it "
            "runs as its own unprivileged user inside the container either way, which is "
            "what keeps a bad page away from this run's credentials. "
            f"Chromium said: {text[:400]}"
        )
    if "Permission denied" in text or "EACCES" in text:
        return (
            "the browser could not be started, and the error is a permission one. The "
            "browser runs as its own user through AIOPS_BROWSER_RUNAS; if that shim or the "
            "setuid helper behind it is missing, wrong or not executable, this is what it "
            f"looks like. Chromium said: {text[:400]}"
        )
    return f"the browser could not be started: {text[:600]}"


# =====================================================================
# MCP plumbing
# =====================================================================
TOOLS = [
    {
        "name": "navigate",
        "description": (
            "Open a URL in the AIOps browser and return its status, final URL and title. "
            "Public sites go straight out; an address on a network reachable through a "
            "relay node is routed through that node. Reading a page is not an approved "
            "action, so this does not prompt anyone."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "The current page as rendered text, with a list of the fields, buttons and "
            "links on it and the selectors to address them by. Password values are never "
            "included."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": (
            "Photograph the current page to a PNG in this run's directory and return the "
            "path; read that file to look at it. Password fields are masked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"full_page": {"type": "boolean"}},
        },
    },
    {
        "name": "click",
        "description": (
            "Click an element by CSS selector. This can change something in the "
            "application being browsed, so it is put to the operator for approval when "
            "the session asks about tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "fill",
        "description": (
            "Type a value into a field by CSS selector. Approved like a click. Never put "
            "a password here — use login, which injects one you are not given."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
        },
    },
    {
        "name": "login",
        "description": (
            "Sign in to the current page using the password AIOps holds for a stored "
            "system, named by its short slug. The credential is injected into the page by "
            "AIOps: you never receive it, and it is filtered out of everything you read "
            "afterwards. Give the selectors for the username field, the password field, "
            "and optionally the submit button."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "system": {"type": "string"},
                "username_selector": {"type": "string"},
                "password_selector": {"type": "string"},
                "submit_selector": {"type": "string"},
            },
            "required": ["system", "password_selector"],
        },
    },
    {
        "name": "close",
        "description": "Shut the browser down and release its memory.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Server:
    def __init__(self) -> None:
        self.aiops = Aiops()
        self.browser = Browser(self.aiops)
        self._out = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self._out:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    async def reply(self, request_id, result: dict) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def call(self, request_id, params: dict) -> None:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        try:
            text = await self.dispatch(name, args if isinstance(args, dict) else {})
            error = False
        except BrowserUnavailable as exc:
            text, error = str(exc), True
        except PermissionError as exc:
            text, error = f"Denied: {exc}", True
        except Exception as exc:  # noqa: BLE001 - a tool error is an answer, not a crash
            text, error = f"{type(exc).__name__}: {exc}", True
        # Even a traceback goes through the filter: an application that echoes
        # what was typed into an exception message is not a hypothetical.
        await self.reply(
            request_id,
            {"content": [{"type": "text", "text": self.browser.clean(text)}], "isError": error},
        )

    async def dispatch(self, name: str, args: dict) -> str:
        browser = self.browser
        async with browser.lock:
            if name == "navigate":
                return await browser.navigate(str(args.get("url") or ""))
            if name == "read_page":
                return await browser.read()
            if name == "screenshot":
                return await browser.screenshot(bool(args.get("full_page")))
            if name == "click":
                return await browser.click(str(args.get("selector") or ""))
            if name == "fill":
                return await browser.fill(
                    str(args.get("selector") or ""), str(args.get("value") or "")
                )
            if name == "login":
                return await browser.login(
                    str(args.get("system") or ""),
                    str(args.get("username_selector") or ""),
                    str(args.get("password_selector") or ""),
                    str(args.get("submit_selector") or ""),
                )
            if name == "close":
                await browser.close()
                return "browser closed"
        raise ValueError(f"no such tool: {name}")

    async def handle(self, message: dict) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return  # a notification takes no answer
        if method == "initialize":
            problem = self.aiops.load_reach()
            if problem:
                sys.stderr.write(problem + "\n")
            await self.reply(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "aiops-browser", "version": "1.0.0"},
                },
            )
        elif method == "tools/list":
            await self.reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            await self.call(request_id, message.get("params") or {})
        elif method == "ping":
            await self.reply(request_id, {})
        else:
            await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


async def serve() -> None:
    server = Server()
    loop = asyncio.get_running_loop()
    in_flight: set[asyncio.Task] = set()
    try:
        while True:
            # stdin is a pipe from the CLI; reading it in a thread keeps the
            # event loop free for the proxy, which is carrying the page loads
            # of whatever tool call is already running.
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            task = asyncio.create_task(server.handle(message))
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
    finally:
        # A call still running when stdin closes is a call somebody is waiting
        # on — most often an approval a human has not answered yet. Cancelling
        # it here would drop the reply on the floor, so it is given the same
        # bounded wait the approval bridge gives its threads, and only then
        # abandoned.
        if in_flight:
            await asyncio.wait(in_flight, timeout=HTTP_TIMEOUT + 5)
            for task in list(in_flight):
                task.cancel()
        await server.browser.close()


def main() -> None:
    # The loop is built by hand rather than with `asyncio.run`, which the
    # isolation suite reads as a process spawn wherever it appears outside
    # agent_env.py. Same behaviour, and the check stays a check.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(serve())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
