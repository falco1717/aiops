from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth --------------------------------------------------------------
class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(ORM):
    id: int
    username: str
    is_admin: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None


class UserSummary(BaseModel):
    """The least that lets one user share something with another."""

    id: int
    username: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    is_admin: bool = False
    must_change_password: bool = True


class UserPatch(BaseModel):
    is_admin: bool | None = None
    must_change_password: bool | None = None


class UserPasswordReset(BaseModel):
    new_password: str = Field(min_length=8)
    must_change_password: bool = True


# --- teams -------------------------------------------------------------
class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    member_ids: list[int] = []


class TeamPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    member_ids: list[int] | None = None


class TeamOut(BaseModel):
    id: int
    name: str
    description: str | None
    member_ids: list[int]
    #: How many sessions this team owns, so deleting one can say what it costs.
    session_count: int
    created_at: datetime


# --- workspaces --------------------------------------------------------
class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    path: str
    description: str | None = None


class WorkspaceOut(ORM):
    id: int
    name: str
    path: str
    description: str | None
    created_at: datetime


class WorkspaceStatus(BaseModel):
    exists: bool
    is_git: bool
    branch: str | None = None
    dirty_files: list[str] = []
    head: str | None = None
    error: str | None = None


# --- presets -----------------------------------------------------------
class PresetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str
    model: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    permission_mode: str | None = None
    allowed_tools: str | None = None
    extra_args: list[str] = []
    is_default: bool = False


class PresetOut(ORM):
    id: int
    name: str
    provider: str
    model: str | None
    description: str | None
    system_prompt: str | None
    permission_mode: str | None
    allowed_tools: str | None
    extra_args: list[str]
    is_default: bool


# --- sessions & runs ---------------------------------------------------
class SessionIn(BaseModel):
    title: str | None = None
    provider: str
    model: str | None = None
    account_id: int | None = None
    preset_id: int | None = None
    workspace_id: int | None = None
    team_id: int | None = None
    approval_mode: str | None = None  # ask | auto | bypass
    prompt: str | None = None  # optional first turn
    attachment_ids: list[str] = []


class SessionPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None
    model: str | None = None
    account_id: int | None = None
    preset_id: int | None = None
    workspace_id: int | None = None
    approval_mode: str | None = None
    # Sharing, and handing the session on. Unlike the fields above these are the
    # owner's to change, so they are separated out by the router.
    team_id: int | None = None
    shared_user_ids: list[int] | None = None
    owner_id: int | None = None


class SessionOut(ORM):
    id: str
    title: str
    provider: str
    model: str | None
    account_id: int | None
    preset_id: int | None
    workspace_id: int | None
    approval_mode: str | None
    provider_session_id: str | None
    status: str
    archived: bool
    owner_id: int | None
    team_id: int | None
    shared_user_ids: list[int]
    created_at: datetime
    updated_at: datetime


class PromptIn(BaseModel):
    prompt: str = Field(min_length=1)
    #: Already-uploaded attachments to hand to the agent with this turn.
    attachment_ids: list[str] = []


class RunOut(ORM):
    id: int
    session_id: str
    schedule_id: int | None
    prompt: str
    status: str
    exit_code: int | None
    error: str | None
    account_id: int | None
    failed_over_from_id: int | None
    input_tokens: int | None
    output_tokens: int | None
    context_tokens: int | None
    cost_usd: float | None
    command: list[str]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class EventOut(ORM):
    id: int
    run_id: int
    seq: int
    kind: str
    text: str | None
    tool_name: str | None
    parent_tool_use_id: str | None
    agent_name: str | None
    created_at: datetime


class CapabilityOut(BaseModel):
    name: str
    kind: str
    description: str
    source: str


class AttachmentOut(ORM):
    id: str
    session_id: str
    #: Null while the file is still in the composer, set when the turn is sent.
    run_id: int | None
    filename: str
    content_type: str
    size: int
    created_at: datetime


class SessionFile(BaseModel):
    """One file under the session's workspace, as offered for download."""

    path: str
    size: int
    modified: datetime


class SessionFilesOut(BaseModel):
    root: str
    files: list[SessionFile]
    #: The listing hit its cap. Said out loud so nobody reads a bounded walk as
    #: "this is everything the agent wrote".
    truncated: bool
    max_files: int
    max_depth: int


