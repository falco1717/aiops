"""Pins the app-server adapter to traffic captured from a real `codex app-server`.

Every payload below was copied verbatim off the wire against codex-cli 0.147.0
in the aiops-app container. The protocol is marked experimental by OpenAI and
already renamed itself once (`newConversation`/`sendUserTurn` became
`thread/start`/`turn/start`), so a future rename should fail here rather than
show up as a session that silently never asks anyone for approval.

Runs without the codex binary: only the pure translation and decision helpers
are exercised.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())

from app.providers.codex_appserver import (  # noqa: E402
    APPROVAL_METHODS,
    CodexAppServerAdapter,
    approval_reply,
    approval_summary,
    looks_rate_limited,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def adapter(**kw):
    return CodexAppServerAdapter(prompt="hi", cwd="/tmp/cxws", **kw)


def one(events):
    return events[0] if events else None


THREAD = "019ffdb5-302d-7752-8ab4-9c5c284d581c"
TURN = "019ffdb5-3110-78f2-93f8-59a9e53dfe55"

# --- captured: the server names the thread we must store for a resume -------
a = adapter()
started = a._translate(
    "thread/started",
    {"thread": {"id": THREAD, "cwd": "/tmp/cxws", "cliVersion": "0.147.0", "gitInfo": None}},
)
check("thread/started is the session id we persist",
      one(started) and one(started).kind == "system" and a.conversation_id == THREAD,
      str(a.conversation_id))

# --- captured: a shell call, denied by the operator -------------------------
cmd_started = a._translate(
    "item/started",
    {
        "item": {
            "type": "commandExecution",
            "id": "exec-f76f7f01-b1c6-4796-9098-f1022db786f7",
            "command": "/bin/bash -lc 'rm -f /tmp/cxws/a.txt'",
            "cwd": "/tmp/cxws",
            "status": "inProgress",
            "commandActions": [{"type": "unknown", "command": "rm -f /tmp/cxws/a.txt"}],
            "aggregatedOutput": None,
            "exitCode": None,
        },
        "threadId": THREAD,
        "turnId": TURN,
        "startedAtMs": 1786667949551,
    },
)
ev = one(cmd_started)
check("a starting shell call is a tool_use",
      ev and ev.kind == "tool_use" and ev.tool_name == "shell" and "rm -f" in (ev.text or ""),
      str(ev and (ev.kind, ev.tool_name, ev.text)))

declined = one(a._translate(
    "item/completed",
    {
        "item": {
            "type": "commandExecution",
            "id": "exec-f76f7f01-b1c6-4796-9098-f1022db786f7",
            "command": "/bin/bash -lc 'rm -f /tmp/cxws/a.txt'",
            "status": "declined",
            "aggregatedOutput": None,
            "exitCode": None,
        },
        "threadId": THREAD,
        "turnId": TURN,
        "completedAtMs": 1786667954000,
    },
))
check("a declined command is an errored tool_result, not a silent success",
      declined and declined.kind == "tool_result" and declined.is_error is True,
      str(declined and (declined.kind, declined.is_error)))
check("and says out loud that a human blocked it",
      declined and "denied by the operator" in (declined.text or ""), str(declined and declined.text))

# --- captured: the same call once the operator allowed it -------------------
allowed = one(a._translate(
    "item/completed",
    {
        "item": {
            "type": "commandExecution",
            "id": "exec-17192fd5-3c3f-4381-9251-37a56b37b9ae",
            "command": "/bin/bash -lc 'touch /tmp/cxws/b.txt && echo made-b'",
            "status": "completed",
            "aggregatedOutput": "made-b\n",
            "exitCode": 0,
            "durationMs": 7,
        },
        "threadId": THREAD,
        "turnId": TURN,
        "completedAtMs": 1786667955000,
    },
))
check("an allowed command reports its output",
      allowed and allowed.kind == "tool_result" and allowed.text == "made-b\n"
      and allowed.is_error is False,
      str(allowed and (allowed.kind, allowed.text, allowed.is_error)))

failed = one(a._translate(
    "item/completed",
    {"item": {"type": "commandExecution", "id": "e3", "command": "false",
              "status": "failed", "aggregatedOutput": "boom", "exitCode": 1}},
))
check("a non-zero exit marks the result as an error", failed and failed.is_error is True,
      str(failed and failed.is_error))

# --- captured: assistant text and reasoning ---------------------------------
msg = one(a._translate(
    "item/completed",
    {"item": {"type": "agentMessage", "id": "msg_062cd0b9", "phase": "final_answer",
              "text": "Step 1: Rejected. Step 2: Succeeded."},
     "threadId": THREAD, "turnId": TURN, "completedAtMs": 1},
))
check("agentMessage becomes assistant text",
      msg and msg.kind == "assistant" and msg.text == "Step 1: Rejected. Step 2: Succeeded.",
      str(msg and (msg.kind, msg.text)))
check("the item/started half of a message is not duplicated",
      a._translate("item/started",
                   {"item": {"type": "agentMessage", "id": "m2", "text": ""}}) == [],
      "expected no event")

reasoning = one(a._translate(
    "item/completed",
    {"item": {"type": "reasoning", "id": "rs_1", "summary": ["Checking the sandbox"], "content": []}},
))
check("reasoning maps to thinking", reasoning and reasoning.kind == "thinking",
      str(reasoning and (reasoning.kind, reasoning.text)))

check("our own prompt echoed back is not replayed as an event",
      a._translate("item/completed",
                   {"item": {"type": "userMessage", "id": "u1",
                             "content": [{"type": "text", "text": "hi"}]}}) == [],
      "expected no event")

# --- captured: a file edit ---------------------------------------------------
FILE_ITEM = {
    "type": "fileChange",
    "id": "exec-0fda2d14-3026-430f-9faf-3f4c632887bb",
    "changes": [{"path": "/home/node/cx_probe.md", "kind": {"type": "add"}, "diff": "hello\n"}],
    "status": "inProgress",
}
edit = one(a._translate("item/started", {"item": FILE_ITEM, "threadId": THREAD, "turnId": TURN}))
check("a file change surfaces as an edit tool call",
      edit and edit.kind == "tool_use" and edit.tool_name == "edit"
      and "cx_probe.md" in (edit.text or ""),
      str(edit and (edit.kind, edit.tool_name)))

# --- captured: token usage rides its own notification ------------------------
check("token usage notifications are not shown as transcript noise",
      a._translate("thread/tokenUsage/updated", {
          "threadId": THREAD, "turnId": TURN,
          "tokenUsage": {
              "total": {"totalTokens": 28190, "inputTokens": 28029, "cachedInputTokens": 24064,
                        "cacheWriteInputTokens": 0, "outputTokens": 161, "reasoningOutputTokens": 8},
              "last": {"totalTokens": 14191, "inputTokens": 14105, "cachedInputTokens": 13056,
                       "cacheWriteInputTokens": 0, "outputTokens": 86, "reasoningOutputTokens": 8},
              "modelContextWindow": 258400}}) == [],
      "expected no event")
check("this turn's tokens come from `last`, not the thread total",
      a.usage == {"input_tokens": 14105, "output_tokens": 86,
                  "cache_read_tokens": 13056, "cache_write_tokens": 0},
      str(a.usage))

# --- captured: the end of the turn -------------------------------------------
completed = a._translate("turn/completed", {
    "threadId": THREAD,
    "turn": {"id": TURN, "status": "completed", "error": None, "itemsView": "summary",
             "items": [{"type": "agentMessage", "id": "m9", "phase": "final_answer",
                        "text": "Step 1: Rejected. Step 2: Succeeded."}],
             "startedAt": 1786667949, "completedAt": 1786667960, "durationMs": 11000}})
check("turn/completed is the result event",
      completed and completed[0].kind == "result", str(completed and completed[0].kind))
check("the result carries the final message and the usage",
      completed[0].text == "Step 1: Rejected. Step 2: Succeeded."
      and completed[0].usage == a.usage,
      str((completed[0].text, completed[0].usage)))
check("turn/completed ends the run loop", len(completed) == 2 and completed[1] is not None,
      f"{len(completed)} entries")

b = adapter()
turn_failed = b._translate("turn/completed", {
    "threadId": THREAD,
    "turn": {"id": TURN, "status": "failed", "items": [],
             "error": {"message": "stream disconnected before completion"}}})
check("a failed turn is an error, not a successful result",
      turn_failed and turn_failed[0].kind == "error" and turn_failed[0].is_error,
      str(turn_failed and turn_failed[0].kind))

limited = b._translate("error", {
    "threadId": THREAD, "turnId": TURN, "willRetry": False,
    "error": {"message": "You have hit your usage limit."}})
check("a usage limit is detected so the runner can fail over",
      limited and limited[0].rate_limited is True, str(limited and limited[0].rate_limited))
check("a fatal error ends the run loop", len(limited) == 2, f"{len(limited)} entries")
retrying = b._translate("error", {
    "threadId": THREAD, "turnId": TURN, "willRetry": True,
    "error": {"message": "temporary upstream failure"}})
check("a retryable error does not end the run loop", len(retrying) == 1, f"{len(retrying)} entries")

check("looks_rate_limited ignores ordinary failures",
      looks_rate_limited("permission denied") is False, "")

# --- captured: chatter we deliberately drop or keep --------------------------
warning = one(b._translate("configWarning", {
    "summary": "Codex could not find bubblewrap on PATH.", "details": None}))
check("a config warning is kept, unwrapped from its envelope",
      warning and warning.kind == "system" and warning.text == "Codex could not find bubblewrap on PATH.",
      str(warning and warning.text))
check("mcp startup chatter is dropped",
      b._translate("mcpServer/startupStatus/updated",
                   {"threadId": THREAD, "name": "codex_apps", "status": "ready"}) == [],
      "expected no event")
check("an unrecognised notification is still surfaced",
      one(b._translate("some/newMethod", {"x": 1})).kind == "system", "")

# --- deltas are streamed but never persisted ---------------------------------
quiet = adapter(stream_partials=False)
check("deltas are suppressed unless the run asked for them",
      quiet._translate("item/agentMessage/delta",
                       {"delta": "Ste", "itemId": "m9", "threadId": THREAD, "turnId": TURN}) == [],
      "expected no event")
loud = adapter(stream_partials=True)
delta = one(loud._translate("item/agentMessage/delta",
                            {"delta": "Ste", "itemId": "m9", "threadId": THREAD, "turnId": TURN}))
check("a streamed delta is live-only, never written to the database",
      delta and delta.kind == "delta" and delta.text == "Ste" and delta.persist is False,
      str(delta and (delta.kind, delta.persist)))

# --- captured approval request: the whole point of this adapter --------------
EXEC_APPROVAL = {
    "threadId": THREAD,
    "turnId": TURN,
    "itemId": "exec-f76f7f01-b1c6-4796-9098-f1022db786f7",
    "startedAtMs": 1786667949551,
    "environmentId": "local",
    "command": "/bin/bash -lc 'rm -f /tmp/cxws/a.txt'",
    "cwd": "/tmp/cxws",
    "commandActions": [{"type": "unknown", "command": "rm -f /tmp/cxws/a.txt"}],
    "proposedExecpolicyAmendment": ["rm", "-f", "/tmp/cxws/a.txt"],
    "availableDecisions": ["accept", {"acceptWithExecpolicyAmendment": {
        "execpolicy_amendment": ["rm", "-f", "/tmp/cxws/a.txt"]}}, "cancel"],
}
FILE_APPROVAL = {
    "threadId": THREAD,
    "turnId": TURN,
    "itemId": "exec-0fda2d14-3026-430f-9faf-3f4c632887bb",
    "startedAtMs": 1786668129043,
    "reason": None,
    "grantRoot": None,
}

check("every approval method the binary can send is handled",
      set(APPROVAL_METHODS) == {
          "item/commandExecution/requestApproval",
          "item/fileChange/requestApproval",
          "item/permissions/requestApproval",
          "execCommandApproval",
          "applyPatchApproval",
      },
      str(sorted(APPROVAL_METHODS)))

summary = approval_summary("item/commandExecution/requestApproval", EXEC_APPROVAL)
check("the operator is shown the actual command and where it runs",
      "rm -f /tmp/cxws/a.txt" in summary and "/tmp/cxws" in summary, summary)

# The file-change request names only an itemId; the paths have to come from the
# item/started notification that preceded it, or the human approves a blank.
file_summary = approval_summary("item/fileChange/requestApproval", FILE_APPROVAL, FILE_ITEM)
check("the operator is shown which files an edit would touch",
      "/home/node/cx_probe.md" in file_summary and "add" in file_summary, file_summary)
check("a file-change request with no item context still reads sensibly",
      approval_summary("item/fileChange/requestApproval", FILE_APPROVAL) == "apply file changes",
      approval_summary("item/fileChange/requestApproval", FILE_APPROVAL))

v1_summary = approval_summary("execCommandApproval", {
    "callId": "call_1", "command": ["bash", "-lc", "rm -rf build"],
    "conversationId": THREAD, "cwd": "/tmp/cxws", "parsedCmd": [],
    "reason": "needs to write outside the sandbox"})
check("the legacy exec request renders its argv as a command line",
      "bash -lc rm -rf build" in v1_summary and "needs to write outside" in v1_summary, v1_summary)

# --- decision enums, quoted from the generated schema ------------------------
check("allowing a command answers with the v2 `accept` decision",
      approval_reply("item/commandExecution/requestApproval", EXEC_APPROVAL, True, None)
      == {"decision": "accept"}, "")
check("denying a command answers `decline`, which lets the turn continue",
      approval_reply("item/commandExecution/requestApproval", EXEC_APPROVAL, False, "no")
      == {"decision": "decline"}, "")
check("allowing an edit answers with the v2 `accept` decision",
      approval_reply("item/fileChange/requestApproval", FILE_APPROVAL, True, None)
      == {"decision": "accept"}, "")
check("denying an edit answers `decline`",
      approval_reply("item/fileChange/requestApproval", FILE_APPROVAL, False, None)
      == {"decision": "decline"}, "")

check("the legacy exec request uses the v1 ReviewDecision vocabulary",
      approval_reply("execCommandApproval", {}, True, None) == {"decision": "approved"}, "")
check("a legacy denial carries the operator's reason back to the model",
      approval_reply("execCommandApproval", {}, False, "that would delete the build")
      == {"decision": {"denied": {"rejection": "that would delete the build"}}}, "")
check("a legacy denial with no reason still sends a valid rejection",
      approval_reply("applyPatchApproval", {}, False, None)
      == {"decision": {"denied": {"rejection": "denied by the operator"}}}, "")

PERMS = {"threadId": THREAD, "turnId": TURN, "itemId": "p1", "cwd": "/tmp/cxws",
         "startedAtMs": 1, "reason": "write outside the workspace",
         "permissions": {"fileSystem": {"writableRoots": ["/etc"]}, "network": None}}
check("a permission escalation answers with a granted profile, not a decision",
      approval_reply("item/permissions/requestApproval", PERMS, True, None)
      == {"permissions": PERMS["permissions"], "scope": "turn"}, "")
check("a denied escalation grants nothing at all",
      approval_reply("item/permissions/requestApproval", PERMS, False, "too broad")
      == {"permissions": {}, "scope": "turn"}, "")

# --- fail closed: every way a human answer can go missing is a denial --------
async def slow(kind, tool, summary, request):
    await asyncio.sleep(30)
    return True, None


async def boom(kind, tool, summary, request):
    raise RuntimeError("callback exploded")


async def yes(kind, tool, summary, request):
    return True, None


nobody = adapter(on_approval=None)
check("a run with no approval handler denies rather than proceeding",
      asyncio.run(nobody._ask("command_execution", "shell", "rm -rf /", {}))[0] is False, "")

slowpoke = adapter(on_approval=slow, approval_timeout=0.2)
verdict, note = asyncio.run(slowpoke._ask("command_execution", "shell", "rm -rf /", {}))
check("an unanswered approval times out into a denial", verdict is False, str(verdict))
check("and the model is told why", note and "0.2s" in note, str(note))

broken = adapter(on_approval=boom)
verdict, note = asyncio.run(broken._ask("command_execution", "shell", "rm -rf /", {}))
check("a handler that raises denies instead of taking the exception out of band",
      verdict is False and "callback exploded" in (note or ""), str((verdict, note)))

ok = adapter(on_approval=yes)
check("a plain yes is still a yes",
      asyncio.run(ok._ask("command_execution", "shell", "ls", {})) == (True, None), "")

# --- resume plumbing ---------------------------------------------------------
resumed = adapter(resume_id=THREAD)
check("a resumed run starts out already knowing its thread id",
      resumed.conversation_id == THREAD, str(resumed.conversation_id))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
