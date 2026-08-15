"""User management, display names, forced password changes, and the migration.

The migration test matters most: it simulates a database created *before*
is_admin existed, which is exactly what an upgrade hits in production. Getting
it wrong locks the operator out of the new screens. `display_name` rides the
same fixture, so the additive column is proved against a database that has
never seen it rather than against one `create_all` has just built.
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
from app.models import User as UserModel  # noqa: E402
from app.names import (  # noqa: E402
    MAX_DISPLAY_NAME,
    clean_display_name,
    display_name,
    summarise,
)

# --- the resolver itself ----------------------------------------------------
# Exercised directly, not only through a route. It is the one place the
# fallback rule is written down, so it is the one place worth pinning: every
# screen in the product reads a name through this function, and a change here
# that only the routes covered would pass its tests while renaming everybody.
check("display name wins when set",
      display_name(UserModel(username="wsmith", display_name="Walt")) == "Walt")
check("username is the fallback when unset",
      display_name(UserModel(username="wsmith", display_name=None)) == "wsmith")
check("a blank display name falls back rather than rendering empty",
      display_name(UserModel(username="wsmith", display_name="   ")) == "wsmith")
check("nobody is still named something",
      display_name(None) == "someone")
# Not unique, deliberately. Two Walts is the case the username disambiguates.
check("two users may share a display name",
      display_name(UserModel(username="wsmith", display_name="Walt"))
      == display_name(UserModel(username="wjones", display_name="Walt")))
check("whitespace-only normalises to null, not to a blank string",
      clean_display_name("   ") is None, repr(clean_display_name("   ")))
check("None stays None", clean_display_name(None) is None)
check("a name is trimmed", clean_display_name("  Walt  ") == "Walt")
check("an over-long name is truncated rather than stored whole",
      clean_display_name("W" * 400) == "W" * MAX_DISPLAY_NAME,
      str(len(clean_display_name("W" * 400))))
_summary = summarise(UserModel(id=7, username="wsmith", display_name="Walt"))
check("UserSummary carries both names",
      (_summary.id, _summary.username, _summary.display_name) == (7, "wsmith", "Walt"),
      str(_summary))

with TestClient(app) as c:
    # --- migration ---------------------------------------------------
    con = sqlite3.connect(DB_PATH)
    cols = {r[1] for r in con.execute("PRAGMA table_info(users)")}
    con.close()
    for col in ("is_admin", "must_change_password", "last_login_at", "display_name"):
        check(f"migration added users.{col}", col in cols, str(sorted(cols)))
    # Additive means additive: the pre-existing row keeps its name and gains a
    # null, and nothing backfills it — null already means "call them admin".
    con = sqlite3.connect(DB_PATH)
    legacy_row = con.execute("SELECT username, display_name FROM users WHERE id = 1").fetchone()
    con.close()
    check("migration left the existing user's display name null (no backfill)",
          legacy_row == ("admin", None), str(legacy_row))

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

    # --- display names, as an administrator ---------------------------
    check("a user created without one has a null display name",
          r.status_code == 200 and all(u["display_name"] is None for u in r.json()),
          str([u.get("display_name") for u in r.json()]))

    r = c.post("/api/users", json={"username": "wsmith", "password": "wsmithpassword",
                                   "display_name": "Walt"})
    check("admin can create a user with a display name",
          r.status_code == 201 and r.json()["display_name"] == "Walt", r.text[:200])
    walt_id = r.json()["id"] if r.status_code == 201 else None

    # The whole reason the username stays the unique thing.
    r = c.post("/api/users", json={"username": "wjones", "password": "wjonespassword",
                                   "display_name": "Walt"})
    check("a second user may have the same display name",
          r.status_code == 201 and r.json()["display_name"] == "Walt", r.text[:200])
    walt2_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/users", json={"username": "wsmith", "password": "anotherpassword",
                                   "display_name": "Someone Else"})
    check("but the username is still unique", r.status_code == 409, str(r.status_code))

    r = c.post("/api/users", json={"username": "blankname", "password": "blanknamepass",
                                   "display_name": "   "})
    check("a whitespace-only display name is stored as null, not as a gap",
          r.status_code == 201 and r.json()["display_name"] is None, r.text[:200])
    blank_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/users", json={"username": "toolong", "password": "toolongpassword",
                                   "display_name": "W" * 129})
    check("an over-long display name is rejected at the edge",
          r.status_code == 422, str(r.status_code))

    # UserSummary is the shape every screen that names other people reads, and
    # it has to carry the display name or the resolver on the client has
    # nothing to resolve.
    r = c.get("/api/users/directory")
    dirn = {u["username"]: u for u in r.json()} if r.status_code == 200 else {}
    check("the directory carries display names",
          dirn.get("wsmith", {}).get("display_name") == "Walt", str(dirn.get("wsmith")))
    check("the directory carries the username too, for telling two Walts apart",
          {dirn.get("wsmith", {}).get("username"), dirn.get("wjones", {}).get("username")}
          == {"wsmith", "wjones"}, str(sorted(dirn)))
    check("the directory exposes nothing else about a person",
          all(set(u) == {"id", "username", "display_name"} for u in r.json()),
          str(r.json()[:2]))

    r = c.patch(f"/api/users/{walt2_id}", json={"display_name": "Walt J"})
    check("admin can set somebody else's display name",
          r.status_code == 200 and r.json()["display_name"] == "Walt J", r.text[:200])
    r = c.patch(f"/api/users/{walt2_id}", json={"display_name": "  "})
    check("admin clearing it with blanks stores null, not a blank",
          r.status_code == 200 and r.json()["display_name"] is None, r.text[:200])
    r = c.patch(f"/api/users/{walt_id}", json={"display_name": None})
    check("admin can clear a display name explicitly",
          r.status_code == 200 and r.json()["display_name"] is None, r.text[:200])
    # Not mentioning the field is not the same as sending null.
    r = c.patch(f"/api/users/{walt2_id}", json={"display_name": "Walt J"})
    r = c.patch(f"/api/users/{walt2_id}", json={"must_change_password": False})
    check("a patch that does not mention the name leaves it alone",
          r.status_code == 200 and r.json()["display_name"] == "Walt J", r.text[:200])

    for uid in (walt_id, walt2_id, blank_id):
        if uid:
            c.delete(f"/api/users/{uid}")

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

    # --- a display name is yours to set; everyone else's is not -------
    # Two permissions on one column. Deciding what you are called is
    # self-service; deciding what somebody *else* is called is an admin act.
    # Folding them into one route is how a non-admin reaches a PATCH that also
    # carries `is_admin`, so the split is the thing being tested.
    r = c.patch("/api/users/me", json={"display_name": "Alice A"})
    check("a non-admin can set their own display name",
          r.status_code == 200 and r.json()["display_name"] == "Alice A", r.text[:200])
    r = c.get("/api/auth/me")
    check("and it comes back on the session",
          r.json().get("display_name") == "Alice A", r.text[:200])

    r = c.patch("/api/users/me", json={"display_name": "   "})
    check("blanking your own name clears it rather than leaving a gap",
          r.status_code == 200 and r.json()["display_name"] is None, r.text[:200])
    r = c.patch("/api/users/me", json={"display_name": "Alice A"})

    # "me" must not be swallowed by the admin `/{user_id}` route, which takes
    # an int and would 422 on it before this route was ever considered.
    check("the self route is not shadowed by the admin one", r.status_code == 200,
          f"{r.status_code} {r.text[:160]}")

    r = c.patch("/api/users/me", json={"display_name": "Alice", "is_admin": True})
    check("the self route cannot be used to grant yourself admin",
          r.status_code == 200 and r.json()["is_admin"] is False, r.text[:200])
    r = c.get("/api/auth/me")
    check("and the admin flag really did not move",
          r.json().get("is_admin") is False, r.text[:200])

    r = c.patch("/api/users/me", json={"display_name": "A" * 129})
    check("your own name is length-checked too", r.status_code == 422, str(r.status_code))

    r = c.patch(f"/api/users/{admin_id}", json={"display_name": "Pwned"})
    check("a non-admin cannot rename somebody else", r.status_code == 403, str(r.status_code))

    # --- non-admin is still locked out of admin surfaces --------------
    r = c.get("/api/users")
    check("non-admin cannot list users", r.status_code == 403, str(r.status_code))
    r = c.post("/api/accounts", json={"name": "Sneaky", "provider": "claude"})
    check("non-admin cannot create accounts", r.status_code == 403, str(r.status_code))
    r = c.get("/api/accounts")
    check("but everyone can see which accounts exist", r.status_code == 200, str(r.status_code))

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
    acct = c.post("/api/accounts", json={"name": "Test Claude", "provider": "claude"})
    check("admin can create a named account", acct.status_code == 201, acct.text[:220])
    aid = acct.json()["id"] if acct.status_code == 201 else None
    check("account gets an isolated credential dir",
          aid and "claude-test-claude" in acct.json()["config_dir"], str(acct.json().get("config_dir")))
    check("new account starts signed out", acct.json().get("signed_in") in (False, None),
          str(acct.json().get("signed_in")))

    r = c.get(f"/api/accounts/{aid}/login")
    check("login status is idle before starting", r.json().get("status") == "idle", r.text[:160])
    r = c.get("/api/accounts/999999/login")
    check("unknown account rejected", r.status_code == 404, str(r.status_code))
    r = c.post(f"/api/accounts/{aid}/login/code", json={"code": "abc"})
    check("submitting a code with no flow is a clean 400", r.status_code == 400, r.text[:160])
    r = c.post(f"/api/accounts/{aid}/login")
    check(
        "starting a sign-in with no CLI installed fails cleanly",
        r.status_code == 400 and "not installed" in r.text.lower(),
        f"{r.status_code} {r.text[:160]}",
    )
    r = c.patch(f"/api/accounts/{aid}", json={"fallback_account_id": aid})
    check("an account cannot fall back to itself", r.status_code == 400, r.text[:160])

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
