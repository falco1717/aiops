"""Covers who can see a session, and what that lets them do.

Sessions used to be visible to everyone signed in, so this suite is written as
the inverse of that: for every endpoint that reads or drives a conversation, a
user who was not let in must get a 404. The approval checks are the ones with
teeth — answering an approval runs a command on this server, so an approval that
leaks past session visibility is a privilege escalation, not an information leak.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-teams.db")
# Read back rather than assumed: the suite pokes the database directly to plant
# an approval, and the runner may have pointed the app at a different file.
DB_FILE = os.environ["AIOPS_DATABASE_URL"].split("///", 1)[-1]
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-teams-test-"))
os.environ.setdefault(
    "AIOPS_WORKSPACE_ROOT", os.path.join(os.getcwd(), ".test-team-workspaces")
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def login(client, username, password):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def make_user(client, username):
    """Create a user who can use the API immediately, and return their id."""
    r = client.post("/api/users", json={
        "username": username, "password": f"{username}password1",
        "is_admin": False, "must_change_password": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def plant_approval(session_id: str, run_id: int) -> int:
    """A pending approval on a real run, without waiting for a real agent.

    Written straight to the database rather than through the broker: the broker
    holds a future in memory for a process that would have to exist, while the
    row is what the API reads and what the decide endpoint must refuse to answer
    for the wrong person.
    """
    con = sqlite3.connect(DB_FILE)
    try:
        cursor = con.execute(
            "INSERT INTO approvals"
            " (run_id, session_id, provider, kind, tool_name, summary, status, created_at)"
            " VALUES (?, ?, 'claude', 'exec', 'Bash', 'rm -rf /', 'pending', ?)",
            (run_id, session_id, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def rows_for_user(table: str, user_id: int) -> int:
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (user_id,)).fetchone()[0]
    finally:
        con.close()


with TestClient(app) as c:
    login(c, "admin", "devpassword123")
    walt = make_user(c, "walt")
    nina = make_user(c, "nina")
    otheradmin = c.post("/api/users", json={
        "username": "otheradmin", "password": "otheradminpassword1",
        "is_admin": True, "must_change_password": False,
    }).json()["id"]

    # --- a session belongs to whoever made it -----------------------------
    login(c, "walt", "waltpassword1")
    r = c.post("/api/sessions", json={"provider": "claude", "title": "Walt's work"})
    check("a user can create a session", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    session = r.json()
    sid = session["id"]
    check("and owns it", session["owner_id"] == walt, str(session.get("owner_id")))
    check("with nobody else on it", session["shared_user_ids"] == []
          and session["team_id"] is None, str(session)[:200])

    # A run to hang an approval off, and a file to try to read.
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "do a thing"})
    run_id = c.get(f"/api/sessions/{sid}/runs").json()[0]["id"]
    upload = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("notes.txt", b"walt's notes", "text/plain")},
    )
    check("the owner can attach a file", upload.status_code == 201, upload.text[:200])
    attachment_id = upload.json()["id"]

    # --- another user sees nothing of it ----------------------------------
    login(c, "nina", "ninapassword1")
    r = c.get("/api/sessions")
    check("someone else's session is not in the list",
          r.status_code == 200 and all(s["id"] != sid for s in r.json()), r.text[:200])

    blocked = [
        ("GET", f"/api/sessions/{sid}", None),
        ("GET", f"/api/sessions/{sid}/transcript", None),
        ("GET", f"/api/sessions/{sid}/runs", None),
        ("GET", f"/api/sessions/{sid}/events", None),
        ("GET", f"/api/sessions/{sid}/capabilities", None),
        ("GET", f"/api/sessions/{sid}/files", None),
        ("GET", f"/api/sessions/{sid}/files/download?path=notes.txt", None),
        ("GET", f"/api/sessions/{sid}/attachments", None),
        ("GET", f"/api/sessions/{sid}/attachments/{attachment_id}/download", None),
        ("DELETE", f"/api/sessions/{sid}/attachments/{attachment_id}", None),
        ("GET", f"/api/sessions/{sid}/events/1/raw", None),
        ("GET", f"/api/usage/session/{sid}", None),
        ("POST", f"/api/sessions/{sid}/prompt", {"prompt": "run this for me"}),
        ("PATCH", f"/api/sessions/{sid}", {"title": "mine now"}),
        ("DELETE", f"/api/sessions/{sid}", None),
    ]
    for method, path, payload in blocked:
        r = c.request(method, path, json=payload)
        check(f"{method} {path.split(sid)[-1] or '(the session)'} is 404 for an outsider",
              r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    r = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("theirs.txt", b"x", "text/plain")},
    )
    check("an outsider cannot upload into it either", r.status_code == 404, str(r.status_code))
    r = c.get(f"/api/runs/{run_id}")
    check("nor read the run that carries the prompt", r.status_code == 404, str(r.status_code))
    r = c.post(f"/api/runs/{run_id}/cancel")
    check("nor cancel it", r.status_code == 404, str(r.status_code))
    r = c.get("/api/runs")
    check("and it is absent from the run list",
          r.status_code == 200 and all(row["id"] != run_id for row in r.json()), r.text[:200])

    # --- approvals follow the same rule -----------------------------------
    approval_id = plant_approval(sid, run_id)

    r = c.get("/api/approvals")
    check("an outsider's approval list is empty",
          r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.get("/api/approvals", params={"session_id": sid})
    check("even when they ask for that session by id",
          r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.post(f"/api/approvals/{approval_id}/decide", json={"allowed": True})
    check("and deciding it is 404, not a command run on their say-so",
          r.status_code == 404, f"{r.status_code} {r.text[:160]}")

    login(c, "walt", "waltpassword1")
    r = c.get("/api/approvals", params={"session_id": sid})
    check("the owner does see the approval", r.status_code == 200 and len(r.json()) == 1,
          r.text[:200])

    # --- direct sharing ---------------------------------------------------
    r = c.patch(f"/api/sessions/{sid}", json={"shared_user_ids": [nina]})
    check("the owner can share it by name", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("and the session says who with",
          r.status_code == 200 and r.json()["shared_user_ids"] == [nina], r.text[:200])

    login(c, "nina", "ninapassword1")
    r = c.get("/api/sessions")
    check("a shared session appears in the list",
          r.status_code == 200 and any(s["id"] == sid for s in r.json()), r.text[:200])
    check("and is readable", c.get(f"/api/sessions/{sid}").status_code == 200)
    check("including the transcript",
          c.get(f"/api/sessions/{sid}/transcript").status_code == 200)
    check("and the attachment",
          c.get(f"/api/sessions/{sid}/attachments/{attachment_id}/download").status_code == 200)
    r = c.get("/api/approvals", params={"session_id": sid})
    check("a sharee can see the pending approval",
          r.status_code == 200 and len(r.json()) == 1, r.text[:200])
    r = c.delete(f"/api/sessions/{sid}")
    check("but cannot delete somebody else's session", r.status_code == 403, str(r.status_code))
    r = c.patch(f"/api/sessions/{sid}", json={"shared_user_ids": []})
    check("nor share it onward", r.status_code == 403, str(r.status_code))

    login(c, "walt", "waltpassword1")
    c.patch(f"/api/sessions/{sid}", json={"shared_user_ids": []})
    login(c, "nina", "ninapassword1")
    r = c.get(f"/api/sessions/{sid}")
    check("unsharing takes the access away again", r.status_code == 404, str(r.status_code))
    r = c.get("/api/approvals")
    check("and the approval with it", r.status_code == 200 and r.json() == [], r.text[:200])

    # --- teams ------------------------------------------------------------
    login(c, "walt", "waltpassword1")
    r = c.post("/api/teams", json={"name": "Platform", "member_ids": [walt]})
    check("a non-admin cannot create a team", r.status_code == 403, str(r.status_code))

    login(c, "admin", "devpassword123")
    r = c.post("/api/teams", json={
        "name": "Platform", "description": "the on-call crew", "member_ids": [walt, nina],
    })
    check("an admin can create a team", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    team = r.json()
    team_id = team["id"]
    check("with the members it was given", sorted(team["member_ids"]) == sorted([walt, nina]),
          str(team["member_ids"]))
    r = c.post("/api/teams", json={"name": "Platform"})
    check("a duplicate team name is refused", r.status_code == 409, str(r.status_code))

    login(c, "walt", "waltpassword1")
    r = c.get("/api/teams")
    check("a member can list the teams they are in",
          r.status_code == 200 and [t["id"] for t in r.json()] == [team_id], r.text[:200])
    r = c.patch(f"/api/sessions/{sid}", json={"team_id": team_id})
    check("and put a session into one", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    login(c, "nina", "ninapassword1")
    r = c.get("/api/sessions")
    check("a team session is visible to another member",
          r.status_code == 200 and any(s["id"] == sid for s in r.json()), r.text[:200])
    check("and readable", c.get(f"/api/sessions/{sid}/transcript").status_code == 200)
    r = c.get("/api/approvals", params={"session_id": sid})
    check("a team member can answer its approvals",
          r.status_code == 200 and len(r.json()) == 1, r.text[:200])

    # Somebody outside the team is still outside.
    login(c, "admin", "devpassword123")
    ivan = make_user(c, "ivan")
    login(c, "ivan", "ivanpassword1")
    r = c.get(f"/api/sessions/{sid}")
    check("a non-member sees nothing of a team session", r.status_code == 404, str(r.status_code))
    r = c.get("/api/teams")
    check("and lists no teams", r.status_code == 200 and r.json() == [], r.text[:200])

    login(c, "walt", "waltpassword1")
    r = c.patch(f"/api/sessions/{sid}", json={"team_id": team_id + 999})
    check("a session cannot be put in a team that does not exist",
          r.status_code == 400, str(r.status_code))

    login(c, "ivan", "ivanpassword1")
    r = c.post("/api/sessions", json={"provider": "claude", "team_id": team_id})
    check("nor into one you are not a member of", r.status_code == 403,
          f"{r.status_code} {r.text[:160]}")

    # Removing someone from the team removes their access with it.
    login(c, "admin", "devpassword123")
    r = c.patch(f"/api/teams/{team_id}", json={"member_ids": [walt]})
    check("an admin can change who is in a team", r.status_code == 200, r.text[:200])
    login(c, "nina", "ninapassword1")
    r = c.get(f"/api/sessions/{sid}")
    check("leaving the team ends the access", r.status_code == 404, str(r.status_code))
    r = c.get("/api/approvals")
    check("including the approval it carried", r.status_code == 200 and r.json() == [],
          r.text[:200])

    # --- admins keep visibility, unlike stored systems ---------------------
    login(c, "otheradmin", "otheradminpassword1")
    r = c.get("/api/sessions")
    check("an admin sees a session they were never given",
          r.status_code == 200 and any(s["id"] == sid for s in r.json()), r.text[:200])
    check("and can read it", c.get(f"/api/sessions/{sid}/transcript").status_code == 200)
    r = c.get("/api/approvals", params={"session_id": sid})
    check("an admin can unstick a paused agent",
          r.status_code == 200 and len(r.json()) == 1, r.text[:200])

    # --- ownership transfer ------------------------------------------------
    login(c, "walt", "waltpassword1")
    r = c.patch(f"/api/sessions/{sid}", json={"owner_id": 10_000})
    check("handing a session to a user who does not exist is refused",
          r.status_code == 400, str(r.status_code))
    r = c.patch(f"/api/sessions/{sid}", json={"owner_id": nina, "team_id": None})
    check("the owner can hand it over", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("and the session says so", r.status_code == 200 and r.json()["owner_id"] == nina,
          r.text[:200])

    r = c.get(f"/api/sessions/{sid}")
    check("the previous owner has lost it", r.status_code == 404, str(r.status_code))
    login(c, "nina", "ninapassword1")
    r = c.get(f"/api/sessions/{sid}")
    check("and the new owner has it", r.status_code == 200, str(r.status_code))
    r = c.patch(f"/api/sessions/{sid}", json={"shared_user_ids": [walt]})
    check("who can now share it themselves", r.status_code == 200, r.text[:200])
    login(c, "walt", "waltpassword1")
    check("giving the old owner access back",
          c.get(f"/api/sessions/{sid}").status_code == 200)

    # --- deleting a team leaves its sessions with their owner --------------
    login(c, "nina", "ninapassword1")
    r = c.post("/api/sessions", json={"provider": "claude", "title": "team work"})
    team_session = r.json()["id"]
    login(c, "admin", "devpassword123")
    c.patch(f"/api/teams/{team_id}", json={"member_ids": [walt, nina]})
    login(c, "nina", "ninapassword1")
    c.patch(f"/api/sessions/{team_session}", json={"team_id": team_id})
    login(c, "walt", "waltpassword1")
    check("a fresh team session is visible to the other member",
          c.get(f"/api/sessions/{team_session}").status_code == 200)

    login(c, "admin", "devpassword123")
    r = c.get("/api/teams")
    listed = next(t for t in r.json() if t["id"] == team_id)
    check("the team reports how many sessions it holds", listed["session_count"] == 1,
          str(listed))
    r = c.delete(f"/api/teams/{team_id}")
    check("an admin can delete a team", r.status_code == 204, str(r.status_code))
    login(c, "walt", "waltpassword1")
    r = c.get(f"/api/sessions/{team_session}")
    check("its sessions survive but are private again", r.status_code == 404,
          str(r.status_code))
    login(c, "nina", "ninapassword1")
    check("still owned by whoever made them",
          c.get(f"/api/sessions/{team_session}").status_code == 200)

    # --- deleting a user must not leave a grant behind ---------------------
    login(c, "admin", "devpassword123")
    c.post("/api/teams", json={"name": "Ops", "member_ids": [walt, nina]})
    check("the departing user is in a team and holds a share",
          rows_for_user("team_members", walt) == 1 and rows_for_user("session_shares", walt) == 1)
    r = c.delete(f"/api/users/{walt}")
    check("a user with shared sessions can be deleted", r.status_code == 204,
          f"{r.status_code} {r.text[:200]}")
    check("their shares are gone with them, not left for the next id to inherit",
          rows_for_user("session_shares", walt) == 0)
    check("and their team memberships too", rows_for_user("team_members", walt) == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
