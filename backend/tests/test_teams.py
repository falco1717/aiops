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
import time
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
# A throwaway directory: creating a provider account makes its credential
# directory, and the real one is not something a test should be writing into.
os.environ.setdefault(
    "AIOPS_ACCOUNTS_ROOT", os.path.join(os.getcwd(), ".test-team-accounts")
)

from fastapi.testclient import TestClient  # noqa: E402

from app import browsing  # noqa: E402
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


def rows_for_session(table: str, session_id: str) -> int:
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    finally:
        con.close()


def scalar(sql: str, *params):
    con = sqlite3.connect(DB_FILE)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


def stranded_sessions() -> int:
    """Sessions visible to nobody at all: no owner, no team, no named sharee.

    Asked of the database rather than the API precisely because no user could see
    one to report it — which is the whole problem with leaving one behind.
    """
    return scalar(
        "SELECT COUNT(*) FROM sessions WHERE owner_id IS NULL AND team_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM session_shares s WHERE s.session_id = sessions.id)"
    )


def settle(client, session_id: str) -> None:
    """Wait for a session's turns to stop, so row counts are not a race.

    There is no `claude` on PATH here, so a run fails almost at once — but "almost"
    is not "before the next assertion".
    """
    for _ in range(80):
        rows = client.get(f"/api/sessions/{session_id}/runs")
        if rows.status_code != 200:
            return
        if all(row["status"] not in ("queued", "running") for row in rows.json()):
            return
        time.sleep(0.05)


