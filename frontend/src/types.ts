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
  account_id: number | null;
  approval_mode: ApprovalMode | null;
  preset_id: number | null;
  workspace_id: number | null;
  provider_session_id: string | null;
  status: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
};

export type Run = {
  id: number;
  session_id: string;
  schedule_id: number | null;
  prompt: string;
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

export type Transcript = {
  session: Session;
  runs: Run[];
  events: AgentEvent[];
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
