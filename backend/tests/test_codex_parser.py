"""Pins the Codex adapter to output captured from a real `codex exec --json`.

The Codex event schema has changed between releases, and an earlier version of
this adapter was written from documentation that turned out not to match the
binary. These lines were copied verbatim from a live run, so a future schema
drift shows up here rather than as an empty transcript in the UI.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from app.providers.codex import CodexProvider  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


p = CodexProvider()

# --- captured verbatim from `codex exec --json` (codex-cli 0.147.0) ---------
LIVE = [
    '{"type":"thread.started","thread_id":"019ffd6e-5c0e-7bf2-bd3f-6f63ced69229"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"hello from codex"}}',
    '{"type":"turn.completed","usage":{"input_tokens":13730,"cached_input_tokens":11008,'
    '"cache_write_input_tokens":0,"output_tokens":8,"reasoning_output_tokens":0}}',
]

events = [p.parse_line(line) for line in LIVE]

check("thread.started yields the session id",
      events[0] and events[0].provider_session_id == "019ffd6e-5c0e-7bf2-bd3f-6f63ced69229",
      str(events[0] and events[0].provider_session_id))
check("turn.started is not surfaced as noise", events[1] is None, str(events[1]))
check("agent_message becomes assistant text",
      events[2] and events[2].kind == "assistant" and events[2].text == "hello from codex",
      str(events[2] and (events[2].kind, events[2].text)))
check("turn.completed is the result", events[3] and events[3].kind == "result", str(events[3] and events[3].kind))
check("token usage is captured for the usage page",
      events[3] and events[3].usage == {
          "input_tokens": 13730, "output_tokens": 8,
          "cache_read_tokens": 11008, "cache_write_tokens": 0,
      },
      str(events[3] and events[3].usage))

# --- item types beyond a plain message -------------------------------------
shell_start = p.parse_line(
    '{"type":"item.started","item":{"id":"i1","type":"command_execution","command":"ls -la"}}'
)
check("a starting shell call is a tool_use",
      shell_start and shell_start.kind == "tool_use" and "ls -la" in (shell_start.text or ""),
      str(shell_start and (shell_start.kind, shell_start.text)))

shell_done = p.parse_line(
    '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"ls -la",'
    '"exit_code":0,"aggregated_output":"total 0"}}'
)
check("a finished shell call is a tool_result",
      shell_done and shell_done.kind == "tool_result" and shell_done.is_error is False,
      str(shell_done and (shell_done.kind, shell_done.is_error)))

failed = p.parse_line(
    '{"type":"item.completed","item":{"id":"i2","type":"command_execution","command":"false",'
    '"exit_code":1,"aggregated_output":"boom"}}'
)
check("a non-zero exit marks the result as an error", failed and failed.is_error is True,
      str(failed and failed.is_error))

reasoning = p.parse_line('{"type":"item.completed","item":{"type":"reasoning","text":"thinking..."}}')
check("reasoning maps to thinking", reasoning and reasoning.kind == "thinking",
      str(reasoning and reasoning.kind))

edit = p.parse_line(
    '{"type":"item.completed","item":{"type":"file_change","changes":[{"path":"a.py"}]}}'
)
check("file changes surface as an edit tool call",
      edit and edit.kind == "tool_use" and edit.tool_name == "edit", str(edit and edit.kind))

limited = p.parse_line('{"type":"turn.failed","error":{"message":"You have hit your usage limit"}}')
check("a usage limit is detected so failover can trigger",
      limited and limited.is_error and limited.rate_limited, str(limited and limited.rate_limited))

noise = p.parse_line("Reading additional input from stdin...")
check("the stdin notice is not shown as an agent message", noise is None, str(noise))

junk = p.parse_line("some unexpected plain text")
check("other non-JSON output is still kept", junk and junk.kind == "system", str(junk))

# --- argv --------------------------------------------------------------
spec = p.build_run(
    prompt="hi", model="gpt-5.6", provider_session_id=None, permission_mode="read-only",
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
    account_env={"CODEX_HOME": "/tmp/x"},
)
check("exec argv has no --ask-for-approval (rejected by `codex exec`)",
      "--ask-for-approval" not in spec.argv, " ".join(spec.argv))
check("exec argv skips the git-repo check so non-repo workspaces work",
      "--skip-git-repo-check" in spec.argv, " ".join(spec.argv))
check("account credentials are routed via CODEX_HOME",
      spec.env.get("CODEX_HOME") == "/tmp/x", str(spec.env))
check("prompt is passed last", spec.argv[-1] == "hi", " ".join(spec.argv))

resume = p.build_run(
    prompt="again", model=None, provider_session_id="abc123", permission_mode=None,
    system_prompt=None, allowed_tools=None, extra_args=[], stream_partials=False,
)
check("a follow-up turn resumes the thread",
      resume.argv[1:3] == ["exec", "resume"] and "abc123" in resume.argv, " ".join(resume.argv))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
