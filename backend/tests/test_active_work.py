"""Covers the feed behind the working indicator: /api/runs/active.

The indicator exists because the only place AIOps ever said an agent was working
was inside the transcript of the session you were already looking at, scrolled
to the block of the run that happened to be live. This endpoint answers the same
question from anywhere — so the properties worth proving are about *scope* and
about *honesty*.

Scope, and it is the reason this file is long:

  * a signed-out caller gets nothing;
  * a user sees turns in their own sessions, in sessions shared with them by
    name, and in sessions owned by a team they are in;
  * a user sees nothing at all of a session they are not in;
  * **an administrator sees nothing extra**. That asymmetry is deliberate and
    was asked for explicitly. It is easy to undo by accident — nearly every
    other admin surface in the app widens — so it is checked from both
    directions: an admin outside a session sees none of it, while the ordinary
    people who are in it see all of it.

Honesty:

  * a queued turn is reported as queued, with no start time, no tool count and
    no steps. It has not been handed to an agent, and dressing it up as one that
    is starting would put the same words on two different states;
  * a running turn carries the tail of what it has actually done, so the
    indicator reads a turn off the same events the transcript does rather than
    offering a second opinion about it;
  * that tail is bounded and its text is cut short, because this is polled from
    every screen in the app and one tool result can be enormous;
  * running turns are listed before queued ones, and the queue is in the order
    it will run.

Most of the rows here are written straight into the database rather than driven
through a real turn. That is on purpose: every scope check needs one turn that
stays in flight while four different people look at it, and a real stand-in turn
gives a window of about a second, which is a race rather than a test. The live
turn is still exercised — the first block does that — but only for the things
that need a genuinely running agent.
"""
import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-active-work.db")
DB_FILE = os.environ["AIOPS_DATABASE_URL"].split("///", 1)[-1]
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-work-ws-"))
os.environ.setdefault(
    "AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-work-attach-")
)
os.environ.setdefault(
    "AIOPS_ACCOUNTS_ROOT", tempfile.mkdtemp(prefix="aiops-work-accounts-")
)

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_claude_cli.py")

from app.providers import PROVIDERS  # noqa: E402
from app.providers.base import RunSpec  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402

_original = ClaudeProvider.build_run


def patched(self, **kwargs):
    """The same seam test_queue.py uses: the real argv, with a stand-in binary."""
    spec = _original(self, **kwargs)
    return RunSpec(
        argv=[sys.executable, FAKE, *spec.argv[1:]],
        env=spec.env,
        assigned_session_id=spec.assigned_session_id,
    )


ClaudeProvider.build_run = patched
PROVIDERS["claude"] = ClaudeProvider()

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Event,
    Run,
    Session as SessionRow,
    SessionShare,
    Team,
    TeamMember,
    User as UserRow,
    utcnow,
)
from app.main import app  # noqa: E402
from app.routers.runs import RECENT_STEPS, STEP_TEXT_CHARS  # noqa: E402
from app.security import hash_password  # noqa: E402

failures = []

PASSWORD = "workpassword1"


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


#: The step text written into the parked noisy run, comfortably over the cap so
#: the truncation is actually exercised rather than being a no-op.
LOUD = "x" * (STEP_TEXT_CHARS * 4)
#: More steps than the tail carries, so the bound is exercised too.
LOUD_STEPS = RECENT_STEPS + 15


