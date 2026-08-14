"""ssh's ProxyCommand for a system that is reached through a relay node.

Run as a script, never imported: `ssh` starts it with the host and port it
wants and then treats this process's stdin and stdout as the connection. All it
does is hand those bytes to the AIOps forwarder on the loopback, which passes
them to the node holding the far network open.

Deliberately dependency-free and app-free — it runs as a grandchild of the
agent process, so importing the application would drag a settings object and a
database engine into something whose only job is to copy bytes.
"""
from __future__ import annotations

import os
import selectors
import socket
import sys

PROTOCOL = "AIOPS-RELAY/1"
CHUNK = 64 * 1024


def fail(message: str) -> int:
    # ssh shows a ProxyCommand's stderr to the operator, and the agent sees it
    # in the tool output. This is the only place a relay failure is explained.
    sys.stderr.write(f"aiops-relay: {message}\n")
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return fail("usage: relay_connect.py <node-slug> <host> <port>")
    node, host, port = argv[1], argv[2], argv[3]

    if node == "-":
        return fail(
            "this system is bound to a relay node that no longer exists. "
            "Point it at another node, or clear the relay setting to connect directly."
        )

    token = os.environ.get("AIOPS_RELAY_TOKEN", "")
    address = os.environ.get("AIOPS_RELAY_ADDR", "")
    if not token or not address:
        return fail("no relay credentials in this run's environment")
    forward_host, _, forward_port = address.rpartition(":")
    if not forward_port.isdigit():
        return fail(f"malformed relay address {address!r}")

    try:
        sock = socket.create_connection((forward_host, int(forward_port)), timeout=30)
    except OSError as exc:
        return fail(f"the AIOps relay forwarder is not reachable ({exc})")

    try:
        sock.sendall(f"{PROTOCOL} {token} {node} {host} {port}\n".encode())
        status = read_line(sock)
        if not status.startswith("OK"):
            return fail(status.partition(" ")[2] or status or "the relay refused the connection")
        sock.settimeout(None)
        pump(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return 0


def read_line(sock: socket.socket) -> str:
    """The forwarder's one-line verdict, read a byte at a time.

    Nothing may be over-read here: everything after the newline is already the
    connection itself, and buffering it would lose the far host's first bytes.
    """
    out = bytearray()
    while len(out) < 1024:
        try:
            byte = sock.recv(1)
        except OSError:
            break
        if not byte or byte == b"\n":
            break
        out += byte
    return out.decode("utf-8", errors="replace").strip()


def pump(sock: socket.socket) -> None:
    """Copy between stdin/stdout and the forwarder until either end stops."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    selector = selectors.DefaultSelector()
    selector.register(stdin, selectors.EVENT_READ, "in")
    selector.register(sock, selectors.EVENT_READ, "sock")

    while True:
        for key, _ in selector.select():
            if key.data == "in":
                data = os.read(stdin.fileno(), CHUNK)
                if not data:
                    return
                try:
                    sock.sendall(data)
                except OSError:
                    return
            else:
                try:
                    data = sock.recv(CHUNK)
                except OSError:
                    return
                if not data:
                    return
                stdout.write(data)
                stdout.flush()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
