"""Covers the stored-systems API over real HTTP.

This suite exists because the feature shipped with every response-bearing
endpoint returning 500 while 254 other checks passed: nothing exercised
/api/targets at all. The checks below are therefore weighted towards the two
things that actually matter — that a secret never comes back out, and that an
ordinary edit cannot destroy a stored credential.
"""
import glob
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

for _stale in ("./test-targets.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-targets.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
# Without this the API refuses to store anything, which is itself tested below.
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
# A real run is driven below, so the workspace root has to be somewhere this
# process may create directories.
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-target-test-ws-")
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# A throwaway key, generated here so no real credential is ever in the repo.
tmpdir = tempfile.mkdtemp(prefix="aiops-target-test-")
key_path = os.path.join(tmpdir, "probe")
subprocess.run(
    ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path, "-q"],
    check=True, capture_output=True,
)
with open(key_path) as fh:
    PRIVATE_KEY = fh.read()
# A distinctive middle slice, so "did this leak?" is a real search and not just
# a check for the BEGIN header.
KEY_CHUNK = PRIVATE_KEY.splitlines()[2][:24]

with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    r = c.post("/api/targets", json={
        "name": "Probe Box",
        "hostname": "127.0.0.1",
        "username": "node",
        "port": 22,
        "auth_type": "key",
        "private_key": PRIVATE_KEY,
        "description": "created by test_targets.py",
    })
    check("creating a system succeeds", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    created = r.json() if r.status_code == 201 else {}
    target_id = created.get("id")
    check("the response carries every declared field",
          r.status_code == 201 and "created_at" in created,
          str(sorted(created))[:200])
    check("the short name an agent types is derived from the name",
          created.get("slug") == "probe-box", str(created.get("slug")))

    r = c.get("/api/targets")
    check("systems are listed", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.text
    check("a stored key is reported as present", r.status_code == 200
          and r.json()[0]["has_private_key"] is True)

    # The property this whole feature stands on.
    check("no key material comes back from the API",
          "BEGIN" not in body and KEY_CHUNK not in body and "PRIVATE" not in body,
          "LEAKED KEY MATERIAL" if "BEGIN" in body else "clean")
    # Anchored on the opening quote: `has_private_key` legitimately contains
    # `private_key"`, so an unanchored search flags the very boolean indicators
    # that prove no secret field is present.
    check("the response has no field that could carry a secret",
          not any(k in body for k in ('"private_key"', '"password"', '"passphrase"')),
          body[:200])

    # An edit that does not retype the key must not destroy it.
    r = c.patch(f"/api/targets/{target_id}", json={"port": 2222})
    check("editing a system succeeds", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    check("the edit applied", r.status_code == 200 and r.json()["port"] == 2222)
    check("editing without retyping the key keeps it",
          r.status_code == 200 and r.json()["has_private_key"] is True,
          "the stored key was wiped by an unrelated edit" if r.status_code == 200 else "")

    # Replacing it should work, though.
    r = c.patch(f"/api/targets/{target_id}", json={"private_key": PRIVATE_KEY.replace("b", "c")})
    check("a key can be replaced", r.status_code == 200 and r.json()["has_private_key"] is True)

    r = c.patch(f"/api/targets/{target_id}", json={"host_key_policy": "no"})
    check("host key checking cannot be turned off", r.status_code == 400, str(r.status_code))
    r = c.patch(f"/api/targets/{target_id}", json={"auth_type": "telepathy"})
    check("an unknown auth type is rejected", r.status_code == 400, str(r.status_code))

    r = c.post("/api/targets", json={
        "name": "Probe Box", "hostname": "h", "username": "u", "auth_type": "key",
    })
    check("a duplicate name is refused rather than shadowing the first",
          r.status_code == 409, str(r.status_code))

    # --- privacy: a stored credential belongs to whoever stored it ---------
    c.post("/api/users", json={
        "username": "walt", "password": "waltpassword1", "is_admin": False,
        "must_change_password": False,
    })
    c.post("/api/users", json={
        "username": "otheradmin", "password": "adminpassword1", "is_admin": True,
        "must_change_password": False,
    })
    users = {u["username"]: u["id"] for u in c.get("/api/users").json()}

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})

    r = c.get("/api/targets")
    check("someone else's system is invisible, not merely locked",
          r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.patch(f"/api/targets/{target_id}", json={"port": 99})
    check("and cannot be edited — 404, which does not confirm it exists",
          r.status_code == 404, str(r.status_code))

    r = c.post("/api/targets", json={
        "name": "Walt Box", "hostname": "10.0.0.9", "username": "walt",
        "auth_type": "key", "private_key": PRIVATE_KEY,
    })
    check("a non-admin can store their own system", r.status_code == 201,
          f"{r.status_code} {r.text[:200]}")
    walt_target = r.json()["id"] if r.status_code == 201 else None
    check("and owns what they created", r.status_code == 201 and r.json()["my_level"] == "owner")

    # The property Jordan asked for.
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "otheradmin", "password": "adminpassword1"})
    r = c.get("/api/targets")
    check("an admin does NOT see a system somebody else stored",
          r.status_code == 200 and all(t["id"] != walt_target for t in r.json()),
          r.text[:300])
    r = c.get("/api/targets")
    check("no key material leaks to an admin either",
          "BEGIN" not in r.text and KEY_CHUNK not in r.text)
    r = c.delete(f"/api/targets/{walt_target}")
    check("an admin cannot delete a system they were not given",
          r.status_code == 404, str(r.status_code))

    # Sharing, and what each level permits.
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})
    r = c.patch(f"/api/targets/{walt_target}",
                json={"grants": [{"user_id": users["admin"], "level": "use"}]})
    check("the owner can share it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    r = c.get("/api/targets")
    shared = [t for t in r.json() if t["id"] == walt_target]
    check("a shared system appears for the grantee", len(shared) == 1)
    check("with the level it was granted at", shared and shared[0]["my_level"] == "use")
    r = c.patch(f"/api/targets/{walt_target}", json={"port": 2200})
    check("but 'use' cannot change it", r.status_code == 403, str(r.status_code))
    r = c.delete(f"/api/targets/{walt_target}")
    check("and 'use' cannot delete it", r.status_code == 403, str(r.status_code))

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "walt", "password": "waltpassword1"})
    c.patch(f"/api/targets/{walt_target}",
            json={"grants": [{"user_id": users["admin"], "level": "manage"}]})
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    r = c.patch(f"/api/targets/{walt_target}", json={"port": 2200})
    check("'manage' can change it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/targets/{walt_target}", json={"owner_id": users["admin"]})
    check("but only the owner can hand it over", r.status_code == 403, str(r.status_code))

    # Offboarding: ownership passes to a manager rather than stranding a system.
    r = c.delete(f"/api/users/{users['walt']}")
    check("deleting the owner succeeds when a manager can inherit",
          r.status_code == 204, f"{r.status_code} {r.text[:200]}")
    r = c.get("/api/targets")
    inherited = [t for t in r.json() if t["id"] == walt_target]
    check("and the system is now owned by that manager",
          len(inherited) == 1 and inherited[0]["my_level"] == "owner",
          str(inherited[:1])[:200])

    # With nobody able to inherit, the delete must be refused, not orphan it.
    c.post("/api/users", json={
        "username": "lone", "password": "lonepassword1", "is_admin": False,
        "must_change_password": False,
    })
    lone_id = {u["username"]: u["id"] for u in c.get("/api/users").json()}["lone"]
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "lone", "password": "lonepassword1"})
    c.post("/api/targets", json={
        "name": "Lone Box", "hostname": "10.0.0.10", "username": "lone",
        "auth_type": "key", "private_key": PRIVATE_KEY,
    })
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    r = c.delete(f"/api/users/{lone_id}")
    check("deleting a user who owns an unmanageable system is refused",
          r.status_code == 409, str(r.status_code))
    check("and the refusal names what is in the way",
          "Lone Box" in r.text, r.text[:200])
    r = c.get("/api/users")
    check("so the user still exists",
          any(u["id"] == lone_id for u in r.json()))

    r = c.get("/api/users/directory")
    check("anyone can look up who to share with", r.status_code == 200, str(r.status_code))
    check("the directory exposes usernames and nothing more",
          r.status_code == 200 and set(r.json()[0]) == {"id", "username"},
          str(r.json()[:1])[:160])

