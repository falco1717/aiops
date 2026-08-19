from __future__ import annotations

import json
import re
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
    # Taken from `codex debug models` ("Render the raw model catalog as JSON")
    # on codex-cli 0.147.0, in the catalog's own priority order and filtered to
    # the entries it marks visibility="list" and supported_in_api=true. The list
    # that used to be here named three models the catalog has never heard of.
    models = [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
    ]
    permission_modes = ["read-only", "workspace-write", "danger-full-access"]
    # Also from that catalog: `supported_reasoning_levels`. The union sits in
    # `efforts` and the models that accept fewer are named below, because Codex
    # takes any string here and only rejects it once the turn is under way.
    efforts = ["low", "medium", "high", "xhigh", "max", "ultra"]
    efforts_by_model = {
        "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
        "gpt-5.5": ["low", "medium", "high", "xhigh"],
        "gpt-5.4": ["low", "medium", "high", "xhigh"],
        "gpt-5.4-mini": ["low", "medium", "high", "xhigh"],
    }
    #: True for the provider, not for this class: an "ask" turn is routed to
    #: CodexAppServerAdapter by the runner, because `codex exec` below has no
    #: way to stop and put a question to a human.
    supports_interactive_approval = True

    #: Sandbox tier used when a preset has not pinned one. "ask" maps to the
    #: same tier as "auto" because this adapter cannot pause for a human — the
    #: app-server adapter handles that mode.
    SANDBOX_MODES = {
        "ask": "workspace-write",
        "auto": "workspace-write",
        "bypass": "danger-full-access",
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
        #: Accepted and ignored. Codex loads MCP servers from its own config
        #: file rather than from the command line, so wiring the browser in here
        #: would mean writing into a CLI's state directory on every turn. Left
        #: unbuilt rather than half-built: a Codex session gets no browser, and
        #: says so, instead of getting tools that fail on first use.
        browser: bool = False,
        #: Accepted and ignored, for the same reason as `browser` above: the
        #: pull-request bridge is wired in via the command line too.
        github: bool = False,
    ) -> RunSpec:
        argv = [settings.codex_bin, "exec"]
        if provider_session_id:
            argv += ["resume", provider_session_id]
        argv.append("--json")

        if model:
            argv += ["--model", model]
        if effort:
            # There is no --effort flag on this binary; the reasoning level is a
            # config key. Spelling it wrong is not silent — `--strict-config`
            # reports "unknown configuration field" — but the *value* is only
            # checked when the model is called, hence the validation upstream.
            argv += ["-c", f"model_reasoning_effort={effort}"]
        # `codex exec` cannot ask a human anything: --ask-for-approval is
        # rejected here (it belongs to the interactive command) and forcing
        # `-c approval_policy=...` still reports "approval: never". Interactive
        # approvals therefore go through the app-server adapter instead; this
        # path only ever runs unattended, so all it picks is a sandbox tier.
        argv += ["--sandbox", permission_mode or self.SANDBOX_MODES.get(approval_mode, "workspace-write")]
        # Codex refuses to run outside a git repository unless told otherwise.
        # AIOps workspaces are operator-chosen directories that often are not
        # repos, so without this every run in one would fail before starting.
        argv.append("--skip-git-repo-check")
        argv += self.split_args(extra_args)

        # Codex has no --append-system-prompt; fold any preset instructions into
        # the prompt itself so presets behave the same across providers.
        full_prompt = f"{system_prompt.strip()}\n\n---\n\n{prompt}" if system_prompt else prompt
        # `--` first, so everything after it is the prompt whatever it starts
        # with. Without it `codex exec` reads a prompt beginning with a dash as
        # an option and dies in argument parsing before the model is called —
        # which the provider-handoff briefing hit every single time, its first
        # line being a `--- HANDOFF BRIEFING ---` rule. Any operator message
        # starting with a dash was already failing the same way.
        argv += ["--", full_prompt]

        return RunSpec(argv=argv, env=dict(account_env or {}))

    def parse_line(self, line: str) -> NormalizedEvent | None:
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            # The CLI prints a notice about stdin before the stream begins.
            if "additional input from stdin" in line:
                return None
            return NormalizedEvent(kind="system", text=line, raw={"stdout": line})

        session_id = _find_session_id(data)
        # Older builds nested the payload under "msg"; current ones are flat.
        body = data.get("msg") if isinstance(data.get("msg"), dict) else data
        etype = str(body.get("type") or data.get("type") or "event")

        if etype in ("thread.started", "session.created", "session_configured", "task_started"):
            return NormalizedEvent(
                kind="system", text="session started", raw=data, provider_session_id=session_id
            )

        # The main carrier in current builds: one envelope, many item types.
        if etype in ("item.completed", "item.started", "item.updated"):
            item = body.get("item") if isinstance(body.get("item"), dict) else {}
            return self._parse_item(item, data, session_id, started=etype == "item.started")

        if etype in ("turn.completed", "task_complete", "turn_complete"):
            usage = body.get("usage") or {}
            return NormalizedEvent(
                kind="result",
                text=_extract_text(body) or "completed",
                raw=data,
                provider_session_id=session_id,
                cost_usd=_find_cost(body),
                usage={
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cache_read_tokens": int(usage.get("cached_input_tokens") or 0),
                    "cache_write_tokens": int(usage.get("cache_write_input_tokens") or 0),
                }
                if usage
                else None,
            )

        if etype in ("turn.failed", "error", "stream_error"):
            text = _stringify(body.get("error") or body.get("message") or body)
            return NormalizedEvent(
                kind="error",
                text=text,
                raw=data,
                is_error=True,
                rate_limited=_looks_rate_limited(text),
                provider_session_id=session_id,
            )

        # Pre-item-envelope builds emitted these directly.
        if etype in ("agent_message", "assistant_message", "message"):
            text = _extract_text(body)
            return (
                NormalizedEvent(kind="assistant", text=text, raw=data, provider_session_id=session_id)
                if text
                else None
            )
        if etype in ("agent_reasoning", "reasoning"):
            text = _extract_text(body)
            return (
                NormalizedEvent(kind="thinking", text=text, raw=data, provider_session_id=session_id)
                if text
                else None
            )

        if etype == "turn.started":
            return None  # no information beyond the status we already track

        return NormalizedEvent(
            kind="system", text=etype, raw=data, provider_session_id=session_id
        )

    @staticmethod
    def _parse_item(
        item: dict[str, Any], data: dict[str, Any], session_id: str | None, started: bool
    ) -> NormalizedEvent | None:
        itype = str(item.get("type") or "")

        if itype in ("agent_message", "assistant_message"):
            if started:
                return None  # the completed event carries the text
            text = _extract_text(item)
            return (
                NormalizedEvent(kind="assistant", text=text, raw=data, provider_session_id=session_id)
                if text
                else None
            )

        if itype == "reasoning":
            text = _extract_text(item)
            return (
                NormalizedEvent(kind="thinking", text=text, raw=data, provider_session_id=session_id)
                if text
                else None
            )

        if itype in ("command_execution", "local_shell_call", "shell_call"):
            command = _stringify(item.get("command") or item.get("input"))
            if started:
                return NormalizedEvent(
                    kind="tool_use", tool_name="shell", text=command, raw=data,
                    provider_session_id=session_id,
                )
            exit_code = item.get("exit_code")
            output = _stringify(
                item.get("aggregated_output") or item.get("output") or item.get("stdout")
            )
            return NormalizedEvent(
                kind="tool_result",
                text=output or command,
                raw=data,
                is_error=bool(exit_code),
                provider_session_id=session_id,
            )

        if itype in ("file_change", "patch_apply"):
            changes = item.get("changes") or item.get("files") or item
            return NormalizedEvent(
                kind="tool_use", tool_name="edit", text=_stringify(changes), raw=data,
                provider_session_id=session_id,
            )

        if itype in ("mcp_tool_call", "web_search"):
            return NormalizedEvent(
                kind="tool_use",
                tool_name=itype,
                text=_stringify(item.get("query") or item.get("arguments") or item),
                raw=data,
                provider_session_id=session_id,
            )

        if itype == "error":
            text = _stringify(item.get("message") or item)
            return NormalizedEvent(
                kind="error", text=text, raw=data, is_error=True,
                rate_limited=_looks_rate_limited(text), provider_session_id=session_id,
            )

        if itype == "todo_list":
            return NormalizedEvent(
                kind="system", text=_stringify(item.get("items") or item), raw=data,
                provider_session_id=session_id,
            )

        if started:
            return None
        return NormalizedEvent(
            kind="system", text=itype or "item", raw=data, provider_session_id=session_id
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


#: Codex wording when a plan's quota is exhausted, so the runner can fail over.
_LIMIT_PATTERNS = re.compile(
    r"(usage limit|rate limit|rate_limit|quota|too many requests|429"
    r"|limit reached|try again (?:later|in))",
    re.IGNORECASE,
)


def _looks_rate_limited(text: str) -> bool:
    return bool(text) and bool(_LIMIT_PATTERNS.search(text))


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
