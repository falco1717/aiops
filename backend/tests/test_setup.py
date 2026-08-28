"""First-run setup: the "create your admin account" screen and its API.

Three things have to be true at once, and each gets its own scenario because
each needs its own process — `Settings` is read once from the environment at
import time, so "AIOPS_ADMIN_PASSWORD set" and "unset" cannot coexist inside
one running app:

1. Unset, zero users: `needs_setup` is true, POST /api/setup creates the
   admin, and a second POST is refused — the concurrent-request version of
   that refusal is the sharpest check in this file (see `test_concurrency`).
2. Set: bootstrap creates the admin at startup exactly as it always has, and
   the setup screen never appears — checked in a subprocess with its own
   environment (`_run_admin_password_scenario`), the same way test_relay.py
   spawns a real process rather than trying to fake process-wide state.
3. Neither of the above, but a user already exists anyway — the shape of the
   real production instance at 10.0.3.67, simulated the way test_users.py
   simulates a pre-upgrade database: by writing the row with sqlite3 directly,
   so nothing here depends on bootstrap or setup having run first.
"""
import concurrent.futures
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.getcwd())


def _sqlite_url(path: str) -> str:
    """A `sqlite+aiosqlite:///` URL safe to splice into a *child* script's
    source text as a plain string literal.

    Forward slashes only — a Windows absolute path's backslashes would
    otherwise land between quotes in the generated `-c` source and get read
    back as escape sequences (`\\t`, `\\U...`) by that child interpreter.
    """
    return "sqlite+aiosqlite:///" + path.replace(os.sep, "/")


DB_PATH = os.path.abspath("./test_setup.db")
os.environ["AIOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ["AIOPS_COOKIE_SECURE"] = "false"
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ.setdefault("AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-setup-test-"))
os.environ.setdefault("AIOPS_WORKSPACE_ROOT", os.path.join(os.getcwd(), ".test-setup-workspaces"))
os.environ.setdefault("AIOPS_ACCOUNTS_ROOT", os.path.join(os.getcwd(), ".test-setup-accounts"))
# The whole point of this suite: run_all.sh exports this for every other
# suite, and here it must be genuinely unset rather than merely defaulted, or
# scenario 1 would boot with an admin already created and never see
# needs_setup=True at all.
os.environ["AIOPS_ADMIN_PASSWORD"] = ""

for stale in (DB_PATH,):
    if os.path.exists(stale):
        os.remove(stale)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- scenario 1: fresh instance, no AIOPS_ADMIN_PASSWORD -------------------
with TestClient(app) as c:
    r = c.get("/api/setup/status")
    check(
        "a fresh instance with no admin password needs setup",
        r.status_code == 200 and r.json() == {"needs_setup": True},
        r.text[:200],
    )

    r = c.get("/api/auth/me")
    check("nobody is signed in yet", r.status_code == 401, str(r.status_code))

    # Below the app's own floor (PasswordChangeIn / UserCreate / UserPasswordReset
    # all use Field(min_length=8) — this reuses the same rule, not a new one).
    r = c.post("/api/setup", json={"username": "admin", "password": "short"})
    check(
        "a password under 8 characters is rejected",
        r.status_code == 422,
        f"{r.status_code}: {r.text[:200]}",
    )

    r = c.get("/api/setup/status")
    check("still needs setup after a rejected attempt", r.json()["needs_setup"] is True)

    r = c.post("/api/setup", json={"username": "admin", "password": "devpassword123"})
    check("setup succeeds with a valid username and password", r.status_code == 201, r.text[:200])
    if r.status_code == 201:
        body = r.json()
        check("the created account is an administrator", body.get("is_admin") is True, str(body))
        check("the created account is the username submitted", body.get("username") == "admin")

    check(
        "setup logged the new admin in directly — no re-login needed",
        c.get("/api/auth/me").status_code == 200
        and c.get("/api/auth/me").json()["username"] == "admin",
    )

    r = c.get("/api/setup/status")
    check("setup is no longer needed once an admin exists", r.json() == {"needs_setup": False})

    r = c.post("/api/setup", json={"username": "someone-else", "password": "devpassword123"})
    check(
        "a second POST /api/setup is refused, not silently ignored",
        r.status_code == 409,
        f"{r.status_code}: {r.text[:200]}",
    )
    check(
        "the refusal reads as normal operation, not as something broken",
        "already" in r.json().get("detail", "").lower(),
        r.text[:200],
    )

    # The setup cookie signed the first admin straight in, so this client is
    # already authenticated as them — which is itself part of what is being
    # checked here, alongside there being exactly one row.
    r = c.get("/api/users")
    check(
        "the refused second submission created no second user",
        r.status_code == 200 and len(r.json()) == 1 and r.json()[0]["username"] == "admin",
        r.text[:300],
    )