async def seed():
    """Everything that has to be in the database before the app is talked to.

    Rows first, on their own loop, then HTTP — the order test_browser.py uses,
    and for its reason: the engine is disposed at the end so no pooled
    connection survives into the client's loop. Two event loops sharing one
    aiosqlite connection is the kind of failure that only shows up when
    something is slow.

    Nothing here goes through the runner, so these turns stay queued for the
    length of the run. Each lives in a session of its own so no later prompt can
    dispatch one by accident.
    """
    await init_db()
    async with SessionLocal() as db:
        walt = UserRow(username="walt", display_name="Walt Ops",
                       password_hash=hash_password(PASSWORD))
        carol = UserRow(username="carol", password_hash=hash_password(PASSWORD))
        boss = UserRow(username="boss", password_hash=hash_password(PASSWORD), is_admin=True)
        db.add_all([walt, carol, boss])
        await db.commit()

        team = Team(name="ops")
        db.add(team)
        await db.commit()
        # Walt and Carol only. The administrator is deliberately left out: an
        # admin who happened to be in the team would make the checks below pass
        # for the wrong reason.
        db.add_all([
            TeamMember(team_id=team.id, user_id=walt.id),
            TeamMember(team_id=team.id, user_id=carol.id),
        ])

        private = SessionRow(id="s-private", title="Walt's own work",
                             provider="claude", owner_id=walt.id)
        shared = SessionRow(id="s-shared", title="Shared by name",
                            provider="claude", owner_id=walt.id)
        of_team = SessionRow(id="s-team", title="The team's work",
                             provider="claude", owner_id=walt.id, team_id=team.id)
        loud = SessionRow(id="s-loud", title="A noisy turn",
                          provider="claude", owner_id=walt.id)
        lined = SessionRow(id="s-line", title="In order",
                           provider="claude", owner_id=walt.id)
        db.add_all([private, shared, of_team, loud, lined])
        await db.commit()
        db.add(SessionShare(session_id=shared.id, user_id=carol.id))

        def parked(session_id, prompt, status="queued"):
            run = Run(session_id=session_id, prompt=prompt, provider="claude",
                      requested_by_id=walt.id, status=status)
            if status == "running":
                run.started_at = utcnow()
            db.add(run)
            return run

        alone = parked("s-private", "a turn nobody else can see")
        by_name = parked("s-shared", "a turn shared by name")
        by_team = parked("s-team", "a turn the team can see")
        noisy = parked("s-loud", "a turn that has said a great deal", status="running")
        first = parked("s-line", "first")
        second = parked("s-line", "second")
        third = parked("s-line", "third")
        await db.commit()

        for seq in range(1, LOUD_STEPS + 1):
            db.add(Event(run_id=noisy.id, session_id="s-loud", seq=seq,
                         kind="tool_use", text=LOUD, tool_name="Bash"))
        await db.commit()

        ids = {
            "walt": walt.id,
            "carol": carol.id,
            "boss": boss.id,
            "alone": alone.id,
            "by_name": by_name.id,
            "by_team": by_team.id,
            "noisy": noisy.id,
            "line": [first.id, second.id, third.id],
        }
    await engine.dispose()
    return ids


ID = asyncio.run(seed())


def login(client, username, password=PASSWORD):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def active(client):
    r = client.get("/api/runs/active")
    assert r.status_code == 200, r.text
    return r.json()


def run_ids(rows):
    return sorted(row["run_id"] for row in rows)


