"""Exercises the runner + Claude stream-json parser against a stand-in CLI.

The real CLI can't run here, so ClaudeProvider.build_run is redirected at
fake_claude_cli.py. Everything downstream — subprocess supervision, line parsing,
event persistence, websocket fan-out, session-id capture, cost, the one-turn
guard and cancellation — is the production code path.
"""
import os
import sys
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_claude_cli.py")

from app.providers import PROVIDERS  # noqa: E402
from app.providers.base import RunSpec  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402

_original = ClaudeProvider.build_run


def patched(self, **kwargs):
    spec = _original(self, **kwargs)
    # Replace argv[0] ("claude") with `python fake_claude_cli.py`, keep every flag.
    return RunSpec(
        argv=[sys.executable, FAKE, *spec.argv[1:]],
        env=spec.env,
        assigned_session_id=spec.assigned_session_id,
    )


ClaudeProvider.build_run = patched
PROVIDERS["claude"] = ClaudeProvider()

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    ws = c.post("/api/workspaces", json={"name": "stream-demo", "path": "stream-demo"}).json()
    sess = c.post(
        "/api/sessions", json={"provider": "claude", "model": "opus", "workspace_id": ws["id"]}
    ).json()
    sid = sess["id"]

    with c.websocket_connect(f"/api/ws?session_id={sid}") as socket:
        check("websocket connected", socket.receive_json()["type"] == "connected")

        run = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "Summarize the README"}).json()
        check("prompt accepted", "id" in run, str(run)[:200])

        # The stand-in sleeps between deltas, so the session is genuinely busy
        # here. This used to be a 409; a message sent mid-turn is queued now.
        second = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "and again"})
        check("a message sent mid-turn is accepted rather than refused",
              second.status_code == 202, f"{second.status_code} {second.text[:140]}")
        check("and it waits rather than starting a second agent on one session",
              second.status_code == 202 and second.json()["status"] == "queued",
              second.text[:200])
        # Taken straight back out, so the rest of this suite still watches
        # exactly one turn. The queue itself is exercised in test_queue.py.
        undo = c.post(f"/api/runs/{second.json()['id']}/withdraw")
        check("a queued message can be withdrawn before it starts",
              undo.status_code == 202, f"{undo.status_code} {undo.text[:140]}")

        kinds, deltas, finished = [], [], None
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = socket.receive_json()
            if msg["type"] == "event":
                (deltas if msg["kind"] == "delta" else kinds).append(msg)
            # Scoped to the turn under test: the withdrawn message announces
            # itself as finished too, and it is not this one.
            elif msg["type"] == "run.finished" and msg["run_id"] == run["id"]:
                finished = msg
                break

        check("run.finished received", finished is not None, str(finished)[:200])
        check("run succeeded", finished and finished["status"] == "succeeded",
              str(finished and (finished["status"], finished.get("error")))[:200])
        check("cost reported over the socket", finished and finished["cost_usd"] == 0.0123,
              str(finished and finished["cost_usd"]))
        check("token deltas streamed", len(deltas) == 3, f"{len(deltas)} deltas")
        check("deltas carry text", "".join(d["text"] for d in deltas) == "Checking the README",
              repr("".join(d["text"] for d in deltas)))

        streamed_kinds = [k["kind"] for k in kinds]
        print(f"       streamed kinds: {streamed_kinds}")
        for expected in ("system", "thinking", "tool_use", "tool_result", "assistant", "result"):
            check(f"{expected} event streamed", expected in streamed_kinds)

    # --- persistence --------------------------------------------------
    events = c.get(f"/api/sessions/{sid}/events").json()
    persisted = [e["kind"] for e in events]
    print(f"       persisted kinds: {persisted}")
    check("deltas are not persisted", "delta" not in persisted, str(persisted))
    check("six events persisted", len(events) == 6, str(len(events)))
    check("seq is 1..n in order", [e["seq"] for e in events] == list(range(1, len(events) + 1)),
          str([e["seq"] for e in events]))

    tool = next((e for e in events if e["kind"] == "tool_use"), None)
    check("tool name captured", tool and tool["tool_name"] == "Read", str(tool))
    check("tool input summarized to the file path", tool and tool["text"] == "README.md", str(tool))

    assistant = next((e for e in events if e["kind"] == "assistant"), None)
    check("assistant text captured",
          assistant and "sample project" in (assistant["text"] or ""), str(assistant)[:200])

    raw = c.get(f"/api/sessions/{sid}/events/{events[0]['id']}/raw").json()["raw"]
    check("raw payload retained", isinstance(raw, dict) and raw.get("subtype") == "init", str(raw)[:200])

    # --- session state ------------------------------------------------
    after = c.get(f"/api/sessions/{sid}").json()
    check("provider session id captured", after["provider_session_id"], str(after["provider_session_id"]))
    check("session back to idle", after["status"] == "idle", after["status"])
    check("title derived from first prompt", after["title"] == "Summarize the README", after["title"])

    run_row = c.get(f"/api/runs/{run['id']}").json()
    check("cost persisted on the run", run_row["cost_usd"] == 0.0123, str(run_row["cost_usd"]))
    check("exit code recorded", run_row["exit_code"] == 0, str(run_row["exit_code"]))
    # The CLIs write harmless notices to stderr; a successful run must not be
    # decorated with one and look broken.
    check("a succeeded run carries no error text", not run_row["error"], str(run_row["error"]))
    check("token counts persisted", (run_row["input_tokens"] or 0) > 0, str(run_row["input_tokens"]))

    # --- second turn resumes rather than starting fresh ---------------
    prior_session = after["provider_session_id"]
    run2 = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "now check the tests"}).json()
    for _ in range(80):
        time.sleep(0.25)
        row = c.get(f"/api/runs/{run2['id']}").json()
        if row["status"] not in ("queued", "running"):
            break
    check("second turn succeeded", row["status"] == "succeeded", f"{row['status']} {row.get('error')}")
    check("second turn used --resume", "--resume" in row["command"], " ".join(row["command"])[:220])
    check("resumed the same provider session", prior_session in row["command"],
          " ".join(row["command"])[:220])
    # Three rows, not two: the withdrawn message is one of them. A message that
    # was accepted and then taken back is part of what happened here, and
    # dropping it from the transcript would hide it from everyone else in a
    # shared session.
    check("transcript holds both turns and the withdrawn message",
          len(c.get(f"/api/sessions/{sid}/transcript").json()["runs"]) == 3,
          str(len(c.get(f"/api/sessions/{sid}/transcript").json()["runs"])))

    # --- cancellation -------------------------------------------------
    run3 = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "long one"}).json()
    time.sleep(0.4)  # mid-stream, between deltas
    cancelled = c.post(f"/api/runs/{run3['id']}/cancel")
    check("cancel accepted", cancelled.status_code == 202, f"{cancelled.status_code} {cancelled.text[:140]}")
    for _ in range(80):
        time.sleep(0.25)
        row3 = c.get(f"/api/runs/{run3['id']}").json()
        if row3["status"] not in ("queued", "running"):
            break
    check("cancelled run reached a terminal state",
          row3["status"] in ("cancelled", "failed"), row3["status"])
    check("session recovered to idle after cancel",
          c.get(f"/api/sessions/{sid}").json()["status"] in ("idle", "error"),
          c.get(f"/api/sessions/{sid}").json()["status"])

    # --- timezone-aware schedules (tzdata fix) ------------------------
    s = c.post("/api/schedules", json={
        "name": "Nightly", "cron": "0 3 * * *", "timezone_name": "America/Chicago",
        "prompt": "run the suite", "provider": "claude", "workspace_id": ws["id"],
        "session_mode": "continue",
    })
    check("schedule with a real timezone created", s.status_code == 201, s.text[:200])
    if s.status_code == 201:
        check("next_run_at computed", bool(s.json()["next_run_at"]), str(s.json()["next_run_at"]))
        print(f"       next run: {s.json()['next_run_at']}")
    bad = c.post("/api/schedules", json={
        "name": "BadCron", "cron": "not a cron", "prompt": "x", "provider": "claude"})
    check("invalid cron rejected (not masked by tz error)",
          bad.status_code == 400 and "cron" in bad.text.lower(), bad.text[:200])

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
