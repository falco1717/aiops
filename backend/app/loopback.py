"""Who is really on the other end of the socket, and the gate built on that.

`/api/internal/*` is the callback surface for two processes that run *inside*
this container — the MCP approval bridge and the browser bridge. Both of them
talk to `http://127.0.0.1:8000`. Every other request this application ever sees
arrives from Traefik, over the docker network, from an address that is not the
loopback. So the transport peer separates the two populations cleanly, and
nothing the caller is able to write separates them at all. That is the whole
reason the gate below is a peer check and not a header check.

Why `request.client` is *not* the thing to check
------------------------------------------------
Uvicorn is started with `--proxy-headers --forwarded-allow-ips *`, which wraps
the application in `ProxyHeadersMiddleware`. That middleware **overwrites**
`scope["client"]` with an address taken out of `X-Forwarded-For`, and with
`*` as the trusted-hosts setting it takes the *left-most* entry of that header
— the one nobody upstream has verified and any public caller can put there.
`request.client.host` therefore reads `127.0.0.1` for anybody on the internet
who simply says `X-Forwarded-For: 127.0.0.1`. A gate written against the
obvious attribute would be no gate at all.

`ratelimit.client_address()` is worse for this purpose and deliberately so: it
reads `X-Forwarded-For` *first*, on purpose, because the sign-in throttle wants
the real browser behind the proxy. It is right for that and must not be reused
here.

What is checked instead
-----------------------
`RawPeerMiddleware` sits at the very outside of the ASGI stack — outside
`ProxyHeadersMiddleware`, which is the only position from which the untouched
peer is visible — and copies it into the scope before anything can rewrite it.
`build_asgi()` is what guarantees that ordering, and the container runs
`app.main:asgi`, not `app.main:app`, so that it is always in force.
"""

from __future__ import annotations

import ipaddress
import logging

from fastapi import HTTPException, Request, status

log = logging.getLogger("aiops.loopback")

#: Scope key holding the transport peer as uvicorn first saw it. Namespaced so
#: it cannot collide with anything ASGI defines.
RAW_CLIENT = "aiops.raw_client"

#: Path prefix that only in-container callers may reach.
INTERNAL_PREFIX = "/api/internal"

#: Refusals are shaped exactly like FastAPI's own "no such route" answer, and
#: that is the point: a 403 would confirm the endpoint exists.
NOT_FOUND_BODY = b'{"detail":"Not Found"}'


def _is_loopback_address(host: str | None) -> bool:
    if not host:
        return False
    # Uvicorn hands over IPv6 hosts unbracketed, and a scope id ("::1%lo0")
    # would otherwise fail to parse.
    host = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an address at all — a unix socket path, or TestClient's
        # "testclient". Neither is a loopback TCP peer.
        return False


def peer_is_loopback(scope) -> bool:
    """True only if this request genuinely arrived over the loopback interface.

    Reads the peer captured by `RawPeerMiddleware`. If that key is missing the
    stack was not built by `build_asgi` — a test harness, or `uvicorn
    app.main:app` in development — and `scope["client"]` is then the real peer
    *provided* nothing has rewritten it. The only thing that rewrites it is a
    proxy middleware acting on a forwarded header, so the presence of one of
    those headers in that situation is treated as disqualifying rather than
    guessed about. Fail closed: a stack assembled wrongly refuses the internal
    routes instead of quietly opening them.
    """
    if RAW_CLIENT in scope:
        client = scope[RAW_CLIENT]
    else:
        headers = scope.get("headers") or ()
        for name, _value in headers:
            if name in (b"x-forwarded-for", b"x-real-ip", b"forwarded"):
                return False
        client = scope.get("client")
    return _is_loopback_address(client[0] if client else None)


class RawPeerMiddleware:
    """Copies the transport peer into the scope before anything rewrites it.

    Pure ASGI rather than `BaseHTTPMiddleware` because it has to be able to sit
    outside `ProxyHeadersMiddleware`, which is applied to the whole app rather
    than added to it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope[RAW_CLIENT] = scope.get("client")
        return await self.app(scope, receive, send)


class LoopbackOnlyMiddleware:
    """Refuses `/api/internal/*` to anything that did not come over loopback.

    The load-bearing half of the fix, and it lives here rather than only on the
    two routers so that a third internal router added later is covered the
    moment it is mounted.
    """

    def __init__(self, app, prefix: str = INTERNAL_PREFIX) -> None:
        self.app = app
        self.prefix = prefix

    def _guarded(self, scope) -> bool:
        path = scope.get("path") or ""
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and self._guarded(scope):
            if not peer_is_loopback(scope):
                # Logged because a request for this path from off-box is either
                # a scan or a leaked run token being tried, and both are worth
                # knowing about. The peer named is the real one.
                raw = scope.get(RAW_CLIENT) or scope.get("client")
                log.warning(
                    "Refused %s %s from %s — %s/* is reachable over loopback only",
                    scope.get("method", scope["type"]),
                    scope.get("path"),
                    raw[0] if raw else "an unknown peer",
                    self.prefix,
                )
                return await self._refuse(scope, send)
        return await self.app(scope, receive, send)

    async def _refuse(self, scope, send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": status.HTTP_404_NOT_FOUND,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(NOT_FOUND_BODY)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": NOT_FOUND_BODY})


async def require_loopback(request: Request) -> None:
    """Router-level restatement of the middleware above.

    Not redundant in the way it looks: it keeps the rule visible where the
    routes are declared, and it still holds if this app is ever mounted inside
    another one where the middleware above is not in the chain. Silent, because
    the middleware does the logging in the normal case.
    """
    if not peer_is_loopback(request.scope):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def build_asgi(app):
    """The ASGI stack the container serves: raw peer captured outermost.

    Uvicorn would apply `ProxyHeadersMiddleware` itself given `--proxy-headers`,
    but it applies it *outside* everything, which is exactly where the raw peer
    is lost. Applying it here instead — with the same `trusted_hosts="*"` the
    command line used, so `request.base_url` still comes back as `https://…`
    for the node installer commands, and the sign-in throttle still sees real
    client addresses — puts one layer above it that remembers who actually
    connected.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    return RawPeerMiddleware(ProxyHeadersMiddleware(app, trusted_hosts="*"))
