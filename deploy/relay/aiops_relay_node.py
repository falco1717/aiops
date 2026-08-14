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
import threading
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


# --- a small websocket client -----------------------------------------
class WebSocketError(Exception):
    pass


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
        cls, url: str, headers: dict[str, str], *, verify: bool = True, timeout: float = 30
    ) -> "WebSocket":
        parts = urlsplit(url)
        secure = parts.scheme in ("wss", "https")
        port = parts.port or (443 if secure else 80)
        host = parts.hostname or ""
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        raw = socket.create_connection((host, port), timeout=timeout)
        if secure:
            context = ssl.create_default_context()
            if not verify:
                # Only reachable via --insecure, which the installer refuses to
                # pass silently: a relay carries an SSH session that
                # authenticates itself, but the credential in the handshake
                # above is worth protecting on its own.
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            raw = context.wrap_socket(raw, server_hostname=host)

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
        if "101" not in status:
            raw.close()
            raise WebSocketError(f"server refused the upgrade: {status}")

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
def enrol(base_url: str, token: str, verify: bool) -> dict:
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
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read().decode())


# --- the agent ---------------------------------------------------------
class RelayAgent:
    def __init__(self, base_url: str, credential: str, verify: bool, max_streams: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.credential = credential
        self.verify = verify
        self.max_streams = max_streams
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

    def run_forever(self) -> int:
        backoff = 1.0
        while not self.stop.is_set():
            try:
                verdict = self.session()
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
            verify=self.verify,
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
                elif kind == "open":
                    self.spawn(ws, event)
                elif kind == "hello":
                    log.info("registered with AIOps as node %r", event.get("node"))
                elif kind == "denied":
                    log.warning("AIOps refused this node: %s", event.get("reason"))
        finally:
            ws.close()
        return "connected"

    @staticmethod
    def _closed_because(code: int | None) -> str | None:
        """What a close means for whether it is worth dialling again."""
        if code == CLOSE_REVOKED:
            return "revoked"
        if code == CLOSE_NOT_APPROVED:
            log.info("this node is enrolled but not yet approved in AIOps; waiting")
            return None
        if code == CLOSE_UNAUTHENTICATED:
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
                verify=self.verify,
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


def write_credential(path: str, credential: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # Created private before anything is written to it, so the secret is never
    # briefly world-readable.
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as fh:
        fh.write(credential + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="AIOps relay node agent")
    parser.add_argument("--url", default=os.environ.get("AIOPS_RELAY_URL", ""),
                        help="AIOps base URL, e.g. https://aiops.example.com")
    parser.add_argument("--token", default=os.environ.get("AIOPS_RELAY_TOKEN", ""),
                        help="one-time enrolment token (only needed the first time)")
    parser.add_argument("--state-dir", default=os.environ.get("AIOPS_RELAY_STATE_DIR", "/var/lib/aiops-relay"))
    parser.add_argument("--max-streams", type=int,
                        default=int(os.environ.get("AIOPS_RELAY_MAX_STREAMS", "64")))
    parser.add_argument("--insecure", action="store_true",
                        default=os.environ.get("AIOPS_RELAY_INSECURE", "") == "1",
                        help="skip TLS verification (for a self-signed AIOps only)")
    parser.add_argument("--enrol-only", action="store_true",
                        help="exchange the token for a credential and exit")
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
        log.warning("TLS verification is OFF for this connection (--insecure)")

    credential_path = os.path.join(args.state_dir, "credential")
    credential = read_credential(credential_path)

    if credential is None:
        if not args.token:
            log.error(
                "this node has no credential and no enrolment token. Register the node "
                "in AIOps, then run the installer again with --token."
            )
            return 2
        try:
            result = enrol(args.url.rstrip("/"), args.token, not args.insecure)
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

    agent = RelayAgent(args.url, credential, not args.insecure, args.max_streams)
    log.info("aiops-relay %s starting; AIOps at %s", VERSION, args.url)
    try:
        return agent.run_forever()
    except KeyboardInterrupt:
        agent.stop.set()
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
