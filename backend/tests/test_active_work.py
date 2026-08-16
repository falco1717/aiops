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

Most of the rows here are parked directly in the database rather than driven
through a real turn. That is on purpose: every scope check needs one turn that
stays in flight while four different people look at it, and a real stand-in turn
gives a window of about a second, which is a race rather than a test. A real
turn is still exercised — the last block does that — but only for the part that
needs an agent the runner actually started.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import Event, Run, utcnow  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.runs import RECENT_STEPS, STEP_TEXT_CHARS  # noqa: E402

failures = []

PASSWORD_SUFFIX = "password1"


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


#: Step text for the parked noisy run, comfortably over the cap so the
#: truncation is exercised rather than being a no-op.
LOUD = "x" * (STEP_TEXT_CHARS * 4)
#: More steps than the tail carries, so the bound is exercised too.
LOUD_STEPS = RECENT_STEPS + 15


def park(session_id, requested_by_id, prompt, *, status="queued", steps=0):
    """Write one unfinished turn straight into the database and leave it there.

    Nothing dispatches it, so it stays in flight for the length of this run —
    which is what every scope check below needs, and what a real stand-in turn
    (about a second long) cannot provide without becoming a race.

    Its own engine, created and disposed here. The application's engine belongs
    to the loop the test client runs the app on; borrowing a pooled connection
    from it onto this one is the kind of failure that only appears when
    something is slow. A second connection to the same SQLite file is not.

    It must also run *after* the app has started: startup deliberately clears
    every queued or running row it finds, because a restart kills every agent
    subprocess and those rows would otherwise stay 'running' forever.
    """
    async def go():
        engine = create_async_engine(settings.database_url)
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                run = Run(
                    session_id=session_id,
                    prompt=prompt,
                    provider="claude",
                    requested_by_id=requested_by_id,
                    status=status,
                    started_at=utcnow() if status == "running" else None,
                )
                db.add(run)
                await db.flush()
                for seq in range(1, steps + 1):
                    db.add(Event(run_id=run.id, session_id=session_id, seq=seq,
                                 kind="tool_use", text=LOUD, tool_name="Bash"))
                await db.commit()
                return run.id
        finally:
            await engine.dispose()

    return asyncio.run(go())


def login(client, username, password="devpassword123"):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def as_user(client, username):
    login(client, username, f"{username}{PASSWORD_SUFFIX}")


