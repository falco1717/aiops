export type User = {
  id: number;
  username: string;
  is_admin: boolean;
  must_change_password: boolean;
  created_at: string;
  last_login_at: string | null;
};

/** An in-progress provider sign-in driven from the browser. */
export type LoginFlow = {
  provider: string;
  status:
    | "idle"
    | "starting"
    | "awaiting_user"
    | "completing"
    | "success"
    | "failed"
    | "cancelled"
    | "expired";
  verification_url: string | null;
  user_code: string | null;
  needs_code: boolean;
  message: string | null;
  expires_in: number;
};

export type Workspace = {
  id: number;
  name: string;
  path: string;
  description: string | null;
  created_at: string;
};

export type WorkspaceStatus = {
  exists: boolean;
  is_git: boolean;
  branch: string | null;
  head: string | null;
  dirty_files: string[];
  error: string | null;
};

export type Preset = {
  id: number;
  name: string;
  provider: string;
  model: string | null;
  /** Reasoning effort, from the provider's own list. Null = CLI default. */
  effort: string | null;
  description: string | null;
  system_prompt: string | null;
  permission_mode: string | null;
  allowed_tools: string | null;
  extra_args: string[];
  is_default: boolean;
};

/** A tool call the agent is parked on, waiting for a human answer. */
export type Approval = {
  id: number;
  run_id: number;
  session_id: string;
  provider: string;
  kind: string;
  tool_name: string | null;
  summary: string | null;
  request: Record<string, unknown> | null;
  status: "pending" | "allowed" | "denied" | "expired" | "cancelled";
  decided_by_id: number | null;
  decided_at: string | null;
  note: string | null;
  created_at: string;
};

/** ask = pause and let a human decide; auto = approve edits; bypass = no checks. */
export type ApprovalMode = "ask" | "auto" | "bypass";

export type Session = {
  id: string;
  title: string;
  provider: string;
  model: string | null;
  /** Null falls back to the preset's, then to the CLI's own default. */
  effort: string | null;
  account_id: number | null;
  approval_mode: ApprovalMode | null;
  preset_id: number | null;
  workspace_id: number | null;
  provider_session_id: string | null;
  /**
   * A provider switch has happened and the briefing has not been sent yet, so
   * the next turn is the handoff. Neither CLI can load the other's session, so
   * the incoming agent starts fresh with a written summary — the UI says that
   * out loud rather than letting the thread look continuous.
   */
  handoff_pending: boolean;
  status: string;
  archived: boolean;
  owner_id: number | null;
  /** The team whose members all see this session, if it belongs to one. */
  team_id: number | null;
  /** People it was shared with by name, beside any team. */
  shared_user_ids: number[];
  created_at: string;
  updated_at: string;
};

/** A group of people who see each other's sessions. Membership is admin-managed. */
export type Team = {
  id: number;
  name: string;
  description: string | null;
  member_ids: number[];
  session_count: number;
  created_at: string;
};

