"""Who can reach `/api/internal/*`, measured rather than asserted.

This suite exists because of a bug that every other suite was structurally
blind to. The two internal routers — the approval bridge and the browser bridge
— carried docstrings saying they were unreachable from outside the container.
They were mounted on the same public app as everything else, so they were not:
against production, from the open internet,

    POST /api/internal/browser/credential  ->  401 Unknown or expired run token

which is a route that hands back a stored system password in **plaintext** to
any caller holding a run token. A run token is a per-run secret that lives in
an agent's environment, so a leak of one — a log line, a paste, a process
listing, a compromised agent — went from "needs a shell in the container" to
"is a curl from anywhere".

Nothing caught it because every existing test drives the app in-process, where
there is no network for a route to be exposed on. So the property here is
tested the only way it can be: by building the **production ASGI stack** and
presenting a peer address that is not the loopback.

The other half of the bug is subtler and is pinned below too. Uvicorn is served
with proxy headers, and `ProxyHeadersMiddleware` **rewrites** `scope["client"]`
out of `X-Forwarded-For` — taking the left-most entry, which is entirely
caller-supplied. So a gate written against the obvious attribute,
`request.client.host`, would be satisfied by anyone who typed
`X-Forwarded-For: 127.0.0.1`. The forged-header checks are the point of this
file; the plain non-loopback ones only prove the door is shut at all.
"""

import os
import sys

sys.path.insert(0, os.getcwd())

for _stale in ("./test-internal-exposure.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-internal-exposure.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.loopback import (  # noqa: E402
    RAW_CLIENT,
    build_asgi,
    peer_is_loopback,
    require_loopback,
)
from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


#: Every route on the internal prefix, with a body that would be valid if the
#: caller were entitled to be here. The token is nonsense on purpose: what is
#: being measured is whether the *route* answers, and a route that is reachable
#: says "Unknown or expired run token" rather than "Not Found".
INTERNAL = [
    ("/api/internal/approvals", {"token": "nonsense"}),
    ("/api/internal/browser/credential", {"token": "nonsense", "system": "anything"}),
    ("/api/internal/browser/screenshot", {"token": "nonsense"}),
    ("/api/internal/browser/reach", {"token": "nonsense"}),
    ("/api/internal/browser/log", {"token": "nonsense", "action": "opened"}),
]

#: What Traefik's peer looks like from inside the container: an address on the
#: docker network, which is what every request from the internet arrives as.
FROM_THE_PROXY = ("172.19.0.7", 51234)
#: And what the two bridges look like.
FROM_A_BRIDGE = ("127.0.0.1", 45678)

