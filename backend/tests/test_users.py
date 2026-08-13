"""User management, forced password changes, and the schema migration.

The migration test matters most: it simulates a database created *before*
is_admin existed, which is exactly what an upgrade hits in production. Getting
it wrong locks the operator out of the new screens.
"""
import os
import sqlite3
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

DB_PATH = os.path.abspath("./test_users.db")
os.environ["AIOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_USERNAME", "admin")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_COOKIE_SECURE"] = "false"

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- simulate a pre-upgrade database ---------------------------------------
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app.security import hash_password  # noqa: E402

legacy = sqlite3.connect(DB_PATH)
legacy.execute(
    "CREATE TABLE users ("
    " id INTEGER NOT NULL PRIMARY KEY,"
    " username VARCHAR(64) NOT NULL,"
    " password_hash VARCHAR(255) NOT NULL,"
    " created_at TIMESTAMP"
    ")"
)
legacy.execute("CREATE UNIQUE INDEX ix_users_username ON users (username)")
legacy.execute(
    "INSERT INTO users (id, username, password_hash, created_at) VALUES (1, ?, ?, datetime('now'))",
    ("admin", hash_password("devpassword123")),
)
legacy.commit()
cols_before = {r[1] for r in legacy.execute("PRAGMA table_info(users)")}
legacy.close()
check("fixture really is the old schema", "is_admin" not in cols_before, str(sorted(cols_before)))

from app.main import app  # noqa: E402

