"""Exercises the tool-approval loop end to end, without an agent CLI.

The point of this feature is that a real subprocess stops and waits for a
human, so the tests that matter are the ones about *blocking*: that a request
parks until answered, that every way a run can end releases it, and that
nothing ever fails open. The MCP bridge is driven as a real subprocess against
a stub AIOps, because its contract with Claude is a wire format — asserting on
imported functions would not prove the JSON-RPC framing is right.
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.getcwd())

# A fresh file each run, so "exactly one pending row" stays a meaningful check.
for _stale in ("./test-approvals.db",):
    if os.path.exists(_stale):
        os.remove(_stale)

os.environ.setdefault("AIOPS_DATABASE_URL", "sqlite+aiosqlite:///./test-approvals.db")
os.environ.setdefault("AIOPS_JWT_SECRET", "test")
os.environ.setdefault("AIOPS_SCHEDULER_ENABLED", "false")

from sqlalchemy import select  # noqa: E402

from app.approvals import ApprovalBroker, RunTokens  # noqa: E402
from app.db import init_db  # noqa: E402
from app.models import Approval, Run, Session  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.providers.claude import ClaudeProvider  # noqa: E402
from app.providers.codex import CodexProvider  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


# --- argv construction -----------------------------------------------------
claude = ClaudeProvider()

check(
    "claude no longer offers the permission mode the CLI rejects",
    "default" not in claude.permission_modes and "manual" in claude.permission_modes,
    str(claude.permission_modes),
)

ask = claude.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="ask", approval_token="tok-123",
)
check("ask mode asks the CLI to route permissions through our bridge",
      "--permission-prompt-tool" in ask.argv
      and ask.argv[ask.argv.index("--permission-prompt-tool") + 1] == "mcp__aiops__ask",
      " ".join(ask.argv[-4:]))
check("ask mode registers the bridge as an MCP server",
      "--mcp-config" in ask.argv
      and "mcp_approver.py" in ask.argv[ask.argv.index("--mcp-config") + 1],
      "mcp-config missing")
check("ask mode uses a permission mode this CLI accepts",
      ask.argv[ask.argv.index("--permission-mode") + 1] == "manual")
check("the bridge gets the run token, and only through the environment",
      ask.env.get("AIOPS_APPROVAL_TOKEN") == "tok-123" and "tok-123" not in " ".join(ask.argv),
      str(sorted(ask.env)))

bypass = claude.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="bypass", approval_token=None,
)
check("bypass turns permission checks off",
      bypass.argv[bypass.argv.index("--permission-mode") + 1] == "bypassPermissions")
check("bypass does not attach the approval bridge",
      "--permission-prompt-tool" not in bypass.argv and "AIOPS_APPROVAL_TOKEN" not in bypass.env)

auto = claude.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="auto", approval_token=None,
)
check("auto accepts edits without asking",
      auto.argv[auto.argv.index("--permission-mode") + 1] == "acceptEdits")

pinned = claude.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode="plan",
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="bypass", approval_token=None,
)
check("a preset's explicit permission mode still wins",
      pinned.argv[pinned.argv.index("--permission-mode") + 1] == "plan")

codex = CodexProvider()
cx = codex.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    approval_mode="bypass", approval_token=None,
)
check("codex bypass drops its sandbox",
      cx.argv[cx.argv.index("--sandbox") + 1] == "danger-full-access")
check("codex exec is never told to ask for approval (the flag is rejected there)",
      "--ask-for-approval" not in cx.argv and "-a" not in cx.argv)


# --- the broker ------------------------------------------------------------
async def broker_tests():
    await init_db()
    async with SessionLocal() as db:
        sess = Session(provider="claude", title="approval tests")
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        run = Run(session_id=sess.id, prompt="p", status="running")
        db.add(run)
        await db.commit()
        await db.refresh(run)
        session_id, run_id = sess.id, run.id

    broker = ApprovalBroker()

    # 1. A request blocks until answered, and the answer is what comes back.
    started = time.monotonic()
    waiter = asyncio.create_task(
        broker.request(
            run_id=run_id, session_id=session_id, provider="claude",
            tool_name="Bash", summary="Bash: ssh host", request={"command": "ssh host"},
            timeout=10,
        )
    )
    await asyncio.sleep(0.25)
    check("the agent is still parked while nobody has answered", not waiter.done())

    async with SessionLocal() as db:
        pending = list(await db.scalars(select(Approval).where(Approval.status == "pending")))
    check("the pending request is persisted, so a page reload still shows it", len(pending) == 1,
          f"{len(pending)} pending")
    check("it records what the agent actually wants to do",
          pending[0].tool_name == "Bash" and pending[0].request == {"command": "ssh host"},
          str(pending[0].request))
    row_id = pending[0].id

    await broker.decide(row_id, allowed=True, note=None, user_id=None)
    decision = await waiter
    check("allow releases the agent", decision.allowed)
    check("it really did wait, rather than returning immediately",
          time.monotonic() - started >= 0.25)

    async with SessionLocal() as db:
        stored = await db.get(Approval, row_id)
        check("the decision is recorded for later audit", stored.status == "allowed", stored.status)

    # 2. Answering twice fails rather than resolving a dead future.
    again = await broker.decide(row_id, allowed=False, note=None, user_id=None)
    check("a second answer to the same request is refused", again is False)

    # 3. A timeout denies — an unanswered agent must not hang forever.
    timed = await broker.request(
        run_id=run_id, session_id=session_id, provider="claude",
        tool_name="Bash", summary="slow", request={}, timeout=0.3,
    )
    check("no answer means denied, not allowed", timed.allowed is False)
    check("the denial explains itself", "denied" in (timed.note or "").lower(), str(timed.note))

    # 4. A run ending releases anything parked on it.
    parked = asyncio.create_task(
        broker.request(
            run_id=run_id, session_id=session_id, provider="claude",
            tool_name="Write", summary="w", request={}, timeout=30,
        )
    )
    await asyncio.sleep(0.2)
    await broker.cancel_run(run_id)
    released = await asyncio.wait_for(parked, timeout=2)
    check("cancelling a run unblocks its pending approvals", released.allowed is False)
    check("and says why", "cancelled" in (released.note or "").lower(), str(released.note))

    # 5. Tokens are per-run and die with the run.
    tokens = RunTokens()
    token = tokens.issue(run_id, session_id)
    check("a token resolves to its own run", tokens.resolve(token) == (run_id, session_id))
    tokens.revoke(run_id)
    check("a revoked token is worthless", tokens.resolve(token) is None)
    check("an unknown token is rejected", tokens.resolve("made-up") is None)


asyncio.run(broker_tests())


# --- the MCP bridge, driven as a real subprocess ---------------------------
class StubAIOps(BaseHTTPRequestHandler):
    """Stands in for the app, so the bridge's HTTP contract is exercised."""

    decision = {"allowed": True, "note": None, "updated_input": None}
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        StubAIOps.seen.append(json.loads(body))
        payload = json.dumps(StubAIOps.decision).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), StubAIOps)
