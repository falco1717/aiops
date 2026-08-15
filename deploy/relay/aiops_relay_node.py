#!/usr/bin/env python3
"""AIOps relay node agent.

Holds one outbound connection to AIOps and, when asked, opens a TCP connection
on this network and copies bytes between it and AIOps. That is the whole job.

It is deliberately easy to see and easy to remove. It runs under its own
account as a named service, logs every connection it opens with the address and
port, keeps its credential in one file, and is uninstalled by one command. It
does not survive its own removal, hide from a process listing, or touch
anything on this machine besides its own state directory.

What it never has: any provider login, any SSH key, any prompt or transcript.
What crosses it is an encrypted SSH session between the AIOps server and the
far host, and it holds no key for that.

Stdlib only, including the websocket implementation below — the agent has to
install cleanly on whatever is already on a machine, and a dependency is a
thing that has to be resolved on a network that may be exactly the one AIOps
cannot reach yet.
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import logging
import operator
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

VERSION = "1.0.0"
CHUNK = 64 * 1024

log = logging.getLogger("aiops-relay")

# Close codes the server uses. Only one of them means "stop trying".
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NOT_APPROVED = 4403
CLOSE_REVOKED = 4410

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _apply_mask(payload: bytes, mask: bytes) -> bytes:
    """XOR a frame against its 4-byte mask.

    `map` over two byte strings does the work in C. Spelling it out with a
    Python-level loop costs roughly an order of magnitude, which on a 64 KiB
    frame is the difference between a relay you forget about and one that shows
    up in `top` during a file copy.
    """
    if not payload:
        return payload
    repeated = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return bytes(map(operator.xor, payload, repeated))


# --- why a connection failed -------------------------------------------
class WebSocketError(Exception):
    pass


class EgressError(WebSocketError):
    """A dial to AIOps that did not arrive, named for what actually stopped it.

    The reason this hierarchy exists at all: a node on a corporate VPN reported
    only "not connected", and every one of the causes below arrived as the same
    opaque OSError in the same log line. Somebody staring at that line cannot
    tell a name that does not resolve from a gateway that will not tunnel from
    a certificate they are being asked to trust, and those have three different
    fixes. Each subclass carries its own finished sentence, so the log says
    which one happened and what to do about it without anyone guessing.

    They subclass WebSocketError so the existing `except (OSError,
    WebSocketError)` in the reconnect loop keeps catching all of them: the
    classification changes what is *said*, never whether a failure is survived.
    """


class NameNotResolved(EgressError):
    pass


class TCPRefused(EgressError):
    pass


class TCPTimedOut(EgressError):
    pass


class NetworkUnreachable(EgressError):
    pass


class ProxyRefusedConnect(EgressError):
    """The proxy answered the CONNECT with something other than 200."""

    def __init__(self, message: str, status_line: str = "") -> None:
        super().__init__(message)
        #: The proxy's own status line, kept so --diagnose can quote it.
        self.status_line = status_line


class CertificateNotTrusted(EgressError):
    pass


class TLSFailed(EgressError):
    pass


class CredentialRejected(EgressError):
    """AIOps answered the upgrade with 401/403. Not a network fault."""


class UpgradeRefused(EgressError):
    """AIOps (or something impersonating it) answered the upgrade with neither
    101 nor an authentication status."""


# --- getting out of this network ---------------------------------------
class ProxyTarget:
    """A parsed HTTP proxy, and the one safe way to write it down.

    `safe_url` is what goes in a log or a diagnosis. A proxy URL routinely
    carries a password, and a node's log is read over somebody's shoulder,
    pasted into a ticket, and shipped off the machine by whatever collects
    logs there.
    """

    __slots__ = ("host", "port", "username", "password")

    def __init__(self, host: str, port: int, username: str | None, password: str | None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def safe_url(self) -> str:
        if self.username is None:
            return f"http://{self.address}"
        return f"http://{self.username}:***@{self.address}"

    @property
    def authorization(self) -> str | None:
        if self.username is None:
            return None
        raw = f"{self.username}:{self.password or ''}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def __repr__(self) -> str:  # so an accidental %r in a log line is still safe
        return f"ProxyTarget({self.safe_url})"


class ProxyConfigError(Exception):
    pass


def parse_proxy(url: str) -> ProxyTarget:
    """A proxy URL, as a host to dial and a credential to present.

    `proxy.corp:8080` with no scheme is accepted because that is how every
    Windows dialog and every `netsh` line writes one.
    """
    text = (url or "").strip()
    if not text:
        raise ProxyConfigError("the proxy is empty")
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme in ("socks4", "socks5", "socks5h"):
        raise ProxyConfigError(
            f"{parts.scheme} proxies are not supported by this agent, only HTTP "
            "CONNECT proxies (http://host:port)."
        )
    if parts.scheme != "http":
        raise ProxyConfigError(
            f"a {parts.scheme!r} proxy is not supported; write it as http://host:port. "
            "The tunnel this opens is HTTP CONNECT, and the TLS to AIOps runs inside it."
        )
    if not parts.hostname:
        raise ProxyConfigError(f"{url!r} has no host in it")
    return ProxyTarget(parts.hostname, parts.port or 8080, parts.username, parts.password)


def no_proxy_entry_for(host: str, no_proxy: str) -> str | None:
    """The NO_PROXY entry that covers `host`, or None.

    NO_PROXY is the half of proxy configuration that people forget, and it is
    the half that matters here: a node whose AIOps is *inside* the corporate
    network must not be sent to the gateway that only knows how to reach the
    outside. Matching follows the convention every other client uses -
    `*` is everything, `example.com` covers `example.com` and anything under
    it, a leading dot means the same thing, and a `host:port` entry is compared
    on its host.
    """
    target = (host or "").strip().strip("[]").lower().rstrip(".")
    if not target:
        return None
    for raw in (no_proxy or "").replace(",", " ").split():
        entry = raw.strip()
        if not entry:
            continue
        if entry == "*":
            return entry
        candidate = entry.lower().rstrip(".")
        if "://" in candidate:
            candidate = candidate.split("://", 1)[1]
        # A port qualifier is stripped rather than honoured: AIOps is one
        # host:port, and a NO_PROXY that names the host is meant for it.
        if candidate.count(":") == 1:
            candidate = candidate.split(":", 1)[0]
        candidate = candidate.strip("[]").lstrip(".")
        if not candidate:
            continue
        if target == candidate or target.endswith("." + candidate):
            return entry
    return None


def parse_netsh_proxy(output: str) -> tuple[str | None, list[str]]:
    """`netsh winhttp show proxy` output, as (proxy, bypass list).

    netsh output is localised, so it is read by shape rather than by label: the
    "Direct access" case has no `label : value` line at all, the proxy line's
    value holds a host:port or an `http=` scheme qualifier, and the bypass
    line's value is a list of patterns. Matching on the English labels would
    work on the machine it was written on and nowhere else.
    """
    server: str | None = None
    bypass: list[str] = []
    for line in output.splitlines():
        # Partitioned on the first colon only: the value on the right is
        # allowed to hold colons of its own, which `host:port` always does.
        _, separator, value = line.partition(":")
        value = value.strip()
        if not separator or not value:
            continue
        looks_like_server = "=" in value or any(
            part.rsplit(":", 1)[-1].isdigit() and ":" in part for part in value.split(";")
        )
        if server is None and looks_like_server:
            # `http=host:port;https=host:port` or a bare `host:port`. The https
            # entry is the one that matters: AIOps is reached over TLS.
            chosen = None
            for part in value.split(";"):
                part = part.strip()
                if part.lower().startswith("https="):
                    chosen = part.split("=", 1)[1]
                    break
                if part.lower().startswith("http="):
                    chosen = chosen or part.split("=", 1)[1]
                elif "=" not in part:
                    chosen = chosen or part
            if chosen:
                server = chosen.strip()
            continue
        if "*" in value or "<local>" in value.lower():
            bypass = [item.strip() for item in value.replace(",", ";").split(";") if item.strip()]
    return server, bypass


def windows_system_proxy() -> tuple[str | None, list[str]]:
    """The machine-wide WinHTTP proxy and its bypass list, or (None, []).

    WinHTTP, not the per-user WinINET settings behind Internet Options - and
    not what Python's own `urllib.request.getproxies()` would return here,
    because on Windows that reads HKCU. This agent runs as a *service*, under an
    account that never interactively logs on and has no loaded user hive of its
    own, so a proxy somebody configured in their browser sits in a hive this
    process cannot see. `netsh winhttp show proxy` reads the HKLM setting, which
    is the one a service actually inherits and the one an administrator
    populates with `netsh winhttp import proxy source=ie`. Reading the wrong
    hive would look like it worked on a developer machine and fail on every real
    install - which is the same shape as the bug this node was already carrying.
    """
    if os.name != "nt":
        return None, []
    try:
        result = subprocess.run(
            ["netsh.exe", "winhttp", "show", "proxy"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, []
    return parse_netsh_proxy(result.stdout)


def bypassed_by_windows_list(host: str, bypass: list[str]) -> str | None:
    """The WinHTTP bypass entry that covers `host`, or None.

    Windows writes these as globs, and `<local>` for "anything without a dot in
    it", which is not a spelling NO_PROXY has.
    """
    target = (host or "").lower()
    for entry in bypass:
        item = entry.strip().lower()
        if item == "<local>":
            if "." not in target:
                return entry
            continue
        if fnmatch.fnmatch(target, item):
            return entry
    return None


class ProxyDecision:
    """Which proxy this node will use for one host, and where that came from.

    The `source` is carried around because "it is not using a proxy" and "it is
    using the wrong proxy" are both things an operator has to be able to see,
    and neither is visible from the outcome alone.
    """

    __slots__ = ("target", "source")

    def __init__(self, target: ProxyTarget | None, source: str) -> None:
        self.target = target
        self.source = source

    def __str__(self) -> str:
        if self.target is None:
            return f"no proxy ({self.source})"
        return f"{self.target.safe_url} (from {self.source})"


class Egress:
    """Everything about how this node gets out: which proxy, and whose CAs.

    One object rather than three arguments threaded through every dial, because
    the control channel, each relayed stream and enrolment all have to make the
    same choice, and enrolment making a different one is exactly the bug that
    let a node install cleanly and then never connect.
    """

    #: The order a proxy is looked for. --proxy (or AIOPS_RELAY_PROXY, which is
    #: the same setting persisted by the installers) is first and is not subject
    #: to NO_PROXY: somebody who names a proxy on the command line has said what
    #: they want. Everything after it is inference, and NO_PROXY overrules
    #: inference.
    ENV_ORDER = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")

    def __init__(
        self,
        *,
        verify: bool = True,
        ca_bundle: str | None = None,
        proxy: str | None = None,
        environ: dict | None = None,
        system_proxy=None,
    ) -> None:
        self.verify = verify
        self.ca_bundle = ca_bundle or None
        self.explicit_proxy = (proxy or "").strip() or None
        self.environ = os.environ if environ is None else environ
        # Injectable so the tests can exercise the Windows branch on Linux, and
        # so `netsh` is run at most once per process rather than per dial.
        self._system_proxy = system_proxy
        self._system_cache: tuple[str | None, list[str]] | None = None

    # -- proxy selection ------------------------------------------------
    def _system(self) -> tuple[str | None, list[str]]:
        if self._system_proxy is not None:
            return self._system_proxy() if callable(self._system_proxy) else self._system_proxy
        if self._system_cache is None:
            self._system_cache = windows_system_proxy()
        return self._system_cache

    def _from_environment(self, name: str) -> str:
        return (self.environ.get(name) or "").strip()

    def proxy_for(self, host: str) -> ProxyDecision:
        """Which proxy, if any, this node dials `host` through."""
        if self.explicit_proxy:
            return ProxyDecision(parse_proxy(self.explicit_proxy), "--proxy")

        no_proxy = self._from_environment("NO_PROXY") or self._from_environment("no_proxy")
        matched = no_proxy_entry_for(host, no_proxy)
        if matched is not None:
            return ProxyDecision(None, f"NO_PROXY entry {matched!r} covers {host}")

        for name in self.ENV_ORDER:
            value = self._from_environment(name)
            if value:
                return ProxyDecision(parse_proxy(value), name)

        server, bypass = self._system()
        if server:
            skipped = bypassed_by_windows_list(host, bypass)
            if skipped is not None:
                return ProxyDecision(
                    None, f"the machine's WinHTTP bypass list ({skipped!r}) covers {host}"
                )
            return ProxyDecision(parse_proxy(server), "the machine's WinHTTP settings")
        return ProxyDecision(None, "none is configured")

    # -- TLS ------------------------------------------------------------
    def tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.ca_bundle:
            # Additive: the system's own authorities stay loaded, and the
            # corporate one is added to them. That is what makes --ca-bundle
            # the right answer to a TLS-inspecting gateway and --insecure the
            # wrong one - verification keeps happening, against one more CA.
            context.load_verify_locations(cafile=self.ca_bundle)
        if not self.verify:
            # Only reachable via --insecure, which the installer refuses to
            # pass silently: a relay carries an SSH session that authenticates
            # itself, but the enrolment credential in the handshake above is
            # worth protecting on its own.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def wrap(self, sock: socket.socket, host: str) -> ssl.SSLSocket:
        try:
            return self.tls_context().wrap_socket(sock, server_hostname=host)
        except ssl.SSLCertVerificationError as exc:
            sock.close()
            raise CertificateNotTrusted(
                f"TLS: the certificate {host} presented was not trusted - "
                f"{getattr(exc, 'verify_message', None) or exc.reason or exc}. If this "
                "network inspects TLS, a gateway is re-signing the certificate with an "
                "authority this machine does not trust yet: export that authority's "
                "certificate and pass it with --ca-bundle PATH. Prefer that to "
                "--insecure, which turns verification off for the handshake the "
                "enrolment credential travels in."
            ) from None
        except ssl.SSLError as exc:
            sock.close()
            raise TLSFailed(
                f"TLS: the handshake with {host} failed - {exc}. The TCP connection was "
                "made, so this is not a routing or firewall problem; something is "
                "terminating or rewriting TLS between here and AIOps."
            ) from None
        except OSError as exc:
            sock.close()
            raise TLSFailed(f"TLS: the handshake with {host} failed - {exc}") from None

    # -- dialling -------------------------------------------------------
    def dial(self, host: str, port: int, *, timeout: float) -> tuple[socket.socket, ProxyDecision]:
        """A TCP connection to host:port, through a proxy if there is one."""
        decision = self.proxy_for(host)
        if decision.target is None:
            return tcp_connect(host, port, timeout), decision
        proxy = decision.target
        sock = tcp_connect(
            proxy.host, proxy.port, timeout, label=f"the proxy at {proxy.address}"
        )
        open_tunnel(sock, proxy, host, port)
        return sock, decision


def tcp_connect(host: str, port: int, timeout: float, label: str | None = None) -> socket.socket:
    """`socket.create_connection`, with the failure named rather than raw."""
    where = label or f"{host}:{port}"
    try:
        return socket.create_connection((host, port), timeout=timeout)
    except socket.gaierror as exc:
        raise NameNotResolved(
            f"DNS: the name {host!r} did not resolve ({exc.strerror or exc}). Nothing was "
            "dialled. On a VPN this is usually the tunnel's resolver not knowing this "
            "name, or split DNS sending it to the wrong one."
        ) from None
    except (socket.timeout, TimeoutError):
        raise TCPTimedOut(
            f"TCP: {where} did not answer within {timeout:g}s. The packets left and "
            "nothing came back, which is what a firewall that drops rather than refuses "
            "looks like. If this network requires an HTTP proxy to reach anything "
            "outside it, give the node one with --proxy http://host:port."
        ) from None
    except ConnectionRefusedError:
        raise TCPRefused(
            f"TCP: {where} refused the connection. Something is at that address and said "
            "no, so the port is closed or nothing is listening on it."
        ) from None
    except OSError as exc:
        raise NetworkUnreachable(
            f"TCP: {where} could not be reached - {exc.strerror or exc}. There is no route "
            "from this machine to that address right now."
        ) from None


def _read_http_head(sock: socket.socket, limit: int = 64 * 1024) -> str:
    """Read exactly up to the end of an HTTP header block and no further.

    A byte at a time, deliberately. The socket is handed straight to
    `wrap_socket` the moment the tunnel opens, so a buffered reader that read
    ahead past the blank line would swallow the first bytes of the TLS
    handshake into a buffer nothing ever reads again.
    """
    head = bytearray()
    while b"\r\n\r\n" not in head and b"\n\n" not in head:
        if len(head) > limit:
            raise ProxyRefusedConnect(
                "PROXY: the proxy sent more than 64 KiB of headers in answer to CONNECT "
                "and never finished. That is not a proxy speaking HTTP."
            )
        try:
            byte = sock.recv(1)
        except (socket.timeout, TimeoutError):
            raise ProxyRefusedConnect(
                "PROXY: the proxy accepted the connection but never answered the CONNECT "
                "request. It may be filtering the destination silently."
            ) from None
        if not byte:
            raise ProxyRefusedConnect(
                "PROXY: the proxy closed the connection without answering the CONNECT "
                "request."
            )
        head += byte
    return head.decode("latin-1")


def open_tunnel(sock: socket.socket, proxy: ProxyTarget, host: str, port: int) -> None:
    """Ask an HTTP proxy to tunnel to host:port, or raise saying why not.

    Nothing of AIOps' own travels in this request. The Authorization header
    carrying this node's credential belongs inside the TLS session that is
    established *after* the 200 below, because everything up to it crosses the
    proxy hop in the clear and is written into the proxy's access log.
    """
    lines = [
        f"CONNECT {host}:{port} HTTP/1.1",
        f"Host: {host}:{port}",
        f"User-Agent: aiops-relay/{VERSION}",
        "Proxy-Connection: keep-alive",
    ]
    authorization = proxy.authorization
    if authorization is not None:
        lines.append(f"Proxy-Authorization: {authorization}")
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    except OSError as exc:
        sock.close()
        raise ProxyRefusedConnect(
            f"PROXY: the CONNECT request to {proxy.address} could not be sent - {exc}."
        ) from None

    try:
        head = _read_http_head(sock)
    except ProxyRefusedConnect:
        sock.close()
        raise
    status_line = head.split("\n", 1)[0].strip()
    fields = status_line.split(" ", 2)
    code = 0
    if len(fields) >= 2 and fields[1].isdigit():
        code = int(fields[1])
    if code == 200:
        return

    sock.close()
    advice = (
        " That is 'proxy authentication required': put the credentials in the proxy URL, "
        "as --proxy http://user:password@host:port."
        if code == 407
        else " A proxy that allows CONNECT only to certain ports will answer like this; so "
        "will one that has not been told this destination is allowed."
    )
    raise ProxyRefusedConnect(
        f"PROXY: {proxy.safe_url} refused to open a tunnel to {host}:{port}. It answered: "
        f"{status_line!r}.{advice}",
        status_line=status_line,
    )


# --- a small websocket client -----------------------------------------
class WebSocket:
    """Enough of RFC 6455 to be a client, and nothing else.

    No extensions, no compression, no fragmentation on send. Control frames are
    handled where they arrive so a caller only ever sees application data.
    """

    def __init__(self, sock: socket.socket, reader) -> None:
        self.sock = sock
        self.reader = reader
        #: The code the server closed with, once it has. It is the difference
        #: between "wait, you are not approved yet" and "stop, you are revoked".
        self.close_code: int | None = None
        self._send_lock = threading.Lock()
        self._closed = False

    @classmethod
    def connect(
        cls, url: str, headers: dict[str, str], *, egress: "Egress | None" = None,
        timeout: float = 30,
    ) -> "WebSocket":
        egress = egress or Egress()
        parts = urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        port = parts.port or (443 if secure else 80)
        host = parts.hostname or ""
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        raw, decision = egress.dial(host, port, timeout=timeout)
        if secure:
            raw = egress.wrap(raw, host)
        elif decision.target is not None:
            # A plain ws:// through a proxy puts the Authorization header below
            # on the wire in the clear, over a hop somebody else runs and logs.
            # It still works, and is refused nowhere, but it is said out loud.
            log.warning(
                "this node is reaching AIOps over plain HTTP through %s, so its "
                "credential crosses that proxy unencrypted. Use an https:// AIOps URL.",
                decision.target.safe_url,
            )

        key = base64.b64encode(secrets.token_bytes(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{name}: {value}" for name, value in headers.items()]
        raw.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        stream = raw.makefile("rb")
        status = stream.readline().decode("latin-1").strip()
        received: dict[str, str] = {}
        while True:
            line = stream.readline().decode("latin-1").strip()
            if not line:
                break
            name, _, value = line.partition(":")
            received[name.strip().lower()] = value.strip()
        fields = status.split(" ", 2)
        code = int(fields[1]) if len(fields) >= 2 and fields[1].isdigit() else 0
        if code != 101:
            raw.close()
            if code in (401, 403):
                raise CredentialRejected(
                    f"AUTH: AIOps answered {code} to the websocket upgrade. That is an "
                    "authentication failure and not a network fault: this node's "
                    "credential has been revoked or deleted, or the node was "
                    "re-enrolled somewhere else. Register the node in AIOps again and "
                    "re-run the installer with a fresh enrolment token."
                )
            raise UpgradeRefused(
                f"HTTP: the server answered {status!r} to the websocket upgrade instead "
                "of 101. Something is reachable at that address; it is not agreeing to "
                "this connection. A proxy error page or a captive portal answering in "
                "AIOps' place shows up exactly here."
            )

        expected = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if received.get("sec-websocket-accept") != expected:
            raw.close()
            raise WebSocketError("the server's handshake did not match this connection")

        return cls(raw, stream)

    # -- framing -------------------------------------------------------
    def _read(self, count: int) -> bytes:
        data = self.reader.read(count)
        if data is None or len(data) < count:
            raise WebSocketError("connection closed mid-frame")
        return data

    def _frame(self, opcode: int, payload: bytes) -> bytes:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        # Every client frame is masked; the standard requires it and servers
        # drop connections that forget.
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        mask = secrets.token_bytes(4)
        header += mask
        return bytes(header) + _apply_mask(payload, mask)

    def send(self, payload: bytes, opcode: int = 0x2) -> None:
        with self._send_lock:
            if self._closed:
                raise WebSocketError("socket already closed")
            self.sock.sendall(self._frame(opcode, payload))

    def send_json(self, message: dict) -> None:
        self.send(json.dumps(message).encode(), opcode=0x1)

    def recv(self) -> tuple[int, bytes] | None:
        """The next application message, or None once the peer has closed."""
        buffered = bytearray()
        message_opcode = 0
        while True:
            first, second = self._read(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            masked = bool(second & 0x80)
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if masked else b""
            payload = self._read(length) if length else b""
            if masked:
                payload = _apply_mask(payload, mask)

            if opcode == 0x8:
                if len(payload) >= 2:
                    self.close_code = struct.unpack("!H", payload[:2])[0]
                self.close()
                return None
            if opcode == 0x9:
                self.send(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue

            if opcode != 0x0:
                message_opcode = opcode
            buffered += payload
            if fin:
                return message_opcode, bytes(buffered)

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.sock.sendall(self._frame(0x8, b""))
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass


# --- what this machine says about itself -------------------------------
def local_networks() -> list[str]:
    """The subnets this node can reach, for the operator's benefit.

    Self-reported and used for nothing but display: AIOps decides what may be
    reached through a node from its own stored systems, never from this list.
    """
    found: list[str] = []
    try:
        output = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
        for line in output.splitlines():
            parts = line.split()
            if "inet" in parts:
                found.append(parts[parts.index("inet") + 1])
    except (OSError, subprocess.SubprocessError):
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.0.2.1", 53))  # documentation address; nothing is sent
            found.append(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
    return [address for address in found if not address.startswith("127.")][:32]


# --- enrolment ---------------------------------------------------------
def enrol(base_url: str, token: str, egress: Egress) -> dict:
    """Spend the one-time token for a long-lived credential.

    This dials out too, and it has to make exactly the same egress choices as
    the runtime connection does. It used to make its own: a bare
    `create_default_context` and whatever `urlopen`'s default opener inferred,
    which on Windows means the *per-user* WinINET proxy out of HKCU. On a
    machine that needs a proxy, that combination made install-time enrolment
    fail on the VPN even where the running service would later have been fine,
    and the operator never got as far as the part that worked.
    """
    body = json.dumps(
        {
            "token": token,
            "version": VERSION,
            "hostname": socket.gethostname(),
            "networks": local_networks(),
        }
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/api/relay/enroll",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    host = urlsplit(base_url).hostname or ""
    decision = egress.proxy_for(host)
    # An explicit ProxyHandler, always - never the default opener. Passing the
    # empty dict is how urllib is told "no proxy for this", and it is what makes
    # a NO_PROXY match here mean the same thing it means for the websocket.
    proxies = {}
    if decision.target is not None:
        proxies = {"http": decision.target.safe_url, "https": decision.target.safe_url}
        if decision.target.username is not None:
            # Only now, and only into the handler; `safe_url` above is what is
            # allowed to be logged.
            authority = (
                f"{decision.target.username}:{decision.target.password or ''}"
                f"@{decision.target.address}"
            )
            proxies = {"http": f"http://{authority}", "https": f"http://{authority}"}
    log.info("enrolling with AIOps: %s", decision)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPSHandler(context=egress.tls_context()),
    )
    with opener.open(request, timeout=30) as response:
        return json.loads(response.read().decode())


# --- the agent ---------------------------------------------------------
#: The states a node publishes about itself, and whether an installer should
#: call the install a success on seeing one. "pending" counts: a node that has
#: reached AIOps and been told it is not approved yet is working exactly as
#: documented, and waiting for a human is not a failed install.
STATUS_WORKING = ("connected", "pending")


class RelayAgent:
    def __init__(self, base_url: str, credential: str, egress: Egress, max_streams: int,
                 status_path: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.egress = egress
        self.max_streams = max_streams
        #: One word in one file, rewritten as the node's situation changes. It
        #: exists because every installer needs the same answer to the same
        #: question - did this node actually reach AIOps - and each of the three
        #: was otherwise reduced to scraping a log it may not even be able to
        #: read. An installer that cannot answer it declares success over a node
        #: that will never connect, which is how a service spent an hour
        #: flapping while the UI said "Never connected".
        self.status_path = status_path
        #: The slug AIOps knows this node by, once it has said so.
        self.registered_as = ""
        self.open_streams = 0
        self._count_lock = threading.Lock()
        self.stop = threading.Event()

    @property
    def ws_base(self) -> str:
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        return f"{scheme}://{parts.netloc}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.credential}", "User-Agent": f"aiops-relay/{VERSION}"}

    def note_status(self, state: str, detail: str = "") -> None:
        """Publish what this node's situation is, for anything that asks.

        Written through a temporary file and renamed, so a reader never catches
        it half-written. Never fatal: a node that cannot write this file still
        carries traffic, and refusing to run because a status file failed would
        be trading the job for the report of the job.
        """
        if not self.status_path:
            return
        line = f"{state} {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {detail}".strip()
        temporary = self.status_path + ".new"
        try:
            os.makedirs(os.path.dirname(self.status_path) or ".", exist_ok=True)
            with open(temporary, "w") as handle:
                handle.write(line + "\n")
            os.replace(temporary, self.status_path)
        except OSError as exc:
            log.debug("could not write the status file %s: %s", self.status_path, exc)

    def run_forever(self) -> int:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                verdict = self.session()
            except EgressError as exc:
                # Already a finished sentence naming which of the half-dozen
                # ways out of this network failed. Logged as-is, at the level
                # that says whether retrying can help.
                if isinstance(exc, CredentialRejected):
                    log.error("%s", exc)
                else:
                    log.warning("%s", exc)
                verdict = None
            except (OSError, WebSocketError) as exc:
                log.warning("connection to AIOps failed: %s", exc)
                verdict = None
            if verdict == "revoked":
                log.error(
                    "AIOps has revoked this node. It will carry no further traffic. "
                    "Uninstall it with aiops-relay-uninstall."
                )
                return 0
            if verdict == "connected":
                backoff = 1.0
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)
        return 0

    def session(self) -> str | None:
        """One control connection, from dial to close."""
        ws = WebSocket.connect(
            f"{self.ws_base}/api/relay/connect",
            self.headers(),
            egress=self.egress,
        )
        # Longer than the server's 25s heartbeat, so an idle link is not
        # mistaken for a dead one, and short enough that a dropped route is
        # noticed in under two minutes.
        ws.sock.settimeout(90)
        log.info("connected to %s", self.base_url)
        ws.send_json(
            {
                "type": "ready",
                "version": VERSION,
                "hostname": socket.gethostname(),
                "networks": local_networks(),
            }
        )
        try:
            while not self.stop.is_set():
                try:
                    message = ws.recv()
                except (socket.timeout, TimeoutError):
                    log.warning("no heartbeat from AIOps; reconnecting")
                    return "connected"
                if message is None:
                    return self._closed_because(ws.close_code)
                _, payload = message
                try:
                    event = json.loads(payload.decode())
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "ping":
                    ws.send_json({"type": "pong"})
                    # Refreshed on every heartbeat, so the timestamp in the file
                    # means "as of now" rather than "at some point since boot".
                    self.note_status("connected", self.registered_as)
                elif kind == "open":
                    self.spawn(ws, event)
                elif kind == "hello":
                    self.registered_as = str(event.get("node") or "")
                    log.info("registered with AIOps as node %r", event.get("node"))
                    self.note_status("connected", self.registered_as)
                elif kind == "denied":
                    log.warning("AIOps refused this node: %s", event.get("reason"))
                    self.note_status("denied", str(event.get("reason") or ""))
        finally:
            ws.close()
        return "connected"

    def _closed_because(self, code: int | None) -> str | None:
        """What a close means for whether it is worth dialling again."""
        if code == CLOSE_REVOKED:
            self.note_status("revoked")
            return "revoked"
        if code == CLOSE_NOT_APPROVED:
            log.info("this node is enrolled but not yet approved in AIOps; waiting")
            # A working install, not a failed one: the node found AIOps, AIOps
            # knows who it is, and what is left is a human clicking Approve.
            self.note_status("pending", "waiting to be approved in AIOps")
            return None
        if code == CLOSE_UNAUTHENTICATED:
            self.note_status("unauthenticated")
            log.error(
                "AIOps does not recognise this node's credential. It may have been "
                "deleted, or re-enrolled elsewhere. Re-run the installer with a new "
                "enrolment token to fix it."
            )
            return None
        log.info("AIOps closed the connection (code %s)", code)
        return "connected"

    def spawn(self, control: WebSocket, event: dict) -> None:
        stream_id = str(event.get("stream") or "")
        host = str(event.get("host") or "")
        port = int(event.get("port") or 0)
        if not stream_id or not host or not port:
            return
        with self._count_lock:
            if self.open_streams >= self.max_streams:
                log.warning("refusing %s:%s — %d connections already open", host, port, self.open_streams)
                control.send_json(
                    {"type": "open.failed", "stream": stream_id, "error": "node connection limit reached"}
                )
                return
            self.open_streams += 1
        thread = threading.Thread(
            target=self._proxy, args=(control, stream_id, host, port), daemon=True,
            name=f"relay-{host}-{port}",
        )
        thread.start()

    def _proxy(self, control: WebSocket, stream_id: str, host: str, port: int) -> None:
        # Logged before anything is attempted: what this node was asked to
        # reach is the one fact an operator here will want, and it must be in
        # the journal whether or not the connection succeeds.
        log.info("AIOps asked for a connection to %s:%s", host, port)
        far = None
        ws = None
        try:
            try:
                far = socket.create_connection((host, port), timeout=20)
            except OSError as exc:
                log.warning("could not reach %s:%s — %s", host, port, exc)
                control.send_json(
                    {"type": "open.failed", "stream": stream_id, "error": f"{host}:{port}: {exc}"}
                )
                return
            # Dialled only once the far host has answered, so the arrival of
            # this socket is itself the server's signal that the host is up.
            ws = WebSocket.connect(
                f"{self.ws_base}/api/relay/stream?stream={stream_id}",
                self.headers(),
                egress=self.egress,
            )
            far.settimeout(None)
            log.info("relaying %s:%s", host, port)
            self._pump(far, ws)
        except (OSError, WebSocketError) as exc:
            log.warning("relay to %s:%s ended: %s", host, port, exc)
        finally:
            for closeable in (ws, far):
                try:
                    if closeable is not None:
                        closeable.close()
                except OSError:
                    pass
            with self._count_lock:
                self.open_streams -= 1
            log.info("connection to %s:%s closed", host, port)

    @staticmethod
    def _pump(far: socket.socket, ws: WebSocket) -> None:
        """Copy both ways until either end stops."""
        done = threading.Event()

        def far_to_aiops() -> None:
            try:
                while True:
                    data = far.recv(CHUNK)
                    if not data:
                        return
                    ws.send(data)
            except (OSError, WebSocketError):
                return
            finally:
                done.set()

        pumper = threading.Thread(target=far_to_aiops, daemon=True)
        pumper.start()
        try:
            while True:
                message = ws.recv()
                if message is None:
                    return
                _, payload = message
                if payload:
                    far.sendall(payload)
        except (OSError, WebSocketError):
            return
        finally:
            done.set()
            try:
                far.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            pumper.join(timeout=5)


# --- state on disk -----------------------------------------------------
def read_credential(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _current_sid() -> str | None:
    """The SID of the account this process is running as, or None.

    By SID rather than by name because every name involved is either localised
    (BUILTIN\\Administrators) or ambiguous once a domain is in the picture.
    """
    try:
        result = subprocess.run(
            ["whoami.exe", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields = [field.strip('" ') for field in result.stdout.strip().split('",')]
    for field in reversed(fields):
        if field.startswith("S-1-"):
            return field
    return None


def _restrict_on_windows(path: str) -> bool:
    """Make `path` readable only by this account, SYSTEM and administrators.

    The mode argument to os.open does nothing on Windows: the file lands with
    whatever %ProgramData% hands down, which by default includes
    BUILTIN\\Users:(OI)(CI)(RX). Measured on a real install, the credential came
    out 0o100666 and every local user could read it. There is no stdlib call
    that sets a Windows DACL, and this agent takes no dependencies, so it shells
    out to icacls - which is on every Windows and is what the installer uses for
    the same job.

    Returns whether the permissions were actually tightened, so the caller can
    decide whether writing a secret here is a good idea.
    """
    if os.name != "nt":
        return True
    sid = _current_sid()
    if sid is None:
        return False
    grants = ["*S-1-5-18:(F)", "*S-1-5-32-544:(F)", f"*{sid}:(F)"]
    try:
        result = subprocess.run(
            ["icacls.exe", path, "/inheritance:r", "/grant:r", *grants, "/Q"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        log.warning("could not restrict %s: %s", path, result.stdout.strip() or result.stderr.strip())
        return False
    return True


def write_credential(path: str, credential: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # Created empty, made private, and only then written to, so the secret is
    # never briefly world-readable. On POSIX the mode on os.open does that in
    # one step; on Windows the mode is ignored entirely and the DACL has to be
    # rewritten as a second step, which is why the file is opened empty first
    # and the credential goes in afterwards.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        if not _restrict_on_windows(path):
            log.warning(
                "could not restrict the permissions on %s. It may be readable by "
                "other users on this machine; check them before trusting this node.",
                path,
            )
        with os.fdopen(handle, "w") as fh:
            handle = None  # fdopen owns it now
            fh.write(credential + "\n")
    finally:
        if handle is not None:
            os.close(handle)


# --- telling somebody why it will not connect --------------------------
#: The one-line verdict for each way out of this network that can fail. The
#: exception's own message says what to do; this says what happened, in the
#: words somebody would use to describe it to a colleague.
VERDICTS: list[tuple[type, str]] = [
    (NameNotResolved, "This machine cannot look up the name AIOps is at."),
    (TCPRefused, "The address answered and the port is shut."),
    (TCPTimedOut,
     "Nothing on the way to AIOps answered at all. That is the shape of a network "
     "that requires a proxy to reach anything outside it, or of a firewall that "
     "drops rather than refuses."),
    (NetworkUnreachable, "There is no route from this machine to AIOps."),
    (ProxyRefusedConnect, "The proxy is reachable and will not tunnel to AIOps."),
    (CertificateNotTrusted,
     "The connection arrives, and this machine does not trust the certificate it is "
     "given. Something is inspecting TLS in between."),
    (TLSFailed, "The connection arrives and the TLS handshake does not finish."),
    (CredentialRejected,
     "The network is fine. AIOps is reachable and refused this node's credential."),
    (UpgradeRefused,
     "Something answered at AIOps' address and did not agree to a relay connection."),
]


def _verdict_for(exc: Exception) -> str:
    for kind, sentence in VERDICTS:
        if isinstance(exc, kind):
            return sentence
    return "The connection to AIOps failed."


def _say(step: str, outcome: str, detail: str = "") -> None:
    print(f"  {step:<22}{outcome:<14}{detail}".rstrip())


def _paragraph(text: str, indent: str = "  ") -> None:
    for line in textwrap.wrap(text, width=76):
        print(indent + line)


def interpreter_notes() -> list[str]:
    """Anything about *this* Python that stops a Windows service from running it.

    This is here because of a real failure that had nothing to do with the
    network: the installer picked the interpreter that answered on the
    administrator's PATH, which was a per-user install under that person's
    profile. The service runs as the virtual account NT SERVICE\\AIOpsRelayNode,
    which is neither SYSTEM nor an administrator nor that person, and a user
    profile grants none of them anything - so `Process.Start` was refused, the
    service host logged an exit code and no reason, and the node looked like a
    network problem for as long as anyone cared to look.

    Every note below is written from the point of view of "would the service
    account be able to run this", not "did it run for me just now" - because it
    just ran for whoever is reading, by definition.
    """
    notes: list[str] = []
    if os.name != "nt":
        return notes
    executable = os.path.abspath(sys.executable or "")
    lowered = executable.lower()
    if "\\windowsapps\\" in lowered:
        notes.append(
            "This interpreter is a Microsoft Store app execution alias "
            f"({executable}). Those are per-user stubs; a service cannot rely on one. "
            "Install Python machine-wide: winget install --id Python.Python.3.12 "
            "--scope machine"
        )
    profile = os.environ.get("USERPROFILE", "")
    users_root = os.path.join(os.environ.get("SystemDrive", "C:") + os.sep, "Users").lower()
    if (profile and lowered.startswith(profile.lower() + os.sep)) or lowered.startswith(users_root):
        notes.append(
            f"This interpreter lives inside a user profile ({executable}). Windows "
            "grants a profile to SYSTEM, administrators and its owner and to nobody "
            "else, so the relay service account cannot execute it even though it "
            "works when you run it yourself. Install Python machine-wide: winget "
            "install --id Python.Python.3.12 --scope machine"
        )
    return notes


def _current_account() -> str:
    try:
        result = subprocess.run(
            ["whoami.exe"] if os.name == "nt" else ["id", "-un"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def _certificate_summary(sock) -> str:
    """Who signed the certificate that was presented, in one line."""
    try:
        peer = sock.getpeercert() or {}
    except (ValueError, OSError):
        return "not readable"
    if not peer:
        return "not checked, because TLS verification is off (--insecure)"

    def field(name: str, part: str) -> str:
        for rdn in peer.get(name, ()):
            for key, value in rdn:
                if key == part:
                    return value
        return ""

    issuer = field("issuer", "commonName") or field("issuer", "organizationName") or "unnamed"
    subject = field("subject", "commonName") or "unnamed"
    return f"issued to {subject}, signed by {issuer}"


def diagnose(url: str, egress: Egress, credential: str | None) -> int:
    """Run the whole dial one stage at a time and say, in words, what happened.

    This exists for one moment: somebody's laptop says "not connected", they are
    on a VPN, and they need to know within a minute whether the problem is DNS,
    the gateway, the certificate or their credential. Every line is meant to be
    readable by a person under mild stress on a phone screen, which is why it is
    printed rather than logged and why there is a conclusion at the bottom.
    """
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)

    print()
    print(f"AIOps relay node {VERSION} - connection check")
    print(f"  AIOps at              {url}")
    print(f"  Reaching              {host} port {port} over {'TLS' if secure else 'plain HTTP'}")
    print(f"  From                  {socket.gethostname()}, "
          f"addresses {', '.join(local_networks()) or 'none found'}")
    print(f"  Credential            {'stored' if credential else 'none yet (not enrolled)'}")
    # Printed before any network step because it is the one that was missed:
    # a node can fail with a perfect network and an interpreter its service
    # account cannot execute, and nothing about the network says so.
    print(f"  Running as            {_current_account()}")
    print(f"  Interpreter           {sys.executable} (Python "
          f"{'.'.join(str(n) for n in sys.version_info[:3])})")
    print()
    notes = interpreter_notes()
    if notes:
        print("  This interpreter is a problem for the service, whatever the network does:")
        for note in notes:
            _paragraph(note, indent="    ")
        print()

    failure: Exception | None = None
    proxy_decision: ProxyDecision | None = None
    aiops_said: dict | None = None

    # 1. name resolution
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        addresses = sorted({info[4][0] for info in infos})
        _say("1. Name resolution", "OK", f"{host} -> {', '.join(addresses)}")
    except socket.gaierror as exc:
        _say("1. Name resolution", "FAILED", f"{host}: {exc.strerror or exc}")
        failure = NameNotResolved(
            f"DNS: the name {host!r} did not resolve ({exc.strerror or exc}). Nothing was "
            "dialled. On a VPN this is usually the tunnel's resolver not knowing this "
            "name, or split DNS sending it to the wrong one."
        )

    # 2. which proxy, and where it was learned from
    if failure is None:
        try:
            proxy_decision = egress.proxy_for(host)
        except ProxyConfigError as exc:
            _say("2. Proxy", "UNUSABLE", str(exc))
            failure = exc
        else:
            if proxy_decision.target is None:
                _say("2. Proxy", "not used", proxy_decision.source)
            else:
                _say("2. Proxy", "in use",
                     f"{proxy_decision.target.safe_url} (from {proxy_decision.source})")

    # 3. TCP, to the proxy if there is one and to AIOps if there is not
    sock = None
    if failure is None and proxy_decision is not None:
        proxy = proxy_decision.target
        where = f"the proxy at {proxy.address}" if proxy else f"{host}:{port}"
        began = time.monotonic()
        try:
            sock = (
                tcp_connect(proxy.host, proxy.port, 15, label=where) if proxy
                else tcp_connect(host, port, 15)
            )
            _say("3. TCP connection", "OK", f"{where} in {time.monotonic() - began:.2f}s")
        except EgressError as exc:
            _say("3. TCP connection", "FAILED", where)
            failure = exc

    # 4. the CONNECT tunnel
    if sock is not None and proxy_decision is not None and proxy_decision.target is not None:
        try:
            open_tunnel(sock, proxy_decision.target, host, port)
            _say("4. Proxy tunnel", "OK", f"CONNECT {host}:{port} accepted")
        except ProxyRefusedConnect as exc:
            _say("4. Proxy tunnel", "FAILED", exc.status_line or "no answer")
            failure = exc
            sock = None
    elif failure is None:
        _say("4. Proxy tunnel", "not needed", "there is no proxy in the way")

    # 5. TLS
    if sock is not None and secure:
        try:
            wrapped = egress.wrap(sock, host)
        except EgressError as exc:
            _say("5. TLS", "FAILED", "the certificate was not accepted")
            failure = exc
            sock = None
        else:
            _say("5. TLS", "OK", f"{wrapped.version()}, {_certificate_summary(wrapped)}")
            sock = wrapped
    elif sock is not None:
        _say("5. TLS", "not used", "this AIOps URL is plain HTTP")

    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass

    # 6. the real thing, over the code path that runs in production
    if failure is None:
        scheme = "wss" if secure else "ws"
        headers = {"User-Agent": f"aiops-relay/{VERSION}"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        # The stream endpoint rather than the control one, and this matters:
        # AIOps keeps a single control channel per node and closes the old one
        # when a new arrives, so diagnosing a node whose service is running
        # would knock that service off its connection. /stream authenticates
        # identically, answers with the same close codes, and displaces
        # nothing - it simply has no pending connection to hand over.
        probe = f"/api/relay/stream?stream=diagnose-{secrets.token_hex(8)}"
        try:
            ws = WebSocket.connect(
                f"{scheme}://{parts.netloc}{probe}", headers, egress=egress, timeout=20
            )
        except EgressError as exc:
            if isinstance(exc, CredentialRejected) and not credential:
                # Expected, and it is the answer: the whole path works.
                _say("6. AIOps handshake", "REACHED",
                     "AIOps answered, and asked for a credential this node does not have yet")
            else:
                _say("6. AIOps handshake", "FAILED", "")
                failure = exc
        else:
            # The upgrade succeeding is not the end of it. AIOps accepts the
            # socket and *then* says whether it knows this node, so the answer
            # to "is this node approved" is one frame further on - and it is
            # the other thing somebody running this wants to know.
            ws.sock.settimeout(15)
            answer = None
            try:
                message = ws.recv()
                if message is not None:
                    answer = json.loads(message[1].decode())
            except (OSError, WebSocketError, ValueError):
                answer = None
            code = (answer or {}).get("code")
            if code == CLOSE_NOT_APPROVED:
                _say("6. AIOps handshake", "REACHED",
                     "AIOps knows this node and it is not approved yet")
                aiops_said = answer
            elif code == CLOSE_UNAUTHENTICATED and not credential:
                # Expected, and it is the answer: everything up to AIOps works,
                # and this node simply has not enrolled yet.
                _say("6. AIOps handshake", "REACHED",
                     "AIOps answered, and asked for a credential this node does not have yet")
            elif code == CLOSE_UNAUTHENTICATED:
                _say("6. AIOps handshake", "REFUSED",
                     "AIOps does not recognise this node's credential")
                failure = CredentialRejected(
                    "AUTH: AIOps answered the handshake by rejecting this node's "
                    "credential. That is an authentication failure and not a network "
                    "fault: the node has been revoked or deleted, or was re-enrolled "
                    "somewhere else. Register it in AIOps again and re-run the installer "
                    "with a fresh enrolment token."
                )
            elif code == CLOSE_REVOKED:
                _say("6. AIOps handshake", "REFUSED", "AIOps has revoked this node")
                aiops_said = answer
            else:
                # Authenticated, approved, and there was nothing pending for it
                # to pick up - which is exactly what a healthy node looks like
                # on this endpoint.
                _say("6. AIOps handshake", "OK",
                     "AIOps accepted this node's credential")
            ws.close()

    print()
    if failure is None:
        print("Conclusion")
        if notes:
            _paragraph(
                "The network is fine: this machine can reach AIOps right now. What is "
                "wrong is the interpreter above - the service account cannot run it, so "
                "the agent never starts and the node never connects."
            )
            print()
            _paragraph(notes[0])
            print()
            return 1
        if aiops_said is not None:
            _paragraph(
                "The network is fine: this machine reaches AIOps and AIOps answers. What "
                f"it said is: {aiops_said.get('reason')}. That is a decision made in "
                "AIOps, not a fault on this machine - a node waiting to be approved is "
                "approved by an administrator under Nodes."
            )
            print()
            return 0
        _paragraph(
            "This node can reach AIOps from this machine right now. If it still shows as "
            "not connected, the agent is not running: check the service, and read its "
            "log. A log holding only 'service host:' lines means the agent process never "
            "started - the reason is in the Application event log under the service's "
            "own name, not in that file."
        )
        print()
        return 0

    print("Conclusion")
    _paragraph(_verdict_for(failure))
    print()
    _paragraph(str(failure))
    print()
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="AIOps relay node agent")
    parser.add_argument("--url", default=os.environ.get("AIOPS_RELAY_URL", ""),
                        help="AIOps base URL, e.g. https://aiops.example.com")
    parser.add_argument("--token", default=os.environ.get("AIOPS_RELAY_TOKEN", ""),
                        help="one-time enrolment token (only needed the first time)")
    parser.add_argument("--state-dir", default=os.environ.get("AIOPS_RELAY_STATE_DIR", "/var/lib/aiops-relay"))
    parser.add_argument("--max-streams", type=int,
                        default=int(os.environ.get("AIOPS_RELAY_MAX_STREAMS", "64")))
    parser.add_argument("--proxy", default=os.environ.get("AIOPS_RELAY_PROXY", ""),
                        help="HTTP CONNECT proxy to reach AIOps through, e.g. "
                             "http://proxy.corp:8080. Takes precedence over HTTPS_PROXY, "
                             "ALL_PROXY and the machine's own settings")
    parser.add_argument("--ca-bundle", default=os.environ.get("AIOPS_RELAY_CA_BUNDLE", ""),
                        help="PEM file of extra certificate authorities to trust, for a "
                             "network that inspects TLS. Prefer this to --insecure")
    parser.add_argument("--insecure", action="store_true",
                        default=os.environ.get("AIOPS_RELAY_INSECURE", "") == "1",
                        help="skip TLS verification entirely (for a self-signed AIOps "
                             "only; --ca-bundle is the right answer to TLS inspection)")
    parser.add_argument("--enrol-only", action="store_true",
                        help="exchange the token for a credential and exit")
    parser.add_argument("--diagnose", action="store_true",
                        help="say, step by step, whether this machine can reach AIOps, "
                             "and stop")
    args = parser.parse_args(argv[1:])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stdout,
    )
    if not args.url:
        log.error("no AIOps URL given (--url or AIOPS_RELAY_URL)")
        return 2
    if args.insecure:
        log.warning(
            "TLS verification is OFF for this connection (--insecure). If this is here "
            "because a gateway on this network re-signs certificates, --ca-bundle PATH "
            "is the fix: it keeps verification on against that gateway's own authority, "
            "and the enrolment credential travels in this handshake."
        )
    if args.ca_bundle and not os.path.isfile(args.ca_bundle):
        log.error("the CA bundle %s does not exist or is not a file", args.ca_bundle)
        return 2

    egress = Egress(
        verify=not args.insecure,
        ca_bundle=args.ca_bundle or None,
        proxy=args.proxy or None,
    )
    try:
        # Resolved once, at startup, so a mistyped proxy is a refusal here
        # rather than a reconnect loop that never says what is wrong.
        decision = egress.proxy_for(urlsplit(args.url).hostname or "")
    except ProxyConfigError as exc:
        log.error("the proxy this node was given is unusable: %s", exc)
        return 2

    credential_path = os.path.join(args.state_dir, "credential")
    credential = read_credential(credential_path)

    if args.diagnose:
        return diagnose(args.url.rstrip("/"), egress, credential)

    log.info("reaching AIOps %s", decision)
    if args.ca_bundle:
        log.info("trusting the extra certificate authorities in %s", args.ca_bundle)

    if credential is None:
        if not args.token:
            log.error(
                "this node has no credential and no enrolment token. Register the node "
                "in AIOps, then run the installer again with --token."
            )
            return 2
        try:
            result = enrol(args.url.rstrip("/"), args.token, egress)
        except urllib.error.HTTPError as exc:
            log.error("enrolment refused by AIOps: %s %s", exc.code, exc.read().decode()[:300])
            return 1
        except (urllib.error.URLError, OSError) as exc:
            log.error("could not reach AIOps to enrol: %s", exc)
            return 1
        credential = result["credential"]
        write_credential(credential_path, credential)
        log.info(
            "enrolled as node %r (%s). %s",
            result.get("slug"), result.get("status"), result.get("message", ""),
        )
        if args.enrol_only:
            return 0

    agent = RelayAgent(args.url, credential, egress, args.max_streams,
                       status_path=os.path.join(args.state_dir, "status"))
    log.info("aiops-relay %s starting; AIOps at %s", VERSION, args.url)
    # The interpreter, in the first few lines, every time. When a node fails
    # because a service account cannot run the Python the installer chose, this
    # line is the difference between reading the answer and inferring it.
    log.info("running on %s (Python %s)", sys.executable,
             ".".join(str(n) for n in sys.version_info[:3]))
    for note in interpreter_notes():
        log.warning("%s", note)
    try:
        return agent.run_forever()
    except KeyboardInterrupt:
        agent.stop.set()
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
