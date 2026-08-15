"""Pins subagent handling to output captured from a real Claude Code run.

The feature had been coded for and never exercised: the first live run through
`ClaudeProvider` was what produced these lines. They are copied verbatim from
`claude -p --output-format stream-json --verbose --forward-subagent-text`
(claude-cli 2.1.232, model sonnet) driving one `general-purpose` subagent that
ran a Bash command, with the long thinking signatures and token counters
elided and the `init` line abridged. Nothing else was touched.

What matters here and is easy to break silently:

* the spawning call is named `Agent`, not `Task` — the CLI renamed it, and a
  parser that only knows `Task` records the spawn with no subagent name;
* a child message identifies its subagent with `subagent_type`, not
  `agent_name`;
* the CLI narrates the subagent with `task_*` `system` lines that arrive
  *interleaved* with that subagent's own messages, so leaving them
  unattributed splits one subagent into several blocks in the UI.

Deliberately offline: it replays recorded text and spends no API quota.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from app.providers.claude import ClaudeProvider  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        failures.append(label)


p = ClaudeProvider()

SPAWN_ID = "toolu_013TzCgnCEoerKxpgRyw9Fdn"

# --- captured from a live run ----------------------------------------------
LIVE = [
    '{"type":"system","subtype":"init","cwd":"/workspaces","session_id":"d1528175",'
    '"tools":["Task","Bash","Read","Write"],'
    '"mcp_servers":[{"name":"aiops","status":"connected"}],"model":"claude-sonnet-5",'
    '"permissionMode":"default","slash_commands":["goal","usage"],'
    '"agents":["claude","Explore","general-purpose","Plan"],'
    '"claude_code_version":"2.1.232"}',

    '{"type":"assistant","message":{"model":"claude-sonnet-5","id":"msg_1","type":"message",'
    '"role":"assistant","content":[{"type":"tool_use","id":"' + SPAWN_ID + '","name":"Agent",'
    '"input":{"description":"Run ping loopback test","prompt":"Using the Bash tool, run the '
    'command:\\n\\nping -c 1 -W 1 127.0.0.1","subagent_type":"general-purpose",'
    '"run_in_background":false},"caller":{"type":"direct"}}]},"parent_tool_use_id":null,'
    '"session_id":"d1528175"}',

    '{"type":"system","subtype":"task_started","task_id":"a981945b","tool_use_id":"' + SPAWN_ID + '",'
    '"description":"Run ping loopback test","subagent_type":"general-purpose",'
    '"task_type":"local_agent","session_id":"d1528175"}',

    '{"type":"user","message":{"role":"user","content":[{"type":"text","text":"Using the Bash '
    'tool, run the command:\\n\\nping -c 1 -W 1 127.0.0.1"}]},"parent_tool_use_id":"' + SPAWN_ID + '",'
    '"session_id":"d1528175","subagent_type":"general-purpose",'
    '"task_description":"Run ping loopback test"}',

    '{"type":"system","subtype":"task_progress","task_id":"a981945b","tool_use_id":"' + SPAWN_ID + '",'
    '"description":"Running Ping localhost once with 1 second timeout",'
    '"subagent_type":"general-purpose","last_tool_name":"Bash","session_id":"d1528175"}',

    '{"type":"assistant","message":{"model":"claude-sonnet-5","id":"msg_2","type":"message",'
    '"role":"assistant","content":[{"type":"tool_use","id":"toolu_01AoCG3a","name":"Bash",'
    '"input":{"command":"ping -c 1 -W 1 127.0.0.1","description":"Ping localhost once"}}]},'
    '"parent_tool_use_id":"' + SPAWN_ID + '","session_id":"d1528175",'
    '"subagent_type":"general-purpose","task_description":"Run ping loopback test"}',

    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result",'
    '"content":"Exit code 127\\n/bin/bash: line 1: ping: command not found","is_error":true,'
    '"tool_use_id":"toolu_01AoCG3a"}]},"parent_tool_use_id":"' + SPAWN_ID + '",'
    '"session_id":"d1528175","subagent_type":"general-purpose",'
    '"task_description":"Run ping loopback test"}',

    '{"type":"assistant","message":{"model":"claude-sonnet-5","id":"msg_3","type":"message",'
    '"role":"assistant","content":[{"type":"text","text":"The command failed."}]},'
    '"parent_tool_use_id":"' + SPAWN_ID + '","session_id":"d1528175",'
    '"subagent_type":"general-purpose","task_description":"Run ping loopback test"}',

    '{"type":"system","subtype":"task_updated","task_id":"a981945b",'
    '"patch":{"status":"completed","end_time":1786815733495},"session_id":"d1528175"}',

    '{"type":"system","subtype":"task_notification","task_id":"a981945b",'
    '"tool_use_id":"' + SPAWN_ID + '","status":"completed",'
    '"summary":"The command failed.","session_id":"d1528175"}',

    '{"type":"user","message":{"role":"user","content":[{"tool_use_id":"' + SPAWN_ID + '",'
    '"type":"tool_result","content":[{"type":"text","text":"The command failed."}]}]},'
    '"parent_tool_use_id":null,"session_id":"d1528175"}',

    '{"type":"assistant","message":{"model":"claude-sonnet-5","id":"msg_4","type":"message",'
    '"role":"assistant","content":[{"type":"text","text":"DONE"}]},'
    '"parent_tool_use_id":null,"session_id":"d1528175"}',

    '{"type":"result","subtype":"success","is_error":false,"session_id":"d1528175",'
    '"result":"DONE","permission_denials":[],"total_cost_usd":0.0605067}',
]

events = [e for e in (p.parse_line(line) for line in LIVE) if e is not None]

# --- the flag that makes a subagent visible at all -------------------------
spec = p.build_run(
    prompt="hi",
    model=None,
    provider_session_id=None,
    permission_mode=None,
    system_prompt=None,
    allowed_tools=None,
    extra_args=[],
    stream_partials=False,
    approval_mode="ask",
    approval_token="t",
)
check("a run forwards subagent text, or only their tool calls arrive",
      "--forward-subagent-text" in spec.argv, str(spec.argv))
check("no preset means no --allowedTools, so the spawning tool stays available",
      "--allowedTools" not in spec.argv, str(spec.argv))

restricted = p.build_run(
    prompt="hi", model=None, provider_session_id=None, permission_mode=None,
    system_prompt=None, allowed_tools="Bash,Read", extra_args=[], stream_partials=False,
    approval_mode="ask", approval_token="t",
)
check("a preset that lists tools passes the list through verbatim",
      "--allowedTools" in restricted.argv
      and restricted.argv[restricted.argv.index("--allowedTools") + 1] == "Bash,Read",
      str(restricted.argv))

# --- the spawn -------------------------------------------------------------
spawns = [e for e in events if e.spawns_tool_use_id]
check("the spawning call is recorded, under the name this CLI uses",
      len(spawns) == 1 and spawns[0].kind == "tool_use" and spawns[0].tool_name == "Agent",
      str([(e.tool_name, e.kind) for e in spawns]))
check("the spawn exposes the id its children point back at",
      spawns and spawns[0].spawns_tool_use_id == SPAWN_ID,
      str(spawns and spawns[0].spawns_tool_use_id))
check("the spawn is named from subagent_type",
      spawns and spawns[0].agent_name == "general-purpose",
      str(spawns and spawns[0].agent_name))

# --- the children ----------------------------------------------------------
children = [e for e in events if e.parent_tool_use_id]
check("every subagent event names its parent call",
      children and all(e.parent_tool_use_id == SPAWN_ID for e in children),
      str({e.parent_tool_use_id for e in children}))
check("and every subagent *message* is named at parse time, without the runner's spawn map",
      children and all(
          e.agent_name == "general-purpose" for e in children if e.kind != "system"
      ),
      str({(e.kind, e.agent_name) for e in children}))

sub_tools = [(e.tool_name, e.text) for e in children if e.kind == "tool_use"]
check("a subagent's own tool call is recorded as a nested tool_use",
      sub_tools == [("Bash", "ping -c 1 -W 1 127.0.0.1")], str(sub_tools))
check("the main loop's own events stay unattributed",
      [e.kind for e in events if not e.parent_tool_use_id][-2:] == ["assistant", "result"],
      str([e.kind for e in events if not e.parent_tool_use_id]))

# --- the progress narration ------------------------------------------------
notes = [e for e in events if (e.raw or {}).get("subtype", "").startswith("task_")]
check("the CLI's task_* narration is nested rather than left at the top level",
      notes and all(e.parent_tool_use_id == SPAWN_ID for e in notes),
      str([(e.raw.get("subtype"), e.parent_tool_use_id) for e in notes]))
check("a progress line says what the subagent is doing, not just 'task_progress'",
      any(e.text == "Running Ping localhost once with 1 second timeout" for e in notes),
      str([e.text for e in notes]))
check("the status patch with no tool_use_id is dropped, not orphaned",
      not any((e.raw or {}).get("subtype") == "task_updated" for e in events),
      str([(e.raw or {}).get("subtype") for e in events]))


# --- what the UI will actually draw ----------------------------------------
def group(items):
    """Mirrors groupSubagents() in frontend/src/views/Chat.tsx."""
    rows: list = []
    for e in items:
        if not e.parent_tool_use_id:
            rows.append(["event", None, [e]])
            continue
        if rows and rows[-1][0] == "subagent" and rows[-1][1] == e.parent_tool_use_id:
            rows[-1][2].append(e)
        else:
            rows.append(["subagent", e.parent_tool_use_id, [e]])
    return rows


rows = group(events)
groups = [r for r in rows if r[0] == "subagent"]
check("one subagent draws as exactly one block, not one per interruption",
      len(groups) == 1, f"{len(groups)} groups: {[len(g[2]) for g in groups]}")
check("and that block holds every one of the subagent's steps",
      groups and len(groups[0][2]) == len(children),
      str(groups and (len(groups[0][2]), len(children))))
# The UI labels a block from the first event in it that carries a name, so a
# progress note with none of its own does not leave the block anonymous.
check("and the block ends up labelled with the subagent's name",
      groups and next((e.agent_name for e in groups[0][2] if e.agent_name), None)
      == "general-purpose",
      str(groups and [e.agent_name for e in groups[0][2]]))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All checks passed.")
