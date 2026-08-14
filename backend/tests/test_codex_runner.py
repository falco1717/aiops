"""Exercises the runner's two execution paths, with no agent binary present.

Codex is the only provider with two ways to run a turn: `codex exec` for
unattended work, and the app-server adapter for a turn that must stop and ask a
human. Which one a run gets is decided by (provider, approval_mode) alone, and
getting it wrong is silent — an "ask" session that quietly took the exec path
would run commands nobody approved. So the checks here are mostly about *which
path was taken*, and then about the four things the adapter path owes the rest
of the system: approvals reaching the broker, the thread id being stored so the
next turn resumes, usage landing on the run row, and cancel actually stopping it.

`CodexAppServerAdapter` is replaced with a stand-in, because the real one is
already covered end to end by test_codex_appserver.py and needs the binary. The
runner, broker, database, websocket hub and HTTP surface are all real. The
non-interactive paths are proven by their *failure*: with no `codex` or `claude`
on PATH they must die with "Executable not found", which only happens if they
really did try to spawn a CLI.
"""
import os
import sys
import time

from fastapi.testclient import TestClient

sys.path.insert(0, os.getcwd())

for _stale in ("./test-codex-runner.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ["AIOPS_DATABASE_URL"] = "sqlite+aiosqlite:///./test-codex-runner.db"
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_ADMIN_PASSWORD", "devpassword123")
os.environ.setdefault("AIOPS_COOKIE_SECURE", "false")
os.environ["AIOPS_SCHEDULER_ENABLED"] = "false"
# Lets the suite store one system, so "does the ssh environment reach the
# adapter?" can be asked about a real credential rather than a bare PATH.
os.environ.setdefault("AIOPS_SECRET_KEY", "test-credential-encryption-key")
# The path the runner must not silently widen. Every check below assumes the
# default tier, so pin it rather than inherit the operator's environment.
os.environ["AIOPS_CODEX_INTERACTIVE_SANDBOX"] = "workspace-write"

from app import runner as runner_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.providers.base import NormalizedEvent  # noqa: E402
from app.providers.codex import CodexProvider  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- the stand-in adapter --------------------------------------------------
class FakeAdapter:
    """Speaks the CodexAppServerAdapter contract the runner depends on.

    Deliberately never sets `provider_session_id` on an event: the only way the
    session can learn the thread id here is the runner reading
    `adapter.conversation_id` after the turn, which is the behaviour under test.
    """

    instances: list["FakeAdapter"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.prompt = kwargs.get("prompt") or ""
        self.on_approval = kwargs.get("on_approval")
        self.resume_id = kwargs.get("resume_id")
        self.conversation_id = self.resume_id or f"thread-{len(FakeAdapter.instances) + 1}"
        self.usage = {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_tokens": 3,
            "cache_write_tokens": 4,
        }
        self.answers: list[tuple[bool, str | None]] = []
        self.closes = 0
        self.finished = False
        FakeAdapter.instances.append(self)

    async def run(self):
        yield NormalizedEvent(kind="system", text="session started")
        yield NormalizedEvent(kind="delta", text="thinking", persist=False)
        if self.prompt.startswith("ask:"):
            allowed, note = await self.on_approval(
                "command_execution",
                "shell",
                "rm -rf /tmp/demo",
                {"command": ["rm", "-rf", "/tmp/demo"], "cwd": "/tmp"},
            )
            self.answers.append((allowed, note))
            yield NormalizedEvent(
                kind="tool_result",
                tool_name="shell",
                text="ran it" if allowed else f"denied: {note}",
                is_error=not allowed,
            )
        # A turn that never reached `turn/completed` reports no usage in its
        # events; the runner must still take the adapter's own tally.
        carries_usage = not self.prompt.startswith("nousage:")
        yield NormalizedEvent(
            kind="result", text="done", usage=self.usage if carries_usage else None
        )
        self.finished = True

    async def close(self):
        self.closes += 1


runner_mod.CodexAppServerAdapter = FakeAdapter

from app.main import app  # noqa: E402


# --- helpers ---------------------------------------------------------------
def wait_for_run(c, run_id, done=("succeeded", "failed", "cancelled", "timeout"), timeout=30):
    deadline = time.time() + timeout
    row = {}
    while time.time() < deadline:
        row = c.get(f"/api/runs/{run_id}").json()
        if row.get("status") in done:
            return row
        time.sleep(0.05)
    return row


def wait_for_approval(c, run_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = c.get("/api/approvals", params={"run_id": run_id, "status": "pending"}).json()
        if rows:
            return rows[0]
        time.sleep(0.05)
    return None


def new_session(c, provider, approval_mode, ws_id):
    return c.post(
        "/api/sessions",
        json={
            "provider": provider,
            "model": None,
            "workspace_id": ws_id,
            "approval_mode": approval_mode,
        },
    ).json()


# --- provider declaration --------------------------------------------------
check(
    "codex now declares that it can ask a human",
    CodexProvider.supports_interactive_approval is True,
)
check(
    "the interactive sandbox tier is a setting, and defaults to the sandboxed one",
    settings.codex_interactive_sandbox == "workspace-write"
    and "codex_interactive_sandbox" in type(settings).model_fields,
    settings.codex_interactive_sandbox,
)


with TestClient(app) as c:
    c.post("/api/auth/login", json={"username": "admin", "password": "devpassword123"})
    ws = c.post("/api/workspaces", json={"name": "codex-demo", "path": "codex-demo"}).json()
    # A stored system, so the per-run ssh config and its password variable exist
    # and can be looked for in what the adapter was handed.
    target = c.post(
        "/api/targets",
        json={
            "name": "Probe Box",
            "hostname": "127.0.0.1",
            "username": "node",
            "auth_type": "password",
            "password": "hunter2",
        },
    )
    check("a system is stored for this run", target.status_code == 201, target.text[:200])

    # --- codex + ask: the adapter path, start to finish -------------------
    sess = new_session(c, "codex", "ask", ws["id"])
    sid = sess["id"]

    with c.websocket_connect(f"/api/ws?session_id={sid}") as socket:
        socket.receive_json()  # connected
        run = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "look around"}).json()
        streamed, finished = [], None
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = socket.receive_json()
            if msg["type"] == "event":
                streamed.append(msg)
            elif msg["type"] == "run.finished":
                finished = msg
                break

    check("codex ask-mode ran through the adapter", len(FakeAdapter.instances) == 1,
          f"{len(FakeAdapter.instances)} adapter(s) built")
    adapter = FakeAdapter.instances[0]
    check("the turn completed", finished and finished["status"] == "succeeded",
          str(finished and (finished["status"], finished.get("error")))[:200])

    # The shared event sink: the adapter's events get the same treatment the
    # subprocess path gives a parsed stdout line.
    kinds = [e["kind"] for e in streamed]
    check("adapter events reach the websocket", set(kinds) >= {"system", "delta", "result"},
          str(kinds))
    events = c.get(f"/api/sessions/{sid}/events").json()
    persisted = [e["kind"] for e in events]
    check("adapter events are persisted", persisted == ["system", "result"], str(persisted))
    check("deltas stream but are not persisted",
          "delta" in kinds and "delta" not in persisted, str(kinds))
    check("seq numbering is contiguous",
          [e["seq"] for e in events] == list(range(1, len(events) + 1)),
          str([e["seq"] for e in events]))

    # --- what the adapter was handed --------------------------------------
    check("the adapter got the session's workspace as cwd",
          adapter.kwargs["cwd"].endswith("codex-demo"), adapter.kwargs["cwd"])
    check("the adapter runs at the configured sandbox tier",
          adapter.kwargs["sandbox"] == "workspace-write", str(adapter.kwargs["sandbox"]))
    check("the ssh-target environment is threaded through to the adapter",
          adapter.kwargs["env"].get("NO_COLOR") == "1"
          and "AIOPS_SSH_CONFIG" in adapter.kwargs["env"]
          and adapter.kwargs["env"].get("AIOPS_SSHPASS_PROBE_BOX") == "hunter2",
          str(sorted(adapter.kwargs["env"])))
    check("the stored systems are described to the agent",
          "probe-box" in (adapter.kwargs["system_prompt"] or ""),
          str(adapter.kwargs["system_prompt"])[:200])
    check("an approval callback is attached", callable(adapter.on_approval))
    check("the first turn does not try to resume", adapter.resume_id is None,
          str(adapter.resume_id))

    # --- what the runner did with the result -------------------------------
    after = c.get(f"/api/sessions/{sid}").json()
    check("conversation id persisted for the next turn",
          after["provider_session_id"] == "thread-1", str(after["provider_session_id"]))
    check("session returned to idle", after["status"] == "idle", after["status"])

    row = c.get(f"/api/runs/{run['id']}").json()
    check("run.command names the real launch and the tier",
          "app-server" in " ".join(row["command"]) and "workspace-write" in " ".join(row["command"]),
          str(row["command"]))
    check("usage landed on the run row, counted once",
          (row["input_tokens"], row["output_tokens"]) == (11, 22),
          str((row["input_tokens"], row["output_tokens"])))
    check("context tokens derived like the subprocess path",
          row["context_tokens"] == 11 + 3 + 4, str(row["context_tokens"]))
    check("the adapter was closed when the turn ended", adapter.closes >= 1, str(adapter.closes))

    # --- turn two resumes the stored thread --------------------------------
    run2 = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "nousage: and again"}).json()
    row2 = wait_for_run(c, run2["id"])
    check("second turn resumed the stored thread",
          FakeAdapter.instances[1].resume_id == "thread-1",
          str(FakeAdapter.instances[1].resume_id))
    check("usage is taken from the adapter when no event carried it",
          (row2["input_tokens"], row2["output_tokens"]) == (11, 22),
          str((row2["input_tokens"], row2["output_tokens"])))

    # --- approvals reach the broker ---------------------------------------
    for verdict, allowed in (("deny", False), ("allow", True)):
        before = len(FakeAdapter.instances)
        run3 = c.post(
            f"/api/sessions/{sid}/prompt", json={"prompt": f"ask: please {verdict}"}
        ).json()
        pending = wait_for_approval(c, run3["id"])
        check(f"[{verdict}] the agent parked on a broker approval", pending is not None,
              str(pending)[:200])
        if pending:
            check(f"[{verdict}] the request describes what it wants to do",
                  pending["tool_name"] == "shell"
                  and "rm -rf /tmp/demo" in (pending["summary"] or "")
                  and pending["provider"] == "codex",
                  str(pending)[:200])
            still = c.get(f"/api/runs/{run3['id']}").json()
            check(f"[{verdict}] the run is still running while it waits",
                  still["status"] == "running", still["status"])
            decided = c.post(
                f"/api/approvals/{pending['id']}/decide",
                json={"allowed": allowed, "note": f"operator said {verdict}"},
            )
            check(f"[{verdict}] the decision was accepted", decided.status_code == 200,
                  decided.text[:200])
        row3 = wait_for_run(c, run3["id"])
        asked = FakeAdapter.instances[before]
        check(f"[{verdict}] the decision reached the adapter",
              asked.answers == [(allowed, f"operator said {verdict}")], str(asked.answers))
        check(f"[{verdict}] the approval row settled",
              c.get("/api/approvals", params={"run_id": run3["id"]}).json()[0]["status"]
              == ("allowed" if allowed else "denied"))
        texts = " ".join(e["text"] or "" for e in c.get(f"/api/sessions/{sid}/events").json())
        check(f"[{verdict}] the outcome is visible in the transcript",
              ("ran it" if allowed else "denied: operator said deny") in texts)

    # --- cancelling a turn that is parked on a human ----------------------
    before = len(FakeAdapter.instances)
    run4 = c.post(f"/api/sessions/{sid}/prompt", json={"prompt": "ask: never answered"}).json()
    pending = wait_for_approval(c, run4["id"])
    check("a fourth turn parked", pending is not None, str(pending)[:200])
    cancelled = c.post(f"/api/runs/{run4['id']}/cancel")
    check("cancel accepted", cancelled.status_code == 202, cancelled.text[:200])
    row4 = wait_for_run(c, run4["id"])
    parked = FakeAdapter.instances[before]
    check("cancel closed the adapter", parked.closes >= 1, str(parked.closes))
    check("cancel released the human it was waiting on", parked.answers
          and parked.answers[0][0] is False, str(parked.answers))
    check("the run settled as cancelled", row4["status"] == "cancelled", str(row4["status"]))
    check("the approval was not left pending",
          c.get("/api/approvals", params={"run_id": run4["id"]}).json()[0]["status"] != "pending",
          str(c.get("/api/approvals", params={"run_id": run4["id"]}).json()[0]["status"]))
    check("the session is usable again",
          c.get(f"/api/sessions/{sid}").json()["status"] == "idle")

    # --- every other combination keeps the subprocess path ----------------
    built = len(FakeAdapter.instances)
    cases = [
        ("codex", "auto", "codex"),
        ("codex", "bypass", "codex"),
        ("claude", "ask", "claude"),
        ("claude", "auto", "claude"),
    ]
    for provider, mode, binary in cases:
        other = new_session(c, provider, mode, ws["id"])
        run5 = c.post(f"/api/sessions/{other['id']}/prompt", json={"prompt": "hello"}).json()
        row5 = wait_for_run(c, run5["id"])
        check(
            f"{provider} + {mode} spawns the CLI instead of the adapter",
            row5["status"] == "failed" and "Executable not found" in (row5["error"] or "")
            and binary in (row5["error"] or ""),
            f"{row5['status']}: {(row5['error'] or '')[:120]}",
        )
    check("no adapter was built for any of them", len(FakeAdapter.instances) == built,
          f"{len(FakeAdapter.instances) - built} unexpected adapter(s)")

    # --- a scheduled ask-mode codex run is unattended, so no adapter ------
    sched = c.post(
        "/api/schedules",
        json={
            "name": "nightly",
            "cron": "0 3 * * *",
            "timezone_name": "UTC",
            "prompt": "nightly sweep",
            "provider": "codex",
            "workspace_id": ws["id"],
            "session_mode": "new",
        },
    ).json()
    fired = c.post(f"/api/schedules/{sched['id']}/run").json()
    row6 = wait_for_run(c, fired["run_id"])
    check("a scheduled codex run does not park on a human",
          len(FakeAdapter.instances) == built and "Executable not found" in (row6["error"] or ""),
          f"{row6['status']}: {(row6['error'] or '')[:120]}")


print()
if failures:
    print(f"{len(failures)} check(s) failed: " + "; ".join(failures))
    sys.exit(1)
print("all checks passed")
