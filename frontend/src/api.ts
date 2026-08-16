import type {
  Account,
  ActiveRun,
  AgentEvent,
  Approval,
  ApprovalAnswer,
  Attachment,
  Capability,
  Exposure,
  LoginFlow,
  NodeEnrolment,
  Preset,
  RelayNode,
  SessionContext,
  SessionFiles,
  Usage,
  ProviderInfo,
  Run,
  Schedule,
  Session,
  Target,
  Team,
  Transcript,
  User,
  UserSummary,
  Workspace,
  WorkspaceStatus,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  // FormData must set its own Content-Type: the multipart boundary is generated
  // by the browser, and overriding the header leaves the server unable to parse
  // a body it was told the shape of but not where the parts start.
  const json = init.body !== undefined && !(init.body instanceof FormData);
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: json ? { "Content-Type": "application/json" } : undefined,
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: any) => d.msg).join("; ");
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const body = (data: unknown) => JSON.stringify(data);

export const api = {
  // auth
  me: () => request<User>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<User>("/api/auth/login", { method: "POST", body: body({ username, password }) }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/auth/password", {
      method: "POST",
      body: body({ current_password, new_password }),
    }),

  // users (admin)
  users: () => request<User[]>("/api/users"),
  /** Names only, available to any signed-in user, for sharing and attribution. */
  userDirectory: () => request<UserSummary[]>("/api/users/directory"),
  /** Your own display name. Self-service — separate route, separate permission
   *  from the admin PATCH below, which also carries `is_admin`. */
  updateProfile: (data: { display_name: string | null }) =>
    request<User>("/api/users/me", { method: "PATCH", body: body(data) }),
  createUser: (data: {
    username: string;
    password: string;
    display_name?: string | null;
    is_admin: boolean;
    must_change_password: boolean;
  }) => request<User>("/api/users", { method: "POST", body: body(data) }),
  patchUser: (
    id: number,
    data: {
      is_admin?: boolean;
      must_change_password?: boolean;
      display_name?: string | null;
    },
  ) => request<User>(`/api/users/${id}`, { method: "PATCH", body: body(data) }),
  resetUserPassword: (id: number, new_password: string, must_change_password: boolean) =>
    request<void>(`/api/users/${id}/password`, {
      method: "POST",
      body: body({ new_password, must_change_password }),
    }),
  deleteUser: (id: number) => request<void>(`/api/users/${id}`, { method: "DELETE" }),

  // teams — everyone sees their own; only admins can change one
  teams: () => request<Team[]>("/api/teams"),
  createTeam: (data: { name: string; description?: string | null; member_ids: number[] }) =>
    request<Team>("/api/teams", { method: "POST", body: body(data) }),
  patchTeam: (
    id: number,
    data: { name?: string; description?: string | null; member_ids?: number[] },
  ) => request<Team>(`/api/teams/${id}`, { method: "PATCH", body: body(data) }),
  deleteTeam: (id: number) => request<void>(`/api/teams/${id}`, { method: "DELETE" }),

  // providers
  providers: () => request<ProviderInfo[]>("/api/providers"),

  // provider accounts
  accounts: () => request<Account[]>("/api/accounts"),
  createAccount: (data: {
    name: string;
    provider: string;
    description?: string | null;
    is_default?: boolean;
  }) => request<Account>("/api/accounts", { method: "POST", body: body(data) }),
  patchAccount: (
    id: number,
    data: {
      name?: string;
      description?: string | null;
      is_default?: boolean;
      fallback_account_id?: number | null;
      allowed_user_ids?: number[];
    },
  ) => request<Account>(`/api/accounts/${id}`, { method: "PATCH", body: body(data) }),
  deleteAccount: (id: number) => request<void>(`/api/accounts/${id}`, { method: "DELETE" }),
  clearAccountLimit: (id: number) =>
    request<Account>(`/api/accounts/${id}/clear-limit`, { method: "POST" }),
  startAccountLogin: (id: number) =>
    request<LoginFlow>(`/api/accounts/${id}/login`, { method: "POST" }),
  accountLoginStatus: (id: number) => request<LoginFlow>(`/api/accounts/${id}/login`),
  submitAccountLoginCode: (id: number, code: string) =>
    request<LoginFlow>(`/api/accounts/${id}/login/code`, { method: "POST", body: body({ code }) }),
  cancelAccountLogin: (id: number) =>
    request<void>(`/api/accounts/${id}/login`, { method: "DELETE" }),
  accountLogout: (id: number) =>
    request<{ status: string; detail: string }>(`/api/accounts/${id}/logout`, { method: "POST" }),

  // usage
  usage: () => request<Usage>("/api/usage"),
  sessionUsage: (id: string) => request<SessionContext>(`/api/usage/session/${id}`),

  // workspaces
  workspaces: () => request<Workspace[]>("/api/workspaces"),
  createWorkspace: (data: { name: string; path: string; description?: string }) =>
    request<Workspace>("/api/workspaces", { method: "POST", body: body(data) }),
  deleteWorkspace: (id: number) =>
    request<void>(`/api/workspaces/${id}`, { method: "DELETE" }),
  workspaceStatus: (id: number) => request<WorkspaceStatus>(`/api/workspaces/${id}/status`),
  workspaceDiff: (id: number, staged = false) =>
    request<{ diff: string }>(`/api/workspaces/${id}/diff?staged=${staged}`),

  // presets
  presets: () => request<Preset[]>("/api/presets"),
  createPreset: (data: Partial<Preset>) =>
    request<Preset>("/api/presets", { method: "POST", body: body(data) }),
  updatePreset: (id: number, data: Partial<Preset>) =>
    request<Preset>(`/api/presets/${id}`, { method: "PUT", body: body(data) }),
  deletePreset: (id: number) => request<void>(`/api/presets/${id}`, { method: "DELETE" }),

  // sessions
  sessions: (archived = false) => request<Session[]>(`/api/sessions?archived=${archived}`),
  createSession: (data: {
    title?: string;
    provider: string;
    model?: string | null;
    effort?: string | null;
    account_id?: number | null;
    preset_id?: number | null;
    workspace_id?: number | null;
    team_id?: number | null;
    approval_mode?: string | null;
    prompt?: string;
  }) => request<Session>("/api/sessions", { method: "POST", body: body(data) }),
  session: (id: string) => request<Session>(`/api/sessions/${id}`),
  patchSession: (id: string, data: Partial<Session>) =>
    request<Session>(`/api/sessions/${id}`, { method: "PATCH", body: body(data) }),
  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  /** `sinceEventId` returns only events newer than that id — used on reconnect. */
  transcript: (id: string, sinceEventId = 0) =>
    request<Transcript>(
      `/api/sessions/${id}/transcript${sinceEventId ? `?since_event_id=${sinceEventId}` : ""}`,
    ),
  capabilities: (id: string) => request<Capability[]>(`/api/sessions/${id}/capabilities`),
  renameSession: (id: string, title: string) =>
    request<Session>(`/api/sessions/${id}`, { method: "PATCH", body: body({ title }) }),
  /**
   * Send a message. Accepted mid-turn, in which case it is queued and becomes
   * the next turn — it is not handed to the turn already running, which is a
   * headless CLI process with nothing listening on stdin.
   */
  prompt: (id: string, prompt: string, attachment_ids: string[] = []) =>
    request<Run>(`/api/sessions/${id}/prompt`, {
      method: "POST",
      body: body({ prompt, attachment_ids }),
    }),
  /** Stop the turn in flight *and* discard everything queued behind it. */
  stopSession: (id: string) =>
    request<{ stopped_run_id: number | null; withdrawn_run_ids: number[] }>(
      `/api/sessions/${id}/stop`,
      { method: "POST" },
    ),
  events: (id: string) => request<AgentEvent[]>(`/api/sessions/${id}/events`),

  /** Who would read what your own stored systems produce in this session. */
  exposure: (id: string) => request<Exposure>(`/api/sessions/${id}/exposure`),
  /**
   * Record that you understand it. `viewerIds` is the audience you were shown:
   * the server refuses with 409 if the list has changed since, so agreeing to a
   * smaller group cannot be recorded as agreeing to a larger one.
   */
  acknowledgeExposure: (id: string, viewerIds: number[]) =>
    request<Exposure>(`/api/sessions/${id}/exposure/ack`, {
      method: "POST",
      body: body({ viewer_ids: viewerIds }),
    }),

  // attachments — files sent to the agent, and files it produced
  uploadAttachment: (id: string, file: File) => {
    const form = new FormData();
    form.append("file", file, file.name);
    return request<Attachment>(`/api/sessions/${id}/attachments`, {
      method: "POST",
      body: form,
    });
  },
  attachments: (id: string) => request<Attachment[]>(`/api/sessions/${id}/attachments`),
  deleteAttachment: (id: string, attachmentId: string) =>
    request<void>(`/api/sessions/${id}/attachments/${attachmentId}`, { method: "DELETE" }),
  attachmentUrl: (id: string, attachmentId: string) =>
    `/api/sessions/${id}/attachments/${attachmentId}/download`,
  sessionFiles: (id: string) => request<SessionFiles>(`/api/sessions/${id}/files`),
  sessionFileUrl: (id: string, path: string) =>
    `/api/sessions/${id}/files/download?path=${encodeURIComponent(path)}`,

  // targets — systems an agent can reach by name
  targets: () => request<Target[]>("/api/targets"),
  createTarget: (data: Record<string, unknown>) =>
    request<Target>("/api/targets", { method: "POST", body: body(data) }),
  updateTarget: (id: number, data: Record<string, unknown>) =>
    request<Target>(`/api/targets/${id}`, { method: "PATCH", body: body(data) }),
  deleteTarget: (id: number) => request<void>(`/api/targets/${id}`, { method: "DELETE" }),

  // relay nodes — jump points on other networks
  nodes: () => request<RelayNode[]>("/api/nodes"),
  /** Admin-only: what has enrolled and is waiting to be let in. */
  pendingNodes: () => request<RelayNode[]>("/api/nodes/pending"),
  // The only two calls that ever return a readable enrolment token.
  createNode: (data: Record<string, unknown>) =>
    request<NodeEnrolment>("/api/nodes", { method: "POST", body: body(data) }),
  reissueNodeToken: (id: number) =>
    request<NodeEnrolment>(`/api/nodes/${id}/token`, { method: "POST" }),
  updateNode: (id: number, data: Record<string, unknown>) =>
    request<RelayNode>(`/api/nodes/${id}`, { method: "PATCH", body: body(data) }),
  approveNode: (id: number) =>
    request<RelayNode>(`/api/nodes/${id}/approve`, { method: "POST" }),
  revokeNode: (id: number) => request<RelayNode>(`/api/nodes/${id}/revoke`, { method: "POST" }),
  deleteNode: (id: number) => request<void>(`/api/nodes/${id}`, { method: "DELETE" }),

  // approvals — an agent is parked waiting on each pending one
  approvals: (params: { session_id?: string; status?: string } = {}) => {
    const query = new URLSearchParams(
      Object.entries(params).filter(([, v]) => v) as [string, string][],
    ).toString();
    return request<Approval[]>(`/api/approvals${query ? `?${query}` : ""}`);
  },
  /**
   * `answers` belongs only to an AskUserQuestion approval, and allowing one
   * without them is refused server-side — the tool would report back that the
   * questions went unanswered.
   */
  decideApproval: (
    id: number,
    allowed: boolean,
    note?: string | null,
    answers?: ApprovalAnswer[] | null,
  ) =>
    request<Approval>(`/api/approvals/${id}/decide`, {
      method: "POST",
      body: body({ allowed, note: note ?? null, answers: answers ?? null }),
    }),
  eventRaw: (sessionId: string, eventId: number) =>
    request<{ raw: unknown }>(`/api/sessions/${sessionId}/events/${eventId}/raw`),

  // runs
  runs: (limit = 50) => request<Run[]>(`/api/runs?limit=${limit}`),
  /**
   * Every turn still in flight that this user is allowed to see — the feed
   * behind the working indicator. Scoped by the ordinary session-visibility
   * rule, so an administrator sees their own work and nobody else's.
   */
  activeRuns: () => request<ActiveRun[]>("/api/runs/active"),
  cancelRun: (id: number) => request<unknown>(`/api/runs/${id}/cancel`, { method: "POST" }),
  /**
   * Take a queued message back out of the line. Refused with 409 once the agent
   * has picked it up — unsending something nobody has read is a different act
   * from killing an agent mid-command, so it is a different call.
   */
  withdrawRun: (id: number) =>
    request<{ status: string }>(`/api/runs/${id}/withdraw`, { method: "POST" }),

  // schedules
  schedules: () => request<Schedule[]>("/api/schedules"),
  createSchedule: (data: Partial<Schedule>) =>
    request<Schedule>("/api/schedules", { method: "POST", body: body(data) }),
  updateSchedule: (id: number, data: Partial<Schedule>) =>
    request<Schedule>(`/api/schedules/${id}`, { method: "PUT", body: body(data) }),
  deleteSchedule: (id: number) => request<void>(`/api/schedules/${id}`, { method: "DELETE" }),
  runSchedule: (id: number) =>
    request<{ run_id: number; session_id: string }>(`/api/schedules/${id}/run`, {
      method: "POST",
    }),
};

/** Opens the live event socket for one session.
 *
 * The session is required. Omitting it used to subscribe to every session on the
 * instance, which the server now refuses; nothing here ever asked for that feed,
 * and the Sessions list stays current by refetching on a timer instead.
 */
export function openSocket(sessionId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const query = `?session_id=${encodeURIComponent(sessionId)}`;
  return new WebSocket(`${proto}//${location.host}/api/ws${query}`);
}
