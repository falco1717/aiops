"""Covers messaging a session while the agent is already working.

The session used to answer a second prompt with 409 for the whole length of a
turn, which meant the composer was dead for minutes at a time. It queues them
now — and the point of most of these checks is that it is *only* a queue. Both
CLIs are driven headless, one process per turn, stdin at /dev/null, so nothing
can be handed to a turn that has already started. A message sent mid-turn
becomes the next turn.

So the properties worth proving are:

  * a message sent mid-turn is accepted, and waits;
  * exactly one agent ever runs against one session — the runs never overlap,
    which is what keeps two turns from racing each other's `--resume` state;
  * they run in the order they were sent, and neither of two simultaneous
    senders loses a turn or gets it run twice;
  * each turn is attributed to whoever actually sent it, which is what decides
    whose stored credentials the runner materialises for it;
  * the queue drains on its own when a turn ends, *including* when that turn
    failed — one bad turn must not strand everything behind it;
  * stopping or deleting a session clears its queue rather than leaking runs;
  * a queued message can be taken back, and a running one cannot be taken back
    by the same control;
  * and a provider switch is still refused mid-turn. That one is deliberately
    not relaxed alongside the composer: a switch throws away the provider
    session id every queued turn would resume from, so unlike a message it
    cannot simply wait its turn.
"""
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-queue.db")
DB_FILE = os.environ["AIOPS_DATABASE_URL"].split("///", 1)[-1]
if os.path.exists(DB_FILE):
    os.remove(DB_FILE)

os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
os.environ.setdefault("AIOPS_WORKSPACE_ROOT", tempfile.mkdtemp(prefix="aiops-queue-ws-"))
os.environ.setdefault(
    "AIOPS_ATTACHMENTS_ROOT", tempfile.mkdtemp(prefix="aiops-queue-attach-")
)
os.environ.setdefault(
    "AIOPS_ACCOUNTS_ROOT", tempfile.mkdtemp(prefix="aiops-queue-accounts-")
)

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_claude_cli.py")

from app.providers import PROVIDERS  # noqa: E402
from app.providers.base import RunSpec  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402

_original = ClaudeProvider.build_run


def patched(self, **kwargs):
    """Same seam test_runner.py uses: the real argv, with a stand-in binary.

    The stand-in sleeps for about a second per turn, which is what makes "the
    session is busy" a real window here rather than a race.
    """
    spec = _original(self, **kwargs)
    return RunSpec(
        argv=[sys.executable, FAKE, *spec.argv[1:]],
        env=spec.env,
        assigned_session_id=spec.assigned_session_id,
    )


ClaudeProvider.build_run = patched
PROVIDERS["claude"] = ClaudeProvider()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def login(client, username, password="devpassword123"):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text


