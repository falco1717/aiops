"""Covers relay nodes: enrolment, approval, access, routing, and the data path.

Two halves. The first drives the API through Starlette's TestClient and asserts
the rules — a token is spent when it is used, an unapproved node carries
nothing, a revoked one is refused at the socket, and an administrator gets no
more implicit access to a route into somebody's network than to a stored
credential.

The second runs the app under a real uvicorn on a real port and starts the
actual node agent from deploy/relay as a subprocess, then pushes bytes through
the whole chain: the ProxyCommand helper, the loopback forwarder, the node's
websocket, and a TCP listener the agent has to open on its side. That half
exists because everything in the first half would still pass against a relay
that generated correct-looking config and quietly moved no bytes at all.
"""
import asyncio
import fnmatch
import hashlib
import io
import os
import socket
import subprocess
import sys
import threading
import time
import zipfile

sys.path.insert(0, os.getcwd())

REPO = os.path.dirname(os.getcwd())
RELAY_SRC = os.path.join(REPO, "deploy", "relay")
AGENT = os.path.join(RELAY_SRC, "aiops_relay_node.py")

for _stale in ("./test-relay.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-relay.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
# Short enough that the "node is not connected" path does not stall the suite.
os.environ.setdefault("AIOPS_RELAY_CONNECT_TIMEOUT_SECONDS", "8")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# =====================================================================
# Part one: the rules
# =====================================================================
with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    r = c.post("/api/nodes", json={"name": "Salt Net", "description": "created by test_relay.py"})
    check("registering a node succeeds", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    created = r.json() if r.status_code == 201 else {}
    node = created.get("node", {})
    node_id = node.get("id")
    token = created.get("enrolment_token", "")
    check("the short name a system points at is derived from the name",
          node.get("slug") == "salt-net", str(node.get("slug")))
    check("a new node starts pending, not usable",
          node.get("status") == "pending", str(node.get("status")))
    check("an enrolment token is issued", len(token) > 20)
    check("the installer line carries the token so it is never retyped",
          token in created.get("install_hint", ""), created.get("install_hint", "")[:120])

    r = c.get("/api/nodes")
    body = r.text
    check("nodes are listed", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("but the token is not readable afterwards", token not in body, "TOKEN LEAKED")
    check("only that a token is outstanding", r.json()[0]["enrolment_pending"] is True)

    # --- the downloadable installer --------------------------------------
    # The command above says "run this from inside that folder". Until there
    # was a download, there was no folder: deploy/relay was not in the runtime
    # image at all, so on a fresh Windows box — the only place the instruction
    # mattered — it could not be followed.
    expected_members = {
        "linux": ["aiops_relay_node.py", "install.sh", "aiops-relay-node.service", "README.md"],
        "windows": ["aiops_relay_node.py", "install.ps1", "uninstall.ps1", "README.md"],
        "docker": ["aiops_relay_node.py", "Dockerfile", "docker-compose.yml", "README.md"],
    }
    bundles = {}
    for platform, members in expected_members.items():
        r = c.get(f"/api/nodes/installer/{platform}")
        check(f"the {platform} installer downloads", r.status_code == 200,
              f"{r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            continue
        check(f"the {platform} download is served as a zip",
              r.headers.get("content-type") == "application/zip",
              str(r.headers.get("content-type")))
        check(f"and named so it is obvious what it is on disk ({platform})",
              f'filename="aiops-relay-node-{platform}.zip"'
              in r.headers.get("content-disposition", ""),
              r.headers.get("content-disposition", ""))
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        bundles[platform] = archive
        check(f"the {platform} bundle holds exactly what that platform needs",
              archive.namelist() == members, str(archive.namelist()))
        # The whole point of not templating the token into a file: a zip in a
        # Downloads folder outlives the token's own lifetime and is copied
        # wherever the folder is copied.
        leaked = [n for n in archive.namelist() if token.encode() in archive.read(n)]
        check(f"and the enrolment token is nowhere inside it ({platform})",
              not leaked, f"TOKEN LEAKED in {leaked}")

    check("nothing anywhere in the three bundles carries the token",
          all(token.encode() not in a.read(n) for a in bundles.values() for n in a.namelist()))

    # Byte-identical, member by member, against the source the tests already
    # run the agent from. A zip built through text mode would round-trip most
    # of this fine and quietly rewrite the two files that cannot survive it.
    mismatched = []
    for platform, archive in bundles.items():
        for name in archive.namelist():
            with open(os.path.join(RELAY_SRC, name), "rb") as fh:
                if hashlib.sha256(fh.read()).digest() != hashlib.sha256(archive.read(name)).digest():
                    mismatched.append(f"{platform}/{name}")
    check("every member is byte-identical to the file in deploy/relay",
          not mismatched, f"altered in transit: {mismatched}")

    # The property the Windows installer actually depends on. PowerShell 5.1
    # reads a BOM-less .ps1 as ANSI, and uninstall.ps1 then fails to parse at
    # all — a silent way to ship a node that cannot be removed.
    if "windows" in bundles:
        win = bundles["windows"]
        for name in ("install.ps1", "uninstall.ps1"):
            blob = win.read(name)
            check(f"{name} still starts with the UTF-8 BOM after zipping",
                  blob[:3] == b"\xef\xbb\xbf", blob[:8].hex())
            check(f"{name} is still ASCII-only after the BOM",
                  all(b < 128 for b in blob[3:]),
                  next((f"byte {i} = {b:#x}" for i, b in enumerate(blob[3:]) if b >= 128), ""))
        check("install.ps1 survives the round trip byte for byte",
              hashlib.sha256(win.read("install.ps1")).hexdigest()
              == hashlib.sha256(open(os.path.join(RELAY_SRC, "install.ps1"), "rb").read()).hexdigest())
        check("uninstall.ps1 survives the round trip byte for byte",
              hashlib.sha256(win.read("uninstall.ps1")).hexdigest()
              == hashlib.sha256(open(os.path.join(RELAY_SRC, "uninstall.ps1"), "rb").read()).hexdigest())

    # `sudo ./install.sh` is only the documented command if unzip yields
    # something executable.
    if "linux" in bundles:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in bundles["linux"].infolist()}
        check("install.sh unzips executable, or the documented command fails",
              modes.get("install.sh", 0) & 0o111, oct(modes.get("install.sh", 0)))
        check("and the README does not", not modes.get("README.md", 0) & 0o111,
              oct(modes.get("README.md", 0)))

    r = c.get("/api/nodes/installer/solaris")
    check("an installer for a platform there is no installer for is a 404",
          r.status_code == 404, str(r.status_code))
    check("and the refusal says which platforms there are",
          "windows" in r.text and "linux" in r.text, r.text[:200])

    c.post("/api/auth/logout")
    r = c.get("/api/nodes/installer/linux")
    check("the installer is not anonymous — it is not secret, but it is not open",
          r.status_code == 401, str(r.status_code))
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    # --- enrolment ------------------------------------------------------
    r = c.post("/api/relay/enroll", json={"token": f"{node_id}.wrong-secret-entirely"})
    check("a wrong token is refused", r.status_code == 401, str(r.status_code))
    r = c.post("/api/relay/enroll", json={"token": "not-even-shaped-right"})
    check("a malformed token is refused the same way", r.status_code == 401, str(r.status_code))

    r = c.post("/api/relay/enroll", json={
        "token": token, "version": "1.0.0", "hostname": "probe-node",
        "networks": ["10.10.20.0/28"],
    })
    check("enrolling with a valid token succeeds", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    enrolled = r.json() if r.status_code == 200 else {}
    credential = enrolled.get("credential", "")
    check("and returns a credential for later reconnects", len(credential) > 20)
    check("which is not the enrolment token", credential != token)
    check("enrolling does not make the node usable",
          enrolled.get("status") == "pending", str(enrolled.get("status")))

    # The property the whole enrolment story rests on.
    r = c.post("/api/relay/enroll", json={"token": token})
    check("the same token cannot be used twice", r.status_code == 401, str(r.status_code))

    r = c.get("/api/nodes")
    listed = r.json()[0]
    check("the node reports what it says it is", listed["reported_hostname"] == "probe-node")
    check("and which networks it claims to reach", listed["networks"] == ["10.10.20.0/28"])
    check("no credential comes back from the API",
          credential not in r.text and "credential" not in r.text, "CREDENTIAL LEAKED")

    # --- an unapproved node carries nothing -----------------------------
    auth = {"Authorization": f"Bearer {credential}"}
    with c.websocket_connect("/api/relay/connect", headers=auth) as ws:
        first = ws.receive_json()
    check("an unapproved node is refused its control channel",
          first.get("type") == "denied" and first.get("code") == 4403,
          str(first)[:160])

    r = c.post(f"/api/nodes/{node_id}/approve")
    check("an administrator can approve it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("and it becomes approved", r.status_code == 200 and r.json()["status"] == "approved")

    with c.websocket_connect("/api/relay/connect", headers=auth) as ws:
        hello = ws.receive_json()
    check("an approved node is let in", hello.get("type") == "hello", str(hello)[:160])

    with c.websocket_connect("/api/relay/connect", headers={"Authorization": "Bearer 1.nonsense"}) as ws:
        denied = ws.receive_json()
    check("a wrong credential is refused on reconnect, not just at enrolment",
          denied.get("type") == "denied" and denied.get("code") == 4401, str(denied)[:160])

    # --- binding a stored system to a node ------------------------------
    r = c.post("/api/targets", json={
        "name": "Behind Relay", "hostname": "10.10.20.9", "username": "alice",
        "auth_type": "password", "password": "hunter2", "relay_node_id": node_id,
    })
    check("a system can be bound to a node", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    relayed_id = r.json()["id"] if r.status_code == 201 else None
    check("and says so", r.status_code == 201 and r.json()["relay_node_id"] == node_id)

    r = c.post("/api/targets", json={
        "name": "Nowhere", "hostname": "10.0.0.1", "username": "u",
        "auth_type": "password", "password": "x", "relay_node_id": 9999,
    })
    check("a system cannot be bound to a node that does not exist",
          r.status_code == 400, str(r.status_code))

    r = c.delete(f"/api/nodes/{node_id}")
    check("a node with systems behind it cannot be deleted out from under them",
          r.status_code == 409, str(r.status_code))
    check("and the refusal names what is in the way", "Behind Relay" in r.text, r.text[:200])

    # --- access control, matching the stored-systems model ---------------
    c.post("/api/users", json={"username": "walt", "password": "waltpassword1",
                               "is_admin": False, "must_change_password": False})
    c.post("/api/users", json={"username": "otheradmin", "password": "adminpassword1",
                               "is_admin": True, "must_change_password": False})
    users = {u["username"]: u["id"] for u in c.get("/api/users").json()}

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})
    r = c.get("/api/nodes")
    check("somebody else's node is invisible, not merely locked",
          r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.patch(f"/api/nodes/{node_id}", json={"description": "mine now"})
    check("and cannot be edited — 404, which does not confirm it exists",
          r.status_code == 404, str(r.status_code))
    r = c.post(f"/api/nodes/{node_id}/approve")
    check("a non-admin cannot approve a node", r.status_code == 403, str(r.status_code))

    r = c.post("/api/nodes", json={"name": "Walt Net"})
    check("a non-admin can register their own node", r.status_code == 201,
          f"{r.status_code} {r.text[:200]}")
    walt_node = r.json()["node"]["id"] if r.status_code == 201 else None
    walt_token = r.json()["enrolment_token"] if r.status_code == 201 else ""
    check("and owns it", r.status_code == 201 and r.json()["node"]["my_level"] == "owner")

    r = c.post("/api/targets", json={
        "name": "Walt Relayed", "hostname": "10.9.9.9", "username": "walt",
        "auth_type": "password", "password": "x", "relay_node_id": node_id,
    })
    check("a node cannot be borrowed by someone it was not shared with",
          r.status_code == 400, f"{r.status_code} {r.text[:160]}")

    # The property Jordan asked for, in the same words as test_targets.py.
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "otheradmin", "password": "adminpassword1"})
    r = c.get("/api/nodes")
    check("an admin does NOT see a node somebody else registered",
          r.status_code == 200 and all(n["id"] != walt_node for n in r.json()), r.text[:300])
    r = c.patch(f"/api/nodes/{walt_node}", json={"description": "taken"})
    check("nor can an admin edit one they were not given", r.status_code == 404, str(r.status_code))

    # Approval is the one thing being an administrator does confer, and it is
    # visibility of the node's own description rather than a right to use it.
    c.post("/api/relay/enroll", json={"token": walt_token, "hostname": "walt-box"})
    r = c.get("/api/nodes/pending")
    check("an admin can see what is waiting to be approved",
          r.status_code == 200 and any(n["id"] == walt_node for n in r.json()),
          f"{r.status_code} {r.text[:200]}")
    check("with no access level of their own on it",
          all(n["my_level"] == "" for n in r.json() if n["id"] == walt_node))
    r = c.post(f"/api/nodes/{walt_node}/approve")
    check("and can approve it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = c.get("/api/nodes")
    check("approving it still does not hand the admin the route",
          all(n["id"] != walt_node for n in r.json()), r.text[:200])

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})
    r = c.patch(f"/api/nodes/{walt_node}", json={
        "grants": [{"user_id": users["admin"], "level": "use"}]})
    check("the owner can share a node", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    shared = [n for n in c.get("/api/nodes").json() if n["id"] == walt_node]
    check("a shared node appears for the grantee", len(shared) == 1)
    check("with the level it was granted at", shared and shared[0]["my_level"] == "use")
    r = c.patch(f"/api/nodes/{walt_node}", json={"description": "nope"})
    check("but 'use' cannot change it", r.status_code == 403, str(r.status_code))

    # --- revocation -----------------------------------------------------
    r = c.post(f"/api/nodes/{node_id}/revoke")
    check("a node can be revoked", r.status_code == 200 and r.json()["status"] == "revoked",
          f"{r.status_code} {r.text[:200]}")
    with c.websocket_connect("/api/relay/connect", headers=auth) as ws:
        gone = ws.receive_json()
    check("a revoked node is refused immediately",
          gone.get("type") == "denied" and gone.get("code") == 4410, str(gone)[:160])
    r = c.post(f"/api/nodes/{node_id}/approve")
    check("and cannot simply be approved again", r.status_code == 409, str(r.status_code))
    r = c.post(f"/api/nodes/{node_id}/token")
    check("nor re-enrolled", r.status_code == 409, str(r.status_code))

    # --- what a run's ssh config actually says --------------------------
    from app import relay, ssh_targets  # noqa: E402
    from app.crypto import encrypt  # noqa: E402
    from app.models import RelayNode, Target  # noqa: E402

    live = RelayNode(id=7, name="Salt Net", slug="salt-net", status="approved", grants=[])
    direct = Target(id=1, name="Direct", slug="direct", hostname="10.0.3.9", port=22,
                    username="alice", auth_type="password", password_enc=encrypt("x"),
                    host_key_policy="accept-new", grants=[], owner_id=1)
    behind = Target(id=2, name="Behind", slug="behind", hostname="10.10.20.9", port=2222,
                    username="alice", auth_type="password", password_enc=encrypt("x"),
                    host_key_policy="accept-new", grants=[], owner_id=1, relay_node_id=7)

    ctx = ssh_targets.prepare([direct, behind], {7: live})
    with open(os.path.join(ctx.root, "config")) as fh:
        config = fh.read()
    behind_block = config.split("Host behind")[1]
    direct_block = config.split("Host direct")[1].split("Host ")[0]
    check("a system bound to a node routes through it",
          "ProxyCommand" in behind_block and "relay_connect.py" in behind_block,
          behind_block[:200])
    check("and the ProxyCommand names that node",
          "salt-net" in behind_block.split("ProxyCommand")[1].split("\n")[0],
          behind_block.split("ProxyCommand")[1].split("\n")[0][:200])
    check("a system not bound to one still connects directly",
          "ProxyCommand" not in direct_block, direct_block[:200])
    check("the run is given a relay token to use it with",
          bool(ctx.env.get("AIOPS_RELAY_TOKEN")) and bool(ctx.env.get("AIOPS_RELAY_ADDR")))
    check("the token authorises exactly the host that was materialised",
          relay.tokens.allows(ctx.env["AIOPS_RELAY_TOKEN"], "salt-net", "10.10.20.9", 2222))
    check("and nothing else on that node's network",
          not relay.tokens.allows(ctx.env["AIOPS_RELAY_TOKEN"], "salt-net", "10.10.20.1", 22))
    check("the agent is told the hop exists",
          "via relay node Salt Net" in ssh_targets.describe([behind], {7: live}))

    stale_token = ctx.env["AIOPS_RELAY_TOKEN"]
    ctx.cleanup()
    check("and the token dies with the run",
          not relay.tokens.allows(stale_token, "salt-net", "10.10.20.9", 2222))

    # A binding whose node has gone must fail, never silently dial direct.
    orphan = ssh_targets.prepare([behind], {})
    with open(os.path.join(orphan.root, "config")) as fh:
        orphan_config = fh.read()
    check("a system whose node vanished does not fall back to a direct connection",
          "ProxyCommand" in orphan_config
          and orphan_config.split("ProxyCommand")[1].split("\n")[0].strip().endswith("- %h %p"),
          orphan_config.split("ProxyCommand")[1].split("\n")[0][:200])
    orphan.cleanup()

    # --- subnet routing: what may be allowed ----------------------------
    # A node reaches a stored system's exact host and port. Reaching a whole
    # LAN through it is a separate grant, made here and nowhere else.
    r = c.post("/api/nodes", json={"name": "Insurance Net"})
    subnet_id = r.json()["node"]["id"]
    subnet_token = r.json()["enrolment_token"]
    check("a new node starts with no subnet reach",
          r.json()["node"]["allowed_cidrs"] == [], str(r.json()["node"]["allowed_cidrs"]))
    check("and with the only port a subnet route defaults to",
          r.json()["node"]["allowed_ports"] == [22], str(r.json()["node"]["allowed_ports"]))

    # The node then says it can see a great deal. None of it is a grant.
    c.post("/api/relay/enroll", json={
        "token": subnet_token, "hostname": "198.51.100.5",
        "networks": ["198.51.100.0/24", "10.0.0.0/8", "8.8.8.0/24"],
    })
    c.post(f"/api/nodes/{subnet_id}/approve")
    row = next(n for n in c.get("/api/nodes").json() if n["id"] == subnet_id)
    check("a node that enrolled claiming three networks still reaches none of them",
          row["allowed_cidrs"] == [] and len(row["networks"]) == 3, str(row["allowed_cidrs"]))

    refusals = {
        "8.8.8.0/24": "it is public, which is the open-proxy case",
        "10.0.0.0/8": "a /8 is wider than a node is ever a route to",
        "0.0.0.0/0": "the whole internet is not a LAN",
        "198.51.100.0/33": "it is not a network",
        "not-a-network": "it is not anything",
        "fd00::/16": "IPv6, which no generated Host pattern could match",
    }
    for bad, why in refusals.items():
        r = c.patch(f"/api/nodes/{subnet_id}", json={"allowed_cidrs": [bad]})
        check(f"{bad} is refused because {why}", r.status_code == 400,
              f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/nodes/{subnet_id}", json={"allowed_cidrs": ["8.8.8.0/24"]})
    check("and a public range is refused in words that say why, not 'invalid'",
          "open proxy" in r.text, r.text[:250])

    r = c.patch(f"/api/nodes/{subnet_id}",
                json={"allowed_cidrs": [f"10.{n}.0.0/16" for n in range(11)]})
    check("a list longer than the cap is refused", r.status_code == 400,
          f"{r.status_code} {r.text[:160]}")
    for bad_port in ([0], [65536], ["ssh"], list(range(1, 12))):
        r = c.patch(f"/api/nodes/{subnet_id}", json={"allowed_ports": bad_port})
        check(f"port list {str(bad_port)[:24]} is refused", r.status_code == 400,
              f"{r.status_code} {r.text[:160]}")

    r = c.patch(f"/api/nodes/{subnet_id}",
                json={"allowed_cidrs": ["198.51.100.0/24"], "allowed_ports": [22, 8006]})
    check("a private /24 is accepted", r.status_code == 200, f"{r.status_code} {r.text[:250]}")
    check("and is what the node is allowed, no more",
          r.status_code == 200 and r.json()["allowed_cidrs"] == ["198.51.100.0/24"],
          r.text[:200])
    check("the ranges the node claimed for itself are still only claims",
          r.status_code == 200 and "10.0.0.0/8" in r.json()["networks"]
          and "10.0.0.0/8" not in r.json()["allowed_cidrs"], r.text[:250])
    r = c.patch(f"/api/nodes/{subnet_id}", json={"allowed_cidrs": ["198.51.100.5/24"]})
    check("a range written as an address inside it is stored as the range",
          r.status_code == 200 and r.json()["allowed_cidrs"] == ["198.51.100.0/24"],
          r.text[:200])
    c.patch(f"/api/nodes/{subnet_id}", json={"allowed_ports": [22, 8006]})

    # 'use' is permission to route through a node, never to widen it.
    c.patch(f"/api/nodes/{subnet_id}", json={"grants": [{"user_id": users["walt"], "level": "use"}]})
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})
    r = c.patch(f"/api/nodes/{subnet_id}", json={"allowed_cidrs": ["10.10.0.0/16"]})
    check("someone with 'use' cannot widen what the node reaches",
          r.status_code == 403, f"{r.status_code} {r.text[:160]}")
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    # A revoked node with a range set must still be worth nothing.
    c.patch(f"/api/nodes/{node_id}", json={"allowed_cidrs": ["10.10.20.0/24"]})

    # --- subnet routing: the gate ---------------------------------------
    # Everything below asks the authorisation function the forwarder asks, at
    # the point it asks it. The ssh config is checked separately and never
    # stands in for this.
    reach = RelayNode(id=11, name="Acme Insurance", slug="acme-insurance", status="approved",
                      grants=[], networks=["172.31.0.0/16", "0.0.0.0/0"],
                      allowed_cidrs=["198.51.100.0/24"], allowed_ports=[22, 8006])
    ctx = ssh_targets.prepare([], {}, [reach], who="tester (run 1, session s-1)")
    check("a node with a subnet gives a run a relay token with no stored systems at all",
          ctx is not None and bool(ctx.env.get("AIOPS_RELAY_TOKEN")))
    tok = ctx.env["AIOPS_RELAY_TOKEN"]
    check("the gate allows an in-range address on an allowed port",
          relay.tokens.allows(tok, "acme-insurance", "198.51.100.42", 22))
    check("and on the second allowed port",
          relay.tokens.allows(tok, "acme-insurance", "198.51.100.42", 8006))
    check("the gate refuses an in-range address on a port that was not allowed",
          not relay.tokens.allows(tok, "acme-insurance", "198.51.100.42", 3389))
    check("the gate refuses an address outside the range",
          not relay.tokens.allows(tok, "acme-insurance", "192.168.89.42", 22))
    check("a subnet is reachable through the node it was set on and no other",
          not relay.tokens.allows(tok, "salt-net", "198.51.100.42", 22))
    check("what a node reports about itself authorises nothing — 172.31/16",
          not relay.tokens.allows(tok, "acme-insurance", "172.31.4.4", 22))
    check("nor does a node claiming the entire internet",
          not relay.tokens.allows(tok, "acme-insurance", "8.8.8.8", 22))
    check("a name is never matched against a range, only an address",
          not relay.tokens.allows(tok, "acme-insurance", "printer.lan", 22))
    ctx.cleanup()
    check("and the subnet dies with the run like everything else",
          not relay.tokens.allows(tok, "acme-insurance", "198.51.100.42", 22))

    # --- the bypass test ------------------------------------------------
    # A /25 has no glob spelling. The config is therefore written wider than
    # the range it stands for — deliberately, and only because the config is
    # not what decides. This is that gap, exercised on purpose.
    narrow = RelayNode(id=12, name="Half A Subnet", slug="half-net", status="approved",
                       grants=[], networks=[],
                       allowed_cidrs=["198.51.100.0/25"], allowed_ports=[22])
    half = ssh_targets.prepare([], {}, [narrow], who="tester (run 2, session s-2)")
    with open(os.path.join(half.root, "config")) as fh:
        half_config = fh.read()
    host_line = next(
        (ln.strip() for ln in half_config.splitlines() if ln.startswith("Host ")), ""
    )
    check("a /25 is written as the whole /24 around it, because a glob cannot say /25",
          host_line == "Host 198.51.100.*", host_line)
    check("so ssh really would route .200 — the glob matches it",
          fnmatch.fnmatch("198.51.100.200", host_line.split(" ", 1)[1]), host_line)
    half_tok = half.env["AIOPS_RELAY_TOKEN"]
    check("BYPASS: the gate refuses .200 regardless, because the CIDR is what it checks",
          not relay.tokens.allows(half_tok, "half-net", "198.51.100.200", 22))
    check("while .100, genuinely inside the /25, is allowed",
          relay.tokens.allows(half_tok, "half-net", "198.51.100.100", 22))
    check("and the config says out loud that no credential comes with a subnet",
          "No credential is stored" in half_config, half_config[:400])
    check("a subnet host is routed through the node rather than dialled from here",
          "ProxyCommand" in half_config and "half-net" in half_config)
    half.cleanup()

    # An octet-aligned range needs no widening, and must not get any.
    exact = RelayNode(id=13, name="Exact", slug="exact-net", status="approved", grants=[],
                      networks=[], allowed_cidrs=["10.20.30.0/24"], allowed_ports=[22])
    exact_ctx = ssh_targets.prepare([], {}, [exact])
    with open(os.path.join(exact_ctx.root, "config")) as fh:
        exact_line = next(ln.strip() for ln in fh if ln.startswith("Host "))
    check("a /24 is a glob exactly, and is written as one", exact_line == "Host 10.20.30.*",
          exact_line)
    exact_ctx.cleanup()

    # --- what the agent is told -----------------------------------------
    briefing = ssh_targets.describe([], {}, [reach])
    check("the agent is told the subnet is reachable", "198.51.100.0/24" in briefing, briefing[:300])
    check("and through which node", "Acme Insurance" in briefing, briefing[:300])
    check("and on which ports", "22, 8006" in briefing, briefing[:300])
    check("and that no credential comes with it",
          "No credential is stored" in briefing, briefing[:400])
    check("and that being refused is a policy limit, not a broken network",
          "policy limit" in briefing, briefing[:600])
    both = ssh_targets.describe([behind], {7: live}, [reach])
    check("stored systems and subnets are described as the different offers they are",
          "via relay node Salt Net" in both and "198.51.100.0/24" in both, both[:400])

    # --- what the installers promise ------------------------------------
    # Found live: with Restart=always, a node told it was revoked exits
    # cleanly, is restarted five seconds later, reconnects, is told again, and
    # does that on somebody else's network forever.
    unit = open(os.path.join(REPO, "deploy", "relay", "aiops-relay-node.service")).read()
    check("a revoked node is not restarted into a reconnect loop by systemd",
          "Restart=on-failure" in unit and "Restart=always" not in unit,
          [ln for ln in unit.splitlines() if ln.startswith("Restart")])
    compose = open(os.path.join(REPO, "deploy", "relay", "docker-compose.yml")).read()
    check("nor by Docker", "restart: on-failure" in compose,
          [ln for ln in compose.splitlines() if "restart:" in ln])
    installer = open(os.path.join(REPO, "deploy", "relay", "install.sh")).read()
    check("the installer ships the unit that is reviewed here, rather than its own copy",
          "aiops-relay-node.service" in installer and "[Service]" not in installer)
    check("and leaves one command that removes all of it",
          "/usr/local/sbin/aiops-relay-uninstall" in installer and "userdel" in installer)


# =====================================================================
# Who a subnet route is materialised for
# =====================================================================
# Against the real database rather than in-memory objects, because the whole
# question is which rows come back for which person.


async def subnet_scope_checks():
    from sqlalchemy import select as sql_select  # noqa: E402

    from app import relay as relay_mod  # noqa: E402
    from app.db import SessionLocal  # noqa: E402
    from app.models import User as UserRow  # noqa: E402

    async with SessionLocal() as db:
        people = {
            name: await db.scalar(sql_select(UserRow).where(UserRow.username == name))
            for name in ("admin", "walt", "otheradmin")
        }
        owner_sees = [n.slug for n in await relay_mod.subnet_nodes_for(db, people["admin"])]
        check("the node's owner gets its subnet materialised",
              owner_sees == ["insurance-net"], str(owner_sees))
        check("a node of theirs with no CIDR set gives them nothing",
              "salt-net" not in owner_sees, str(owner_sees))

        granted = [n.slug for n in await relay_mod.subnet_nodes_for(db, people["walt"])]
        check("someone granted 'use' on the node gets the subnet too",
              granted == ["insurance-net"], str(granted))
        check("but not through the node they own with nothing allowed on it",
              "walt-net" not in granted, str(granted))

        stranger = [n.slug for n in await relay_mod.subnet_nodes_for(db, people["otheradmin"])]
        check("a user with no access to the node gets no subnet route at all",
              stranger == [], str(stranger))
        check("and that user is an administrator — approving a node is not routing through it",
              people["otheradmin"].is_admin)
        check("nobody at all gets a subnet route with no user to scope it to",
              await relay_mod.subnet_nodes_for(db, None) == [])

        # The revoked node had a range set on it above. It is still revoked.
        node = await db.scalar(sql_select(relay_mod.RelayNode).where(
            relay_mod.RelayNode.slug == "salt-net"))
        check("the revoked node really does hold a CIDR, so the exclusion is the status",
              node is not None and node.allowed_cidrs == ["10.10.20.0/24"],
              str(node.allowed_cidrs if node else None))


asyncio.run(subnet_scope_checks())


# =====================================================================
# Part two: bytes, end to end, through the real agent
# =====================================================================
print()
print("--- data path (real agent, real sockets) ---")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app import relay  # noqa: E402

API_PORT = free_port()
ECHO_PORT = free_port()
STATE_DIR = os.path.abspath("./.relay-state")
os.makedirs(STATE_DIR, exist_ok=True)
for stale in ("credential",):
    if os.path.exists(os.path.join(STATE_DIR, stale)):
        os.remove(os.path.join(STATE_DIR, stale))


def echo_session(conn):
    """One connection: say hello, then mirror what is said."""
    conn.sendall(b"FAR-HOST-BANNER\n")
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            conn.sendall(data.upper())
    except OSError:
        pass
    finally:
        conn.close()


def echo_server(port, stop):
    """Stands in for the far host, one thread per connection.

    Threaded rather than serving one caller at a time, which is what it used to
    do. A relayed connection is closed through four hops — helper, forwarder,
    node websocket, node's own socket — and the far end learning about it is
    the last thing to happen. Serving serially meant the *next* connection sat
    unaccepted in the backlog behind a caller that had already gone, and since
    the kernel completes a handshake into the backlog, everything upstream
    reported an open connection that would never say anything. The suite hung
    rather than failed, which is the worst way for a test harness to be wrong.
    """
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(4)
    listener.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, _ = listener.accept()
        except socket.timeout:
            continue
        threading.Thread(target=echo_session, args=(conn,), daemon=True).start()
    listener.close()


stop_echo = threading.Event()
threading.Thread(target=echo_server, args=(ECHO_PORT, stop_echo), daemon=True).start()

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=API_PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(200):
    if server.started:
        break
    time.sleep(0.05)

agent = None
api = httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=20)
try:
    api.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    made = api.post("/api/nodes", json={"name": "Live Net"}).json()
    live_id = made["node"]["id"]
    live_slug = made["node"]["slug"]

    agent = subprocess.Popen(
        [sys.executable, AGENT,
         "--url", f"http://127.0.0.1:{API_PORT}",
         "--token", made["enrolment_token"],
         "--state-dir", STATE_DIR],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    def wait_for(predicate, seconds=25):
        deadline = time.time() + seconds
        while time.time() < deadline:
            row = next((n for n in api.get("/api/nodes").json() if n["id"] == live_id), None)
            if row and predicate(row):
                return row
            time.sleep(0.25)
        return None

    row = wait_for(lambda n: n["enrolled_at"] is not None)
    check("the real agent enrols itself against a running server", row is not None, str(row)[:200])
    check("and is still pending until approved", row and row["status"] == "pending")
    check("its credential is on disk, readable by nobody else",
          os.path.exists(os.path.join(STATE_DIR, "credential"))
          and (os.name == "nt"
               or oct(os.stat(os.path.join(STATE_DIR, "credential")).st_mode & 0o777) == "0o600"))

    online_while_pending = wait_for(lambda n: n["online"], seconds=4)
    check("an unapproved node cannot get a control channel at all",
          online_while_pending is None, str(online_while_pending)[:160])

    api.post(f"/api/nodes/{live_id}/approve")
    row = wait_for(lambda n: n["online"])
    check("once approved, the agent connects and stays connected", row is not None, str(row)[:200])
    check("and AIOps records what it is", row and row["version"] == "1.0.0")

    # --- the actual bytes ------------------------------------------------
    allowed = relay.tokens.issue({(live_slug, "127.0.0.1", ECHO_PORT)})
    helper_env = {
        **os.environ,
        "AIOPS_RELAY_TOKEN": allowed,
        "AIOPS_RELAY_ADDR": f"127.0.0.1:{relay.hub.forwarder_port}",
    }
    helper = os.path.join(os.getcwd(), "app", "relay_connect.py")

    proxy = subprocess.Popen(
        [sys.executable, helper, live_slug, "127.0.0.1", str(ECHO_PORT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=helper_env,
    )
    banner = proxy.stdout.read(16)
    check("a ProxyCommand reaches the far host through the node",
          banner == b"FAR-HOST-BANNER\n", repr(banner))
    proxy.stdin.write(b"through the relay\n")
    proxy.stdin.flush()
    echoed = proxy.stdout.read(18)
    check("and bytes travel both ways over it",
          echoed == b"THROUGH THE RELAY\n", repr(echoed))
    proxy.stdin.close()
    proxy.wait(timeout=15)

    # The node was asked for one address. It must be no use for any other.
    denied = subprocess.run(
        [sys.executable, helper, live_slug, "127.0.0.1", str(API_PORT)],
        input=b"", capture_output=True, env=helper_env, timeout=30,
    )
    check("the same run cannot point the node at a different host",
          denied.returncode == 1 and b"not permitted" in denied.stderr,
          denied.stderr.decode()[:200])

    # --- the same thing again, but reached as a subnet -------------------
    # Everything above went through an exact (node, host, port) route. This
    # goes through a CIDR rule, over the same sockets, so the widened gate is
    # exercised in the data path rather than only against its own function.
    from app.models import RelayNode as NodeRow  # noqa: E402

    ranged = NodeRow(
        name="Live Net", slug=live_slug, status="approved", grants=[],
        # A private /24 that happens to contain the loopback the echo server is
        # on. 127.0.0.0/8 would be refused by validation; this is not.
        networks=["10.0.0.0/8"], allowed_cidrs=["127.0.0.0/24"], allowed_ports=[ECHO_PORT],
    )
    subnet_env = {
        **os.environ,
        "AIOPS_RELAY_TOKEN": relay.tokens.issue(
            set(), relay.subnet_rules(ranged), "tester (run 3, session s-3)"
        ),
        "AIOPS_RELAY_ADDR": f"127.0.0.1:{relay.hub.forwarder_port}",
    }
    ranged_proxy = subprocess.Popen(
        [sys.executable, helper, live_slug, "127.0.0.1", str(ECHO_PORT)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=subnet_env,
    )
    banner = ranged_proxy.stdout.read(16)
    check("a host inside an allowed subnet is reached with no stored system for it",
          banner == b"FAR-HOST-BANNER\n", repr(banner))
    ranged_proxy.stdin.close()
    ranged_proxy.wait(timeout=15)

    wrong_port = subprocess.run(
        [sys.executable, helper, live_slug, "127.0.0.1", str(API_PORT)],
        input=b"", capture_output=True, env=subnet_env, timeout=30,
    )
    check("the same host on a port the subnet does not allow is refused at the socket",
          wrong_port.returncode == 1 and b"not permitted" in wrong_port.stderr,
          wrong_port.stderr.decode()[:200])
    outside = subprocess.run(
        [sys.executable, helper, live_slug, "127.1.0.1", str(ECHO_PORT)],
        input=b"", capture_output=True, env=subnet_env, timeout=30,
    )
    check("and an address outside the /24 is refused even on the allowed port",
          outside.returncode == 1 and b"not permitted" in outside.stderr,
          outside.stderr.decode()[:200])
    relay.tokens.revoke(subnet_env["AIOPS_RELAY_TOKEN"])

    # --- revocation, against a live connection ---------------------------
    api.post(f"/api/nodes/{live_id}/revoke")
    row = wait_for(lambda n: not n["online"], seconds=15)
    check("revoking drops the node's live connection", row is not None, str(row)[:200])

    refused = subprocess.run(
        [sys.executable, helper, live_slug, "127.0.0.1", str(ECHO_PORT)],
        input=b"", capture_output=True, env=helper_env, timeout=30,
    )
    check("and nothing can be routed through it afterwards",
          refused.returncode == 1 and b"not connected" in refused.stderr,
          refused.stderr.decode()[:200])

    relay.tokens.revoke(allowed)
    time.sleep(1.5)
    agent.terminate()
    output = agent.communicate(timeout=15)[0] or ""
    check("the agent says out loud which host it was asked to reach",
          f"connection to 127.0.0.1:{ECHO_PORT}" in output,
          output[-400:])
    check("and that it was revoked, rather than retrying forever",
          "revoked" in output.lower(), output[-400:])
finally:
    if agent is not None and agent.poll() is None:
        agent.kill()
    api.close()
    stop_echo.set()
    server.should_exit = True
    time.sleep(1.0)

import shutil  # noqa: E402

shutil.rmtree(STATE_DIR, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
