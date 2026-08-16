"""Covers the browser an agent drives: where it may go, what it may change,
and what it is never allowed to learn.

Four halves, in the order they matter.

The first is the routing decision — a pure function over the reach document a
run is given, exercised with no browser anywhere near it. The second is the
**bypass test**: the reach document is deliberately forged to claim more than
the run was granted, pushed through the real SOCKS5 proxy and the real
loopback forwarder, and refused by `RelayTokens.allows` — which is the point,
because that document lives on the agent's side of the boundary and is
therefore not permission. The third drives the tool implementations against a
stand-in page, so approval gating and credential redaction are tested against
the production code paths without needing Chromium. The fourth launches the
real browser and asserts on the actual artefacts, and is skipped where
Playwright's Chromium is not installed — it runs inside the image, which is
where it can.
"""

import asyncio
import ipaddress
import logging
import os
import sys

sys.path.insert(0, os.getcwd())

for _stale in ("./test-browser.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-browser.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_RELAY_CONNECT_TIMEOUT_SECONDS", "5")

from fastapi.testclient import TestClient  # noqa: E402

from app import browsing, relay, ssh_targets  # noqa: E402
from app.bridge import mcp_browser as mb  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.main import app  # noqa: E402
from app.models import RelayNode, Target  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def skip(label, why):
    print(f"[skip] {label} — {why}")


# The reach a run gets when a node has been opened to one office network on the
# port Sonarr actually listens on. Everything below is measured against it.
REACH = {
    "routes": [{"node": "office", "host": "backup.lan", "port": 22}],
    "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [80, 8989]}],
    "systems": [{"slug": "sonarr", "hostname": "198.51.100.20", "username": "admin",
                 "has_password": True}],
}
NOTHING = {"routes": [], "subnets": [], "systems": []}
#: The same office network on a node its owner has opened to every port, which
#: is the answer to having to name 8989 and then 7878 and then the next one.
#: Deliberately keeps a `ports` list, because the column does: the flag is what
#: decides, not the absence of anything.
EVERY_PORT = {
    "routes": [],
    "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [22],
                 "all_ports": True}],
    "systems": [],
}


def resolves_to(*addresses):
    return lambda host: list(addresses)


# =====================================================================
# 1. Where a browser request is allowed to go
# =====================================================================
print("\n--- routing ---")

r = mb.decide_route("198.51.100.42", 8989, REACH)
check("an in-range address on an allowed port is routed through the node",
      r.kind == "relay" and r.node == "office", repr(r))

r = mb.decide_route("198.51.100.42", 80, REACH)
check("and on the node's other allowed port", r.kind == "relay", repr(r))

r = mb.decide_route("198.51.100.42", 3389, REACH)
check("an in-range address on a port the node does not allow is refused",
      r.kind == "refuse", repr(r))
check("and the refusal names the port, so it can be opened deliberately",
      "3389" in r.reason, r.reason)
check("and names the node it would have to be opened on",
      "office" in r.reason and "Nodes page" in r.reason, r.reason)
check("and lists the ports that node does allow",
      "80" in r.reason and "8989" in r.reason, r.reason)

r = mb.decide_route("192.168.89.42", 8989, REACH)
check("an address outside every range is refused", r.kind == "refuse", repr(r))
check("and is not quietly dialled from this server instead",
      "refuses to dial" in r.reason or "not inside any network" in r.reason, r.reason)

r = mb.decide_route("printer.lan", 80, REACH, resolver=resolves_to("198.51.100.7"))
check("a NAME is never matched against a node's range, only an address",
      r.kind == "refuse", repr(r))
check("and says to browse it by address instead",
      "by its address" in r.reason, r.reason)

r = mb.decide_route("203.0.113.10", 8000, NOTHING)
check("a private address with no node at all is refused, not dialled",
      r.kind == "refuse", repr(r))

r = mb.decide_route("127.0.0.1", 8000, REACH)
check("loopback is refused — the app's own API lives there", r.kind == "refuse", repr(r))
r = mb.decide_route("172.18.0.2", 5432, REACH)
check("so is the container's own docker network", r.kind == "refuse", repr(r))
r = mb.decide_route("169.254.169.254", 80, REACH)
check("and so is link-local, which is where cloud metadata lives",
      r.kind == "refuse", repr(r))

r = mb.decide_route("example.com", 443, REACH, resolver=resolves_to("93.184.216.34"))
check("a public site is dialled directly, with no node involved",
      r.kind == "direct" and not r.node, repr(r))
check("and is dialled at the address that was actually resolved, not resolved again",
      r.address == "93.184.216.34", repr(r))

r = mb.decide_route("evil.example", 443, REACH, resolver=resolves_to("93.184.216.34", "203.0.113.10"))
check("a name resolving to both a public and a private address is refused outright",
      r.kind == "refuse", repr(r))

r = mb.decide_route("internal.example.com", 443, REACH, resolver=resolves_to("198.51.100.42"))
check("a public NAME pointing at an in-range address is still refused",
      r.kind == "refuse", repr(r))

r = mb.decide_route("example.com", 8080, REACH, resolver=resolves_to("93.184.216.34"))
check("a public host on a non-browsing port is refused", r.kind == "refuse", repr(r))

r = mb.decide_route("backup.lan", 22, REACH)
check("an exact triple an operator stored is reachable by name, as ssh's is",
      r.kind == "relay" and r.node == "office", repr(r))
