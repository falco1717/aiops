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
    preset_id: int | None = None
    workspace_id: int | None = None
    prompt: str | None = None  # optional first turn


class SessionPatch(BaseModel):
    title: str | None = None
    archived: bool | None = None
    model: str | None = None
    preset_id: int | None = None
    workspace_id: int | None = None


class SessionOut(ORM):
    id: str
    title: str
    provider: str
    model: str | None
    preset_id: int | None
    workspace_id: int | None
    provider_session_id: str | None
    status: str
    archived: bool
    created_at: datetime
    updated_at: datetime


class PromptIn(BaseModel):
    prompt: str = Field(min_length=1)


class RunOut(ORM):
    id: int
    session_id: str
    schedule_id: int | None
    prompt: str
    status: str
    exit_code: int | None
    error: str | None
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
    created_at: datetime


class TranscriptOut(BaseModel):
    session: SessionOut
    runs: list[RunOut]
    events: list[EventOut]


# --- schedules ---------------------------------------------------------
class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    cron: str
    timezone_name: str = "UTC"
    prompt: str = Field(min_length=1)
    provider: str
    model: str | None = None
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