def settle(client, sid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = client.get(f"/api/sessions/{sid}/runs").json()
        if not any(r["status"] in ("queued", "running") for r in rows):
            return rows
        time.sleep(0.1)
    return client.get(f"/api/sessions/{sid}/runs").json()


with TestClient(app) as client:
    # --- it is not a public list ---------------------------------------
    client.post("/api/auth/logout")
    check("signed out gets 401", client.get("/api/runs/active").status_code == 401)

    # --- scope, which is the point --------------------------------------
    mine = [ID["alone"], ID["by_name"], ID["by_team"], ID["noisy"], *ID["line"]]
    login(client, "walt")
    check("the owner sees every turn of their own", run_ids(active(client)) == sorted(mine),
          str(run_ids(active(client))))

    login(client, "carol")
    check("a reader sees the sessions they are in and nothing else",
          run_ids(active(client)) == sorted([ID["by_name"], ID["by_team"]]),
          str(run_ids(active(client))))

    # The asymmetry, written down as a test because it is the thing most likely
    # to be "fixed" by somebody adding an admin branch. Administering AIOps is
    # not a way into somebody's conversation, and this list must never become
    # the place that leaks one.
    login(client, "boss")
    check("an administrator sees nothing extra", active(client) == [], str(active(client)))

    login(client, "admin", "devpassword123")
    check("nor does the built-in administrator", active(client) == [], str(active(client)))

    # --- what a row says -------------------------------------------------
    login(client, "carol")
    rows = {row["run_id"]: row for row in active(client)}
    named = rows.get(ID["by_name"])
    check("a row names the conversation, not just its id",
          named and named["session_title"] == "Shared by name", str(named))
    check("a row carries the opening of the message",
          named and named["prompt"] == "a turn shared by name", str(named))
    # Resolved server-side, and to what that person is called rather than to
    # their login: the panel is read from screens with no user directory loaded.
    check("a row says whose turn it is, by display name",
          named and named["requested_by"] == "Walt Ops", str(named))

    # A queued turn has not been handed to an agent. The indicator depends on
    # being able to tell that apart from a turn that has started and not spoken.
    check("a queued turn has no start time", named and named["started_at"] is None, str(named))
    check("a queued turn has done nothing",
          named and named["tools"] == 0 and named["recent"] == [], str(named))

    # --- the tail is bounded, and cut short ------------------------------
    login(client, "walt")
    noisy = {row["run_id"]: row for row in active(client)}[ID["noisy"]]
    check("a running turn is reported as running", noisy["status"] == "running")
    check("only the tail of the run travels",
          len(noisy["recent"]) == RECENT_STEPS, str(len(noisy["recent"])))
    check("and it is the tail, ending on the most recent step",
          noisy["recent"][-1]["seq"] == LOUD_STEPS, str(noisy["recent"][-1]["seq"]))
    check("steps arrive oldest first, the order the transcript reads them in",
          [e["seq"] for e in noisy["recent"]] == sorted(e["seq"] for e in noisy["recent"]))
    check("each step's text is cut short",
          all(len(e["text"]) <= STEP_TEXT_CHARS for e in noisy["recent"]),
          str(max(len(e["text"]) for e in noisy["recent"])))
    check("tool calls are counted over the whole run, not just the tail",
          noisy["tools"] == LOUD_STEPS, str(noisy["tools"]))

    # --- order -----------------------------------------------------------
    listed = [row["run_id"] for row in active(client)]
    check("the running turn comes before the queued ones",
          listed[0] == ID["noisy"], str(listed))
    line = [rid for rid in listed if rid in ID["line"]]
    check("and the queue is in the order it will run", line == ID["line"], str(line))

    # --- a real turn in flight -------------------------------------------
    # Everything above is a fixture. This is the one block that proves the
    # endpoint sees an agent the runner actually started.
    login(client, "admin", "devpassword123")
    r = client.post("/api/sessions", json={
        "title": "Rebuild the index", "provider": "claude", "approval_mode": "bypass",
    })
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    check("admin starts with nothing of their own running", active(client) == [],
          str(active(client)))

    posted = client.post(f"/api/sessions/{sid}/prompt", json={"prompt": "go"})
    check("the prompt was accepted", posted.status_code == 202, posted.text[:120])

    live = []
    deadline = time.time() + 20
    while time.time() < deadline:
        live = active(client)
        # Waiting for it to have *said* something, not merely to exist: the
        # claim being tested is that the endpoint carries what a live agent is
        # doing, and an empty tail would satisfy a weaker check.
        if live and live[0]["status"] == "running" and live[0]["recent"]:
            break
        time.sleep(0.05)

    check("a turn the runner started is listed", len(live) == 1, str(live))
    check("with the steps it has taken so far",
          bool(live) and any(e["kind"] for e in live[0]["recent"]), str(live))
    check("and a start time to measure it from",
          bool(live) and live[0]["started_at"] is not None, str(live))

    settle(client, sid)
    check("a finished turn drops out of the list", active(client) == [], str(active(client)))

    # --- the route is not shadowed by /api/runs/{run_id} ------------------
    check("'active' is a route rather than a bad run id",
          client.get("/api/runs/active").status_code == 200)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("All active-work checks passed.")
