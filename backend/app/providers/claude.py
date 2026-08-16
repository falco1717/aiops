from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from ..agent_env import helper_script
from ..config import settings
from .base import NormalizedEvent, Provider, RunSpec

#: Key this server is registered under in --mcp-config; the permission tool is
#: then addressed as mcp__<name>__ask.
MCP_SERVER_NAME = "aiops"
#: The bridge is spawned by the CLI, so it runs as the agent user, which cannot
#: read this package. The image installs a copy where that user can; outside the
#: image this is the package's own file.
BRIDGE_SCRIPT = helper_script(
    "mcp_approver.py",
    str(Path(__file__).resolve().parent.parent / "bridge" / "mcp_approver.py"),
)

#: The browser. Registered under its own name so its tools are addressed as
#: mcp__aiops_browser__navigate and can be named individually in --allowedTools.
BROWSER_SERVER_NAME = "aiops_browser"
BROWSER_SCRIPT = helper_script(
    "mcp_browser.py",
    str(Path(__file__).resolve().parent.parent / "bridge" / "mcp_browser.py"),
)
#: Every browser tool is pre-allowed at the CLI, which is not the loosening it
#: looks like. Two of the three approval modes give the CLI no prompt tool at
#: all, so a tool left unlisted is *denied* rather than asked about, and the
#: browser would simply not work outside "ask". The gating that matters is done
#: by the bridge itself: it reads the session's approval mode and puts a click,
#: a fill or a sign-in to the same broker a Bash call goes to, while a page load
#: and a screenshot — which are reads — go through silently. Leaving it to the
#: CLI's prompt would instead ask about every navigation and nothing else.
BROWSER_TOOLS = ("navigate", "read_page", "screenshot", "click", "fill", "login", "close")


