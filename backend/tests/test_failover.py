"""Named accounts, per-user access, and failover when an account hits its limit.

Uses the stand-in CLI so the whole runner path is real: two accounts are
created, the first is made to report a usage limit, and the run must land on
the second without the operator noticing anything beyond a note.
"""
import os
import sys
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "fake_claude_cli.py")

DB = os.path.abspath("./test_failover.db")
if os.path.exists(DB):
    os.remove(DB)
os.environ["AIOPS_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB}"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
os.environ["AIOPS_COOKIE_SECURE"] = "false"
os.environ["AIOPS_ACCOUNTS_ROOT"] = os.path.abspath("./.test-accounts")
os.environ["AIOPS_WORKSPACE_ROOT"] = os.path.abspath("./.test-workspaces")

from app.providers import PROVIDERS  # noqa: E402
from app.providers.base import RunSpec  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402

_original = ClaudeProvider.build_run


def patched(self, **kwargs):
    spec = _original(self, **kwargs)
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


def wait_for(c, run_id, timeout=40):
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        time.sleep(0.25)
        row = c.get(f"/api/runs/{run_id}").json()
        if row["status"] not in ("queued", "running"):
            return row
    return row


with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})

    primary = c.post(
        "/api/accounts", json={"name": "Jordans Claude", "provider": "claude", "is_default": True}
    ).json()
    backup = c.post("/api/accounts", json={"name": "Walts Claude", "provider": "claude"}).json()
    check("two named accounts created", bool(primary.get("id") and backup.get("id")), str(primary)[:200])
    check(
        "each gets its own credential directory",
        primary["config_dir"] != backup["config_dir"],
        f"{primary['config_dir']} vs {backup['config_dir']}",
    )

    r = c.patch(f"/api/accounts/{primary['id']}", json={"fallback_account_id": backup["id"]})
    check("failover chain configured", r.json().get("fallback_account_id") == backup["id"], r.text[:160])

    ws = c.post("/api/workspaces", json={"name": "fo", "path": "fo"}).json()

    # --- happy path: primary serves the run ---------------------------
    s1 = c.post(
        "/api/sessions",
        json={"provider": "claude", "workspace_id": ws["id"], "account_id": primary["id"]},
    ).json()
    run = c.post(f"/api/sessions/{s1['id']}/prompt", json={"prompt": "hello"}).json()
    row = wait_for(c, run["id"])
    check("run succeeds on the primary account", row["status"] == "succeeded", str(row)[:200])
    check("run records which account served it", row["account_id"] == primary["id"], str(row["account_id"]))
    check("no failover recorded", row["failed_over_from_id"] is None, str(row["failed_over_from_id"]))
    check("token usage captured", (row["input_tokens"] or 0) > 0, str(row["input_tokens"]))
    check("context size captured", (row["context_tokens"] or 0) > 0, str(row["context_tokens"]))

    # --- now make the primary report a usage limit --------------------
    os.environ["FAKE_LIMITED_DIRS"] = primary["config_dir"]

    s2 = c.post(
        "/api/sessions",
        json={"provider": "claude", "workspace_id": ws["id"], "account_id": primary["id"]},
    ).json()
    run2 = c.post(f"/api/sessions/{s2['id']}/prompt", json={"prompt": "second"}).json()
    row2 = wait_for(c, run2["id"])
    check("limited primary fails over rather than failing", row2["status"] == "succeeded",
          f"{row2['status']} {str(row2.get('error'))[:160]}")
    check("the backup account served it", row2["account_id"] == backup["id"], str(row2["account_id"]))
    check("failover is recorded for the operator", row2["failed_over_from_id"] == primary["id"],
          str(row2["failed_over_from_id"]))

    accounts = {a["id"]: a for a in c.get("/api/accounts").json()}
    check("primary is marked limited", bool(accounts[primary["id"]]["limited_until"]),
          str(accounts[primary["id"]]["limited_until"]))

    # A fresh session with no explicit account must not land on the limited one.
    # (Which healthy account it picks depends on what else exists — on a dev box
    # the migration also adopts the developer's own ~/.claude — so the invariant
    # is "not the limited one", not a specific id.)
    s3 = c.post("/api/sessions", json={"provider": "claude", "workspace_id": ws["id"]}).json()
    run3 = c.post(f"/api/sessions/{s3['id']}/prompt", json={"prompt": "third"}).json()
    row3 = wait_for(c, run3["id"])
    check("a limited account is skipped when picking a default",
          row3["account_id"] != primary["id"], str(row3["account_id"]))
    check("and the run still completes", row3["status"] == "succeeded", str(row3["status"]))

    r = c.post(f"/api/accounts/{primary['id']}/clear-limit")
    check("limit can be cleared by an admin", r.json().get("limited_until") is None, r.text[:160])

    # --- no fallback configured: a limit is a clean failure -----------
    lone = c.post("/api/accounts", json={"name": "Lonely Claude", "provider": "claude"}).json()
    os.environ["FAKE_LIMITED_DIRS"] = lone["config_dir"]
    s4 = c.post(
        "/api/sessions",
        json={"provider": "claude", "workspace_id": ws["id"], "account_id": lone["id"]},
    ).json()
    run4 = c.post(f"/api/sessions/{s4['id']}/prompt", json={"prompt": "fourth"}).json()
    row4 = wait_for(c, run4["id"])
    check("without a fallback the run fails", row4["status"] == "failed", str(row4["status"]))
    check("and says why, actionably", "fallback" in (row4["error"] or "").lower(),
          str(row4["error"])[:200])
    os.environ.pop("FAKE_LIMITED_DIRS", None)

    # --- per-user access ----------------------------------------------
    alice = c.post(
        "/api/users", json={"username": "alice", "password": "alicepassword",
                            "must_change_password": False},
    ).json()
    r = c.patch(f"/api/accounts/{backup['id']}", json={"allowed_user_ids": [alice["id"]]})
    check("access can be restricted to named users",
          r.json().get("allowed_user_ids") == [alice["id"]], r.text[:200])

    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "alice", "password": "alicepassword"})
    seen = {a["id"]: a for a in c.get("/api/accounts").json()}
    check("granted user may use the restricted account", seen[backup["id"]]["usable_by_me"] is True)
    check("ungranted accounts stay open to everyone", seen[primary["id"]]["usable_by_me"] is True)

    r = c.post(
        "/api/sessions",
        json={"provider": "claude", "workspace_id": ws["id"], "account_id": backup["id"]},
    )
    check("granted user can start a session on it", r.status_code == 201, r.text[:160])

    # Restrict the primary to admin only, then confirm alice is refused.
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    c.patch(f"/api/accounts/{primary['id']}", json={"allowed_user_ids": [1]})
    c.post("/api/auth/logout")
    c.post("/api/auth/login", json={"username": "alice", "password": "alicepassword"})
    r = c.post(
        "/api/sessions",
        json={"provider": "claude", "workspace_id": ws["id"], "account_id": primary["id"]},
    )
    check("ungranted user is refused that account", r.status_code == 400, f"{r.status_code} {r.text[:160]}")
    check("refusal names the account", "access" in r.text.lower(), r.text[:200])

    # --- usage reporting -----------------------------------------------
    u = c.get("/api/usage").json()
    labels = [w["label"] for w in u["windows"]]
    check("usage windows reported", "Last 5 hours" in labels and "Last 7 days" in labels, str(labels))
    check("usage counts real tokens", u["windows"][0]["total_tokens"] > 0,
          str(u["windows"][0]))
    check("usage is attributed per account", len(u["by_account"]) >= 2, str(u["by_account"])[:200])
    # The measured figures cover this server only; the authoritative plan window
    # comes from the CLI. The note must say so rather than implying otherwise.
    check("usage says what it does and does not cover",
          "this server only" in u["note"], u["note"][:160])

    su = c.get(f"/api/usage/session/{s1['id']}").json()
    check("per-session context reported", (su["last_context_tokens"] or 0) > 0, str(su))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
