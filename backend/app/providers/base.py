from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedEvent:
    """One agent output line, flattened into a shape the UI can render."""

    kind: str  # system|assistant|user|tool_use|tool_result|thinking|result|error|delta
    text: str | None = None
    tool_name: str | None = None
    raw: dict[str, Any] | None = None
    # Side-channel data lifted out of the event for the runner to act on.
    provider_session_id: str | None = None
    cost_usd: float | None = None
    is_error: bool = False
    # Deltas are streamed live but never written to the database.
    persist: bool = True
    # Set when the message came from a subagent rather than the main loop.
    parent_tool_use_id: str | None = None
    agent_name: str | None = None
    # On a tool call that spawns a subagent, the id later messages point back at.
    # Lets the runner label a subagent's steps with the name it was launched as.
    spawns_tool_use_id: str | None = None
    # Token counters from the final result event, for the usage panel.
    usage: dict[str, int] | None = None
    # True when the provider says the account is out of quota, so the runner
    # can fail over to another account instead of surfacing an error.
    rate_limited: bool = False
    # Plan-limit state the CLI reported (window, status, reset time).
    rate_limit_info: dict[str, Any] | None = None
    # Slash commands the CLI advertised for this session.
    available_commands: list[str] | None = None


@dataclass
class RunSpec:
    """Everything the runner needs to launch one turn."""

    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # Session id we chose ourselves, when the provider lets us pick one upfront.
    assigned_session_id: str | None = None


class Provider:
    """Adapter around one agent CLI."""

    name: str = "base"
    #: Models offered in the UI. Free text is also accepted.
    models: list[str] = []
    #: Values accepted by AgentPreset.permission_mode for this provider.
    permission_modes: list[str] = []

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
        raise NotImplementedError

    def parse_line(self, line: str) -> NormalizedEvent | None:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    @staticmethod
    def split_args(value: str | list | None) -> list[str]:
        if not value:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return shlex.split(value)