class TranscriptOut(BaseModel):
    session: SessionOut
    runs: list[RunOut]
    events: list[EventOut]
    attachments: list[AttachmentOut] = []


# --- schedules ---------------------------------------------------------
class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron: str
    timezone_name: str = "UTC"
    prompt: str = Field(min_length=1)
    provider: str
    model: str | None = None
    account_id: int | None = None
    preset_id: int | None = None
    workspace_id: int | None = None
    session_mode: str = "new"  # new | continue
    enabled: bool = True


class ScheduleOut(ORM):
    id: int
    name: str
    cron: str
    timezone_name: str
    prompt: str
    provider: str
    model: str | None
    account_id: int | None
    preset_id: int | None
    workspace_id: int | None
    session_mode: str
    target_session_id: str | None
    enabled: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_status: str | None


# --- providers ---------------------------------------------------------
class ProviderOut(BaseModel):
    name: str
    label: str
    models: list[str]
    permission_modes: list[str]
    binary: str
    available: bool
    version: str | None = None
    authenticated: bool | None = None
    account: str | None = None
    detail: str | None = None


class LoginFlowOut(BaseModel):
    provider: str
    status: str
    verification_url: str | None = None
    user_code: str | None = None
    needs_code: bool = False
    message: str | None = None
    expires_in: int = 0


class LoginCodeIn(BaseModel):
    code: str = Field(min_length=1)


# --- provider accounts -------------------------------------------------
class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str
    description: str | None = None
    is_default: bool = False


class AccountPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None
    fallback_account_id: int | None = None
    allowed_user_ids: list[int] | None = None


class AccountOut(BaseModel):
    id: int
    name: str
    provider: str
    slug: str
    description: str | None
    is_default: bool
    fallback_account_id: int | None
    limited_until: datetime | None
    limit_status: str | None
    limit_window: str | None
    limit_resets_at: datetime | None
    config_dir: str
    signed_in: bool | None
    account_detail: str | None
    allowed_user_ids: list[int]
    usable_by_me: bool


# --- usage -------------------------------------------------------------
class UsageWindow(BaseModel):
    label: str
    since: datetime
    runs: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: float


class SessionContextOut(BaseModel):
    session_id: str
    last_context_tokens: int | None
    peak_context_tokens: int | None
    total_tokens: int
    runs: int
    cost_usd: float


class UsageOut(BaseModel):
    windows: list[UsageWindow]
    by_account: list[dict]
    note: str


# --- approvals ---------------------------------------------------------
class ApprovalOut(ORM):
    id: int
    run_id: int
    session_id: str
    provider: str
    kind: str
    tool_name: str | None
    summary: str | None
    request: dict | None
    status: str
    decided_by_id: int | None
    decided_at: datetime | None
    note: str | None
    created_at: datetime


class ApprovalDecision(BaseModel):
    allowed: bool
    note: str | None = None


# --- targets (systems an agent can reach) ------------------------------
class TargetGrant(BaseModel):
    user_id: int
    level: str = "use"  # use | manage


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=128)
    port: int = Field(default=22, ge=1, le=65535)
    description: str | None = None
    auth_type: str = "key"  # key | password
    # Write-only. Absent means "leave whatever is stored"; empty string clears.
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None
    host_key_policy: str = "accept-new"
    known_host_key: str | None = None
    grants: list[TargetGrant] | None = None


class TargetPatch(BaseModel):
    name: str | None = None
    hostname: str | None = None
    username: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    description: str | None = None
    auth_type: str | None = None
    private_key: str | None = None
    passphrase: str | None = None
    password: str | None = None
    host_key_policy: str | None = None
    known_host_key: str | None = None
    grants: list[TargetGrant] | None = None
    owner_id: int | None = None


class TargetOut(BaseModel):
    """Never carries a secret — only whether one is stored."""

    id: int
    name: str
    slug: str
    hostname: str
    port: int
    username: str
    description: str | None
    auth_type: str
    has_private_key: bool
    has_passphrase: bool
    has_password: bool
    host_key_policy: str
    has_known_host_key: bool
    owner_id: int | None
    grants: list[TargetGrant]
    #: owner | manage | use — what the caller may do with it.
    my_level: str
    created_at: datetime
