from __future__ import annotations

import json
from typing import Any

from ..config import settings
from .base import NormalizedEvent, Provider, RunSpec


class CodexProvider(Provider):
    """Drives the OpenAI Codex CLI via `codex exec --json`.

    Codex does not let us assign a session id upfront, so the id is scraped out
    of the event stream and stored for `codex exec resume <id>`. Its event
    schema has shifted between releases, so the parser below is deliberately
    tolerant: it recognises the shapes we know about, falls back to a generic
    rendering for anything else, and always keeps the raw payload.
    """

    name = "codex"
    models = ["gpt-5.6-terra", "gpt-5.6", "gpt-5.6-codex", "gpt-5-codex"]
    permission_modes = ["read-only", "workspace-write", "danger-full-access"]

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
        argv = [settings.codex_bin, "exec"]
        if provider_session_id:
            argv += ["resume", provider_session_id]
        argv.append("--json")

        if model:
            argv += ["--model", model]
        argv += ["--sandbox", permission_mode or "workspace-write"]
        # Nothing can answer an approval prompt in a headless run.
        argv += ["--ask-for-approval", "never"]
        argv += self.split_args(extra_args)

        # Codex has no --append-system-prompt; fold any preset instructions into
        # the prompt itself so presets behave the same across providers.
        full_prompt = f"{system_prompt.strip()}\n\n---\n\n{prompt}" if system_prompt else prompt
        argv.append(full_prompt)

        return RunSpec(argv=argv, env=dict(account_env or {}))

    def parse_line(self, line: str) -> NormalizedEvent | None:
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return NormalizedEvent(kind="system", text=line, raw={"stdout": line})

        session_id = _find_session_id(data)
        # Older builds nest the payload under "msg"; newer ones are flat.
        body = data.get("msg") if isinstance(data.get("msg"), dict) else data
        etype = str(body.get("type") or data.get("type") or "event")

        if etype in ("session.created", "session_configured", "thread.started", "task_started"):
            return NormalizedEvent(
                kind="system", text="session started", raw=data, provider_session_id=session_id
            )

        if etype in ("agent_message", "assistant_message", "item.completed", "message"):
            text = _extract_text(body)
            if text:
                return NormalizedEvent(
                    kind="assistant", text=text, raw=data, provider_session_id=session_id
                )
            return None

        if etype in ("agent_reasoning", "reasoning"):
            text = _extract_text(body)
            return (
                NormalizedEvent(kind="thinking", text=text, raw=data, provider_session_id=session_id)
                if text
                else None
            )

        if "command" in etype or etype in ("exec_command_begin", "shell_call"):
            return NormalizedEvent(
                kind="tool_use",
                tool_name="shell",
                text=_stringify(body.get("command") or body.get("input")),
                raw=data,
                provider_session_id=session_id,
            )

        if etype in ("exec_command_end", "shell_call_output", "patch_apply_end"):
            return NormalizedEvent(
                kind="tool_result",
                text=_stringify(body.get("stdout") or body.get("output") or body.get("aggregated_output")),
                raw=data,
                is_error=bool(body.get("exit_code")),
                provider_session_id=session_id,
            )

        if etype in ("error", "stream_error"):
            return NormalizedEvent(
                kind="error",
                text=_stringify(body.get("message") or body),
                raw=data,
                is_error=True,
                provider_session_id=session_id,
            )

        if etype in ("task_complete", "turn.completed", "turn_complete"):
            return NormalizedEvent(
                kind="result",
                text=_extract_text(body) or "completed",
                raw=data,
                provider_session_id=session_id,
                cost_usd=_find_cost(body),
            )

        return NormalizedEvent(
            kind="system", text=etype, raw=data, provider_session_id=session_id
        )


def _find_session_id(data: dict[str, Any]) -> str | None:
    for key in ("session_id", "thread_id", "conversation_id", "rollout_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    for container in ("msg", "session", "thread"):
        nested = data.get(container)
        if isinstance(nested, dict):
            found = _find_session_id(nested)
            if found:
                return found
            value = nested.get("id")
            if isinstance(value, str) and value:
                return value
    return None


def _find_cost(body: dict[str, Any]) -> float | None:
    for key in ("total_cost_usd", "cost_usd", "cost"):
        value = body.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _extract_text(body: Any) -> str:
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    for key in ("message", "text", "content", "last_agent_message", "delta"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            text = _stringify(value)
            if text:
                return text
    item = body.get("item")
    if isinstance(item, dict):
        return _extract_text(item)
    return ""


def _stringify(value: Any, limit: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            else:
                parts.append(_stringify(item, limit))
        text = "\n".join(p for p in parts if p)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n… [{len(text) - limit} more chars]"