# --- scenario 3: an instance that already has a user, simulating production ---
def _seeded_production_scenario() -> None:
    """A user planted straight into a fresh database, bootstrap and setup
    never having run — the closest local stand-in for "10.0.3.67 already has
    users", used because SSH to it is read-only-by-policy for this check (see
    the actual server verification in the task report, done with a real
    `psql` read instead of trusting this alone).
    """
    seeded_path = os.path.abspath("./test_setup_seeded.db")
    if os.path.exists(seeded_path):
        os.remove(seeded_path)
    conn = sqlite3.connect(seeded_path)
    conn.execute(
        "CREATE TABLE users ("
        " id INTEGER NOT NULL PRIMARY KEY,"
        " username VARCHAR(64) NOT NULL,"
        " password_hash VARCHAR(255) NOT NULL,"
        " is_admin BOOLEAN NOT NULL DEFAULT 0,"
        " must_change_password BOOLEAN NOT NULL DEFAULT 0,"
        " created_at TIMESTAMP,"
        " last_login_at TIMESTAMP,"
        " display_name VARCHAR(128)"
        ")"
    )
    conn.execute("CREATE UNIQUE INDEX ix_users_username ON users (username)")
    conn.execute(
        "INSERT INTO users (id, username, password_hash, is_admin, created_at) "
        "VALUES (1, 'admin', 'not-a-real-hash', 1, datetime('now'))"
    )
    conn.commit()
    conn.close()

    script = f"""
import os, sys
sys.path.insert(0, {os.getcwd()!r})
os.environ["AIOPS_DATABASE_URL"] = {_sqlite_url(seeded_path)!r}
os.environ["AIOPS_JWT_SECRET"] = "test"
os.environ["AIOPS_ADMIN_PASSWORD"] = ""
os.environ["AIOPS_COOKIE_SECURE"] = "false"
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_ATTACHMENTS_ROOT"] = {tempfile.mkdtemp(prefix="aiops-setup-seeded-")!r}
os.environ["AIOPS_WORKSPACE_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-seeded-ws")!r}
os.environ["AIOPS_ACCOUNTS_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-seeded-acct")!r}

from fastapi.testclient import TestClient
from app.main import app

def check(label, condition, detail=""):
    print(f"[{{'PASS' if condition else 'FAIL'}}] {{label}}" + (f" — {{detail}}" if detail else ""))
    if not condition:
        sys.exit(1)

with TestClient(app) as c:
    r = c.get("/api/setup/status")
    check(
        "an instance seeded with a pre-existing user reports needs_setup=False",
        r.status_code == 200 and r.json() == {{"needs_setup": False}},
        r.text[:200],
    )
    r = c.post("/api/setup", json={{"username": "second-admin", "password": "devpassword123"}})
    check(
        "setup is refused on an instance that already had a user before either "
        "bootstrap or setup ever ran",
        r.status_code == 409,
        f"{{r.status_code}}: {{r.text[:200]}}",
    )
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        failures.append("seeded-production-instance scenario (subprocess)")
        print(result.stderr[-2000:], file=sys.stderr)


_seeded_production_scenario()


# --- scenario 2: AIOPS_ADMIN_PASSWORD set --------------------------------
def _admin_password_scenario() -> None:
    db_path = os.path.abspath("./test_setup_bootstrap.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    script = f"""
import os, sys
sys.path.insert(0, {os.getcwd()!r})
os.environ["AIOPS_DATABASE_URL"] = {_sqlite_url(db_path)!r}
os.environ["AIOPS_JWT_SECRET"] = "test"
os.environ["AIOPS_ADMIN_PASSWORD"] = "devpassword123"
os.environ["AIOPS_ADMIN_USERNAME"] = "admin"
os.environ["AIOPS_COOKIE_SECURE"] = "false"
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_ATTACHMENTS_ROOT"] = {tempfile.mkdtemp(prefix="aiops-setup-bootstrap-")!r}
os.environ["AIOPS_WORKSPACE_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-bootstrap-ws")!r}
os.environ["AIOPS_ACCOUNTS_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-bootstrap-acct")!r}

from fastapi.testclient import TestClient
from app.main import app

def check(label, condition, detail=""):
    print(f"[{{'PASS' if condition else 'FAIL'}}] {{label}}" + (f" — {{detail}}" if detail else ""))
    if not condition:
        sys.exit(1)