class ClaudeProvider(Provider):
    """Drives the Claude Code CLI in headless streaming mode.

    Reference: `claude -p --output-format stream-json` emits newline-delimited
    JSON. We pick the session id ourselves via `--session-id` on the first turn
    so the row is resumable even if the process dies before reporting it, then
    use `--resume <id>` for every later turn.
    """

    name = "claude"
    models = ["opus", "sonnet", "haiku", "fable"]
    # Exactly what this CLI build accepts. Note there is no "default" — passing
    # it is an error, even though the CLI *reports* permissionMode "default"
    # when given "manual".
    permission_modes = [
        "manual",
        "acceptEdits",
        "plan",
        "auto",
        "dontAsk",
        "bypassPermissions",
    ]
    # `claude --effort <level>`, quoted from `claude --help` on 2.1.232:
    # "Effort level for the current session (low, medium, high, xhigh, max)".
    # An unrecognised value is only warned about and then ignored, so the list
    # is validated here rather than left to the CLI.
    efforts = ["low", "medium", "high", "xhigh", "max"]
    supports_interactive_approval = True

    #: Permission mode implied by each AIOps approval mode, when a preset has
    #: not pinned one explicitly.
    APPROVAL_MODES = {
        "ask": "manual",
        "auto": "acceptEdits",
        "bypass": "bypassPermissions",
    }

    def build_run(
        self,
        *,
        prompt: str,
        model: str | None,
        provider_session_id: str | None,
        permission_mode: str | None,
        system_prompt: str | None,
        allowed_tools: str | None,
        extra_args: list[str],
        stream_partials: bool,
        account_env: dict[str, str] | None = None,
        approval_mode: str = "ask",
        approval_token: str | None = None,
        effort: str | None = None,
        browser: bool = False,
    ) -> RunSpec:
        argv = [
            settings.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if stream_partials:
            argv.append("--include-partial-messages")
        if settings.forward_subagent_text:
            # Without this only subagent tool calls appear; with it we get their
            # text and thinking too, so their steps can be shown like the CLI does.
            argv.append("--forward-subagent-text")

        assigned: str | None = None
        if provider_session_id:
            argv += ["--resume", provider_session_id]
        else:
            assigned = str(uuid.uuid4())
            argv += ["--session-id", assigned]

        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]

        # A preset that pins a permission mode wins; otherwise the session's
        # approval mode chooses one.
        mode = permission_mode or self.APPROVAL_MODES.get(approval_mode, "manual")
        argv += ["--permission-mode", mode]

        env = dict(account_env or {})
        servers: dict[str, dict] = {}
        extra_tools: list[str] = []

        if approval_mode == "ask" and approval_token:
            # Without a prompt tool the CLI does not pause on a permission
            # decision — it refuses the call, notes it in `permission_denials`
            # and finishes. Pointing it at our MCP bridge is what turns that
            # refusal into a question a human can answer.
            servers[MCP_SERVER_NAME] = {
                "command": sys.executable,
                "args": [str(BRIDGE_SCRIPT)],
            }
            argv += ["--permission-prompt-tool", f"mcp__{MCP_SERVER_NAME}__ask"]

        if browser and approval_token:
            servers[BROWSER_SERVER_NAME] = {
                "command": sys.executable,
                "args": [str(BROWSER_SCRIPT)],
            }
            extra_tools += [f"mcp__{BROWSER_SERVER_NAME}__{name}" for name in BROWSER_TOOLS]
            # The bridge asks for a click exactly when the session would ask
            # about a Bash call, so it has to be told which mode it is in — the
            # presence of a token is not the same question.
            env["AIOPS_BROWSER_APPROVALS"] = approval_mode

        if servers:
            argv += ["--mcp-config", json.dumps({"mcpServers": servers})]

        if approval_token:
            # Held by both bridges: it names this run and grants nothing else.
            # Issued for a browsing turn even outside "ask" mode, because the
            # browser's reach and its stored credentials are fetched with it and
            # a run without one would simply have no browser.
            env["AIOPS_APPROVAL_TOKEN"] = approval_token
            env["AIOPS_INTERNAL_URL"] = settings.internal_api_url
            env["AIOPS_PROVIDER"] = self.name
            # The bridge must outlast the server's own wait, or *it* is what
            # gives up and the request is denied by a socket rather than
            # decided by a person. Questions get the longest wait, so the
            # bridge is sized against that one and not the default.
            env["AIOPS_APPROVAL_HTTP_TIMEOUT"] = str(
                max(
                    settings.approval_timeout_seconds,
                    settings.approval_question_timeout_seconds,
                )
                + 60
            )

        if allowed_tools or extra_tools:
            # A preset's list is kept exactly as it was written and the browser's
            # tools are added to it: a preset that names its tools is narrowing
            # what the agent may do, and silently dropping its list to make room
            # for ours would be the opposite of that.
            listed = [t for t in (allowed_tools or "").split(",") if t.strip()]
            argv += ["--allowedTools", ",".join([*listed, *extra_tools])]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        argv += self.split_args(extra_args)

        return RunSpec(argv=argv, env=env, assigned_session_id=assigned)

    def parse_line(self, line: str) -> NormalizedEvent | None:
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            # Anything non-JSON on stdout is a CLI notice, not an agent message.
            return NormalizedEvent(kind="system", text=line, raw={"stdout": line})

        etype = data.get("type")

        if etype == "rate_limit_event":
            # The CLI's own view of the plan window — the only headless source
            # of "how much of your 5-hour/weekly allowance is left".
            info = data.get("rate_limit_info") or {}
            window = str(info.get("rateLimitType") or "").replace("_", "-")
            state = str(info.get("status") or "unknown")
            return NormalizedEvent(
                kind="system",
                text=f"plan limit ({window or 'window'}): {state}",
                raw=data,
                provider_session_id=data.get("session_id"),
                rate_limit_info=info,
                # "allowed" is the healthy state; anything else means the
                # window is exhausted or refusing work.
                rate_limited=state not in ("allowed", "", "unknown"),
            )

        if etype == "system":
            subtype = data.get("subtype")
            if subtype in _UNATTRIBUTABLE_TASK_NOTES:
                # A status patch on a subagent that carries no tool_use_id, so it
                # cannot be nested, and no text a human would read ("task_updated").
                # Keeping it would wedge an orphan line into the middle of the
                # subagent's steps and split the group in two.
                return None
            text = f"session started ({data.get('model', 'unknown model')})"
            rate_limited = False
            if subtype == "api_retry":
                text = (
                    f"retrying after {data.get('error')} "
                    f"(attempt {data.get('attempt')}/{data.get('max_retries')})"
                )
                rate_limited = data.get("error") == "rate_limit"
            elif subtype != "init":
                text = subtype or "system"
            commands = data.get("slash_commands") if subtype == "init" else None
            # The CLI narrates a subagent's life with task_started/task_progress/
            # task_updated/task_notification lines that arrive *interleaved* with
            # that subagent's own messages. They carry the spawning call's id, so
            # attribute them to it: left unattributed they read as main-loop
            # events and split one subagent into several fragments in the UI,
            # which groups consecutive events by parent.
            task_parent = data.get("tool_use_id") if _is_task_note(subtype) else None
            return NormalizedEvent(
                kind="system",
                text=_task_note_text(data, text) if task_parent else text,
                raw=data,
                provider_session_id=data.get("session_id"),
                rate_limited=rate_limited,
                available_commands=commands if isinstance(commands, list) else None,
                parent_tool_use_id=task_parent,
                agent_name=data.get("subagent_type") if task_parent else None,
            )

        if etype == "stream_event":
            delta = (data.get("event") or {}).get("delta") or {}
            if delta.get("type") == "text_delta":
                return NormalizedEvent(
                    kind="delta", text=delta.get("text", ""), raw=data, persist=False
                )
            if delta.get("type") == "thinking_delta":
                return NormalizedEvent(
                    kind="delta", text=delta.get("thinking", ""), raw=data, persist=False
                )
            return None

        if etype in ("assistant", "user"):
            # Non-null on messages produced by a subagent; the value is the id of
            # the tool call that spawned it, which is how the UI nests them.
            parent = data.get("parent_tool_use_id")
            # `subagent_type` is what this CLI build actually puts on a child
            # message; the other two are older spellings. Reading it here means a
            # subagent's steps are named at parse time rather than only via the
            # runner's spawn-id map.
            agent = (
                data.get("agent_name")
                or data.get("subagent_name")
                or (data.get("subagent_type") if parent else None)
            )
            blocks = ((data.get("message") or {}).get("content")) or []
            if isinstance(blocks, str):
                return NormalizedEvent(
                    kind=etype, text=blocks, raw=data,
                    parent_tool_use_id=parent, agent_name=agent,
                )
            # A message can carry several blocks; surface the most informative one
            # and keep the whole payload in `raw` for the detail view.
            texts: list[str] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "thinking":
                    thinking = block.get("thinking") or ""
                    if thinking:
                        return NormalizedEvent(
                            kind="thinking", text=thinking, raw=data,
                            parent_tool_use_id=parent, agent_name=agent,
                        )
                elif btype == "tool_use":
                    spawned = _spawned_agent_name(block)
                    return NormalizedEvent(
                        kind="tool_use",
                        tool_name=block.get("name"),
                        text=_summarize_tool_input(block.get("input")),
                        raw=data,
                        parent_tool_use_id=parent,
                        agent_name=agent or spawned,
                        # Only meaningful for a spawn; children reference this id.
                        spawns_tool_use_id=block.get("id") if spawned else None,
                    )
                elif btype == "tool_result":
                    return NormalizedEvent(
                        kind="tool_result",
                        text=_stringify(block.get("content")),
                        raw=data,
                        is_error=bool(block.get("is_error")),
                        parent_tool_use_id=parent,
                        agent_name=agent,
                    )
            joined = "\n".join(t for t in texts if t)
            if not joined:
                return None
            return NormalizedEvent(
                kind=etype, text=joined, raw=data,
                parent_tool_use_id=parent, agent_name=agent,
            )

        if etype == "result":
            is_error = bool(data.get("is_error")) or data.get("subtype") != "success"
            text = data.get("result") or data.get("subtype") or ""
            usage = data.get("usage") or {}
            return NormalizedEvent(
                kind="result",
                text=text,
                raw=data,
                provider_session_id=data.get("session_id"),
                cost_usd=data.get("total_cost_usd"),
                is_error=is_error,
                rate_limited=is_error and _looks_rate_limited(text),
                usage={
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
                    "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
                }
                if usage
                else None,
            )

        return NormalizedEvent(kind="system", text=etype or "event", raw=data)


