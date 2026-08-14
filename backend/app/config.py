from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings are read from AIOPS_-prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="AIOPS_", env_file=".env", extra="ignore")

    # --- storage -------------------------------------------------------
    database_url: str = "postgresql+asyncpg://aiops:aiops@db:5432/aiops"

    # --- auth ----------------------------------------------------------
    jwt_secret: str = "change-me-please"
    jwt_ttl_hours: int = 24 * 30
    # Bootstrap account, created on first startup if no users exist.
    admin_username: str = "admin"
    admin_password: str = ""

    # --- agent execution ----------------------------------------------
    workspace_root: str = "/workspaces"
    # Per-account credential directories live here, inside the persisted home
    # volume, so several provider sign-ins coexist and survive a recreate.
    accounts_root: str = "/home/node/accounts"
    claude_bin: str = "claude"
    codex_bin: str = "codex"
    # Show subagent text and thinking, not just their tool calls.
    forward_subagent_text: bool = True
    # How long to avoid an account after it reports a usage limit.
    account_limit_cooldown_seconds: int = 3600
    max_concurrent_runs: int = 4
    run_timeout_seconds: int = 3600
    # Stream token-level deltas over the websocket (not persisted to the DB).
    stream_partial_messages: bool = True

    # --- tool approvals ------------------------------------------------
    # ask | auto | bypass, used by sessions that have not chosen for themselves.
    # "ask" is the default because an unattended agent on this box can run
    # arbitrary commands; the operator can lower it per session.
    default_approval_mode: str = "ask"
    # How long an agent waits, parked, for a human answer before giving up. A
    # scheduled run at 3am has nobody to ask, so this must not be infinite.
    approval_timeout_seconds: int = 600
    # Port the in-container approval bridge calls back on. This is the app's own
    # listener; nothing is published to the host.
    internal_api_url: str = "http://127.0.0.1:8000"

    # --- sign-in throttling -------------------------------------------
    # AIOps may be the only thing between the internet and an agent that runs
    # shell commands, so the login endpoint locks out after repeated failures.
    login_max_failures: int = 5
    login_failure_window_seconds: int = 900
    login_lockout_seconds: int = 900

    # --- scheduler -----------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 20

    # --- http ----------------------------------------------------------
    cors_origins: str = ""
    cookie_secure: bool = True
    cookie_name: str = "aiops_session"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