with TestClient(app) as c:
    # --- migration ---------------------------------------------------
    con = sqlite3.connect(DB_PATH)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    con.close()
    for col in ("is_admin", "must_change_password", "last_login_at"):
        check(f"migration added users.{col}", col in cols, str(sorted(cols)))

    r = c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    check("pre-existing user can still sign in", r.status_code == 200, r.text[:150])
    check(
        "pre-existing user was promoted to admin (not locked out)",
        r.status_code == 200 and r.json()["is_admin"] is True,
        r.text[:200],
    )
    check("last_login_at recorded", bool(r.json().get("last_login_at")), r.text[:200])

    # --- user creation ------------------------------------------------
    r = c.post(
        "/api/users",
        json={"username": "alice", "password": "alicepassword", "is_admin": False,
              "must_change_password": True},
    )
    check("admin can create a user", r.status_code == 201, r.text[:200])
    alice_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/users", json={"username": "alice", "password": "otherpassword"})
    check("duplicate username rejected", r.status_code == 409, str(r.status_code))

    r = c.post("/api/users", json={"username": "bob", "password": "short"})
    check("short password rejected", r.status_code == 422, str(r.status_code))

    r = c.get("/api/users")
    check("user list", r.status_code == 200 and len(r.json()) == 2, r.text[:200])

    # --- self-protection rules ---------------------------------------
    admin_id = 1
    r = c.delete(f"/api/users/{admin_id}")
    check("cannot delete your own account", r.status_code == 409, r.text[:160])

    r = c.patch(f"/api/users/{admin_id}", json={"is_admin": False})
    check("cannot remove your own admin rights", r.status_code == 409, r.text[:160])

    # --- forced password change is enforced server-side ---------------
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"username": "alice", "password": "alicepassword"})
    check("new user can sign in", r.status_code == 200, r.text[:150])
    check("new user is flagged must_change_password",
          r.json().get("must_change_password") is True, r.text[:200])

    r = c.get("/api/sessions")
    check("forced change blocks the rest of the API", r.status_code == 403, str(r.status_code))
    r = c.get("/api/auth/me")
    check("but /api/auth/* stays reachable", r.status_code == 200, str(r.status_code))

    r = c.post("/api/users", json={"username": "mallory", "password": "mallorypassword"})
    check("non-admin cannot create users", r.status_code == 403, str(r.status_code))

    r = c.post(
        "/api/auth/password",
        json={"current_password": "alicepassword", "new_password": "alicepassword"},
    )
    check("new password must differ from old", r.status_code == 400, r.text[:160])

    r = c.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "brandnewpassword"},
    )
    check("wrong current password rejected", r.status_code == 400, r.text[:160])

    r = c.post(
        "/api/auth/password",
        json={"current_password": "alicepassword", "new_password": "brandnewpassword"},
    )
    check("password change accepted", r.status_code == 204, r.text[:160])

    r = c.get("/api/sessions")
    check("API unblocked after the change", r.status_code == 200, str(r.status_code))
    r = c.get("/api/auth/me")
    check("flag cleared", r.json().get("must_change_password") is False, r.text[:200])

    # --- non-admin is still locked out of admin surfaces --------------
    r = c.get("/api/users")
    check("non-admin cannot list users", r.status_code == 403, str(r.status_code))
    r = c.post("/api/providers/claude/login")
    check("non-admin cannot start a provider sign-in", r.status_code == 403, str(r.status_code))

    # --- last-admin protection ----------------------------------------
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    r = c.patch(f"/api/users/{alice_id}", json={"is_admin": True})
    check("promote alice", r.status_code == 200 and r.json()["is_admin"], r.text[:160])
    r = c.patch(f"/api/users/{alice_id}", json={"is_admin": False})
    check("demote alice again (two admins, allowed)", r.status_code == 200, r.text[:160])

    r = c.post(f"/api/users/{alice_id}/password",
               json={"new_password": "resetpassword1", "must_change_password": True})
    check("admin can reset another user's password", r.status_code == 204, r.text[:160])

    r = c.delete(f"/api/users/{alice_id}")
    check("admin can delete a user", r.status_code == 204, r.text[:160])

    # --- provider login flow surface ----------------------------------
    r = c.get("/api/providers/claude/login")
    check("login status is idle before starting", r.json().get("status") == "idle", r.text[:160])
    r = c.get("/api/providers/nope/login")
    check("unknown provider rejected", r.status_code == 404, str(r.status_code))
    r = c.post("/api/providers/claude/login/code", json={"code": "abc"})
    check("submitting a code with no flow is a clean 400", r.status_code == 400, r.text[:160])
    r = c.post("/api/providers/claude/login")
    check(
        "starting a sign-in with no CLI installed fails cleanly",
        r.status_code == 400 and "not installed" in r.text.lower(),
        f"{r.status_code} {r.text[:160]}",
    )

    # --- skills / slash command discovery -----------------------------
    ws = c.post("/api/workspaces", json={"name": "skilldemo", "path": "skilldemo"}).json()
    sess = c.post(
        "/api/sessions", json={"provider": "claude", "workspace_id": ws["id"]}
    ).json()
    skill_dir = os.path.join(ws["path"], ".claude", "skills", "release-notes")
    os.makedirs(skill_dir, exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: release-notes\ndescription: Draft release notes\n---\n\n# Notes\n")
    cmd_dir = os.path.join(ws["path"], ".claude", "commands")
    os.makedirs(cmd_dir, exist_ok=True)
    with open(os.path.join(cmd_dir, "deploy.md"), "w", encoding="utf-8") as fh:
        fh.write("Ship the current branch\n")

    caps = c.get(f"/api/sessions/{sess['id']}/capabilities").json()
    by_name = {x["name"]: x for x in caps}
    check("workspace skill discovered", "release-notes" in by_name, str(sorted(by_name))[:200])
    check(
        "skill description read from frontmatter",
        by_name.get("release-notes", {}).get("description") == "Draft release notes",
        str(by_name.get("release-notes")),
    )
    check("workspace command discovered", "deploy" in by_name, str(sorted(by_name))[:200])
    check("built-in /goal offered", "goal" in by_name, str(sorted(by_name))[:200])

    # --- sign-in throttling -------------------------------------------
    c.post("/api/auth/logout")
    codes = []
    for _ in range(7):
        codes.append(
            c.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code
        )
    check("repeated bad passwords eventually lock out", 429 in codes, str(codes))
    check("lockout kicks in after a few tries, not immediately",
          codes[0] == 401 and codes.count(401) >= 3, str(codes))
    r = c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    check("correct password is refused while locked out", r.status_code == 429, str(r.status_code))
    check("Retry-After advertised", "retry-after" in {k.lower() for k in r.headers}, str(dict(r.headers))[:200])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