def make_user(client, username, is_admin=False, display_name=None):
    r = client.post("/api/users", json={
        "username": username, "password": f"{username}{PASSWORD_SUFFIX}",
        "display_name": display_name, "is_admin": is_admin,
        "must_change_password": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def new_session(client, title):
    r = client.post("/api/sessions", json={
        "title": title, "provider": "claude", "approval_mode": "bypass",
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


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

    login(client, "admin")
    walt = make_user(client, "walt", display_name="Walt Ops")
    carol = make_user(client, "carol")
    # An administrator who is in none of the sessions below and in no team.
    boss = make_user(client, "boss", is_admin=True)
    team = client.post("/api/teams", json={"name": "ops", "member_ids": [walt, carol]})
    assert team.status_code == 201, team.text
    team_id = team.json()["id"]

    as_user(client, "walt")
    private = new_session(client, "Walt's own work")
    shared = new_session(client, "Shared by name")
    of_team = new_session(client, "The team's work")
    loud = new_session(client, "A noisy turn")
    lined = new_session(client, "In order")
    client.patch(f"/api/sessions/{shared}", json={"shared_user_ids": [carol]})
    client.patch(f"/api/sessions/{of_team}", json={"team_id": team_id})

    alone = park(private, walt, "a turn nobody else can see")
    by_name = park(shared, walt, "a turn shared by name")
    by_team = park(of_team, walt, "a turn the team can see")
    noisy = park(loud, walt, "a turn that has said a great deal",
                 status="running", steps=LOUD_STEPS)
    queue = [park(lined, walt, word) for word in ("first", "second", "third")]

    # --- scope, which is the point --------------------------------------
    as_user(client, "walt")
    check("the owner sees every turn of their own",
          run_ids(active(client)) == sorted([alone, by_name, by_team, noisy, *queue]),
          str(run_ids(active(client))))

    as_user(client, "carol")
    check("a reader sees the sessions they are in and nothing else",
          run_ids(active(client)) == sorted([by_name, by_team]),
          str(run_ids(active(client))))

    # The asymmetry, written down as a test because it is the thing most likely
    # to be "fixed" by somebody adding an admin branch. Administering AIOps is
    # not a way into somebody's conversation, and this list must never become
    # the place that leaks one.
    as_user(client, "boss")
    check("an administrator sees nothing extra", active(client) == [], str(active(client)))
    login(client, "admin")
    check("nor does the built-in administrator", active(client) == [], str(active(client)))

    # --- what a row says -------------------------------------------------
    as_user(client, "carol")
    rows = {row["run_id"]: row for row in active(client)}
    named = rows.get(by_name)
    check("a row names the conversation, not just its id",
          bool(named) and named["session_title"] == "Shared by name", str(named))
    check("a row carries the opening of the message",
          bool(named) and named["prompt"] == "a turn shared by name", str(named))
    # Resolved server-side, and to what that person is called rather than to
    # their login: this list is read from screens with no user directory loaded.
    check("a row says whose turn it is, by display name",
          bool(named) and named["requested_by"] == "Walt Ops", str(named))

    # A queued turn has not been handed to an agent. The indicator depends on
    # telling that apart from a turn that has started and not yet spoken.
    check("a queued turn has no start time",
          bool(named) and named["started_at"] is None, str(named))
    check("a queued turn has done nothing",
          bool(named) and named["tools"] == 0 and named["recent"] == [], str(named))

    # --- the tail is bounded, and cut short ------------------------------
    as_user(client, "walt")
    listed = active(client)
    hot = {row["run_id"]: row for row in listed}[noisy]
    check("a running turn is reported as running", hot["status"] == "running")
    check("only the tail of the run travels",
          len(hot["recent"]) == RECENT_STEPS, str(len(hot["recent"])))
    check("and it is the tail, ending on the most recent step",
          hot["recent"][-1]["seq"] == LOUD_STEPS, str(hot["recent"][-1]["seq"]))
    check("steps arrive oldest first, the order the transcript reads them in",
          [e["seq"] for e in hot["recent"]] == sorted(e["seq"] for e in hot["recent"]))
    check("each step's text is cut short",
          all(len(e["text"]) <= STEP_TEXT_CHARS for e in hot["recent"]),
          str(max(len(e["text"]) for e in hot["recent"])))
    check("tool calls are counted over the whole run, not just the tail",
          hot["tools"] == LOUD_STEPS, str(hot["tools"]))

    # --- order -----------------------------------------------------------
    check("the running turn comes before the queued ones",
          [row["run_id"] for row in listed][0] == noisy,
          str([(r["run_id"], r["status"]) for r in listed]))
    check("and the queue is in the order it will run",
          [rid for rid in (row["run_id"] for row in listed) if rid in queue] == queue,
          str([row["run_id"] for row in listed]))

    # --- a turn the runner actually started ------------------------------
    # Everything above is a fixture. This block is the one that proves the
    # endpoint sees a live agent and carries what it has done.
    login(client, "admin")
    sid = new_session(client, "Rebuild the index")
    check("the administrator starts with nothing of their own running",
          active(client) == [], str(active(client)))

    posted = client.post(f"/api/sessions/{sid}/prompt", json={"prompt": "go"})
    check("the prompt was accepted", posted.status_code == 202, posted.text[:120])

    live = []
    deadline = time.time() + 20
    while time.time() < deadline:
        live = active(client)
        # Waiting for it to have *said* something, not merely to exist: the
        # claim being tested is that this carries what a live agent is doing,
        # and an empty tail would satisfy a weaker check.
        if live and live[0]["status"] == "running" and live[0]["recent"]:
            break
        time.sleep(0.05)

    check("a turn the runner started is listed", len(live) == 1, str(live)[:200])
    check("with the steps it has taken so far",
          bool(live) and bool(live[0]["recent"]), str(live)[:200])
    check("and a start time to measure it from",
          bool(live) and live[0]["started_at"] is not None, str(live)[:200])

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