export type Run = {
  id: number;
  session_id: string;
  schedule_id: number | null;
  prompt: string;
  /** Which agent answered *this* turn. A session can be switched mid-thread, so
   *  this is recorded per turn and must never be read off the session. */
  provider: string | null;
  model: string | null;
  /** True on the turn that carried the post-switch briefing. */
  carries_handoff: boolean;
  status: string;
  exit_code: number | null;
  error: string | null;
  account_id: number | null;
  failed_over_from_id: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  context_tokens: number | null;
  cost_usd: number | null;
  command: string[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type AgentEvent = {
  id: number;
  run_id: number;
  seq: number;
  kind: string;
  text: string | null;
  tool_name: string | null;
  /** Set when the message came from a subagent: the tool call that spawned it. */
  parent_tool_use_id: string | null;
  agent_name: string | null;
  created_at: string;
};

/** One named provider sign-in — "Walt's Claude", "Jordan's Claude". */
export type Account = {
  id: number;
  name: string;
  provider: string;
  slug: string;
  description: string | null;
  is_default: boolean;
  fallback_account_id: number | null;
  limited_until: string | null;
  /** Plan-window state the CLI reported (e.g. five_hour / allowed). */
  limit_status: string | null;
  limit_window: string | null;
  limit_resets_at: string | null;
  config_dir: string;
  signed_in: boolean | null;
  account_detail: string | null;
  allowed_user_ids: number[];
  usable_by_me: boolean;
};

export type UsageWindow = {
  label: string;
  since: string;
  runs: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cost_usd: number;
};

export type AccountUsage = {
  account_id: number;
  name: string;
  provider: string;
  limited_until: string | null;
  runs: number;
  total_tokens: number;
  cost_usd: number;
};

export type Usage = {
  windows: UsageWindow[];
  by_account: AccountUsage[];
  note: string;
};

export type SessionContext = {
  session_id: string;
  last_context_tokens: number | null;
  peak_context_tokens: number | null;
  total_tokens: number;
  runs: number;
  cost_usd: number;
};

/** A skill or slash command the session can use by typing `/name`. */
export type Capability = {
  name: string;
  kind: "skill" | "command" | "builtin";
  description: string;
  source: string;
};

/** A file the operator handed to the agent. `run_id` is null until it is sent. */
export type Attachment = {
  id: string;
  session_id: string;
  run_id: number | null;
  filename: string;
  content_type: string;
  size: number;
  created_at: string;
};

/** A file under the session's workspace, offered for download. */
export type SessionFile = {
  path: string;
  size: number;
  modified: string;
};

export type SessionFiles = {
  root: string;
  files: SessionFile[];
  truncated: boolean;
  max_files: number;
  max_depth: number;
};

export type Transcript = {
  session: Session;
  runs: Run[];
  events: AgentEvent[];
  attachments: Attachment[];
};

export type Schedule = {
  id: number;
  name: string;
  cron: string;
  timezone_name: string;
  prompt: string;
  provider: string;
  model: string | null;
  account_id: number | null;
  preset_id: number | null;
  workspace_id: number | null;
  session_mode: string;
  owner_id: number | null;
  target_session_id: string | null;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  last_status: string | null;
};

export type ProviderInfo = {
  name: string;
  label: string;
  models: string[];
  permission_modes: string[];
  /** Effort levels, weakest first. Empty when this CLI has no such control. */
  efforts: string[];
  /** Models accepting fewer levels than `efforts`, keyed by model name. */
  efforts_by_model: Record<string, string[]>;
  binary: string;
  available: boolean;
  version: string | null;
  authenticated: boolean | null;
  account: string | null;
  detail: string | null;
};

/** Messages pushed over the websocket. */
export type WsMessage =
  | { type: "connected"; topic: string }
  | { type: "ping" }
  | {
      type: "run.started";
      session_id: string;
      run_id: number;
      prompt: string;
      command: string[];
      /** Which named account served this attempt, and what it fell back from. */
      account?: string | null;
      failed_over_from?: string | null;
      attempt?: number;
    }
  | {
      type: "event";
      session_id: string;
      run_id: number;
      kind: string;
      text: string | null;
      tool_name: string | null;
      is_error: boolean;
      parent_tool_use_id?: string | null;
      agent_name?: string | null;
      seq?: number;
    }
  | {
      type: "approval.requested";
      session_id: string;
      run_id: number;
      approval_id: number;
      provider: string;
      kind: string;
      tool_name: string | null;
      summary: string | null;
      request: Record<string, unknown> | null;
    }
  | {
      type: "approval.resolved";
      session_id: string;
      run_id: number;
      approval_id: number;
      status: string;
      note: string | null;
    }
  | {
      type: "run.finished";
      session_id: string;
      run_id: number;
      status: string;
      exit_code: number | null;
      error: string | null;
      cost_usd: number | null;
    };

/** A system an agent can reach by name. Secrets are write-only — the API only
 *  ever reports whether one is stored. */
export type Target = {
  id: number;
  name: string;
  slug: string;
  hostname: string;
  port: number;
  username: string;
  description: string | null;
  auth_type: "key" | "password";
  has_private_key: boolean;
  has_passphrase: boolean;
  has_password: boolean;
  host_key_policy: "accept-new" | "strict";
  has_known_host_key: boolean;
  owner_id: number | null;
  grants: TargetGrant[];
  /** owner | manage | use — what you may do with it. */
  my_level: "owner" | "manage" | "use";
  /** Reached through this relay node instead of from the AIOps server. */
  relay_node_id: number | null;
  created_at: string;
};

export type TargetGrant = {
  user_id: number;
  level: "use" | "manage";
};

/** A machine on another network that AIOps opens connections through.
 *
 *  It holds no credential of ours and runs no agent: it is dialled out from,
 *  told a host and a port, and copies bytes. Nothing here is ever a secret —
 *  the enrolment token is readable exactly once, in the response that mints it.
 */
export type RelayNode = {
  id: number;
  name: string;
  slug: string;
  description: string | null;
  status: "pending" | "approved" | "revoked";
  /** An enrolment token has been issued and not yet spent. */
  enrolment_pending: boolean;
  enrolment_token_expires_at: string | null;
  enrolled_at: string | null;
  last_seen_at: string | null;
  /** Whether its connection is held open right now. */
  online: boolean;
  version: string | null;
  reported_hostname: string | null;
  networks: string[];
  owner_id: number | null;
  grants: NodeGrant[];
  /** owner | manage | use, or "" for an admin seeing one only to approve it. */
  my_level: "owner" | "manage" | "use" | "";
  target_count: number;
  created_at: string;
};

export type NodeGrant = {
  user_id: number;
  level: "use" | "manage";
};

/** How to install the node agent on one kind of machine. */
export type InstallCommand = {
  platform: "linux" | "windows" | "docker";
  label: string;
  command: string;
  /** What has to be true first — elevation, a runtime. */
  note: string | null;
};

/** The one response that carries a readable enrolment token. */
export type NodeEnrolment = {
  node: RelayNode;
  enrolment_token: string;
  expires_at: string | null;
  install_hint: string;
  install: InstallCommand[];
};

/** Just enough about another user to share something with them. */
export type UserSummary = {
  id: number;
  username: string;
};

/** One of your own stored systems, as the exposure warning names it. */
export type ExposureSystem = {
  id: number;
  name: string;
  slug: string;
};

/**
 * What a turn of yours in this session would put in front of whom.
 *
 * Computed by the server from the same visibility rules as everything else, and
 * always from the caller's point of view: `viewers` never includes you, and
 * `systems` is only ever yours. A turn reaches the systems its *requester* can
 * reach, so in a shared session your credentials work inside somebody else's
 * transcript — this is the disclosure of that, not a limit on it.
 */
export type Exposure = {
  session_id: string;
  /** Everyone else who can read this session: owner, sharees, the team. */
  viewers: UserSummary[];
  systems: ExposureSystem[];
  /** Both lists non-empty — there is actually something to warn about. */
  at_stake: boolean;
  acknowledged: boolean;
  acknowledged_at: string | null;
  /** Viewers your standing acknowledgement does not cover; all of them if none. */
  new_viewers: UserSummary[];
  /** The next prompt is refused with 428 until this is false. */
  needs_acknowledgement: boolean;
};