# --- what actually lands on disk for a run ---------------------------------
from app.crypto import decrypt, encrypt  # noqa: E402
from app.models import Target  # noqa: E402
from app import ssh_targets  # noqa: E402

check("a stored secret is not readable without decrypting it",
      encrypt("hunter2") not in (None, "hunter2") and decrypt(encrypt("hunter2")) == "hunter2")

sample = Target(
    id=1, name="Example Box", slug="example-box", hostname="203.0.113.20", port=22,
    username="alice", auth_type="key", private_key_enc=encrypt(PRIVATE_KEY),
    host_key_policy="accept-new", grants=[], owner_id=1,
)
ctx = ssh_targets.prepare([sample])
check("a run gets an ssh config for the stored systems", ctx is not None)
if ctx:
    with open(os.path.join(ctx.root, "config")) as fh:
        config = fh.read()
    check("the config maps the short name to the real host",
          "Host example-box" in config and "HostName 203.0.113.20" in config, config[:200])
    check("only the stored key is offered, so a host that rate-limits auth is not tripped",
          "IdentitiesOnly yes" in config)
    check("host key checking is on in the generated config",
          "StrictHostKeyChecking accept-new" in config)
    key_file = os.path.join(ctx.root, "keys", "example-box")
    check("the key is written for ssh to use", os.path.exists(key_file))
    if os.path.exists(key_file):
        mode = os.stat(key_file).st_mode & 0o777
        # 0640, not 0600: the agent that runs `ssh` is a different user in the
        # same group, and this is the only thing it is allowed to read out of
        # the run directory. Nobody outside that group gets anything, and the
        # group cannot write — see backend/tests/test_isolation.py.
        check("only the app and the agent user can read it",
              mode == ssh_targets.RUN_FILE_MODE == 0o640, oct(mode))
        check("and nobody else can read it at all", not mode & 0o007, oct(mode))
    check("ssh is reachable without flags, via a shim on PATH",
          os.path.exists(os.path.join(ctx.root, "bin", "ssh"))
          and ctx.env["PATH"].startswith(os.path.join(ctx.root, "bin")))
    check("the agent is told which systems exist",
          "example-box" in ssh_targets.describe([sample]))

    root = ctx.root
    ctx.cleanup()
    check("nothing is left on disk once the run ends", not os.path.exists(root), root)

