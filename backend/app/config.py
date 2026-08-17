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
    # Encrypts stored system credentials (SSH keys, passwords). Deliberately
    # separate from jwt_secret: rotating the session secret logs everyone out,
    # while rotating this one makes every stored credential unreadable.
    secret_key: str = ""

    # --- agent execution ----------------------------------------------
    workspace_root: str = "/workspaces"
    # Files the operator attaches to a message. Kept out of the workspaces so an
    # agent turned loose in a repo cannot rewrite what it was handed, and so a
    # `git clean` does not take the evidence with it.
    attachments_root: str = "/attachments"
    max_attachment_bytes: int = 25 * 1024 * 1024
    # A workspace can be a whole monorepo. The files panel is for picking up what
    # the agent just produced, not for browsing a tree, so the walk is bounded on
    # both axes and says so in the UI rather than truncating quietly.
    session_files_max: int = 400
    session_files_max_depth: int = 3
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
    # Size of the briefing handed to an agent that has just been switched into a
    # conversation partway through (see handoff.py). Neither CLI can load the
    # other's session, so this summary is the *only* thing the incoming agent
    # knows about what came before — but it is also the front of a prompt whose
    # real content is the operator's new message, so it cannot grow without
    # bound. Raising it briefs more history; lowering it drops older turns,
    # which the briefing then says out loud rather than truncating quietly.
    handoff_digest_max_chars: int = 24000

    # --- agent isolation -----------------------------------------------
    # The setuid helper that starts an agent process as its own unprivileged
    # user. Empty means agents run as the application's user, which is only
    # safe on a machine where nothing else matters: sharing a uid with the app
    # lets an agent read the app's environment out of /proc and recover every
    # secret AIOps was careful not to hand it. The image sets this; it is a
    # setting so a checkout can be run without the helper compiled.
    agent_runas: str = ""
    # Where the image installs the two standalone scripts an agent has to be
    # able to execute (the approval bridge and the relay ProxyCommand). The
    # application's own source is unreadable to the agent user, so they cannot
    # be run from there. Falls back to the package's copies when absent.
    agent_helper_dir: str = "/opt/aiops-agent"
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
    # The same wait, for Claude's AskUserQuestion. Longer on purpose: an
    # allow/deny is a glance at one command and a tap, but a question is several
    # option descriptions to read, a choice per question and possibly a sentence
    # to type — often on a phone, after a notification. Ten minutes makes that a
    # race; half an hour survives "put the phone down and thought about it"
    # while still being a number a 3am scheduled run eventually gives up at.
    approval_question_timeout_seconds: int = 1800
    # Sandbox tier an interactive ("ask") Codex turn runs under. The human is
    # the gate there, so the tier is only defence in depth — but under Docker's
    # default seccomp and AppArmor profiles bubblewrap cannot build the
    # "workspace-write" sandbox at all, and an *approved* command dies with a
    # bwrap error instead of running. The safe tier stays the default;
    # loosening it is a deliberate operator choice, not something AIOps does
    # quietly (the runner logs what to change when it sees that failure).
    codex_interactive_sandbox: str = "workspace-write"
    # Port the in-container approval bridge calls back on. This is the app's own
    # listener; nothing is published to the host.
    internal_api_url: str = "http://127.0.0.1:8000"

    # --- the agent's browser -------------------------------------------
    # A real Chromium the agent drives through MCP tools. Off unless the image
    # actually has one: a checkout without Playwright installed would otherwise
    # advertise tools to every turn that cannot start, and the CLI would spend
    # the first seconds of each run failing to reach an MCP server. The image
    # and docker-compose.yml both turn it on.
    browser_enabled: bool = False
    # Chromium's own sandbox. It needs an unprivileged user namespace, which
    # Docker's default seccomp profile blocks — the same wall bubblewrap hits
    # for Codex (see BWRAP_HINT in runner.py). "on" keeps it and lets the
    # browser fail loudly with what to change; "off" runs without it. Either
    # way Chromium runs as its own unprivileged user inside the container (see
    # browser_runas), which is what actually bounds a renderer exploit here.
    browser_sandbox: str = "on"
    # The shim Playwright is pointed at instead of its own node binary. It
    # execs the privilege-dropping helper, which drops to a *third* user —
    # neither the app's nor the agent's — before starting the browser stack.
    # That is what keeps a renderer exploit away from the run's decrypted SSH
    # keys, which are readable by the agent's group because `ssh` has to load
    # them. Empty runs the browser as the agent user, which is where it was
    # before this existed; the image sets it.
    browser_runas: str = ""
    # How long one page may take, and how long a browser may live before it is
    # closed out from under a turn that forgot about it. A headless browser is
    # the easiest thing in this container to leave running.
    browser_page_timeout_seconds: int = 30
    browser_session_seconds: int = 900
    # How many captures one turn may take, and therefore how many it may store.
    # Bounds a loop that photographs every scroll position of something large.
    browser_max_screenshots: int = 40
    # Screenshots are kept with the session now, not deleted at the end of the
    # run, so the per-turn count above is no longer the whole bound. These two
    # are: what one capture may weigh, and what one conversation may keep in
    # total across every turn in it.
    #
    # A 1280x900 viewport PNG is a few hundred KB; only a `full_page` capture of
    # a very long page comes near the per-capture figure, and something bigger
    # than that is not a screenshot worth keeping. The session figure is roughly
    # four times what a single uploaded attachment may be, and comfortably more
    # than a heavy browsing session takes (40 captures a turn, several turns) —
    # while still putting a hard ceiling on what one conversation can cost the
    # disk. Over it, new captures are refused rather than old ones evicted.
    browser_screenshot_max_bytes: int = 8 * 1024 * 1024
    browser_session_screenshot_bytes: int = 100 * 1024 * 1024

    # --- relay nodes ---------------------------------------------------
    # Where a run's ProxyCommand helper hands its bytes to the app. Loopback
    # only: the helper is a process inside this container, and nothing outside
    # it has any business speaking that protocol. Port 0 takes an ephemeral one
    # — both ends are in this container, and the run's ssh config is written
    # against whatever was actually bound, so nothing has to agree in advance.
    relay_forwarder_host: str = "127.0.0.1"
    relay_forwarder_port: int = 0
    # How long to wait for a node to reach the far host. Long enough for a
    # sluggish link, short enough that `ssh` reports a failure rather than
    # appearing to hang.
    relay_connect_timeout_seconds: int = 20
    # A node is a jump point, not a load balancer. The cap is here so a runaway
    # agent cannot turn one into a port scanner's worth of open sockets.
    relay_max_streams_per_node: int = 64
    # How long an unused enrolment token stays good. It is single-use anyway;
    # this bounds the window in which a leaked one is worth anything.
    relay_enrolment_token_ttl_hours: int = 24

    # --- sign-in throttling -------------------------------------------
    # AIOps may be the only thing between the internet and an agent that runs
    # shell commands, so the login endpoint locks out after repeated failures.
    login_max_failures: int = 5
    login_failure_window_seconds: int = 900
    login_lockout_seconds: int = 900

    # --- scheduler -----------------------------------------------------
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 20

    # --- credential watch ----------------------------------------------
    # A Claude OAuth token lives 8 hours and the CLI only replaces it when a
    # turn happens to start after it has lapsed. Left alone, that puts the
    # refresh on the critical path of the next turn — and a refresh that fails
    # is found out mid-run. This watch does the refresh beforehand instead; see
    # credentials.py for what it can and cannot fix.
    credential_watch_enabled: bool = True
    credential_check_seconds: int = 300
    # How close to expiry to start trying. The CLI has a refresh margin of its
    # own and will decline until it is inside it, so this is an opportunity to
    # refresh early rather than an instruction to.
    credential_refresh_lead_seconds: int = 1800
    # An attempt that errored says nothing about whether the credential is
    # still good, so unlike a decline it is worth retrying.
    credential_retry_seconds: int = 900
    # The fallback when `claude auth status` alone did not do it: the smallest
    # real turn the CLI will run, since a turn is what triggers a refresh.
    credential_probe_enabled: bool = True
    credential_probe_model: str = "haiku"
    credential_probe_timeout_seconds: int = 120

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
