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

export type Session = {
  id: string;
  title: string;
  provider: string;
  model: string | null;
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
  created_at: string;
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
    }
  | {
      type: "event";
      session_id: string;
      run_id: number;
      kind: string;
      text: string | null;
      tool_name: string | null;
      is_error: boolean;
      seq?: number;
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
