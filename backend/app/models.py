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


class Team(Base):
    """A group of people who work in the same place.

    The opposite of a stored system, deliberately: a credential is private to
    whoever put it in, while a session is somewhere work happens, so it can
    belong to a team and be everybody's. Administrators decide who is in one,
    because membership is what grants that visibility.
    """

    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    members: Mapped[list[TeamMember]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class TeamMember(Base):
    __tablename__ = "team_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_user"),)


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
    # Plan-limit state reported by the CLI itself (Claude emits a
    # rate_limit_event carrying the window type, status and reset time).
    limit_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    limit_window: Mapped[str | None] = mapped_column(String(32), nullable=True)
    limit_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    limit_seen_at: Mapped[datetime | None] = mapped_column(
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
    # Validated against the provider adapter's list, not this comment:
    # claude: default|acceptEdits|plan|auto|dontAsk|bypassPermissions
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
    # ask | auto | bypass — how tool permissions are handled for this session.
    # Null means "use the instance default", so changing that default moves
    # existing sessions with it.
    approval_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Whose session this is. Stored credentials are private to their owner, so
    # the runner needs to know on whose behalf a turn runs before it decides
    # which systems to make reachable from it.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The team this conversation belongs to, if any: every member of it sees
    # this session, and keeps seeing it as people join and leave.
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, index=True
    )
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    # Slash commands the CLI reported for this session at startup — the
    # authoritative list, better than anything we could hardcode.
    available_commands: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    preset: Mapped[AgentPreset | None] = relationship(lazy="joined")
    workspace: Mapped[Workspace | None] = relationship(lazy="joined")
    account: Mapped[ProviderAccount | None] = relationship(
        lazy="joined", foreign_keys=[account_id]
    )
    shares: Mapped[list[SessionShare]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def shared_user_ids(self) -> list[int]:
        return [share.user_id for share in self.shares]


class SessionShare(Base):
    """One session handed to one named user.

    Beside the team route rather than instead of it: a conversation often needs
    one more pair of eyes without moving the whole thing into a team space.
    """

    __tablename__ = "session_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    __table_args__ = (UniqueConstraint("session_id", "user_id", name="uq_session_share_user"),)


class Attachment(Base):
    """A file the operator handed to the agent with a message.

    `id` is generated here and becomes a directory of its own on disk, so the
    client's filename never has to be unique: two uploads of screenshot.png
    coexist, and neither can overwrite the other. `run_id` is null while the
    file is still sitting in the composer and is set when the turn is sent.
    """

    __tablename__ = "attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Sanitised. The name the client sent is never stored or used as a path.
    filename: Mapped[str] = mapped_column(String(255))
    #: What downloads are served as — derived from the extension, never from the
    #: client's Content-Type, and never anything a browser will render inline.
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Target(Base):
    """A system an agent can reach by name — a host plus how to log into it.

    Secrets are stored encrypted (see crypto.py) and are never returned by the
    API: the UI writes them and afterwards only sees whether one is set. At run
    time the private key is loaded into a per-run ssh-agent rather than written
    to disk, so `ssh <slug>` works without the key ever landing in a file.
    """

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    #: What the agent types: `ssh <slug>`.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # key | password
    auth_type: Mapped[str] = mapped_column(String(32), default="key")

    private_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    passphrase_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # accept-new trusts on first use and pins afterwards; strict requires the
    # key below to already be known. Never "no" — that accepts any host key.
    host_key_policy: Mapped[str] = mapped_column(String(32), default="accept-new")
    known_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Reach this host through a relay node instead of from the AIOps server.
    # Null means "connect directly", which is what every system did before
    # relays existed and still the right answer for anything on this network.
    relay_node_id: Mapped[int | None] = mapped_column(
        ForeignKey("relay_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Who controls it. Deleting this user is refused unless the target is first
    # handed to someone with manage rights, so a credential can never end up
    # owned by nobody and visible to no one.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    grants: Mapped[list[TargetAccess]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class TargetAccess(Base):
    """Who may reach a target besides its owner.

    Unlike provider accounts, an empty grant list here means *nobody* but the
    owner — a stored credential is private by default, and administrators get
    no implicit access to one they were not given.
    """

    __tablename__ = "target_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # use: agents in this user's sessions may connect through it.
    # manage: additionally edit it, replace the credential, and grant others.
    level: Mapped[str] = mapped_column(String(16), default="use")

    __table_args__ = (UniqueConstraint("target_id", "user_id", name="uq_target_user"),)


class RelayNode(Base):
    """A machine on some other network that AIOps can open connections through.

    It is a jump point and nothing else: the agents keep running on the AIOps
    server, and a node only ever opens the one TCP connection it is asked for
    and copies bytes. It never holds a provider login, an SSH key, or a
    prompt — what crosses it is an already-encrypted SSH stream whose endpoints
    are the AIOps container and the far host.

    The node dials *out* and holds the connection open, so it works behind NAT
    with no inbound rule. That inverts the usual trust question: since anything
    holding the credential can present itself as this node, the credential is
    checked on every reconnect rather than once at enrolment, and a node stays
    `pending` — unusable — until an administrator says otherwise.
    """

    __tablename__ = "relay_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    #: What a stored system points at, and what appears in a ProxyCommand.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | approved | revoked. Only "approved" may carry traffic; the other
    # two are refused at the socket, not merely hidden from the UI.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)

    # Hashed like a password, never stored or logged in the clear. The
    # enrolment token is shown once when it is minted and is single-use: it is
    # cleared the moment a node exchanges it for a credential, so a token left
    # in a shell history or a config management repo is already spent.
    enrolment_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enrolment_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The long-lived secret the agent presents on every reconnect.
    credential_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    enrolled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Agent version and what the node says about itself. Self-reported, so it
    #: describes the node rather than authorising anything.
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reported_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    networks: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Same rule as a stored system: a route into somebody's network is theirs,
    # and administering AIOps does not confer it. Approval is separate, and is
    # an administrator's call.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    grants: Mapped[list[RelayNodeAccess]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class RelayNodeAccess(Base):
    """Who may route through a node besides its owner.

    Deliberately identical in shape and meaning to TargetAccess: a node is a way
    into a network, so it is private by default and administrators get no
    implicit access to one they were not given.
    """

    __tablename__ = "relay_node_access"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("relay_nodes.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # use: stored systems this user can reach may be routed through it.
    # manage: additionally edit it, re-issue its enrolment token, and grant others.
    level: Mapped[str] = mapped_column(String(16), default="use")

    __table_args__ = (UniqueConstraint("node_id", "user_id", name="uq_relay_node_user"),)


class Approval(Base):
    """One tool call the agent paused on, waiting for a human answer.

    The agent process blocks on this row's decision, so a pending approval is a
    live conversation with a stopped subprocess, not a log entry. Rows are kept
    after the fact because "who let it run `rm -rf`" is the first question asked
    when something goes wrong.
    """

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32))
    # tool | exec | patch — what the agent wants to do, in provider terms.
    kind: Mapped[str] = mapped_column(String(32), default="tool")
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # A short human-readable rendering of the request (the command line, the
    # file being written), so the UI does not have to understand every tool.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # pending | allowed | denied | expired | cancelled
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    decided_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # Who asked for this turn. Stored credentials are materialised for *this*
    # user, not the session's owner: a session can be shared, and anyone able
    # to prompt one would otherwise inherit the owner's keys by typing into it.
    requested_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    # Whose schedule this is. The sessions it creates inherit it, so an
    # unattended run reaches exactly the stored systems its author can reach
    # rather than none at all.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
