from __future__ import annotations

import json
import re
import uuid
from typing import Any

from ..config import settings
from .base import NormalizedEvent, Provider, RunSpec


class ClaudeProvider(Provider):
    """Drives the Claude Code CLI in headless streaming mode.

    Reference: `claude -p --output-format stream-json` emits newline-delimited
    JSON. We pick the session id ourselves via `--session-id` on the first turn
    so the row is resumable even if the process dies before reporting it, then
    use `--resume <id>` for every later turn.
    """

    name = "claude"
    models = ["opus", "sonnet", "haiku", "fable"]
    permission_modes = [
        "default",
        "acceptEdits",
        "plan",
        "auto",
        "dontAsk",
        "bypassPermissions",
    ]

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
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if allowed_tools:
            argv += ["--allowedTools", allowed_tools]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        argv += self.split_args(extra_args)

        return RunSpec(argv=argv, env=dict(account_env or {}), assigned_session_id=assigned)

    def parse_line(self, line: str) -> NormalizedEvent | None:
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            # Anything non-JSON on stdout is a CLI notice, not an agent message.
            return NormalizedEvent(kind="system", text=line, raw={"stdout": line})

        etype = data.get("type")

        if etype == "system":
            subtype = data.get("subtype")
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
            return NormalizedEvent(
                kind="system",
                text=text,
                raw=data,
                provider_session_id=data.get("session_id"),
                rate_limited=rate_limited,
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
            agent = data.get("agent_name") or data.get("subagent_name")
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
                    return NormalizedEvent(
                        kind="tool_use",
                        tool_name=block.get("name"),
                        text=_summarize_tool_input(block.get("input")),
                        raw=data,
                        parent_tool_use_id=parent,
                        agent_name=agent or _spawned_agent_name(block),
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