#: Headers a public caller is free to write, and would write first.
FORGERIES = [
    {"X-Forwarded-For": "127.0.0.1"},
    {"X-Forwarded-For": "127.0.0.1, 10.0.0.1"},
    {"X-Forwarded-For": "::1"},
    {"X-Real-IP": "127.0.0.1"},
    {"Forwarded": "for=127.0.0.1"},
    {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Host": "localhost"},
]

#: The exact bytes FastAPI gives for a route that does not exist. The refusal
#: has to be indistinguishable from it: a 403 would confirm the endpoint is
#: there, which is the one thing worth not telling a scanner.
MISSING = {"detail": "Not Found"}


# =====================================================================
# 1. The premise: the obvious attribute really is forgeable
# =====================================================================
#
# Pinned before anything else, because if this stops being true the comments
# explaining why the gate is written the awkward way become wrong, and somebody
# will "simplify" it back into a hole.
print("--- why request.client is not the thing to check ---")

probe = FastAPI()


@probe.get("/whoami")
async def whoami(request: Request):
    raw = request.scope.get(RAW_CLIENT)
    return {
        "client": request.client.host if request.client else None,
        "raw": raw[0] if raw else None,
        "loopback": peer_is_loopback(request.scope),
    }


probe_stack = build_asgi(probe)
proxied = TestClient(probe_stack, client=FROM_THE_PROXY)

plain = proxied.get("/whoami").json()
check("with no forwarded header, request.client is the real peer",
      plain["client"] == FROM_THE_PROXY[0], str(plain))

forged = proxied.get("/whoami", headers={"X-Forwarded-For": "127.0.0.1"}).json()
check("BYPASS: a forged X-Forwarded-For makes request.client.host read 127.0.0.1",
      forged["client"] == "127.0.0.1", str(forged))
check("...so a gate written on request.client would have let the internet straight in",
      forged["client"] == "127.0.0.1" and forged["raw"] == FROM_THE_PROXY[0], str(forged))
check("the raw transport peer is untouched by the header",
      forged["raw"] == FROM_THE_PROXY[0], str(forged))
check("and that is what the gate reads, so it still says not-loopback",
      forged["loopback"] is False, str(forged))

real = TestClient(probe_stack, client=FROM_A_BRIDGE).get("/whoami").json()
check("a genuine loopback peer is recognised", real["loopback"] is True, str(real))


# =====================================================================
# 2. The internal routes, over the production stack
# =====================================================================
print("\n--- /api/internal from off-box ---")

stack = build_asgi(app)

with TestClient(stack, client=FROM_THE_PROXY) as outside:
    for path, body in INTERNAL:
        r = outside.post(path, json=body)
        check(f"{path} is not reachable from the docker network",
              r.status_code == 404, f"{r.status_code} {r.text[:120]}")
        check(f"{path} is refused as missing rather than forbidden",
              r.json() == MISSING, r.text[:120])

    for headers in FORGERIES:
        label = ", ".join(f"{k}: {v}" for k, v in headers.items())
        bad = [
            f"{path} -> {r.status_code}"
            for path, body in INTERNAL
            if (r := outside.post(path, json=body, headers=headers)).status_code != 404
        ]
        check(f"FORGED [{label}] reaches none of the five internal routes",
              not bad, str(bad))

    # The screenshot route reads a raw body rather than JSON, and the approvals
    # one blocks until a human answers. Neither should get far enough to do
    # either, so both are checked with the payloads that would actually work.
    r = outside.post("/api/internal/browser/screenshot",
                     content=b"\x89PNG\r\n\x1a\n" + b"0" * 512,
                     headers={"x-aiops-token": "nonsense",
                              "x-aiops-screenshot": "screenshot-001.png",
                              "X-Forwarded-For": "127.0.0.1"})
    check("a forged screenshot upload is refused before a byte of it is read",
          r.status_code == 404 and r.json() == MISSING, f"{r.status_code} {r.text[:120]}")

    r = outside.post("/api/internal/approvals",
                     json={"token": "nonsense", "kind": "tool", "tool_name": "Bash"},
                     headers={"x-aiops-token": "nonsense", "X-Forwarded-For": "127.0.0.1"})
    check("and so is a forged approval, rather than it blocking on a human",
          r.status_code == 404 and r.json() == MISSING, f"{r.status_code} {r.text[:120]}")

    # The token may also travel as a header rather than in the body; the gate
    # runs before either is looked at, but the shape is worth pinning.
    r = outside.post("/api/internal/browser/credential", json={"system": "anything"},
                     headers={"x-aiops-token": "nonsense", "X-Forwarded-For": "127.0.0.1"})
    check("a header-borne run token gets no further than a body-borne one",
          r.status_code == 404 and r.json() == MISSING, f"{r.status_code} {r.text[:120]}")

    # The prefix match must not be a bare string test: an unrelated path that
    # merely starts with the same letters is a 404 for the ordinary reason.
    r = outside.get("/api/internalish")
    check("a path that only looks like the prefix is an ordinary 404",
          r.status_code == 404, f"{r.status_code} {r.text[:80]}")

    # The public API, from the same non-loopback peer, is untouched.
    r = outside.get("/api/health")
    check("the public health check still answers from off-box",
          r.status_code == 200 and r.json()["status"] == "ok", r.text[:120])
    r = outside.get("/api/accounts")
    check("and an authenticated route still says 401, not 404",
          r.status_code == 401, f"{r.status_code} {r.text[:120]}")
    r = outside.post("/api/auth/login", json={"username": "nobody", "password": "x" * 12})
    check("sign-in is still reachable from off-box",
          r.status_code in (401, 429), f"{r.status_code} {r.text[:120]}")
    r = outside.post("/api/relay/enroll", json={"token": "nonsense"})
    check("and so is node enrolment, which is meant to be public",
          r.status_code != 404, f"{r.status_code} {r.text[:120]}")

    # =================================================================
    # 3. The bridges, which must still work
    # =================================================================
    #
    # Same running app — a second TestClient over the same stack, differing
    # only in the peer it presents. Lifespan is owned by the block above, so
    # what changes between these two populations is the address and nothing
    # else.
    print("\n--- /api/internal from inside the container ---")

    inside = TestClient(stack, client=FROM_A_BRIDGE)
    for path, body in INTERNAL:
        r = inside.post(path, json=body)
        check(f"{path} still answers a loopback caller",
              r.status_code == 401 and "run token" in r.text,
              f"{r.status_code} {r.text[:120]}")

    # A bridge would never send one, but a loopback caller that does must not
    # be able to talk itself *out* of the loopback either — the gate reads the
    # peer in both directions.
    r = inside.post("/api/internal/browser/reach", json={"token": "nonsense"},
                    headers={"X-Forwarded-For": "8.8.8.8"})
    check("a loopback caller's own forwarded header changes nothing",
          r.status_code == 401, f"{r.status_code} {r.text[:120]}")

    r = TestClient(stack, client=("::1", 45678)).post(
        "/api/internal/browser/reach", json={"token": "nonsense"})
    check("IPv6 loopback counts as loopback too",
          r.status_code == 401, f"{r.status_code} {r.text[:120]}")


# =====================================================================
# 4. Fail closed when the stack is assembled wrongly
# =====================================================================
print("\n--- a stack built without the raw-peer layer ---")

# `uvicorn app.main:app --proxy-headers` would rewrite the client with nothing
# recording the real peer first. There is then no way to tell a genuine
# loopback caller from a forged one, so the answer has to be no.
check("no raw peer recorded and no forwarded header — the client is the peer",
      peer_is_loopback({"type": "http", "client": ("127.0.0.1", 1), "headers": []}) is True)
check("no raw peer recorded but a forwarded header present — refused",
      peer_is_loopback({"type": "http", "client": ("127.0.0.1", 1),
                        "headers": [(b"x-forwarded-for", b"127.0.0.1")]}) is False)
check("the same for x-real-ip",
      peer_is_loopback({"type": "http", "client": ("127.0.0.1", 1),
                        "headers": [(b"x-real-ip", b"127.0.0.1")]}) is False)
check("and for the RFC 7239 spelling",
      peer_is_loopback({"type": "http", "client": ("127.0.0.1", 1),
                        "headers": [(b"forwarded", b"for=127.0.0.1")]}) is False)
check("a peer that is not an address at all is not loopback",
      peer_is_loopback({"type": "http", "client": ("testclient", 50000), "headers": []}) is False)
check("nor is a missing peer",
      peer_is_loopback({"type": "http", "client": None, "headers": []}) is False)
check("nor is a private address that merely sounds local",
      peer_is_loopback({"type": "http", "client": ("203.0.113.10", 1), "headers": []}) is False)
check("a recorded raw peer wins over any header, forged or not",
      peer_is_loopback({"type": "http", "client": ("127.0.0.1", 0),
                        RAW_CLIENT: ("172.19.0.7", 51234),
                        "headers": [(b"x-forwarded-for", b"127.0.0.1")]}) is False)


# =====================================================================
# 5. Structural: nothing new slips onto the prefix ungated
# =====================================================================
print("\n--- the route table ---")


def _dependency_names(route):
    names = set()

    def walk(dependant):
        for sub in dependant.dependencies:
            walk(sub)
        call = getattr(dependant, "call", None)
        if call is not None:
            names.add(getattr(call, "__name__", ""))

    walk(route.dependant)
    return names


internal_routes = [
    r for r in app.routes
    if isinstance(r, APIRoute) and r.path.startswith("/api/internal")
]
for route in internal_routes:
    check(f"{route.path} carries require_loopback at the router as well",
          require_loopback.__name__ in _dependency_names(route))
check("and every route on the prefix is one this suite actually drives",
      {r.path for r in internal_routes} == {p for p, _b in INTERNAL},
      str(sorted(r.path for r in internal_routes)))

#: Routes that answer an unauthenticated caller on purpose. Anything arriving
#: on this list is a new hole; anything leaving it is a new lockout. Both are
#: worth a deliberate edit here rather than a surprise in production.
EXPECTED_PUBLIC = {
    "/api/auth/login",          # the sign-in form itself, throttled per IP and user
    "/api/auth/logout",         # clears a cookie; nothing to leak
    "/api/health",              # the container healthcheck
    "/api/relay/enroll",        # a node presents an enrolment token here
    "/{full_path:path}",        # the SPA shell, deliberately pre-login
}

unauthenticated = set()
for route in app.routes:
    if not isinstance(route, APIRoute):
        continue
    names = _dependency_names(route)
    if names & {"current_user", "current_admin", require_loopback.__name__}:
        continue
    unauthenticated.add(route.path)

check("no route answers an anonymous caller that is not meant to",
      unauthenticated <= EXPECTED_PUBLIC,
      f"unexpected: {sorted(unauthenticated - EXPECTED_PUBLIC)}")
check("and every route on the expected list still exists",
      not EXPECTED_PUBLIC - unauthenticated,
      f"gone: {sorted(EXPECTED_PUBLIC - unauthenticated)}")

# Not a check, because either answer is a defensible choice and a test that
# pinned one would fight whoever made the other: FastAPI's own documentation
# endpoints are enabled by default and answer anybody. They expose the schema
# of every route that is in the schema — the internal ones are not — which is
# a map of the API rather than a way into it. Printed so the next person
# reading this file knows it is a decision and not an oversight.
docs = sorted(
    p for p in (getattr(r, "path", "") for r in app.routes)
    if p in {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)
print(f"[note] FastAPI documentation endpoints served to anonymous callers: {docs}")

# The websocket routes are not APIRoute and so are not in the sweep above.
# Each authenticates inside its handler — a session cookie for /api/ws, a
# node's own credential for the two relay sockets — which is why they take no
# dependency. Named here so the omission is visible.
sockets = sorted(
    getattr(r, "path", "") for r in app.routes
    if r.__class__.__name__ == "APIWebSocketRoute"
)
check("the only websockets are the three that authenticate in their handlers",
      sockets == ["/api/relay/connect", "/api/relay/stream", "/api/ws"], str(sockets))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
