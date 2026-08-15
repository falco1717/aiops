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
    #: Null means "call them by their username". Resolved in app/names.py on
    #: this side and in src/names.ts on the client — never inline.
    display_name: str | None = None
    is_admin: bool
    must_change_password: bool
    created_at: datetime
    last_login_at: datetime | None


class UserSummary(BaseModel):
    """The least that lets one user share something with another.

    Both names travel together. The display name is what a screen shows, and
    the username is what tells two people with the same display name apart —
    display names are not unique and are not meant to be.
    """

    id: int
    username: str
    display_name: str | None = None


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8)
    #: Optional, not unique, and blank is stored as null — see app/names.py.
    display_name: str | None = Field(default=None, max_length=128)
    is_admin: bool = False
    must_change_password: bool = True


class UserPatch(BaseModel):
    """An administrator's edit of somebody else's account.

    `display_name` is genuinely nullable: sending null clears it and puts the
    person back to being called by their username. `exclude_unset` is what
    separates that from "not mentioned".
    """

    is_admin: bool | None = None
    must_change_password: bool | None = None
    display_name: str | None = Field(default=None, max_length=128)


class ProfilePatch(BaseModel):
    """What a user may change about their own account without being an admin.

    Only the name they are shown under. Not the username — that is the login
    and the thing that disambiguates two people with the same display name —
    and not the admin flag, which is the whole point of there being one.
    """

    display_name: str | None = Field(default=None, max_length=128)


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
    #: Reasoning effort, from the provider adapter's list. Null = CLI default.
    effort: str | None = None
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
    effort: str | None
    description: str | None
    system_prompt: str | None
    permission_mode: str | None
    allowed_tools: str | None
    extra_args: list[str]
    is_default: bool


# --- sessions & runs ---------------------------------------------------
class SessionIn(BaseModel):
    #: Blank falls back to the first line of the opening prompt.
    title: str | None = None
    provider: str
    model: str | None = None
    effort: str | None = None
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
    #: Hands the conversation to the other CLI. Not a resume — see handoff.py —
    #: so it clears the provider session id and owes the next turn a briefing.
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
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
    effort: str | None
    account_id: int | None
    preset_id: int | None
    workspace_id: int | None
    approval_mode: str | None
    provider_session_id: str | None
    #: A provider switch has happened and the briefing has not been sent yet, so
    #: the next turn is the handoff. The UI says so rather than letting the
    #: conversation look continuous.
    handoff_pending: bool
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
    #: Who answered *this* turn. A session can be switched between providers, so
    #: this is recorded per turn and must not be inferred from the session.
    provider: str | None
    model: str | None
    #: True on the turn that carried the post-switch briefing.
    carries_handoff: bool
    #: Who sent this turn. Null on turns that predate the column — the UI must
    #: say so rather than guessing, because guessing means labelling somebody
    #: else's message with the reader's own name in a shared session.
    requested_by_id: int | None
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


# --- credential exposure in a shared session ---------------------------
class ExposureSystem(BaseModel):
    """One of the caller's own stored systems, named the way the agent types it.

    No hostname, no credential state, nothing about who else it is shared with:
    the warning needs to name the system, and this is a screen about audiences,
    not a second copy of the systems page.
    """

    id: int
    name: str
    slug: str


class ExposureOut(BaseModel):
    """What a turn of the caller's would expose here, and to whom.

    Always from the caller's point of view. `viewers` excludes them, `systems` is
    only theirs, and nothing in here is information they could not already get
    from /api/users/directory and /api/targets.
    """

    session_id: str
    #: Everyone else who can read this session — owner, named sharees, the team.
    viewers: list[UserSummary]
    #: The caller's systems a turn of theirs in this session would reach.
    systems: list[ExposureSystem]
    #: True when both lists are non-empty, i.e. when there is anything to say.
    at_stake: bool
    acknowledged: bool
    acknowledged_at: datetime | None
    #: Viewers the standing acknowledgement does not cover. Everyone, when there
    #: is none. This is what the confirmation is asked about.
    new_viewers: list[UserSummary]
    #: The first prompt is refused until this is False.
    needs_acknowledgement: bool


