"""End-to-end smoke test for the AIOps API using Starlette's TestClient."""
import os
import sys
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

from app.main import app  # noqa: E402

failures = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


with TestClient(app) as c:
    # --- auth ---------------------------------------------------------
    r = c.get("/api/auth/me")
    check("unauthenticated /me is 401", r.status_code == 401, str(r.status_code))

    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    check("bad password rejected", r.status_code == 401, str(r.status_code))

    r = c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    check("login succeeds", r.status_code == 200, r.text[:200])

    r = c.get("/api/auth/me")
    check("/me after login", r.status_code == 200 and r.json()["username"] == "admin", r.text[:120])

    # --- providers ----------------------------------------------------
    r = c.get("/api/providers")
    check("providers listed", r.status_code == 200 and len(r.json()) == 2, r.text[:300])
    if r.status_code == 200:
        names = {p["name"] for p in r.json()}
        check("both adapters present", names == {"claude", "codex"}, str(names))
        for p in r.json():
            print(f"       {p['name']}: available={p['available']} auth={p['authenticated']}")

    # --- workspaces ---------------------------------------------------
    r = c.post("/api/workspaces", json={"name": "demo", "path": "demo"})
    check("workspace created", r.status_code == 201, r.text[:300])
    ws_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/workspaces", json={"name": "escape", "path": "../../../etc"})
    check("path traversal rejected", r.status_code == 400, f"{r.status_code} {r.text[:160]}")

    r = c.post("/api/workspaces", json={"name": "demo", "path": "demo2"})
    check("duplicate workspace name rejected", r.status_code == 409, str(r.status_code))

    r = c.get(f"/api/workspaces/{ws_id}/status")
    check("workspace status", r.status_code == 200 and r.json()["exists"], r.text[:200])

    # --- presets ------------------------------------------------------
    r = c.post(
        "/api/presets",
        json={
            "name": "Reviewer",
            "provider": "claude",
            "model": "opus",
            "permission_mode": "acceptEdits",
            "system_prompt": "You are a code reviewer.",
            "allowed_tools": "Read,Grep",
            "extra_args": ["--bare"],
        },
    )
    check("preset created", r.status_code == 201, r.text[:300])
    preset_id = r.json()["id"] if r.status_code == 201 else None

    r = c.post("/api/presets", json={"name": "Bad", "provider": "claude", "permission_mode": "nope"})
    check("invalid permission mode rejected", r.status_code == 400, r.text[:200])

    r = c.post("/api/presets", json={"name": "Bad2", "provider": "codex", "allowed_tools": "Read"})
    check("allowed_tools rejected for codex", r.status_code == 400, r.text[:200])

    # --- sessions -----------------------------------------------------
    r = c.post(
        "/api/sessions",
        json={"provider": "claude", "preset_id": preset_id, "workspace_id": ws_id},
    )
    check("session created", r.status_code == 201, r.text[:300])
    sess = r.json() if r.status_code == 201 else {}
    sid = sess.get("id")
    check("session inherits preset model", sess.get("model") == "opus", str(sess.get("model")))

    r = c.post("/api/sessions", json={"provider": "codex", "preset_id": preset_id})
    check("cross-provider preset rejected", r.status_code == 400, r.text[:200])

    r = c.post("/api/sessions", json={"provider": "nope"})
    check("unknown provider rejected", r.status_code == 400, r.text[:200])

    # --- run a turn (the CLI is absent here, so it must fail cleanly) --
    r = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "Summarize README.md"})
    check("prompt accepted", r.status_code == 202, r.text[:300])
    run_id = r.json()["id"] if r.status_code == 202 else None
    if run_id:
        print(f"       command: {' '.join(c.get(f'/api/runs/{run_id}').json()['command']) or '(not yet built)'}")

    # The one-turn-at-a-time guard is verified in smoke_stream.py, where the
    # stand-in CLI stays busy long enough for the race to be deterministic.
    # Here the run fails instantly (no CLI), so a second prompt is legitimately
    # accepted and this file only checks the failure path below.

    # Wait for the runner to finish.
    final = None
    for _ in range(60):
        time.sleep(0.25)
        final = c.get(f"/api/runs/{run_id}").json()
        if final["status"] not in ("queued", "running"):
            break
    check(
        "run reached a terminal state",
        final and final["status"] in ("failed", "succeeded", "timeout", "cancelled"),
        f"status={final and final['status']} error={(final or {}).get('error', '')[:160]}",
    )
    check(
        "missing CLI reported clearly",
        final and "not found" in (final.get("error") or "").lower(),
        (final or {}).get("error", "")[:200],
    )

    r = c.get(f"/api/runs/{run_id}")
    check("command recorded on run", r.status_code == 200 and len(r.json()["command"]) > 0, "")
    if r.status_code == 200:
        print(f"       argv: {r.json()['command']}")

    r = c.get(f"/api/sessions/{sid}")
    check("session id assigned upfront for claude", bool(r.json().get("provider_session_id")),
          str(r.json().get("provider_session_id")))
    check("session left non-running", r.json()["status"] != "running", r.json()["status"])

    r = c.get(f"/api/sessions/{sid}/transcript")
    check("transcript fetched", r.status_code == 200 and len(r.json()["runs"]) >= 1, r.text[:200])

    # --- schedules ----------------------------------------------------
    r = c.post(
        "/api/schedules",
        json={
            "name": "Nightly",
            "cron": "0 3 * * *",
            "timezone_name": "America/Chicago",
            "prompt": "Run the test suite and report failures.",
            "provider": "claude",
            "preset_id": preset_id,
            "workspace_id": ws_id,
            "session_mode": "continue",
        },
    )
    check("schedule created", r.status_code == 201, r.text[:300])
    sched = r.json() if r.status_code == 201 else {}
    check("next_run_at computed", bool(sched.get("next_run_at")), str(sched.get("next_run_at")))
    print(f"       next run: {sched.get('next_run_at')}")

    r = c.post("/api/schedules", json={"name": "Bad", "cron": "not a cron", "prompt": "x", "provider": "claude"})
    check("invalid cron rejected", r.status_code == 400, r.text[:200])

    r = c.post("/api/schedules", json={"name": "BadTz", "cron": "0 3 * * *", "timezone_name": "Mars/Olympus", "prompt": "x", "provider": "claude"})
    check("invalid timezone rejected", r.status_code == 400, r.text[:200])

    if sched:
        r = c.post(f"/api/schedules/{sched['id']}/run")
        check("schedule run-now fires", r.status_code == 202, r.text[:200])
        if r.status_code == 202:
            fired_session = r.json()["session_id"]
            r2 = c.get("/api/schedules")
            target = r2.json()[0]["target_session_id"]
            check("continue-mode pins the session", target == fired_session, f"{target} vs {fired_session}")

    # --- websocket ----------------------------------------------------
    try:
        with c.websocket_connect(f"/api/ws?session_id={sid}") as socket:
            msg = socket.receive_json()
            check("websocket handshake", msg.get("type") == "connected", str(msg))
    except Exception as exc:  # noqa: BLE001
        check("websocket handshake", False, f"{type(exc).__name__}: {exc}")

    # --- spa / health -------------------------------------------------
    r = c.get("/api/health")
    check("health endpoint", r.status_code == 200, r.text[:120])

    r = c.get("/api/nonexistent")
    check("unknown api path is 404", r.status_code == 404, str(r.status_code))

    # --- logout -------------------------------------------------------
    r = c.post("/api/auth/logout")
    check("logout", r.status_code == 204, str(r.status_code))
    r = c.get("/api/auth/me")
    check("session invalidated after logout", r.status_code == 401, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
