import type {
  AgentEvent,
  LoginFlow,
  Preset,
  ProviderInfo,
  Run,
  Schedule,
  Session,
  Transcript,
  User,
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
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: init.body ? { "Content-Type": "application/json" } : undefined,
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
  createUser: (data: {
    username: string;
    password: string;
    is_admin: boolean;
    must_change_password: boolean;
  }) => request<User>("/api/users", { method: "POST", body: body(data) }),
  patchUser: (id: number, data: { is_admin?: boolean; must_change_password?: boolean }) =>
    request<User>(`/api/users/${id}`, { method: "PATCH", body: body(data) }),
  resetUserPassword: (id: number, new_password: string, must_change_password: boolean) =>
    request<void>(`/api/users/${id}/password`, {
      method: "POST",
      body: body({ new_password, must_change_password }),
    }),
  deleteUser: (id: number) => request<void>(`/api/users/${id}`, { method: "DELETE" }),

  // providers
  providers: () => request<ProviderInfo[]>("/api/providers"),
  startProviderLogin: (name: string) =>
    request<LoginFlow>(`/api/providers/${name}/login`, { method: "POST" }),
  providerLoginStatus: (name: string) => request<LoginFlow>(`/api/providers/${name}/login`),
  submitProviderLoginCode: (name: string, code: string) =>
    request<LoginFlow>(`/api/providers/${name}/login/code`, {
      method: "POST",
      body: body({ code }),
    }),
  cancelProviderLogin: (name: string) =>
    request<void>(`/api/providers/${name}/login`, { method: "DELETE" }),
  providerLogout: (name: string) =>
    request<{ status: string; detail: string }>(`/api/providers/${name}/logout`, {
      method: "POST",
    }),

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
    preset_id?: number | null;
    workspace_id?: number | null;
    prompt?: string;
  }) => request<Session>("/api/sessions", { method: "POST", body: body(data) }),
  session: (id: string) => request<Session>(`/api/sessions/${id}`),
  patchSession: (id: string, data: Partial<Session>) =>
    request<Session>(`/api/sessions/${id}`, { method: "PATCH", body: body(data) }),
  deleteSession: (id: string) => request<void>(`/api/sessions/${id}`, { method: "DELETE" }),
  transcript: (id: string) => request<Transcript>(`/api/sessions/${id}/transcript`),
  prompt: (id: string, prompt: string) =>
    request<Run>(`/api/sessions/${id}/prompt`, { method: "POST", body: body({ prompt }) }),
  events: (id: string) => request<AgentEvent[]>(`/api/sessions/${id}/events`),
  eventRaw: (sessionId: string, eventId: number) =>
    request<{ raw: unknown }>(`/api/sessions/${sessionId}/events/${eventId}/raw`),

  // runs
  runs: (limit = 50) => request<Run[]>(`/api/runs?limit=${limit}`),
  cancelRun: (id: number) => request<unknown>(`/api/runs/${id}/cancel`, { method: "POST" }),

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

/** Opens the live event socket, optionally scoped to one session. */
export function openSocket(sessionId?: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : "";
  return new WebSocket(`${proto}//${location.host}/api/ws${query}`);
}
