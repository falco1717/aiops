"""Workspace ownership and sharing — the last unscoped part of the access model.

Written as the mirror of `test_targets.py`, because the rule is deliberately the
same one: a workspace belongs to whoever registered it, is invisible to everyone
else until they are named, and **an administrator who was not named gets 404** —
not 403, which would confirm that a project by that name is registered here.

Two things here are not in the systems suite and are the reason it exists as a
file of its own:

* the **backfill**, exercised against a hand-built pre-upgrade database with no
  `workspaces.owner_id` at all, holding the two workspaces this instance really
  has. A workspace never recorded a creator, so an upgrade that got this wrong
  would hide both of them from everybody, permanently.
* the **run-time** rule. A turn runs as whoever sent it, not as the session's
  owner, so a shared session must not lend out its owner's workspace. That is
  checked by driving a real turn through the runner and reading what the run
  row says about why it failed.
"""
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

DB_PATH = os.path.abspath("./test_workspaces.db")
os.environ["AIOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_USERNAME", "admin")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_COOKIE_SECURE"] = "false"
# A real turn is driven below, so the root has to be somewhere this process may
# create directories.
WS_ROOT = os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-workspace-test-")
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- a database from before workspaces were owned --------------------------
# The shape production is actually on: a `workspaces` table with no owner_id,
# and the two rows this instance has been running with.
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app.security import hash_password  # noqa: E402

LIVE = {"e2e": os.path.join(WS_ROOT, "e2e"), "aiops-src": os.path.join(WS_ROOT, "aiops-src")}
for _path in LIVE.values():
    os.makedirs(_path, exist_ok=True)

legacy = sqlite3.connect(DB_PATH)
legacy.execute(
    "CREATE TABLE users ("
    " id INTEGER NOT NULL PRIMARY KEY,"
    " username VARCHAR(64) NOT NULL,"
    " password_hash VARCHAR(255) NOT NULL,"
    " is_admin BOOLEAN NOT NULL DEFAULT 0,"
    " created_at TIMESTAMP"
    ")"
)
legacy.execute("CREATE UNIQUE INDEX ix_users_username ON users (username)")
legacy.execute(
    "INSERT INTO users (id, username, password_hash, is_admin, created_at)"
    " VALUES (1, ?, ?, 1, datetime('now'))",
    ("admin", hash_password("devpassword123")),
)
legacy.execute(
    "CREATE TABLE workspaces ("
    " id INTEGER NOT NULL PRIMARY KEY,"
    " name VARCHAR(128) NOT NULL UNIQUE,"
    " path VARCHAR(1024) NOT NULL,"
    " description TEXT,"
    " created_at TIMESTAMP"
    ")"
)
for _i, (_name, _path) in enumerate(LIVE.items(), start=1):
    legacy.execute(
        "INSERT INTO workspaces (id, name, path, created_at) VALUES (?, ?, ?, datetime('now'))",
        (_i, _name, _path),
    )
legacy.commit()
_cols_before = {r[1] for r in legacy.execute("PRAGMA table_info(workspaces)")}
legacy.close()
check("fixture really is the old schema", "owner_id" not in _cols_before, str(sorted(_cols_before)))

from fastapi.testclient import TestClient  # noqa: E402

from app.access import LEVELS, workspace_level_for  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User as UserModel, Workspace, WorkspaceAccess  # noqa: E402

# --- the rule itself, before any HTTP --------------------------------------
# The one place it is written down, so it is worth pinning directly: every
# endpoint and the runner read access through this function.
_owner = UserModel(id=1, username="owner")
_other = UserModel(id=2, username="other")
_admin = UserModel(id=3, username="admin2", is_admin=True)
_ws = Workspace(id=1, name="w", path="/tmp/w", owner_id=1, grants=[])
check("the owner owns it", workspace_level_for(_ws, _owner) == "owner")
check("a stranger gets nothing", workspace_level_for(_ws, _other) is None)
check("an administrator gets nothing implicitly",
      workspace_level_for(_ws, _admin) is None, str(workspace_level_for(_ws, _admin)))
check("nobody at all gets nothing", workspace_level_for(_ws, None) is None)
_ws.grants = [WorkspaceAccess(workspace_id=1, user_id=2, level="manage")]
check("a grant is honoured at its level", workspace_level_for(_ws, _other) == "manage")
_ws.grants = [WorkspaceAccess(workspace_id=1, user_id=2, level="nonsense")]
check("an unknown level degrades to 'use' rather than widening",
      workspace_level_for(_ws, _other) == "use")
