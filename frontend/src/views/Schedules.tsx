import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { formatUtc } from "../time";
import type { Account, Preset, ProviderInfo, Schedule, Workspace } from "../types";

const EMPTY = {
  name: "",
  cron: "0 9 * * 1-5",
  timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  prompt: "",
  provider: "claude",
  model: "",
  account_id: "",
  preset_id: "",
  workspace_id: "",
  session_mode: "new",
  enabled: true,
};

type Draft = typeof EMPTY;

export default function Schedules() {
  const [items, setItems] = useState<Schedule[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [s, p, pr, w, a] = await Promise.all([
      api.schedules(),
      api.providers(),
      api.presets(),
      api.workspaces(),
      api.accounts().catch(() => [] as Account[]),
    ]);
    setItems(s);
    setProviders(p);
    setPresets(pr);
    setWorkspaces(w);
    setAccounts(a);
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  // See Presets.tsx — spreading a generic computed key loses the Draft type.
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }) as Draft);

  const reset = () => {
    setDraft(EMPTY);
    setEditingId(null);
  };

  const edit = (s: Schedule) => {
    setEditingId(s.id);
    setDraft({
      name: s.name,
      cron: s.cron,
      timezone_name: s.timezone_name,
      prompt: s.prompt,
      provider: s.provider,
      model: s.model ?? "",
      account_id: s.account_id ? String(s.account_id) : "",
      preset_id: s.preset_id ? String(s.preset_id) : "",
      workspace_id: s.workspace_id ? String(s.workspace_id) : "",
      session_mode: s.session_mode,
      enabled: s.enabled,
    });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const payload = {
      name: draft.name,
      cron: draft.cron,
      timezone_name: draft.timezone_name,
      prompt: draft.prompt,
      provider: draft.provider,
      model: draft.model || null,
      account_id: draft.account_id ? Number(draft.account_id) : null,
      preset_id: draft.preset_id ? Number(draft.preset_id) : null,
      workspace_id: draft.workspace_id ? Number(draft.workspace_id) : null,
      session_mode: draft.session_mode,
      enabled: draft.enabled,
    };
    try {
      if (editingId) await api.updateSchedule(editingId, payload);
      else await api.createSchedule(payload);
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async (s: Schedule) => {
    if (!confirm(`Delete schedule "${s.name}"?`)) return;
    await api.deleteSchedule(s.id);
    if (editingId === s.id) reset();
    await load();
  };

  const runNow = async (s: Schedule) => {
    try {
      await api.runSchedule(s.id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const availablePresets = presets.filter((p) => p.provider === draft.provider);
  // Only accounts this user may actually run against — a schedule pointed at a
  // restricted account would be rejected at fire time, not here.
  const availableAccounts = accounts.filter(
    (a) => a.provider === draft.provider && a.usable_by_me,
  );

  return (
    <div className="main">
      <h1>Schedules</h1>
      <p className="subtitle">
        Cron jobs that hand a prompt to an agent. Times are evaluated in the schedule's own
        timezone, so a 09:00 job stays at 09:00 across daylight-saving changes.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={submit}>
        <div className="grid-2">
          <label>
            <span>Name</span>
            <input value={draft.name} onChange={(e) => set("name", e.target.value)} required />
          </label>
          <label>
            <span>Cron (min hour dom mon dow)</span>
            <input
              className="mono"
              value={draft.cron}
              onChange={(e) => set("cron", e.target.value)}
              required
            />
          </label>
          <label>
            <span>Timezone</span>
            <input
              value={draft.timezone_name}
              onChange={(e) => set("timezone_name", e.target.value)}
              placeholder="America/Chicago"
            />
          </label>
          <label>
            <span>Session handling</span>
            <select value={draft.session_mode} onChange={(e) => set("session_mode", e.target.value)}>
              <option value="new">Fresh session each run</option>
              <option value="continue">Keep appending to one session</option>
            </select>
          </label>
          <label>
            <span>Provider</span>
            <select
              value={draft.provider}
              onChange={(e) => {
                set("provider", e.target.value);
                set("preset_id", "");
                set("account_id", "");
              }}
            >
              {providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Account</span>
            <select value={draft.account_id} onChange={(e) => set("account_id", e.target.value)}>
              <option value="">Default for this provider</option>
              {availableAccounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                  {a.is_default ? " (default)" : ""}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Agent preset</span>
            <select value={draft.preset_id} onChange={(e) => set("preset_id", e.target.value)}>
              <option value="">None</option>
              {availablePresets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Model</span>
            <input
              value={draft.model}
              onChange={(e) => set("model", e.target.value)}
              placeholder="preset / CLI default"
            />
          </label>
          <label>
            <span>Workspace</span>
            <select value={draft.workspace_id} onChange={(e) => set("workspace_id", e.target.value)}>
              <option value="">None (workspace root)</option>
              {workspaces.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          <span>Prompt</span>
          <textarea
            rows={4}
            value={draft.prompt}
            onChange={(e) => set("prompt", e.target.value)}
            required
          />
        </label>
        <label className="row" style={{ marginBottom: 12 }}>
          <input
            type="checkbox"
            style={{ width: 16 }}
            checked={draft.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
          <span style={{ margin: 0 }}>Enabled</span>
        </label>
        <div className="row">
          <button className="primary" type="submit">
            {editingId ? "Save changes" : "Create schedule"}
          </button>
          {editingId && (
            // Without type="button" this submits the form it sits in.
            <button type="button" onClick={reset}>
              Cancel
            </button>
          )}
        </div>
      </form>

      {items.length === 0 ? (
        <div className="empty">No schedules yet.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Cron</th>
              <th>Next run</th>
              <th>Last run</th>
              <th>Session</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td data-label="Name">
                  {s.name}{" "}
                  {!s.enabled && <span className="pill cancelled">disabled</span>}
                  <div style={{ color: "var(--text-dim)", fontSize: 12 }}>
                    {s.provider}
                    {s.model ? ` · ${s.model}` : ""}
                    {s.account_id
                      ? ` · ${accounts.find((a) => a.id === s.account_id)?.name ?? "account"}`
                      : ""}
                  </div>
                </td>
                <td className="mono" data-label="Cron">
                  {s.cron}
                  <div style={{ color: "var(--text-dim)" }}>{s.timezone_name}</div>
                </td>
                <td className="mono" data-label="Next run">
                  {formatUtc(s.next_run_at)}
                </td>
                <td className="mono" data-label="Last run">
                  {formatUtc(s.last_run_at)}
                </td>
                <td data-label="Session">
                  {s.target_session_id ? (
                    <Link to={`/sessions/${s.target_session_id}`}>open</Link>
                  ) : (
                    <span style={{ color: "var(--text-dim)" }}>per run</span>
                  )}
                </td>
                <td className="row actions">
                  <button onClick={() => runNow(s)}>Run now</button>
                  <button onClick={() => edit(s)}>Edit</button>
                  <button className="danger" onClick={() => remove(s)}>
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