r = mb.decide_route("backup.lan", 8989, REACH, resolver=lambda h: [])
check("but only on the port that was stored with it", r.kind == "refuse", repr(r))

r = mb.decide_route("198.51.100.42", 8989, NOTHING)
check("with no reach at all, an internal address is refused",
      r.kind == "refuse", repr(r))
check("and the refusal says access has to come from the operator",
      "relay node" in r.reason, r.reason)

# --- a node opened to every port -------------------------------------
# What the owner asked for: Sonarr on 8989 and Radarr on 7878 without naming
# either. The port comparison is the only thing that moves.
for port in (8989, 7878, 3389, 445, 1, 65535, 22):
    r = mb.decide_route("198.51.100.42", port, EVERY_PORT)
    check(f"an all-ports node routes an in-range address on {port}",
          r.kind == "relay" and r.node == "office", repr(r))

for host in ("192.168.89.42", "203.0.113.10", "172.18.0.2", "127.0.0.1", "169.254.169.254"):
    r = mb.decide_route(host, 8989, EVERY_PORT)
    check(f"and STILL refuses {host}, which is outside the network it carries",
          r.kind == "refuse", repr(r))
    check("and does not dial it from this container instead",
          "not inside any network" in r.reason or "refuses to dial" in r.reason, r.reason)
r = mb.decide_route("192.168.89.42", 3389, EVERY_PORT)
check("on an arbitrary port as much as on a listed one", r.kind == "refuse", repr(r))
r = mb.decide_route("printer.lan", 8989, EVERY_PORT, resolver=resolves_to("198.51.100.7"))
check("a NAME is still never matched against an all-ports node's range",
      r.kind == "refuse", repr(r))
r = mb.decide_route("example.com", 8989, EVERY_PORT, resolver=resolves_to("93.184.216.34"))
check("and a public site is still held to the two browsing ports",
      r.kind == "refuse", repr(r))

# The flag has to be the word true. Everything else is a document this side of
# the boundary said something the app did not.
for pretend in (None, "true", 1, "yes", [], {}, "all"):
    forged_flag = {"routes": [],
                   "subnets": [{"node": "office", "cidr": "198.51.100.0/24",
                                "ports": [22], "all_ports": pretend}],
                   "systems": []}
    r = mb.decide_route("198.51.100.42", 8989, forged_flag)
    check(f"all_ports={pretend!r} is not all-ports — only the boolean is",
          r.kind == "refuse", repr(r))
missing = {"routes": [],
           "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [22]}],
           "systems": []}
r = mb.decide_route("198.51.100.42", 8989, missing)
check("and a subnet entry with no all_ports key at all is an ordinary one",
      r.kind == "refuse", repr(r))
empty_ports = {"routes": [],
               "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [],
                            "all_ports": False}],
               "systems": []}
for port in (22, 8989):
    r = mb.decide_route("198.51.100.42", port, empty_ports)
    check(f"an empty port list is not everything either — {port} is refused",
          r.kind == "refuse", repr(r))

r = mb.decide_route("198.51.100.42", 3389, REACH)
check("and the refusal for a node that is NOT all-ports still says how to fix it",
      "3389" in r.reason and "office" in r.reason and "every port" in r.reason, r.reason)

check("every private range the container can see is non-public to the gate",
      all(not mb._is_public(ipaddress.ip_address(a))
          for a in ("203.0.113.10", "192.168.1.1", "172.16.4.4", "127.0.0.1",
                    "169.254.1.1", "0.0.0.0", "::1", "fd00::1")))
check("and a real public address is",
      mb._is_public(ipaddress.ip_address("8.8.8.8")))


# =====================================================================
# 2. How the browser is launched
# =====================================================================
print("\n--- launch options ---")

opts = mb.launch_options(41234, sandbox=True)
check("the browser is pointed at this process's own SOCKS5 proxy",
      opts["proxy"]["server"] == "socks5://127.0.0.1:41234", str(opts["proxy"]))
check("SOCKS5 rather than an HTTP proxy, so the browser never resolves a name itself",
      opts["proxy"]["server"].startswith("socks5://"))
check("BYPASS: loopback bypass is switched off, or the app's own API would be "
      "reachable without the proxy seeing it",
      opts["proxy"].get("bypass") == "<-loopback>", str(opts["proxy"]))
rule = next((a for a in opts["args"] if a.startswith("--host-resolver-rules")), "")
check("Chromium's own resolver is disabled as a second lock",
      "MAP * ~NOTFOUND" in rule, rule)
check("except for the proxy's own address, which that rule would otherwise "
      "make unreachable — every navigation then fails at the proxy, not the gate",
      "EXCLUDE 127.0.0.1" in rule, rule)
check("the sandbox setting is honoured rather than always disabled",
      opts["chromium_sandbox"] is True
      and mb.launch_options(1, sandbox=False)["chromium_sandbox"] is False)
check("it runs headless", opts["headless"] is True)


# =====================================================================
# 3. THE BYPASS TEST
# =====================================================================
# The reach document is handed to the agent's side of the boundary, so it must
# be assumed forged. Here it is: a run granted a /25 on one port, given a
# document claiming the whole /24, pushed through the real proxy and the real
# forwarder. Deliberately constructed, exactly as the ssh path's widened Host
# glob is in test_relay — and refused for exactly the same reason.
print("\n--- the bypass test ---")


