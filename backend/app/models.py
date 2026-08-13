from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Admins manage users and provider sign-ins. Everyone can drive agents.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Workspace(Base):
    """A directory on the server that agents are allowed to run inside."""

    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    path: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccountAccess(Base):
    """Which users may drive which provider accounts."""

    __tablename__ = "account_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    __table_args__ = (UniqueConstraint("account_id", "user_id", name="uq_account_user"),)


class ProviderAccount(Base):
    """One named sign-in for a provider — "Walt's Claude", "Jordan's Claude".

    Each account gets its own credential directory, handed to the CLI through
    CLAUDE_CONFIG_DIR / CODEX_HOME, so several subscriptions coexist in one
    container without seeing each other.
    """

    __tablename__ = "provider_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(32))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    # When this account is rate-limited, the runner retries the turn here.
    fallback_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Set while a limit is known to be in effect, so we skip it proactively.
    limited_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Stored rather than derived, so an account can adopt a credential
    # directory that already exists — such as the pre-existing ~/.claude login.
    config_dir: Mapped[str] = mapped_column(String(512))

    grants: Mapped[list[AccountAccess]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    def env(self) -> dict[str, str]:
        if self.provider == "claude":
            # Also keeps ~/.claude.json inside the volume; by default the CLI
            # writes it to $HOME, which is not persisted across recreates.
            return {"CLAUDE_CONFIG_DIR": self.config_dir}
        if self.provider == "codex":
            return {"CODEX_HOME": self.config_dir}
        return {}


class AgentPreset(Base):
    """A named (provider, model, permissions, prompt) bundle — 'which agent to use'."""

    __tablename__ = "agent_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider: Mapped[str] = mapped_column(String(32))  # claude | codex
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # claude: default|acceptEdits|plan|dontAsk|bypassPermissions
    # codex:  read-only|workspace-write|danger-full-access
    permission_mode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    allowed_tools: Mapped[str | None] = mapped_column(Text, nullable=True)  # claude only
    extra_args: Mapped[list] = mapped_column(JSON, default=list)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    """A multi-turn conversation with one agent, backed by a provider-side session."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    title: Mapped[str] = mapped_column(String(255), default="Untitled")
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True
    )
    preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_presets.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # The provider CLI's own session identifier, used to resume.
    provider_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="idle")  # idle|running|error
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    preset: Mapped[AgentPreset | None] = relationship(lazy="joined")
    workspace: Mapped[Workspace | None] = relationship(lazy="joined")
    account: Mapped[ProviderAccount | None] = relationship(
        lazy="joined", foreign_keys=[account_id]
    )


class Run(Base):
    """One turn: a prompt sent to the agent and the process that answered it."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    # queued | running | succeeded | failed | cancelled | timeout
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which account actually served the turn — may differ from the session's
    # when a limit triggered failover.
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True
    )
    failed_over_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True
    )
    # Token counters lifted from the provider's final result event, so usage can
    # be totalled without re-reading every raw payload.
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    command: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    """A normalized message from the agent's output stream."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    # system | assistant | user | tool_use | tool_result | thinking | result | error | stderr
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Set when the message came from a subagent: the id of the tool call that
    # spawned it. Lets the UI nest a subagent's steps under its parent.
    parent_tool_use_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_event_run_seq"),)


class Schedule(Base):
    """A cron entry that fires a prompt at an agent."""

    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    cron: Mapped[str] = mapped_column(String(128))
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC")
    prompt: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="SET NULL"), nullable=True
    )
    preset_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_presets.id", ondelete="SET NULL"), nullable=True
    )
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    # "new": a fresh session each firing. "continue": keep appending to one session.
    session_mode: Mapped[str] = mapped_column(String(16), default="new")
    target_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