check("the levels are the same two as everywhere else", LEVELS == ("use", "manage"))


def make_user(client, username, *, admin=False):
    client.post("/api/users", json={
        "username": username, "password": f"{username}password1",
        "is_admin": admin, "must_change_password": False,
    })
    return {u["username"]: u["id"] for u in client.get("/api/users").json()}[username]


def login(client, username, password=None):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={
        "username": username, "password": password or f"{username}password1",
    })
    assert r.status_code == 200, r.text
    return r


def settle(client, run_id, seconds=45):
    """Wait for a run to leave the queue, and hand back the row."""
    row = {}
    for _ in range(seconds * 4):
        row = client.get(f"/api/runs/{run_id}").json()
        if row.get("status") not in ("queued", "running"):
            return row
        time.sleep(0.25)
    return row


with TestClient(app) as c:
    # --- the backfill --------------------------------------------------
    con = sqlite3.connect(DB_PATH)
    cols = {r[1] for r in con.execute("PRAGMA table_info(workspaces)")}
    rows = dict(con.execute("SELECT name, owner_id FROM workspaces"))
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    check("migration added workspaces.owner_id", "owner_id" in cols, str(sorted(cols)))
    check("migration created the workspace_access table",
          "workspace_access" in tables, str(sorted(tables))[:200])
    check("both live workspaces were backfilled to the admin (user 1), not left ownerless",
          rows == {"e2e": 1, "aiops-src": 1}, str(rows))

    login(c, "admin", "devpassword123")
    me = c.get("/api/auth/me").json()
    check("the backfilled owner is the account that was already there",
          me["id"] == 1 and me["username"] == "admin", str(me)[:120])
    r = c.get("/api/workspaces")
    listed = {w["name"]: w for w in r.json()} if r.status_code == 200 else {}
    check("the backfilled workspaces are reachable by their new owner",
          set(listed) == {"e2e", "aiops-src"}, r.text[:300])
    check("and reachable as the owner, not as a guest",
          all(w["my_level"] == "owner" for w in listed.values()),
          str([(n, w["my_level"]) for n, w in listed.items()]))
    check("the owner can still read their status",
          c.get(f"/api/workspaces/{listed['e2e']['id']}/status").status_code == 200)

    # --- ownership on a freshly created one -----------------------------
    r = c.post("/api/workspaces", json={"name": "admin-proj", "path": "admin-proj"})
    check("creating a workspace succeeds", r.status_code == 201, f"{r.status_code} {r.text[:300]}")
    admin_ws = r.json() if r.status_code == 201 else {}
    check("and it belongs to whoever created it",
          admin_ws.get("owner_id") == me["id"] and admin_ws.get("my_level") == "owner",
          str(admin_ws)[:200])
    check("with nobody else on it", admin_ws.get("grants") == [], str(admin_ws.get("grants")))

    # --- a stranger, and an administrator who was not granted -----------
    walt = make_user(c, "walt")
    otheradmin = make_user(c, "otheradmin", admin=True)

    login(c, "walt")
    r = c.get("/api/workspaces")
    check("someone else's workspace is invisible, not merely locked",
          r.status_code == 200 and r.json() == [], r.text[:200])
    for label, resp in (
        ("status", c.get(f"/api/workspaces/{admin_ws['id']}/status")),
        ("diff", c.get(f"/api/workspaces/{admin_ws['id']}/diff")),
        ("patch", c.patch(f"/api/workspaces/{admin_ws['id']}", json={"name": "mine now"})),
        ("delete", c.delete(f"/api/workspaces/{admin_ws['id']}")),
    ):
        check(f"an unrelated user gets 404 from {label} — which does not confirm it exists",
              resp.status_code == 404, str(resp.status_code))

    login(c, "otheradmin")
    r = c.get("/api/workspaces")
    check("an admin does NOT see a workspace somebody else registered",
          r.status_code == 200 and r.json() == [], r.text[:300])
    for label, resp in (
        ("status", c.get(f"/api/workspaces/{admin_ws['id']}/status")),
        ("diff", c.get(f"/api/workspaces/{admin_ws['id']}/diff")),
        ("patch", c.patch(f"/api/workspaces/{admin_ws['id']}", json={"description": "x"})),
        ("delete", c.delete(f"/api/workspaces/{admin_ws['id']}")),
    ):
        check(f"an administrator with no grant gets 404 from {label}",
              resp.status_code == 404, str(resp.status_code))
    r = c.post("/api/sessions", json={"provider": "claude", "workspace_id": admin_ws["id"]})
    check("an administrator cannot point a session at it either",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    check("and the refusal does not confirm whose it is",
          "shared with you" in r.text and "admin" not in r.text, r.text[:200])

    # --- a workspace of one's own ---------------------------------------
    login(c, "walt")
    r = c.post("/api/workspaces", json={"name": "walt-proj", "path": "walt-proj"})
    check("a non-admin can register their own workspace",
          r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    walt_ws = r.json() if r.status_code == 201 else {}
    check("and owns what they created", walt_ws.get("my_level") == "owner")
    r = c.post("/api/sessions", json={"provider": "claude", "workspace_id": walt_ws["id"]})
    check("the owner can point a session at it", r.status_code == 201, r.text[:200])

    # --- 'use' -----------------------------------------------------------
    r = c.patch(f"/api/workspaces/{walt_ws['id']}",
                json={"grants": [{"user_id": otheradmin, "level": "use"}]})
    check("the owner can share it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    login(c, "otheradmin")
    r = c.get("/api/workspaces")
    shared = [w for w in r.json() if w["id"] == walt_ws["id"]]
    check("a shared workspace appears for the grantee", len(shared) == 1, r.text[:200])
    check("with the level it was granted at", shared and shared[0]["my_level"] == "use")
    r = c.post("/api/sessions", json={"provider": "claude", "workspace_id": walt_ws["id"]})
    check("'use' can select it for a session", r.status_code == 201, r.text[:200])
    r = c.patch(f"/api/workspaces/{walt_ws['id']}", json={"description": "mine"})
    check("but 'use' cannot change it", r.status_code == 403, str(r.status_code))
    r = c.patch(f"/api/workspaces/{walt_ws['id']}",
                json={"grants": [{"user_id": walt, "level": "manage"}]})
    check("and 'use' cannot grant it onward", r.status_code == 403, str(r.status_code))
    r = c.delete(f"/api/workspaces/{walt_ws['id']}")
    check("and 'use' cannot delete it", r.status_code == 403, str(r.status_code))
    check("'use' can still read the status of what it may work in",
          c.get(f"/api/workspaces/{walt_ws['id']}/status").status_code == 200)

    # --- 'manage' ---------------------------------------------------------
    login(c, "walt")
    c.patch(f"/api/workspaces/{walt_ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    login(c, "otheradmin")
    r = c.patch(f"/api/workspaces/{walt_ws['id']}", json={"description": "managed"})
    check("'manage' can change it", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/workspaces/{walt_ws['id']}", json={"owner_id": otheradmin})
    check("but only the owner can hand it over", r.status_code == 403, str(r.status_code))
    r = c.patch(f"/api/workspaces/{walt_ws['id']}",
                json={"grants": [{"user_id": otheradmin, "level": "wizard"}]})
    check("an unknown access level is refused", r.status_code == 400, str(r.status_code))

    # --- session create and patch both refuse what you cannot use ---------
    login(c, "walt")
    r = c.post("/api/sessions", json={"provider": "claude", "workspace_id": admin_ws["id"]})
    check("session creation refuses a workspace you cannot use",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    plain = c.post("/api/sessions", json={"provider": "claude"})
    check("a session with no workspace is still fine", plain.status_code == 201, plain.text[:200])
    r = c.patch(f"/api/sessions/{plain.json()['id']}", json={"workspace_id": admin_ws["id"]})
    check("and so does re-pointing an existing session at one",
          r.status_code == 400, f"{r.status_code} {r.text[:200]}")
    r = c.patch(f"/api/sessions/{plain.json()['id']}", json={"workspace_id": walt_ws["id"]})
    check("while re-pointing at one you own is allowed", r.status_code == 200, r.text[:200])
    r = c.post("/api/sessions", json={"provider": "claude", "workspace_id": 9999})
    check("a workspace id that does not exist is refused the same way",
          r.status_code == 400, str(r.status_code))

    # --- at run time: the turn's requester, not the session's owner -------
    # walt owns the workspace and shares the *session* with the admin. The
    # session being shared must not hand the workspace over with it.
    sess = c.post("/api/sessions", json={
        "provider": "claude", "workspace_id": walt_ws["id"], "approval_mode": "bypass",
    }).json()
    c.patch(f"/api/sessions/{sess['id']}", json={"shared_user_ids": [otheradmin]})

    mine = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "the owner's turn"})
    check("the owner can send a turn into their own workspace",
          mine.status_code == 202, f"{mine.status_code} {mine.text[:200]}")
    owner_row = settle(c, mine.json()["id"])
    # There is no `claude` on PATH in the test environment, so this fails — but
    # it has to fail for that reason and not for a workspace one, which is what
    # proves the check let the owner through.
    check("and it is not refused for a workspace reason",
          "workspace" not in (owner_row.get("error") or "").lower()
          or "missing" in (owner_row.get("error") or "").lower(),
          str(owner_row.get("error"))[:200])

    login(c, "admin", "devpassword123")
    r = c.post("/api/workspaces", json={"name": "admin-second", "path": "admin-second"})
    check("(setup) the admin has a workspace of their own", r.status_code == 201, r.text[:200])

    login(c, "otheradmin")
    theirs = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "a guest's turn"})
    check("a sharee can still queue a turn into a session they were let into",
          theirs.status_code == 202, f"{theirs.status_code} {theirs.text[:200]}")
    guest_row = settle(c, theirs.json()["id"])
    check("but the turn fails, because the workspace is not theirs",
          guest_row.get("status") == "failed", str(guest_row)[:200])
    check("and it says so, naming the workspace and the rule",
          "walt-proj" in (guest_row.get("error") or "")
          and "workspace" in (guest_row.get("error") or "").lower(),
          str(guest_row.get("error"))[:300])
    check("it did not quietly run somewhere else instead",
          "root" not in (guest_row.get("error") or "").lower(),
          str(guest_row.get("error"))[:200])

    # Granted, the same turn goes through — the rule is about access, not about
    # who owns the session.
    login(c, "walt")
    c.patch(f"/api/workspaces/{walt_ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "use"}]})
    login(c, "otheradmin")
    allowed = c.post(f"/api/sessions/{sess['id']}/prompt", json={"prompt": "a granted turn"})
    granted_row = settle(c, allowed.json()["id"])
    check("a granted requester's turn is not refused for a workspace reason",
          "not have access to the workspace" not in (granted_row.get("error") or ""),
          str(granted_row.get("error"))[:200])

    # --- offboarding ------------------------------------------------------
    login(c, "walt")
    c.patch(f"/api/workspaces/{walt_ws['id']}",
            json={"grants": [{"user_id": otheradmin, "level": "manage"}]})
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{walt}")
    check("deleting the owner succeeds when a manager can inherit",
          r.status_code == 204, f"{r.status_code} {r.text[:300]}")
    login(c, "otheradmin")
    inherited = [w for w in c.get("/api/workspaces").json() if w["id"] == walt_ws["id"]]
    check("and the workspace is now owned by that manager",
          len(inherited) == 1 and inherited[0]["my_level"] == "owner",
          str(inherited[:1])[:200])

    # With nobody able to inherit, the delete must be refused, not orphan it —
    # a stranded workspace is worse than a hidden one, because sessions still
    # point at it and every turn in one would fail with nobody able to fix it.
    login(c, "admin", "devpassword123")
    lone = make_user(c, "lone")
    login(c, "lone")
    c.post("/api/workspaces", json={"name": "Lone Project", "path": "lone-project"})
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{lone}")
    check("deleting a user who owns an unmanageable workspace is refused",
          r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    check("and the refusal names what is in the way",
          "Lone Project" in r.text, r.text[:250])
    check("so the user still exists",
          any(u["id"] == lone for u in c.get("/api/users").json()))

    # Grants a departing user held elsewhere go with them: SQLite does not
    # enforce ON DELETE and reuses integer ids, so a leftover row is access
    # waiting to be inherited by whoever is created next.
    login(c, "lone")
    c.delete(f"/api/workspaces/"
             f"{[w['id'] for w in c.get('/api/workspaces').json()][0]}")
    login(c, "otheradmin")
    c.patch(f"/api/workspaces/{walt_ws['id']}", json={"grants": [{"user_id": lone, "level": "use"}]})
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{lone}")
    check("a user holding only a grant can be deleted", r.status_code == 204, r.text[:200])
    login(c, "otheradmin")
    left = [w for w in c.get("/api/workspaces").json() if w["id"] == walt_ws["id"]]
    check("and their grant went with them, rather than waiting for the next user",
          left and all(g["user_id"] != lone for g in left[0]["grants"]),
          str(left[:1])[:250])

    # --- the listing stays honest ----------------------------------------
    r = c.get("/api/workspaces")
    check("the list is only what this user may use",
          r.status_code == 200
          and all(w["my_level"] in ("owner", "manage", "use") for w in r.json()),
          r.text[:300])
    c.post("/api/auth/logout")
    check("and nothing is listed without a session at all",
          c.get("/api/workspaces").status_code == 401,
          str(c.get("/api/workspaces").status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