#: Phrases the CLI uses when a plan's quota is exhausted. Matched case-insensitively
#: against the final result text so the runner can fail over to another account
#: rather than reporting a generic failure.
_LIMIT_PATTERNS = re.compile(
    r"(usage limit|rate limit|rate_limit|out of (?:credit|quota)|quota exceeded"
    r"|limit reached|try again (?:later|in)|upgrade to continue)",
    re.IGNORECASE,
)


def _looks_rate_limited(text: str) -> bool:
    return bool(text) and bool(_LIMIT_PATTERNS.search(text))


#: `system` subtypes narrating a subagent's progress. They arrive interleaved
#: with that subagent's own messages and carry `tool_use_id` — the id of the
#: spawning Agent call — so they belong inside the subagent's block.
_TASK_NOTES = frozenset({"task_started", "task_progress", "task_notification"})
#: Same family, but with no `tool_use_id` to attribute them by. Dropped rather
#: than left to orphan themselves in the middle of a subagent's steps.
_UNATTRIBUTABLE_TASK_NOTES = frozenset({"task_updated"})


def _is_task_note(subtype: Any) -> bool:
    return subtype in _TASK_NOTES


def _task_note_text(data: dict[str, Any], fallback: str) -> str:
    """Something readable for a subagent progress line.

    The bare subtype ("task_progress") says nothing; the payload's own
    description or summary is what the CLI shows a human.
    """
    for key in ("description", "summary", "status"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _spawned_agent_name(block: dict[str, Any]) -> str | None:
    """For an Agent/Task tool call, the subagent being launched."""
    if block.get("name") not in ("Agent", "Task"):
        return None
    payload = block.get("input")
    if isinstance(payload, dict):
        name = payload.get("subagent_type") or payload.get("agent") or payload.get("description")
        if isinstance(name, str):
            return name[:120]
    return None


def _summarize_tool_input(value: Any) -> str:
    if not isinstance(value, dict):
        return _stringify(value)
    for key in ("command", "file_path", "path", "pattern", "url", "prompt", "description"):
        if key in value:
            return _stringify(value[key])
    return _stringify(value)


def _stringify(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            else:
                parts.append(_stringify(item, limit))
        text = "\n".join(parts)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"