class ExposureAckIn(BaseModel):
    """Optionally, the audience the client believes it is agreeing to.

    Sent back so a race is caught rather than papered over: if the owner adds
    somebody between the warning being drawn and the button being pressed, the
    agreement on screen was about a smaller group than the one that now exists,
    and it is refused so the question can be asked again. Omitted means "whatever
    the server sees now", which is what a non-interactive client wants.
    """

    viewer_ids: list[int] | None = None


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
    owner_id: int | None
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
    #: Reasoning-effort levels, weakest first. Empty means this CLI has no such
    #: control, and the UI hides the selector rather than offering a dead one.
    efforts: list[str] = []
    #: Models that accept fewer levels than `efforts`, keyed by model name.
    efforts_by_model: dict[str, list[str]] = {}
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
    #: Reach it through this relay node instead of directly. Null connects
    #: from the AIOps server, which is what everything did before relays.
    relay_node_id: int | None = None


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
    relay_node_id: int | None = None


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
    relay_node_id: int | None
    created_at: datetime


# --- relay nodes -------------------------------------------------------
class NodeGrant(BaseModel):
    user_id: int
    level: str = "use"  # use | manage


class NodeIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    grants: list[NodeGrant] | None = None


class NodePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    grants: list[NodeGrant] | None = None
    owner_id: int | None = None
    #: Subnets a run may reach through this node, and on which ports. Only ever
    #: set from here — never from what the node reports about itself.
    #:
    #: Both deliberately loose. Validation belongs to relay.py, which refuses a
    #: public range or a port of 70000 with a sentence saying why; declaring
    #: `list[int]` here would instead have pydantic reject `"ssh"` with a 422
    #: and a message about the shape of the request body, which is not what the
    #: person typing in the box needs to read.
    allowed_cidrs: list[str] | None = None
    allowed_ports: list[int | str] | None = None


class NodeOut(BaseModel):
    """Never carries a token or a credential — only whether one is outstanding."""

    id: int
    name: str
    slug: str
    description: str | None
    status: str  # pending | approved | revoked
    #: True while an unspent enrolment token exists for this node.
    enrolment_pending: bool
    enrolment_token_expires_at: datetime | None
    enrolled_at: datetime | None
    last_seen_at: datetime | None
    #: Whether the node's control channel is held open right now.
    online: bool
    version: str | None
    reported_hostname: str | None
    #: What the node says it can see. Descriptive only — it authorises nothing.
    networks: list[str]
    #: What it has been *allowed* to be used for, which is a different list and
    #: only ever set by a person.
    allowed_cidrs: list[str]
    allowed_ports: list[int]
    owner_id: int | None
    grants: list[NodeGrant]
    #: owner | manage | use — what the caller may do with it.
    my_level: str
    #: How many stored systems route through it, so revoking says what it costs.
    target_count: int
    created_at: datetime


class InstallCommand(BaseModel):
    """How to install the node agent on one kind of machine."""

    platform: str  # linux | windows | docker
    label: str
    command: str
    #: What has to be true before the command works — elevation, a runtime.
    note: str | None = None


class NodeEnrolmentOut(BaseModel):
    """The one and only time an enrolment token is readable."""

    node: NodeOut
    enrolment_token: str
    expires_at: datetime | None
    #: A ready-to-paste installer invocation, so the token is never retyped.
    install_hint: str
    #: The same, per platform. Windows and Docker were previously only
    #: mentioned in prose, which meant guessing at their arguments.
    install: list[InstallCommand] = []


class NodeEnrolIn(BaseModel):
    """What a node presents to trade its one-time token for a credential."""

    token: str = Field(min_length=1)
    version: str | None = None
    hostname: str | None = None
    networks: list[str] = []


class NodeEnrolOut(BaseModel):
    node_id: int
    slug: str
    name: str
    status: str
    credential: str
    message: str