# --- a decrypted key must not outlive the attempt that wrote it -------
# prepare() writes a plaintext private key to a temporary directory, and for a
# long time the `finally` that removed it did not open until ninety lines later.
# Everything in between — a commit that can fail on a row deleted underneath it,
# an await an operator can cancel, a provider that refuses to build a command —
# left the key on disk with nothing left to remove it. The check is written as
# "what is in /tmp afterwards", because that is what an attacker would look at.
import shutil  # noqa: E402

from app import runner as runner_module  # noqa: E402

_pattern = os.path.join(tempfile.gettempdir(), "aiops-ssh-*")
_before = set(glob.glob(_pattern))
_real_publish = runner_module.hub.publish


def _explode(session_id, payload):
    """Fail exactly where the old cleanup handler had not been installed yet."""
    if isinstance(payload, dict) and payload.get("type") == "run.started":
        raise RuntimeError("forced failure after the run was committed")
    return _real_publish(session_id, payload)


runner_module.hub.publish = _explode
row = {}
try:
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
        ws = c.post("/api/workspaces", json={"name": "cred-lifetime", "path": "cred-lifetime"})
        sess = c.post("/api/sessions", json={
            "provider": "claude", "workspace_id": ws.json()["id"], "approval_mode": "bypass",
        }).json()
        started = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "anything"}).json()
        for _ in range(120):
            time.sleep(0.25)
            row = c.get(f"/api/runs/{started['id']}").json()
            if row.get("status") not in ("queued", "running"):
                break
finally:
    runner_module.hub.publish = _real_publish

check("a run that dies after being committed still settles as failed",
      row.get("status") == "failed", str(row.get("status")))
_leaked = sorted(set(glob.glob(_pattern)) - _before)
check("and leaves no decrypted private key behind in /tmp", not _leaked, ", ".join(_leaked))
for _path in _leaked:
    shutil.rmtree(_path, ignore_errors=True)

shutil.rmtree(tmpdir, ignore_errors=True)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