def make_user(client, username):
    r = client.post("/api/users", json={
        "username": username, "password": f"{username}password1",
        "is_admin": False, "must_change_password": False,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def as_user(client, username):
    login(client, username, f"{username}password1")


def runs_of(client, sid):
    return client.get(f"/api/sessions/{sid}/runs").json()


def settle(client, sid, timeout=90):
    """Wait until nothing is queued or running on this session."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = runs_of(client, sid)
        if not any(r["status"] in ("queued", "running") for r in rows):
            return rows
        time.sleep(0.1)
    return runs_of(client, sid)


def moment(value):
    """One timestamp off the API, as a float, comparable with the others.

    Always a float and never a datetime: SQLite hands back naive values and
    Postgres aware ones, and mixing the two in a comparison is a TypeError
    rather than a wrong answer — which would look like a passing test on one
    backend and a crash on the other.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def overlapping(rows):
    """Pairs of runs whose execution windows intersect. Must always be empty.

    The invariant behind the whole feature: however many messages are waiting,
    one session only ever has one agent running against it. Two overlapping
    windows would mean two processes resuming the same provider session id.
    """
    spans = []
    for r in rows:
        start, end = moment(r["started_at"]), moment(r["finished_at"])
        if start is not None and end is not None:
            spans.append((r["id"], start, end))
    clashes = []
    for i, (aid, a0, a1) in enumerate(spans):
        for bid, b0, b1 in spans[i + 1:]:
            if a0 < b1 and b0 < a1:
                clashes.append((aid, bid))
    return clashes


with TestClient(app) as c:
    login(c, "admin")
    ws = c.post("/api/workspaces", json={"name": "queue-demo", "path": "queue-demo"}).json()

    def new_session(provider="claude", **extra):
        payload = {"provider": provider, "workspace_id": ws["id"], **extra}
        r = c.post("/api/sessions", json=payload)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    # --- a message sent mid-turn is accepted, and waits --------------------
    sid = new_session()
    first = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "the first task"})
    check("the first message is accepted", first.status_code == 202, first.text[:200])

    second = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "the second task"})
    check("a message sent mid-turn is accepted, not refused with 409",
          second.status_code == 202, f"{second.status_code} {second.text[:200]}")
    check("and it is reported as queued rather than started",
          second.status_code == 202 and second.json()["status"] == "queued",
          second.text[:200])

    third = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "the third task"})
    check("a third stacks behind the second", third.status_code == 202, third.text[:200])
    check("three distinct turns exist, none merged or dropped",
          len({first.json()["id"], second.json()["id"], third.json()["id"]}) == 3,
          str([first.json()["id"], second.json()["id"], third.json()["id"]]))

    live = runs_of(c, sid)
    check("only one of them is running at a time",
          sum(1 for r in live if r["status"] == "running") <= 1,
          str([(r["id"], r["status"]) for r in live]))
    check("the session reads as busy while messages wait",
          c.get(f"/api/sessions/{sid}").json()["status"] == "running",
          c.get(f"/api/sessions/{sid}").json()["status"])

    # --- the queue drains by itself, in order -----------------------------
    done = settle(c, sid)
    check("every queued message eventually ran",
          all(r["status"] == "succeeded" for r in done),
          str([(r["id"], r["status"], r["error"]) for r in done])[:300])
    check("nothing was left queued", not any(r["status"] == "queued" for r in done))

    ordered = sorted(done, key=lambda r: r["id"])
    check("they ran in the order they were sent",
          [r["prompt"] for r in ordered]
          == ["the first task", "the second task", "the third task"],
          str([r["prompt"] for r in ordered]))
    starts = [moment(r["started_at"]) for r in ordered]
    check("and each one started only after the one before it finished",
          starts == sorted(starts) and not overlapping(done),
          str(overlapping(done)))
    check("the session is idle again once the queue is empty",
          c.get(f"/api/sessions/{sid}").json()["status"] == "idle",
          c.get(f"/api/sessions/{sid}").json()["status"])
    check("each turn resumed the same provider session rather than forking one",
          len({tuple(r["command"]) for r in ordered}) == 3
          and all("--resume" in r["command"] for r in ordered[1:]),
          " ".join(ordered[1]["command"])[:200])

    # --- a failed turn must not strand the queue behind it -----------------
    # A slow failure, on purpose: with an instant one the next message would be
    # started by its own request rather than by the drain, and this would pass
    # without testing the drain at all.
    sid_fail = new_session()
    bad = c.post(f"/api/sessions/{sid_fail}/prompt",
                 json={"prompt": "FAKE_FAIL_AFTER_A_WHILE please"})
    behind = c.post(f"/api/sessions/{sid_fail}/prompt", json={"prompt": "the one behind it"})
    check("a message can queue behind a turn that is going to fail",
          behind.status_code == 202 and behind.json()["status"] == "queued",
          behind.text[:200])

    after_fail = settle(c, sid_fail)
    by_id = {r["id"]: r for r in after_fail}
    check("the failing turn is recorded as failed",
          by_id[bad.json()["id"]]["status"] == "failed",
          str(by_id[bad.json()["id"]]["status"]))
    check("and the message behind it still ran",
          by_id[behind.json()["id"]]["status"] == "succeeded",
          str((by_id[behind.json()["id"]]["status"], by_id[behind.json()["id"]]["error"]))[:200])
    check("a failed turn does not leave its session stuck at running",
          c.get(f"/api/sessions/{sid_fail}").json()["status"] in ("idle", "error"),
          c.get(f"/api/sessions/{sid_fail}").json()["status"])

    # A cancelled turn has to release the queue the same way a failed one does.
    sid_cancel = new_session()
    killed = c.post(f"/api/sessions/{sid_cancel}/prompt", json={"prompt": "this gets stopped"})
    follows = c.post(f"/api/sessions/{sid_cancel}/prompt", json={"prompt": "this still runs"})
    time.sleep(0.4)
    c.post(f"/api/runs/{killed.json()['id']}/cancel")
    after_cancel = {r["id"]: r for r in settle(c, sid_cancel)}
    check("cancelling one turn does not strand the next",
          after_cancel[follows.json()["id"]]["status"] == "succeeded",
          str(after_cancel[follows.json()["id"]]["status"]))
    check("the cancelled turn is terminal",
          after_cancel[killed.json()["id"]]["status"] in ("cancelled", "failed"),
          str(after_cancel[killed.json()["id"]]["status"]))

    # --- taking a message back --------------------------------------------
    sid_undo = new_session()
    running = c.post(f"/api/sessions/{sid_undo}/prompt", json={"prompt": "in flight"})
    waiting = c.post(f"/api/sessions/{sid_undo}/prompt", json={"prompt": "never wanted"})

    r = c.post(f"/api/runs/{running.json()['id']}/withdraw")
    check("the turn the agent is on cannot be withdrawn", r.status_code == 409,
          f"{r.status_code} {r.text[:200]}")
    check("and the refusal points at Stop rather than just saying no",
          "stop" in r.text.lower(), r.text[:200])

    r = c.post(f"/api/runs/{waiting.json()['id']}/withdraw")
    check("a queued message can be withdrawn", r.status_code == 202,
          f"{r.status_code} {r.text[:200]}")
    row = c.get(f"/api/runs/{waiting.json()['id']}").json()
    check("the withdrawn message is cancelled, not deleted",
          row["status"] == "cancelled", str(row["status"]))
    check("and it never started", row["started_at"] is None, str(row["started_at"]))
    check("the withdrawal says it was never run, not that it was interrupted",
          "queue" in (row["error"] or "").lower(), str(row["error"]))

    r = c.post(f"/api/runs/{waiting.json()['id']}/withdraw")
    check("withdrawing it twice is refused rather than silently repeated",
          r.status_code == 409, f"{r.status_code} {r.text[:200]}")

    left = settle(c, sid_undo)
    check("the in-flight turn was untouched by the withdrawal",
          {r["id"]: r["status"] for r in left}[running.json()["id"]] == "succeeded",
          str([(r["id"], r["status"]) for r in left]))
    check("and the withdrawn message never ran",
          all(r["started_at"] is None for r in left if r["id"] == waiting.json()["id"]))

    # --- stop means stop ---------------------------------------------------
    sid_stop = new_session()
    s1 = c.post(f"/api/sessions/{sid_stop}/prompt", json={"prompt": "one"}).json()
    s2 = c.post(f"/api/sessions/{sid_stop}/prompt", json={"prompt": "two"}).json()
    s3 = c.post(f"/api/sessions/{sid_stop}/prompt", json={"prompt": "three"}).json()
    time.sleep(0.4)
    stopped = c.post(f"/api/sessions/{sid_stop}/stop")
    check("stopping a session is accepted", stopped.status_code == 202, stopped.text[:200])
    check("and it reports what it discarded as well as what it killed",
          set(stopped.json()["withdrawn_run_ids"]) == {s2["id"], s3["id"]},
          str(stopped.json()))

    after_stop = settle(c, sid_stop, timeout=30)
    check("nothing is left running or queued after a stop",
          not any(r["status"] in ("queued", "running") for r in after_stop),
          str([(r["id"], r["status"]) for r in after_stop]))
    check("the queued messages were discarded rather than run",
          all(r["started_at"] is None for r in after_stop if r["id"] in (s2["id"], s3["id"])),
          str([(r["id"], r["started_at"]) for r in after_stop]))
    check("a stopped session goes idle",
          c.get(f"/api/sessions/{sid_stop}").json()["status"] in ("idle", "error"),
          c.get(f"/api/sessions/{sid_stop}").json()["status"])

    # --- deleting a session takes its queue with it ------------------------
    sid_gone = new_session()
    d1 = c.post(f"/api/sessions/{sid_gone}/prompt", json={"prompt": "alpha"}).json()
    d2 = c.post(f"/api/sessions/{sid_gone}/prompt", json={"prompt": "beta"}).json()
    time.sleep(0.3)
    r = c.delete(f"/api/sessions/{sid_gone}")
    check("a session with a queue can be deleted", r.status_code == 204, r.text[:200])
    time.sleep(1.5)
    check("the queued run is gone with it, not orphaned",
          c.get(f"/api/runs/{d2['id']}").status_code == 404,
          str(c.get(f"/api/runs/{d2['id']}").status_code))
    check("and so is the one that was running",
          c.get(f"/api/runs/{d1['id']}").status_code == 404,
          str(c.get(f"/api/runs/{d1['id']}").status_code))
    check("no run anywhere is still claiming to be queued for it",
          not any(r["session_id"] == sid_gone for r in c.get("/api/runs?limit=200").json()))

    # --- the switch that is still refused mid-turn -------------------------
    sid_switch = new_session()
    c.post(f"/api/sessions/{sid_switch}/prompt", json={"prompt": "occupying the session"})
    r = c.patch(f"/api/sessions/{sid_switch}", json={"provider": "codex"})
    check("a provider switch is still refused mid-turn", r.status_code == 409,
          f"{r.status_code} {r.text[:200]}")
    check("and the refusal still explains why", "turn" in r.text.lower(), r.text[:200])
    check("the session was not switched underneath the turn",
          c.get(f"/api/sessions/{sid_switch}").json()["provider"] == "claude")
    # ...and it stays refused while a message is merely waiting, because the
    # switch would abandon the session id that queued message resumes from.
    queued_switch = c.post(f"/api/sessions/{sid_switch}/prompt", json={"prompt": "waiting"})
    r = c.patch(f"/api/sessions/{sid_switch}", json={"provider": "codex"})
    check("and refused while a message is queued behind the turn",
          r.status_code == 409, f"{r.status_code} {r.text[:200]}")
    c.post(f"/api/runs/{queued_switch.json()['id']}/withdraw")
    settle(c, sid_switch)
    r = c.patch(f"/api/sessions/{sid_switch}", json={"provider": "codex"})
    check("but allowed once the session is free again", r.status_code == 200, r.text[:200])

    # --- attachments queue with the message they belong to -----------------
    sid_files = new_session()
    c.post(f"/api/sessions/{sid_files}/prompt", json={"prompt": "keep busy"})
    up = c.post(
        f"/api/sessions/{sid_files}/attachments",
        files={"file": ("notes.txt", b"queued payload", "text/plain")},
    )
    check("a file can be uploaded while the agent is working",
          up.status_code == 201, f"{up.status_code} {up.text[:200]}")
    with_file = c.post(
        f"/api/sessions/{sid_files}/prompt",
        json={"prompt": "look at the file", "attachment_ids": [up.json()["id"]]},
    )
    check("and attached to a queued message exactly as to an immediate one",
          with_file.status_code == 202, f"{with_file.status_code} {with_file.text[:200]}")
    bound = [
        a for a in c.get(f"/api/sessions/{sid_files}/attachments").json()
        if a["id"] == up.json()["id"]
    ]
    check("the file is claimed by the queued turn, not left in the composer",
          bound and bound[0]["run_id"] == with_file.json()["id"], str(bound)[:200])
    check("a claimed file cannot be deleted out from under its queued turn",
          c.delete(f"/api/sessions/{sid_files}/attachments/{up.json()['id']}").status_code == 409)
    files_done = {r["id"]: r for r in settle(c, sid_files)}
    check("the queued turn with a file ran normally when its turn came",
          files_done[with_file.json()["id"]]["status"] == "succeeded",
          str(files_done[with_file.json()["id"]])[:200])

    # --- attribution is per message, not per session -----------------------
    bob = make_user(c, "bob")
    carol = make_user(c, "carol")
    sid_shared = new_session()
    # A turn runs as whoever sent it, so both of them need access to the
    # workspace these sessions run in — sharing the session does not lend it
    # out (see test_workspaces.py). Without this their turns are refused
    # before they start, and what this block is measuring is the queue.
    c.patch(f"/api/workspaces/{ws['id']}", json={
        "grants": [{"user_id": bob, "level": "use"}, {"user_id": carol, "level": "use"}],
    })
    r = c.patch(f"/api/sessions/{sid_shared}", json={"shared_user_ids": [bob, carol]})
    check("the session can be shared", r.status_code == 200, r.text[:200])
    admin_run = c.post(f"/api/sessions/{sid_shared}/prompt", json={"prompt": "owner's turn"}).json()

    as_user(c, "bob")
    bob_run = c.post(f"/api/sessions/{sid_shared}/prompt", json={"prompt": "bob's turn"})
    check("a sharee can queue a message into somebody else's busy session",
          bob_run.status_code == 202, f"{bob_run.status_code} {bob_run.text[:200]}")

    as_user(c, "carol")
    carol_run = c.post(f"/api/sessions/{sid_shared}/prompt", json={"prompt": "carol's turn"})
    check("and so can a second one, behind the first",
          carol_run.status_code == 202, f"{carol_run.status_code} {carol_run.text[:200]}")

    login(c, "admin")
    shared_rows = {r["id"]: r for r in settle(c, sid_shared)}
    check("the owner's turn is attributed to the owner",
          shared_rows[admin_run["id"]]["requested_by_id"] == c.get("/api/auth/me").json()["id"],
          str(shared_rows[admin_run["id"]]["requested_by_id"]))
    check("a queued message is attributed to whoever queued it, not to the owner",
          shared_rows[bob_run.json()["id"]]["requested_by_id"] == bob,
          str(shared_rows[bob_run.json()["id"]]["requested_by_id"]))
    check("and the second sender keeps their own attribution too",
          shared_rows[carol_run.json()["id"]]["requested_by_id"] == carol,
          str(shared_rows[carol_run.json()["id"]]["requested_by_id"]))
    check("three people's turns still never overlapped",
          not overlapping(list(shared_rows.values())),
          str(overlapping(list(shared_rows.values()))))
    check("and they ran in the order they were sent",
          [shared_rows[i]["prompt"] for i in sorted(shared_rows)]
          == ["owner's turn", "bob's turn", "carol's turn"],
          str([shared_rows[i]["prompt"] for i in sorted(shared_rows)]))

    # --- two senders at the same instant -----------------------------------
    # The case the id ordering exists for. Both requests are released from a
    # barrier, so they reach the dispatch decision together; whichever row got
    # the lower id must run first, and neither may be lost or run twice.
    sid_race = new_session()
    c.post(f"/api/sessions/{sid_race}/prompt", json={"prompt": "hold the session"})
    gate = threading.Barrier(2)

    def racer(text):
        gate.wait()
        return c.post(f"/api/sessions/{sid_race}/prompt", json={"prompt": text})

    with ThreadPoolExecutor(max_workers=2) as pool:
        both = [f.result() for f in [pool.submit(racer, "race A"), pool.submit(racer, "race B")]]
    check("both simultaneous messages were accepted",
          all(x.status_code == 202 for x in both), str([x.status_code for x in both]))
    check("they produced two turns, not one and not three",
          len({x.json()["id"] for x in both}) == 2, str([x.json()["id"] for x in both]))

    race_rows = settle(c, sid_race)
    check("all three turns ran", len(race_rows) == 3 and
          all(r["status"] == "succeeded" for r in race_rows),
          str([(r["id"], r["status"]) for r in race_rows]))
    check("neither simultaneous turn was run twice or lost",
          sorted(r["prompt"] for r in race_rows)
          == ["hold the session", "race A", "race B"],
          str(sorted(r["prompt"] for r in race_rows)))
    check("and even a dead heat produced no overlapping runs",
          not overlapping(race_rows), str(overlapping(race_rows)))
    check("the lower id went first, which is the order the queue promises",
          [r["id"] for r in sorted(race_rows, key=lambda r: moment(r["started_at"]) or 0)]
          == sorted(r["id"] for r in race_rows),
          str([(r["id"], r["started_at"]) for r in race_rows]))

    # --- who may queue, and who may not ------------------------------------
    sid_private = new_session()
    as_user(c, "bob")
    r = c.post(f"/api/sessions/{sid_private}/prompt", json={"prompt": "not mine"})
    check("queueing into a session you cannot see is still a 404",
          r.status_code == 404, f"{r.status_code} {r.text[:200]}")
    r = c.post(f"/api/sessions/{sid_private}/stop")
    check("and so is stopping one", r.status_code == 404, str(r.status_code))
    login(c, "admin")
    settle(c, sid_private)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