def ws_close_code(client, query: str):
    """The code the live feed hangs up with, or "accepted" if the socket opened."""
    try:
        with client.websocket_connect(f"/api/ws{query}") as socket:
            socket.receive_json()
            return "accepted"
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "code", f"{type(exc).__name__}: {exc}")


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

    # A screenshot too, stored the way the agent's browser stores one. Planted
    # rather than photographed — there is no Chromium here — but through the
    # same call, so the checks below are about a capture that really exists.
    # Without one, "an outsider gets 404" would pass on a session that had no
    # screenshot to leak in the first place.
    browsing.keep_shot(sid, run_id, "screenshot-001.png", b"\x89PNG\r\n\x1a\n" + os.urandom(512))
    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-001.png")
    check("and see what its browser photographed", r.status_code == 200,
          f"{r.status_code} {r.text[:160]}")

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
    # A screenshot is a photograph of a page the agent was signed in to, so it
    # is as private as the transcript it sits in and follows the same rule. It
    # is kept for the life of the session now rather than for the length of the
    # turn, so this check has something real behind it: the capture is on disk
    # and readable by the owner at this moment.
    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-001.png")
    check("nor look at what its browser photographed", r.status_code == 404, str(r.status_code))
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

    # --- who sent which turn ----------------------------------------------
    # The transcript has to say this per turn, not per session. Nina is reading
    # a conversation Walt started, and once she adds to it the two turns have
    # different senders — which is the whole reason the chat view cannot draw
    # every prompt as "you", and cannot fall back to the session owner either.
    c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "and a thing from nina"})
    settle(c, sid)
    runs = c.get(f"/api/sessions/{sid}/transcript").json()["runs"]
    check("the transcript says who asked for each turn",
          all("requested_by_id" in run for run in runs), str(runs[:1])[:200])
    senders = [run["requested_by_id"] for run in runs]
    check("a sharee's turn is attributed to the sharee, not the owner",
          nina in senders, str(senders))
    check("and the owner's own turn still belongs to the owner",
          walt in senders, str(senders))
    check("two people in one session are two different senders",
          len(set(senders)) == 2, str(senders))

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

    # --- an admin gets nothing they were not given -------------------------
    # Admins used to see every session, so that one abandoned by a user who had
    # left could still be unstuck. On an instance where everybody is an admin
    # that made every session readable by everyone, so it is gone; what replaces
    # it is the departing-user rule further down, which leaves nothing stranded
    # for an admin to need to reach. otheradmin owns nothing here, holds no share
    # and is in no team, so every one of these must read as missing.
    login(c, "otheradmin", "otheradminpassword1")
    r = c.get("/api/sessions")
    check("an admin does not see a session they were never given",
          r.status_code == 200 and all(s["id"] != sid for s in r.json()), r.text[:200])
    for method, path, payload in blocked:
        r = c.request(method, path, json=payload)
        check(f"{method} {path.split(sid)[-1] or '(the session)'} is 404 for an admin too",
              r.status_code == 404, f"{r.status_code} {r.text[:120]}")
    r = c.get("/api/approvals", params={"session_id": sid})
    check("an admin's approval list does not carry it either",
          r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.post(f"/api/approvals/{approval_id}/decide", json={"allowed": True})
    check("and an admin cannot decide it, so no command runs on their say-so",
          r.status_code == 404, f"{r.status_code} {r.text[:160]}")
    r = c.get(f"/api/runs/{run_id}")
    check("nor read the run carrying the prompt", r.status_code == 404, str(r.status_code))
    # Still on disk, still 404: being an administrator is not a way to look at
    # a page somebody else's agent was signed in to.
    r = c.get(f"/api/runs/{run_id}/screenshots/screenshot-001.png")
    check("nor look at what its browser photographed", r.status_code == 404, str(r.status_code))
    r = c.get("/api/runs")
    check("nor find it in the run list",
          r.status_code == 200 and all(row["id"] != run_id for row in r.json()), r.text[:200])
    code = ws_close_code(c, f"?session_id={sid}")
    check("nor watch it live", code == 4404, str(code))

    # The firehose. Omitting session_id used to subscribe an admin to every
    # session at once — the same bypass arriving by a different door, and
    # carrying more, because the live feed includes tool calls as they happen.
    code = ws_close_code(c, "")
    check("an admin cannot subscribe to every session at once", code == 4400, str(code))
    login(c, "walt", "waltpassword1")
    code = ws_close_code(c, "")
    check("and neither can anybody else", code == 4400, str(code))
    code = ws_close_code(c, f"?session_id={sid}")
    check("the owner can still watch their own session", code == "accepted", str(code))

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

    # --- schedules are their author's own ----------------------------------
    # ScheduleOut carries `prompt` and `target_session_id`, so an unscoped list
    # handed every signed-in user the full text of everyone's unattended prompts
    # and the id of the session each one writes into.
    login(c, "nina", "ninapassword1")
    nightly = {
        "name": "nina nightly", "cron": "0 2 * * *", "timezone_name": "UTC",
        "prompt": "nina's private prompt", "provider": "claude", "session_mode": "new",
    }
    r = c.post("/api/schedules", json=nightly)
    check("a user can create a schedule", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    nina_sched = r.json()["id"]
    r = c.get("/api/schedules")
    check("and sees it in their own list",
          r.status_code == 200 and [s["id"] for s in r.json()] == [nina_sched], r.text[:300])

    login(c, "ivan", "ivanpassword1")
    r = c.get("/api/schedules")
    check("somebody else's schedule is not in the list",
          r.status_code == 200 and r.json() == [], r.text[:300])
    r = c.put(f"/api/schedules/{nina_sched}", json=nightly)
    check("nor can they edit it", r.status_code == 404, f"{r.status_code} {r.text[:160]}")
    r = c.post(f"/api/schedules/{nina_sched}/run")
    check("nor fire it, which would run under the author's access",
          r.status_code == 404, f"{r.status_code} {r.text[:160]}")
    r = c.delete(f"/api/schedules/{nina_sched}")
    check("nor delete it", r.status_code == 404, str(r.status_code))

    # Being an admin is not a way in either: a schedule is somebody's prompt and
    # firing one reaches the stored systems only its author can reach.
    login(c, "admin", "devpassword123")
    r = c.get("/api/schedules")
    check("an admin's schedule list does not carry it",
          r.status_code == 200 and all(s["id"] != nina_sched for s in r.json()), r.text[:300])
    r = c.delete(f"/api/schedules/{nina_sched}")
    check("and an admin cannot delete it", r.status_code == 404, str(r.status_code))
    login(c, "nina", "ninapassword1")
    r = c.get("/api/schedules")
    check("so it is still there for its owner",
          r.status_code == 200 and [s["id"] for s in r.json()] == [nina_sched], r.text[:300])

    # --- usage counts only what you can see --------------------------------
    login(c, "admin", "devpassword123")
    dora = make_user(c, "dora")
    ella = make_user(c, "ella")
    night = c.post("/api/teams", json={"name": "Nightshift",
                                       "member_ids": [dora, ella]}).json()["id"]
    r = c.post("/api/accounts", json={"name": "Shared Claude", "provider": "claude"})
    check("an admin can add a provider account", r.status_code == 201, r.text[:200])
    account_id = r.json()["id"]

    login(c, "dora", "dorapassword1")
    lone = c.post("/api/sessions", json={
        "provider": "claude", "title": "nobody else's", "account_id": account_id,
    }).json()["id"]
    c.post(f"/api/sessions/{lone}/prompt", json={"prompt": "a turn to leave behind"})
    settle(c, lone)
    u = c.get("/api/usage").json()
    check("your own turns are counted in your usage",
          max(w["runs"] for w in u["windows"]) >= 1, str(u["windows"])[:300])
    mine = next(a for a in u["by_account"] if a["account_id"] == account_id)
    check("and attributed to the account that served them", mine["runs"] >= 1, str(mine))

    login(c, "ivan", "ivanpassword1")
    u = c.get("/api/usage").json()
    check("somebody else's turns are not counted in yours",
          all(w["runs"] == 0 and w["total_tokens"] == 0 and w["cost_usd"] == 0
              for w in u["windows"]), str(u["windows"])[:300])
    theirs = next(a for a in u["by_account"] if a["account_id"] == account_id)
    # Deliberate: the account roster is instance-level and /api/accounts already
    # shows it to everyone, because you have to see an account to pick one and a
    # limited account is why somebody else's run just failed over onto yours.
    # What was never instance-level is the spend.
    check("the account is still listed, by name and provider",
          theirs["name"] == "Shared Claude" and theirs["provider"] == "claude", str(theirs))
    check("but with none of somebody else's spend on it",
          theirs["runs"] == 0 and theirs["total_tokens"] == 0 and theirs["cost_usd"] == 0,
          str(theirs))

    # --- deleting a user must strand nothing --------------------------------
    # An ownerless session used to be fine because admins saw everything. Now
    # that nobody does, one would be invisible for ever — including one holding a
    # parked agent. So each of the departing user's sessions goes to whoever
    # already had a claim on it, or is destroyed if nobody did.
    login(c, "dora", "dorapassword1")
    shared_on = c.post("/api/sessions", json={"provider": "claude",
                                              "title": "handed on"}).json()["id"]
    c.patch(f"/api/sessions/{shared_on}", json={"shared_user_ids": [ella]})
    teamed = c.post("/api/sessions", json={"provider": "claude", "title": "the team's",
                                           "team_id": night}).json()["id"]
    r = c.post("/api/schedules", json={
        "name": "dora nightly", "cron": "0 3 * * *", "timezone_name": "UTC",
        "prompt": "dora's private prompt", "provider": "claude", "session_mode": "new",
    })
    check("the departing user has a schedule of their own", r.status_code == 201, r.text[:200])
    check("and an unshared session with a turn on it",
          rows_for_session("runs", lone) >= 1, str(rows_for_session("runs", lone)))

    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{dora}")
    check("a user who owns sessions can be deleted", r.status_code == 204,
          f"{r.status_code} {r.text[:200]}")

    login(c, "ella", "ellapassword1")
    r = c.get(f"/api/sessions/{shared_on}")
    check("a shared session goes to the person it was shared with",
          r.status_code == 200 and r.json()["owner_id"] == ella, f"{r.status_code} {r.text[:200]}")
    check("who does not also end up holding a share of their own session",
          r.status_code == 200 and r.json()["shared_user_ids"] == [], r.text[:200])
    r = c.get(f"/api/sessions/{teamed}")
    check("a team's session stays with the team", r.status_code == 200, str(r.status_code))
    check("owned by a member who is still there",
          r.status_code == 200 and r.json()["owner_id"] == ella, r.text[:200])
    r = c.get(f"/api/sessions/{lone}")
    check("a session nobody else could ever see is gone", r.status_code == 404,
          str(r.status_code))
    check("and its runs with it", rows_for_session("runs", lone) == 0,
          str(rows_for_session("runs", lone)))
    check("and its events", rows_for_session("events", lone) == 0,
          str(rows_for_session("events", lone)))
    check("their schedules go with them, not left firing unseen",
          scalar("SELECT COUNT(*) FROM schedules WHERE owner_id = ?", dora) == 0)
    check("and nothing at all is left visible to nobody", stranded_sessions() == 0,
          str(stranded_sessions()))

    # An admin is not a backdoor to any of it: the sessions that survived are the
    # ones somebody else already had, and they are still only that person's.
    login(c, "otheradmin", "otheradminpassword1")
    r = c.get("/api/sessions")
    ids = [s["id"] for s in r.json()]
    check("an admin still sees none of the inherited sessions",
          shared_on not in ids and teamed not in ids, str(ids)[:200])

    # --- a team keeps a session even when the team empties out --------------
    login(c, "admin", "devpassword123")
    frank = make_user(c, "frank")
    solo = c.post("/api/teams", json={"name": "Solo", "member_ids": [frank]}).json()["id"]
    login(c, "frank", "frankpassword1")
    held = c.post("/api/sessions", json={"provider": "claude", "title": "the team's alone",
                                         "team_id": solo}).json()["id"]
    login(c, "admin", "devpassword123")
    r = c.delete(f"/api/users/{frank}")
    check("the only member of a team can be deleted", r.status_code == 204, r.text[:200])
    check("their team session is kept rather than destroyed",
          scalar("SELECT COUNT(*) FROM sessions WHERE id = ? AND owner_id IS NULL "
                 "AND team_id = ?", held, solo) == 1)
    r = c.get(f"/api/sessions/{held}")
    check("an admin outside the team still cannot read it", r.status_code == 404,
          str(r.status_code))
    c.patch(f"/api/teams/{solo}", json={"member_ids": [nina]})
    login(c, "nina", "ninapassword1")
    r = c.get(f"/api/sessions/{held}")
    check("and it comes back to whoever is put in the team", r.status_code == 200,
          str(r.status_code))

# --- the two orphan mechanisms must not fight --------------------------------
# The upgrade path assigns ownerless rows to the first admin, which is right for
# data that predates ownership and wrong for the team session just left
# deliberately ownerless. Booting the app again is what would show them fighting:
# "delete the owner, restart" must not be a way for an admin to end up owning
# somebody else's work.
with TestClient(app) as c:
    check("restarting does not hand the team's session to an admin",
          scalar("SELECT COUNT(*) FROM sessions WHERE id = ? AND owner_id IS NULL "
                 "AND team_id = ?", held, solo) == 1)
    login(c, "nina", "ninapassword1")
    check("and the team can still read it",
          c.get(f"/api/sessions/{held}").status_code == 200)
    login(c, "otheradmin", "otheradminpassword1")
    check("while the admin still cannot",
          c.get(f"/api/sessions/{held}").status_code == 404)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