async def bypass_checks():
    await relay.hub.start_forwarder()
    node = RelayNode(id=61, name="Office", slug="office", status="approved", grants=[],
                     networks=[], allowed_cidrs=["198.51.100.0/25"], allowed_ports=[8989])
    ctx = ssh_targets.prepare([], {}, [node], who="tester (run 61, session s-61)")
    check("a run with a subnet node is issued a relay token",
          ctx is not None and bool(ctx.env.get("AIOPS_RELAY_TOKEN")))

    honest = browsing.reach_document([], {}, [node])
    check("the honest reach document says exactly what the node was given",
          honest["subnets"] == [{"node": "office", "cidr": "198.51.100.0/25",
                                 "ports": [8989], "all_ports": False}],
          str(honest["subnets"]))

    forged = {
        "routes": [],
        # Wider than the grant on both axes: a /24 instead of a /25, and ssh's
        # port added to the browsing one.
        "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [22, 8989]}],
        "systems": [],
    }
    check("the forged document does route .200, so the proxy really would try it",
          mb.decide_route("198.51.100.200", 8989, forged).kind == "relay")
    check("and the honest one does not",
          mb.decide_route("198.51.100.200", 8989, honest).kind == "refuse")

    mb.RELAY_TOKEN = ctx.env["AIOPS_RELAY_TOKEN"]
    mb.RELAY_ADDR = ctx.env["AIOPS_RELAY_ADDR"]
    check("the gate itself refuses .200, which is what actually decides",
          not relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.200", 8989))
    check("while .100, genuinely inside the /25, passes it",
          relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.100", 8989))

    lines = []
    proxy = mb.Socks5Proxy(forged, lambda action, **kw: lines.append((action, kw)))
    port = await proxy.start()

    async def ask(host, dest_port):
        """One real SOCKS5 CONNECT at the real proxy."""
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        await reader.readexactly(2)
        payload = host.encode()
        writer.write(b"\x05\x01\x00\x03" + bytes([len(payload)]) + payload
                     + dest_port.to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(10)
        writer.close()
        return reply[1]

    code = await ask("198.51.100.200", 8989)
    check("BYPASS: the browser's own connection to .200 fails despite the forged document",
          code != 0, f"socks reply {code}")
    refusals = [kw for action, kw in lines if action == "failed"]
    check("BYPASS: and it fails at the relay gate, not somewhere incidental",
          any("not permitted to reach that host" in (kw.get("detail") or "")
              for kw in refusals), str(refusals))
    check("BYPASS: nothing was dialled from this container to reach it",
          not any(action == "opened" for action, _ in lines), str(lines))

    lines.clear()
    code = await ask("198.51.100.100", 8989)
    check("an address genuinely inside the grant gets past the gate",
          code != 0, f"socks reply {code}")
    passed = [kw.get("detail") or "" for action, kw in lines if action == "failed"]
    check("and fails only because no node is connected to carry it",
          any("not connected" in text for text in passed), str(passed))
    check("which is a different failure from the refusal above, so the gate is what moved",
          not any("not permitted" in text for text in passed), str(passed))

    lines.clear()
    code = await ask("203.0.113.10", 8000)
    check("BYPASS: the AIOps server's own address is refused before any socket",
          code != 0 and any(action == "refused" for action, _ in lines), str(lines))
    code = await ask("127.0.0.1", 8000)
    check("BYPASS: so is loopback, where this application's API is listening",
          code != 0 and all(action != "opened" for action, _ in lines), str(lines))

    await proxy.stop()
    ctx.cleanup()
    check("and the reach dies with the run, like every other per-run grant",
          not relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.100", 8989))
    await relay.hub.stop_forwarder()


asyncio.run(bypass_checks())


async def all_ports_bypass_checks():
    """The same test again for a node opened to every port.

    All-ports is the one setting that could have turned the gate's port check
    into the whole of the gate. It did not, and this is where that is measured:
    a node given a /25 and every port on it, a document claiming the /24, and a
    real CONNECT for an address outside the grant on a port nothing lists.
    """
    await relay.hub.start_forwarder()
    node = RelayNode(id=62, name="Office", slug="office", status="approved", grants=[],
                     networks=[], allowed_cidrs=["198.51.100.0/25"], allowed_ports=[22],
                     allow_all_ports=True)
    ctx = ssh_targets.prepare([], {}, [node], who="tester (run 62, session s-62)")

    honest = browsing.reach_document([], {}, [node])
    check("the honest document says every port, as its own key beside the list",
          honest["subnets"] == [{"node": "office", "cidr": "198.51.100.0/25",
                                 "ports": [22], "all_ports": True}], str(honest["subnets"]))
    check("and the browser reads it as routing an unlisted port",
          mb.decide_route("198.51.100.100", 8989, honest).kind == "relay")
    check("while the address half of it is unchanged — .200 is outside the /25",
          mb.decide_route("198.51.100.200", 8989, honest).kind == "refuse")

    forged = {
        "routes": [],
        # Wider on the axis all-ports does not touch: the /24 instead of the /25.
        "subnets": [{"node": "office", "cidr": "198.51.100.0/24", "ports": [22],
                     "all_ports": True}],
        "systems": [],
    }
    check("the forged document does route .200, so the proxy really would try it",
          mb.decide_route("198.51.100.200", 8989, forged).kind == "relay")

    mb.RELAY_TOKEN = ctx.env["AIOPS_RELAY_TOKEN"]
    mb.RELAY_ADDR = ctx.env["AIOPS_RELAY_ADDR"]
    check("the gate refuses .200 on an unlisted port, which is what actually decides",
          not relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.200", 8989))
    check("and on the listed one too — being outside the CIDR is the whole answer",
          not relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.200", 22))
    check("while .100, genuinely inside the /25, passes it on a port nobody listed",
          relay.tokens.allows(mb.RELAY_TOKEN, "office", "198.51.100.100", 8989))

    lines = []
    proxy = mb.Socks5Proxy(forged, lambda action, **kw: lines.append((action, kw)))
    port = await proxy.start()

    async def ask(host, dest_port):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        await reader.readexactly(2)
        payload = host.encode()
        writer.write(b"\x05\x01\x00\x03" + bytes([len(payload)]) + payload
                     + dest_port.to_bytes(2, "big"))
        await writer.drain()
        reply = await reader.readexactly(10)
        writer.close()
        return reply[1]

    code = await ask("198.51.100.200", 8989)
    check("BYPASS: an all-ports node still refuses an out-of-range host at the gate",
          code != 0, f"socks reply {code}")
    refusals = [kw for action, kw in lines if action == "failed"]
    check("BYPASS: and it is the gate that refused it, not something incidental",
          any("not permitted to reach that host" in (kw.get("detail") or "")
              for kw in refusals), str(refusals))
    check("BYPASS: nothing was dialled from this container to reach it",
          not any(action == "opened" for action, _ in lines), str(lines))

    lines.clear()
    code = await ask("198.51.100.100", 8989)
    passed = [kw.get("detail") or "" for action, kw in lines if action == "failed"]
    check("an in-range address on an unlisted port gets past the gate",
          any("not connected" in text for text in passed), str(passed))
    check("which is a different failure from the refusal above, so all-ports did move",
          not any("not permitted" in text for text in passed), str(passed))

    lines.clear()
    code = await ask("203.0.113.10", 8000)
    check("BYPASS: the AIOps server's own address is refused for an all-ports node too",
          code != 0 and any(action == "refused" for action, _ in lines), str(lines))
    code = await ask("127.0.0.1", 8000)
    check("BYPASS: and so is loopback, where this application's API is listening",
          code != 0 and all(action != "opened" for action, _ in lines), str(lines))

    await proxy.stop()
    ctx.cleanup()
    await relay.hub.stop_forwarder()


asyncio.run(all_ports_bypass_checks())


# =====================================================================
# 4. What the tools do, against a stand-in page
# =====================================================================
print("\n--- tools, approvals and redaction ---")

SECRET = "hunter2-correct-horse-battery"


class FakeResponse:
    status = 200


class FakePage:
    """Enough of a Playwright page for the tool bodies to run unchanged."""

    def __init__(self, text=""):
        self.url = "http://198.51.100.20:8989/login"
        self.text = text
        self.filled = {}
        self.clicked = []
        self.shots = []

    async def title(self):
        return "Sonarr"

    async def goto(self, url, wait_until=None):
        self.url = url
        return FakeResponse()

    async def evaluate(self, script):
        if "document.body" in script:
            return self.text
        return [["input[password]", "#password", "(value hidden)"]]

    async def fill(self, selector, value):
        self.filled[selector] = value

    async def click(self, selector):
        self.clicked.append(selector)

    async def wait_for_load_state(self, *a, **kw):
        pass

    def locator(self, selector):
        return f"locator:{selector}"

    async def screenshot(self, path=None, full_page=False, mask=None):
        self.shots.append({"path": path, "full_page": full_page, "mask": mask})
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG-fake")


class Calls:
    """Stands in for the loopback API, recording everything asked of it."""

    def __init__(self, allow=True):
        self.allow = allow
        self.posts = []

    def __call__(self, path, payload, timeout=30):
        self.posts.append((path, dict(payload)))
        if path.endswith("/approvals"):
            return {"allowed": self.allow, "note": None if self.allow else "Not this one."}
        if path.endswith("/credential"):
            return {"username": "admin", "secret": SECRET}
        if path.endswith("/reach"):
            return REACH
        return {"ok": True}


def fresh(text="", allow=True, mode="ask"):
    mb.APPROVAL_MODE = mode
    mb.TOKEN = "run-token"
    calls = Calls(allow=allow)
    mb._post = calls
    browser = mb.Browser(mb.Aiops())
    browser._page = FakePage(text)
    return browser, calls


# -- redaction, as a pure function ------------------------------------
check("a secret is replaced wherever it appears in a string",
      mb.redact(f"login failed for {SECRET} at /login", [SECRET])
      == f"login failed for {mb.REDACTED} at /login")
check("every occurrence, not only the first",
      mb.redact(f"{SECRET} {SECRET}", [SECRET]).count(SECRET) == 0)
check("a very short value is left alone rather than turning a page into markers",
      mb.redact("a and b", ["a"]) == "a and b")
check("and nothing at all is a no-op", mb.redact("plain", []) == "plain")


# -- reads happen silently --------------------------------------------
async def read_checks():
    browser, calls = fresh(text="Series: 42 monitored")
    out = await browser.navigate("http://198.51.100.20:8989/")
    check("navigating returns the status, the landing URL and the title",
          "200" in out and "Sonarr" in out, out)
    check("and asks nobody for permission, because reading a page is a read",
          not any(p.endswith("/approvals") for p, _ in calls.posts), str(calls.posts))
    check("but it is written down, with the URL",
          any(p.endswith("/log") and body.get("url", "").endswith("8989/")
              for p, body in calls.posts), str(calls.posts))

    page = await browser.read()
    check("read_page returns the rendered text", "42 monitored" in page, page[:200])
    check("and lists what can be interacted with", "#password" in page, page[:200])
    check("with a password field marked as hidden rather than read",
          "(value hidden)" in page, page[:200])

    with_secret, _ = fresh(text=f"your token is {SECRET}")
    with_secret.secrets.add(SECRET)
    leaked = await with_secret.read()
    check("a secret echoed back onto the page is redacted before the agent sees it",
          SECRET not in leaked and mb.REDACTED in leaked, leaked[:200])

    browser.aiops.reach = REACH
    try:
        await browser.navigate("file:///etc/passwd")
        ok = False
    except ValueError as exc:
        ok = "http" in str(exc)
    check("a file:// URL is refused — it is not a page on a network", ok)


asyncio.run(read_checks())


# -- writes are approved ----------------------------------------------
async def approval_checks():
    browser, calls = fresh(allow=True)
    out = await browser.click("#refresh")
    asked = [body for p, body in calls.posts if p.endswith("/approvals")]
    check("a click raises an approval", len(asked) == 1, str(asked))
    check("and the summary says what would be clicked and where",
          "#refresh" in asked[0]["summary"] and "8989" in asked[0]["summary"],
          str(asked[0]))
    check("and the click then happens", browser._page.clicked == ["#refresh"], out)

    denied, calls = fresh(allow=False)
    try:
        await denied.click("#delete-everything")
        blocked = False
    except PermissionError:
        blocked = True
    check("denying it raises rather than returning quietly", blocked)
    check("and the click really does not happen", denied._page.clicked == [],
          str(denied._page.clicked))

    filled, calls = fresh(allow=False)
    try:
        await filled.fill("#name", "x")
    except PermissionError:
        pass
    check("a denied fill types nothing either", filled._page.filled == {},
          str(filled._page.filled))

    auto, calls = fresh(allow=True, mode="auto")
    await auto.click("#refresh")
    check("in auto mode a click asks nobody, exactly as a Bash call does not",
          not any(p.endswith("/approvals") for p, _ in calls.posts), str(calls.posts))
    check("and still happens", auto._page.clicked == ["#refresh"])
    check("and is still written down",
          any(p.endswith("/log") for p, _ in calls.posts))

    bypass, calls = fresh(allow=True, mode="bypass")
    await bypass.fill("#q", "sonarr")
    check("and in bypass mode too",
          not any(p.endswith("/approvals") for p, _ in calls.posts))


asyncio.run(approval_checks())


# -- the credential never reaches the agent ---------------------------
async def credential_checks():
    logs = []

    class Capture(logging.Handler):
        def emit(self, record):
            logs.append(record.getMessage())

    handler = Capture()
    logging.getLogger("aiops.browser").addHandler(handler)

    browser, calls = fresh(allow=True)
    out = await browser.login("sonarr", "#user", "#password", "#submit")

    check("the password AIOps holds is typed into the page",
          browser._page.filled.get("#password") == SECRET,
          str(list(browser._page.filled)))
    check("and the username with it", browser._page.filled.get("#user") == "admin")
    check("and the form is submitted", browser._page.clicked == ["#submit"])
    check("but the secret is not in what the agent is told back",
          SECRET not in out, out)
    check("and the agent is told it cannot have it", "not available to you" in out, out)
    check("signing in raises an approval, because it changes something",
          any(p.endswith("/approvals") for p, _ in calls.posts))

    sent = "\n".join(str(body) for p, body in calls.posts if not p.endswith("/credential"))
    check("the secret is in nothing this process sends back to AIOps",
          SECRET not in sent, sent[:300])
    check("including the log lines it asks to have written",
          all(SECRET not in message for message in logs), str(logs))
    check("what IS written down is the system and the person",
          any("sonarr" in str(body) for p, body in calls.posts if p.endswith("/log")),
          str(calls.posts))

    leaked = await browser.read()
    check("and every later page read is filtered", SECRET not in leaked)

    # An application echoing a password into an error message is not a
    # hypothetical, so the MCP layer filters what it returns as well.
    server = mb.Server()
    server.browser = browser

    replies = []

    async def capture(message):
        replies.append(message)

    server.send = capture

    async def boom(name, args):
        raise ValueError(f"rejected: {SECRET}")

    server.dispatch = boom
    await server.call(7, {"name": "click", "arguments": {}})
    text = replies[0]["result"]["content"][0]["text"]
    check("a secret escaping through an exception message is redacted at the MCP boundary",
          SECRET not in text and mb.REDACTED in text, text)
    check("and the tool call is reported as an error rather than as a result",
          replies[0]["result"]["isError"] is True)

    logging.getLogger("aiops.browser").removeHandler(handler)


asyncio.run(credential_checks())


# -- screenshots ------------------------------------------------------
async def screenshot_checks():
    import tempfile

    mb.SHOT_DIR = tempfile.mkdtemp(prefix="aiops-browser-test-")
    browser, calls = fresh()
    browser.secrets.add(SECRET)
    out = await browser.screenshot()
    shot = browser._page.shots[0]
    check("a screenshot masks every password field",
          shot["mask"] == [f"locator:{mb.PASSWORD_SELECTOR}"], str(shot))
    check("which is what stops a filled login form being a credential in the transcript",
          mb.PASSWORD_SELECTOR == "input[type=password]")
    check("it lands in this run's own directory",
          shot["path"].startswith(mb.SHOT_DIR), shot["path"])
    check("and the agent is told where to read it", mb.SHOT_DIR in out, out)
    check("and that the masking happened", "masked" in out, out)

    mb.MAX_SHOTS = 2
    await browser.screenshot()
    try:
        await browser.screenshot()
        bounded = False
    except ValueError:
        bounded = True
    check("screenshots are bounded, so a loop cannot fill the disk", bounded)
    mb.MAX_SHOTS = 40


asyncio.run(screenshot_checks())


# =====================================================================
# 5. The application's half
# =====================================================================
print("\n--- the application side ---")

check("a run with no relay reach gets a reach document with no subnets in it",
      browsing.reach_document([], {}, []) == {"routes": [], "subnets": [], "systems": []})

pw_target = Target(id=1, name="Sonarr", slug="sonarr", hostname="198.51.100.20", port=22,
                   username="admin", auth_type="password", password_enc=encrypt(SECRET),
                   grants=[], relay_node_id=None)
key_target = Target(id=2, name="Backup", slug="backup", hostname="backup.lan", port=22,
                    username="root", auth_type="key", private_key_enc=encrypt("KEY"),
                    grants=[], relay_node_id=61)
node = RelayNode(id=61, name="Office", slug="office", status="approved", grants=[],
                 networks=[], allowed_cidrs=["198.51.100.0/24"], allowed_ports=[80, 8989])
doc = browsing.reach_document([pw_target, key_target], {61: node}, [node])
check("a stored system bound to a node becomes an exact triple, as it does for ssh",
      {"node": "office", "host": "backup.lan", "port": 22} in doc["routes"], str(doc["routes"]))
check("a node's subnet becomes a rule with its ports",
      doc["subnets"] == [{"node": "office", "cidr": "198.51.100.0/24", "ports": [80, 8989],
                          "all_ports": False}],
      str(doc["subnets"]))
check("and every entry says so either way, so the browser never has to guess",
      all("all_ports" in entry for entry in doc["subnets"]), str(doc["subnets"]))
check("only systems with a stored password are offered for sign-in",
      [s["slug"] for s in doc["systems"]] == ["sonarr"], str(doc["systems"]))
check("and the document carries no secret of any kind",
      SECRET not in str(doc) and "KEY" not in str(doc), str(doc))

briefing = browsing.describe(doc)
check("the agent is told the browser exists and what to call it",
      "mcp__aiops_browser__" in briefing, briefing[:200])
check("and which networks it can browse", "198.51.100.0/24" in briefing, briefing[:400])
check("and on which ports", "80, 8989" in briefing, briefing[:400])
check("and that a refusal is a policy limit rather than a broken network",
      "policy limit" in briefing, briefing[:200])
check("and that reads are silent while clicks are approved",
      "approval" in briefing and "silently" in briefing, briefing[:200])
check("and which systems it may ask AIOps to sign in as",
      "`sonarr`" in briefing, briefing[:200])
check("and that it will never be given the password",
      "never given to you" in briefing, briefing[:200])
check("a run with no node is told the browser reaches public sites only",
      "public sites only" in browsing.describe(browsing.reach_document()),
      browsing.describe(browsing.reach_document())[:200])

open_node = RelayNode(id=62, name="Office", slug="office", status="approved", grants=[],
                      networks=[], allowed_cidrs=["198.51.100.0/24"], allowed_ports=[22],
                      allow_all_ports=True)
open_doc = browsing.reach_document([], {}, [open_node])
check("an all-ports node's rule carries the flag rather than a made-up port list",
      open_doc["subnets"] == [{"node": "office", "cidr": "198.51.100.0/24", "ports": [22],
                               "all_ports": True}], str(open_doc["subnets"]))
open_briefing = browsing.describe(open_doc)
check("and the agent is told the network is browsable on every port",
      "198.51.100.0/24 on every port" in open_briefing, open_briefing[:500])
check("without a port list beside it that would read as the limit",
      "on port 22" not in open_briefing, open_briefing[:500])
check("while it is still told an address outside the range is a policy refusal",
      "policy limit" in open_briefing, open_briefing[:600])

spec = ClaudeProvider().build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="ask", approval_token="tok", browser=True,
)
check("a browsing turn registers the browser MCP server",
      "mcp_browser.py" in spec.argv[spec.argv.index("--mcp-config") + 1], str(spec.argv))
check("alongside the approval bridge, in one --mcp-config",
      spec.argv.count("--mcp-config") == 1
      and "mcp_approver.py" in spec.argv[spec.argv.index("--mcp-config") + 1])
allowed = spec.argv[spec.argv.index("--allowedTools") + 1]
check("the browser's tools are allowed at the CLI, or two of three modes deny them outright",
      "mcp__aiops_browser__navigate" in allowed and "mcp__aiops_browser__click" in allowed,
      allowed)
check("the bridge is told which approval mode it is enforcing",
      spec.env.get("AIOPS_BROWSER_APPROVALS") == "ask", str(spec.env))

auto_spec = ClaudeProvider().build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools="Bash,Read", extra_args=[], stream_partials=False,
    approval_mode="auto", approval_token="tok", browser=True,
)
auto_allowed = auto_spec.argv[auto_spec.argv.index("--allowedTools") + 1]
check("a preset's own tool list is kept and the browser's added to it",
      auto_allowed.startswith("Bash,Read,mcp__aiops_browser__"), auto_allowed)
check("in auto mode no permission-prompt tool is wired up",
      "--permission-prompt-tool" not in auto_spec.argv)
check("but the browser still is, with the mode it must enforce for itself",
      "mcp_browser.py" in auto_spec.argv[auto_spec.argv.index("--mcp-config") + 1]
      and auto_spec.env.get("AIOPS_BROWSER_APPROVALS") == "auto")

plain = ClaudeProvider().build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="auto", approval_token=None,
)
check("a turn with no browser is exactly the command it always was",
      "--mcp-config" not in plain.argv and "--allowedTools" not in plain.argv, str(plain.argv))


# --- the loopback endpoints ------------------------------------------
def endpoint_checks():
    from app.approvals import run_tokens
    from app.db import SessionLocal, engine, init_db
    from app.models import Run, Session as SessionRow, User as UserRow
    from app.security import hash_password

    async def seed():
        """Rows first, on their own loop, then HTTP — the order test_relay uses.

        The engine is disposed afterwards so no pooled connection survives into
        the client's loop: two event loops sharing one aiosqlite connection is
        the kind of failure that only appears once something is slow.
        """
        await init_db()
        async with SessionLocal() as db:
            owner = UserRow(username="owner", password_hash=hash_password("x" * 12))
            other = UserRow(username="stranger", password_hash=hash_password("x" * 12),
                            is_admin=True)
            db.add_all([owner, other])
            await db.commit()
            target = Target(name="Sonarr", slug="sonarr", hostname="198.51.100.20",
                            port=22, username="admin", auth_type="password",
                            password_enc=encrypt(SECRET), owner_id=owner.id)
            sess = SessionRow(id="s-browser", title="t", provider="claude",
                              owner_id=owner.id)
            db.add_all([target, sess])
            await db.commit()
            mine = Run(session_id=sess.id, prompt="p", status="running",
                       requested_by_id=owner.id)
            theirs = Run(session_id=sess.id, prompt="p", status="running",
                         requested_by_id=other.id)
            db.add_all([mine, theirs])
            await db.commit()
            ids = (mine.id, theirs.id, sess.id, other.is_admin)
        await engine.dispose()
        return ids

    mine, theirs, session_id, stranger_is_admin = asyncio.run(seed())

    with TestClient(app) as client:

        r = client.post("/api/internal/browser/reach", json={"token": "nonsense"})
        check("an unknown run token gets nothing from the browser API",
              r.status_code == 401, r.text[:120])

        token = run_tokens.issue(mine, session_id)
        r = client.post("/api/internal/browser/reach", json={"token": token})
        check("nor does a real token for a run that was never given a browser",
              r.status_code == 403, r.text[:120])

        browsing.grants.issue(mine, doc, None, "owner (run 1)", "")
        r = client.post("/api/internal/browser/reach", json={"token": token})
        check("a run with a browser is told its reach",
              r.status_code == 200 and r.json()["subnets"] == doc["subnets"], r.text[:200])
        check("and the reach it is told carries no credential",
              SECRET not in r.text, r.text[:200])

        logs = []

        class Capture(logging.Handler):
            def emit(self, record):
                logs.append(record.getMessage())

        handler = Capture()
        logging.getLogger("aiops.browser").addHandler(handler)
        client.post("/api/internal/browser/log", json={
            "token": token, "action": "opened", "host": "198.51.100.42",
            "port": 8989, "node": "office"})
        check("every connection is written down on the side the agent cannot edit",
              any("198.51.100.42:8989" in line and "via node office" in line
                  for line in logs), str(logs))
        check("with the person and the run it belongs to",
              any("owner (run 1)" in line for line in logs), str(logs))

        # Credentials follow the requester, not the session's owner.
        r = client.post("/api/internal/browser/credential",
                        json={"token": token, "system": "sonarr"})
        check("the run's requester gets the password for their own stored system",
              r.status_code == 200 and r.json()["secret"] == SECRET, r.text[:120])
        check("and the username to go with it", r.json()["username"] == "admin")

        other_token = run_tokens.issue(theirs, session_id)
        browsing.grants.issue(theirs, doc, None, "stranger (run 2)", "")
        r = client.post("/api/internal/browser/credential",
                        json={"token": other_token, "system": "sonarr"})
        check("somebody else's turn in the same session gets nothing",
              r.status_code == 404, r.text[:160])
        check("and that somebody is an administrator, which buys them nothing here",
              stranger_is_admin)
        check("the refusal does not say whether the system exists",
              "available to whoever asked" in r.text, r.text[:200])

        r = client.post("/api/internal/browser/credential",
                        json={"token": token, "system": "no-such-system"})
        check("an unknown system is refused in the same words",
              r.status_code == 404 and "available to whoever asked" in r.text, r.text[:200])

        check("nothing in the log lines ever contained the secret",
              all(SECRET not in line for line in logs), str(logs))
        logging.getLogger("aiops.browser").removeHandler(handler)

        browsing.grants.revoke(mine)
        r = client.post("/api/internal/browser/credential",
                        json={"token": token, "system": "sonarr"})
        check("and once the run ends its browser can fetch nothing at all",
              r.status_code == 403, r.text[:120])


endpoint_checks()


# =====================================================================
# 6. The real browser, where there is one
# =====================================================================
print("\n--- the real browser ---")


async def live_checks():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        skip("the real browser", "Playwright is not installed in this environment")
        return

    # A reach naming one host nothing can resolve, so what is asserted is the
    # decision and not somebody's DNS. Chromium hands a SOCKS5 proxy the name
    # unresolved, which is the property being relied on.
    seen = []
    proxy = mb.Socks5Proxy(
        {"routes": [{"node": "test-node", "host": "probe.invalid", "port": 8989}],
         "subnets": [], "systems": []},
        lambda action, **kw: seen.append((action, kw)),
    )
    port = await proxy.start()
    options = mb.launch_options(port)
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(**options)
    except Exception as exc:  # noqa: BLE001
        await proxy.stop()
        await pw.stop()
        skip("the real browser", f"Chromium would not start: {str(exc)[:200]}")
        return

    context = await browser.new_context()

    # Every byte the browser sends has to arrive at the routing decision. These
    # two navigations fail on purpose; what is being measured is *where*.
    # 8000 rather than something obviously dead: it is the port this
    # application's own API listens on, which is the thing a browser on the
    # loopback must not be able to reach. (Chromium refuses a handful of ports
    # outright — 9 among them — and a navigation it never makes proves nothing.)
    #
    # On a page of its own, closed afterwards: a navigation that failed is
    # still unwinding when goto() returns, and it destroys the execution
    # context of whatever is put on that page next.
    probe = await context.new_page()
    for url in ("http://probe.invalid:8989/", "http://127.0.0.1:8000/api/health"):
        try:
            await probe.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception:  # noqa: BLE001 - the failure is the point
            pass
    check("a real Chromium reaches the proxy, so the resolver lock has not shut it out",
          bool(seen), str(seen))
    check("and hands it the hostname unresolved, which is why a name can be judged at all",
          any(kw.get("host") == "probe.invalid" for _a, kw in seen), str(seen))
    check("a routed host is taken to the node it was matched against",
          any(kw.get("node") == "test-node" for _a, kw in seen), str(seen))
    check("BYPASS: and a real browser told to open the loopback is refused there",
          any(action == "refused" and kw.get("host") == "127.0.0.1"
              for action, kw in seen), str(seen))
    check("BYPASS: nothing was opened to either of them",
          not any(action == "opened" for action, _kw in seen), str(seen))
    await probe.close()

    page = await context.new_page()
    await page.set_content(
        "<h1>Sign in</h1><form>"
        "<input id=user type=text><input id=password type=password>"
        "<button id=go>Go</button></form>"
    )

    driver = mb.Browser(mb.Aiops())
    driver._page = page
    driver.secrets.add(SECRET)

    text = await driver.read()
    check("a real page reads back as text", "Sign in" in text, text[:200])
    check("with the password field listed but not read",
          "(value hidden)" in text, text[:300])

    # The claim being tested is about pixels, so it is measured on pixels: the
    # password field's own rectangle, photographed with the production mask and
    # then without it, for two passwords of very different lengths.
    field = page.locator(mb.PASSWORD_SELECTOR)
    # The field's own box, exactly: that is the rectangle Playwright paints
    # over, and the rectangle the typed value is rendered into. A pixel outside
    # it belongs to the focus ring, which moves for reasons that have nothing to
    # do with the password.
    clip = await field.bounding_box()
    shots = {}
    for label, value in (("short", "a" * 8), ("long", "z" * 40)):
        await page.fill(mb.PASSWORD_SELECTOR, value)
        shots[(label, "masked")] = await page.screenshot(mask=[field], clip=clip)
        shots[(label, "plain")] = await page.screenshot(clip=clip)

    check("with the mask the password field's own pixels do not depend on what was typed",
          shots[("short", "masked")] == shots[("long", "masked")],
          f"{len(shots[('short', 'masked')])} vs {len(shots[('long', 'masked')])}")
    check("not even on its length — which is what masking buys over the dots",
          shots[("short", "plain")] != shots[("long", "plain")],
          "without the mask the same two values photograph differently, so the "
          "comparison above is measuring something")
    check("and the secret's bytes are nowhere in the image",
          SECRET.encode() not in shots[("short", "masked")])

    import tempfile
    mb.SHOT_DIR = tempfile.mkdtemp(prefix="aiops-browser-live-")
    await page.fill(mb.PASSWORD_SELECTOR, SECRET)
    saved = await driver.screenshot()
    written = open(os.path.join(mb.SHOT_DIR, "screenshot-001.png"), "rb").read()
    check("the tool itself writes a real PNG into this run's directory",
          written[:8] == b"\x89PNG\r\n\x1a\n" and mb.SHOT_DIR in saved, saved)
    check("and a screenshot taken with the secret in the field does not contain it",
          SECRET.encode() not in written)

    await page.fill("#password", SECRET)
    after = await driver.read()
    check("and a page holding the secret still reads back without it", SECRET not in after)

    await browser.close()
    await pw.stop()
    await proxy.stop()


asyncio.run(live_checks())


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