with TestClient(app) as c:
    r = c.get("/api/setup/status")
    check(
        "AIOPS_ADMIN_PASSWORD set: needs_setup is False immediately at startup",
        r.status_code == 200 and r.json() == {{"needs_setup": False}},
        r.text[:200],
    )
    r = c.post("/api/auth/login", json={{"username": "admin", "password": "devpassword123"}})
    check(
        "the bootstrap admin was created exactly as before — same username, same password",
        r.status_code == 200,
        r.text[:200],
    )
    r = c.post("/api/setup", json={{"username": "someone-else", "password": "devpassword123"}})
    check(
        "POST /api/setup is refused when AIOPS_ADMIN_PASSWORD already did the job",
        r.status_code == 409,
        f"{{r.status_code}}: {{r.text[:200]}}",
    )
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        failures.append("AIOPS_ADMIN_PASSWORD bootstrap scenario (subprocess)")
        print(result.stderr[-2000:], file=sys.stderr)


_admin_password_scenario()


# --- concurrency: two "first" submissions racing --------------------------
def _concurrency_scenario() -> None:
    """The guard this whole feature is worried about, driven for real.

    A thread barrier lines every submission up at the same instant rather
    than trusting that dispatch order alone will interleave them, then all of
    them are released together at `client.post`. Exactly one may create a
    user; every other response must refuse rather than silently doing
    nothing or corrupting state into two admins.
    """
    db_path = os.path.abspath("./test_setup_concurrent.db")
    if os.path.exists(db_path):
        os.remove(db_path)

    # Run as its own subprocess for a clean engine bound to this one file,
    # the same reason the other two scenarios above do.
    script = f"""
import concurrent.futures, os, sys, threading
sys.path.insert(0, {os.getcwd()!r})
os.environ["AIOPS_DATABASE_URL"] = {_sqlite_url(db_path)!r}
os.environ["AIOPS_JWT_SECRET"] = "test"
os.environ["AIOPS_ADMIN_PASSWORD"] = ""
os.environ["AIOPS_COOKIE_SECURE"] = "false"
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_ATTACHMENTS_ROOT"] = {tempfile.mkdtemp(prefix="aiops-setup-race-")!r}
os.environ["AIOPS_WORKSPACE_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-race-ws")!r}
os.environ["AIOPS_ACCOUNTS_ROOT"] = {os.path.join(os.getcwd(), ".test-setup-race-acct")!r}

from fastapi.testclient import TestClient
from app.main import app

def check(label, condition, detail=""):
    print(f"[{{'PASS' if condition else 'FAIL'}}] {{label}}" + (f" — {{detail}}" if detail else ""))
    if not condition:
        sys.exit(1)

N = 8
barrier = threading.Barrier(N)
statuses = [None] * N

# One TestClient — one lifespan, one portal, one already-migrated database —
# shared by every thread. Each thread's request still runs as its own
# concurrent task on that portal's event loop, which is what a real deployment
# is too: one process, one loop, many requests in flight together. Opening a
# separate TestClient per thread instead would run `lifespan()` — including
# `create_all` — independently and concurrently against the same database file
# for each one, which is a real race but the wrong one: it is schema setup
# fighting itself, not the two submissions this test means to pit against
# each other.
with TestClient(app) as c:
    def submit(i):
        barrier.wait()
        r = c.post("/api/setup", json={{"username": f"racer-{{i}}", "password": "devpassword123"}})
        statuses[i] = r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as pool:
        list(pool.map(submit, range(N)))

created = statuses.count(201)
refused = statuses.count(409)
check(
    "exactly one concurrent submission was accepted",
    created == 1,
    f"statuses={{statuses}}",
)
check(
    "every other concurrent submission was refused with 409",
    refused == N - 1,
    f"statuses={{statuses}}",
)

with TestClient(app) as c:
    r = c.get("/api/users")
    # Log in as whichever racer actually won, found from the statuses list,
    # to confirm there is exactly one row rather than trusting HTTP codes alone.
    winner = f"racer-{{statuses.index(201)}}"
    login = c.post("/api/auth/login", json={{"username": winner, "password": "devpassword123"}})
    check("the winning racer's account actually exists and can log in", login.status_code == 200)
    users = c.get("/api/users")
    check(
        "exactly one user exists in the database after the race",
        users.status_code == 200 and len(users.json()) == 1,
        users.text[:300],
    )
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    print(result.stdout, end="")
    if result.returncode != 0:
        failures.append("concurrent setup race (subprocess)")
        print(result.stderr[-4000:], file=sys.stderr)


_concurrency_scenario()


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