threading.Thread(target=server.serve_forever, daemon=True).start()
stub_url = f"http://127.0.0.1:{server.server_port}"

BRIDGE = os.path.join("app", "bridge", "mcp_approver.py")


def drive_bridge(messages, env_extra=None, timeout=20):
    env = {
        **os.environ,
        "AIOPS_INTERNAL_URL": stub_url,
        "AIOPS_APPROVAL_TOKEN": "tok-abc",
        "AIOPS_PROVIDER": "claude",
        **(env_extra or {}),
    }
    proc = subprocess.run(
        [sys.executable, BRIDGE],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
])
check("the bridge completes an MCP handshake",
      out and out[0]["result"]["serverInfo"]["name"] == "aiops-approvals", str(out[:1]))
check("a notification is not answered (answering one breaks the protocol)",
      all(m.get("id") != 0 or m is out[0] for m in out) and len(out) == 2, str(len(out)))
check("it advertises exactly the permission tool Claude is pointed at",
      out[1]["result"]["tools"][0]["name"] == "ask", str(out[1]["result"]["tools"][0]["name"]))

StubAIOps.seen.clear()
StubAIOps.decision = {"allowed": True, "note": None, "updated_input": None}
out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "ask",
        "arguments": {
            "tool_name": "Bash",
            "input": {"command": "ssh alice@203.0.113.20 hostname"},
            "tool_use_id": "toolu_1",
        },
    }},
])
call = [m for m in out if m.get("id") == 2][0]
allow = json.loads(call["result"]["content"][0]["text"])
check("an allow is returned in the exact shape Claude validates",
      allow == {"behavior": "allow"}, json.dumps(allow))
check("the request reaches AIOps with its token and a readable summary",
      StubAIOps.seen
      and StubAIOps.seen[0]["token"] == "tok-abc"
      and StubAIOps.seen[0]["summary"] == "Bash: ssh alice@203.0.113.20 hostname",
      json.dumps(StubAIOps.seen[:1])[:200])

StubAIOps.decision = {"allowed": False, "note": "Denied by the AIOps operator.", "updated_input": None}
out = drive_bridge([
    {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "ask", "arguments": {"tool_name": "Write", "input": {"file_path": "/etc/passwd"}},
    }},
])
deny = json.loads([m for m in out if m.get("id") == 3][0]["result"]["content"][0]["text"])
check("a deny carries the operator's reason back to the agent",
      deny["behavior"] == "deny" and deny["message"] == "Denied by the AIOps operator.",
      json.dumps(deny))

# The safety property that matters most: if AIOps is unreachable the bridge
# must refuse, never wave the tool call through.
out = drive_bridge(
    [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "ask", "arguments": {"tool_name": "Bash", "input": {"command": "rm -rf /"}},
        }},
    ],
    env_extra={"AIOPS_INTERNAL_URL": "http://127.0.0.1:1"},
)
closed = json.loads([m for m in out if m.get("id") == 4][0]["result"]["content"][0]["text"])
check("an unreachable AIOps denies rather than failing open",
      closed["behavior"] == "deny", json.dumps(closed))

server.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
