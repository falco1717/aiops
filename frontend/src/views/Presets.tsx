import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Preset, ProviderInfo } from "../types";

const EMPTY = {
  name: "",
  provider: "claude",
  model: "",
  description: "",
  system_prompt: "",
  permission_mode: "",
  allowed_tools: "",
  extra_args: "",
};

type Draft = typeof EMPTY;

export default function Presets() {
  const [items, setItems] = useState<Preset[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [presets, provs] = await Promise.all([api.presets(), api.providers()]);
    setItems(presets);
    setProviders(provs);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const current = providers.find((p) => p.name === draft.provider);

  // The cast is needed because TypeScript widens a spread with a generic
  // computed key and can no longer see it as a Draft.
  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }) as Draft);

  const reset = () => {
    setDraft(EMPTY);
    setEditingId(null);
  };

  const edit = (p: Preset) => {
    setEditingId(p.id);
    setDraft({
      name: p.name,
      provider: p.provider,
      model: p.model ?? "",
      description: p.description ?? "",
      system_prompt: p.system_prompt ?? "",
      permission_mode: p.permission_mode ?? "",
      allowed_tools: p.allowed_tools ?? "",
      extra_args: p.extra_args.join(" "),
    });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    const payload: Partial<Preset> = {
      name: draft.name,
      provider: draft.provider,
      model: draft.model || null,
      description: draft.description || null,
      system_prompt: draft.system_prompt || null,
      permission_mode: draft.permission_mode || null,
      allowed_tools: draft.provider === "claude" ? draft.allowed_tools || null : null,
      extra_args: draft.extra_args.trim() ? draft.extra_args.trim().split(/\s+/) : [],
    };
    try {
      if (editingId) await api.updatePreset(editingId, payload);
      else await api.createPreset(payload);
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async (p: Preset) => {
    if (!confirm(`Delete preset "${p.name}"?`)) return;
    await api.deletePreset(p.id);
    if (editingId === p.id) reset();
    await load();
  };

  return (
    <div className="main">
      <h1>Agents</h1>
      <p className="subtitle">
        A named bundle of provider, model, permissions and standing instructions — pick one when
        starting a session or scheduling a job instead of re-entering the same settings.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={submit}>
        <div className="grid-2">
          <label>
            <span>Name</span>
            <input value={draft.name} onChange={(e) => set("name", e.target.value)} required />
          </label>
          <label>
            <span>Provider</span>
            <select
              value={draft.provider}
              onChange={(e) => {
                set("provider", e.target.value);
                set("permission_mode", "");
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
            <span>Model</span>
            <input
              list="preset-models"
              value={draft.model}
              onChange={(e) => set("model", e.target.value)}
              placeholder="CLI default"
            />
            <datalist id="preset-models">
              {current?.models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>
          <label>
            <span>{draft.provider === "codex" ? "Sandbox" : "Permission mode"}</span>
            <select
              value={draft.permission_mode}
              onChange={(e) => set("permission_mode", e.target.value)}
            >
              <option value="">CLI default</option>
              {current?.permission_modes.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          <span>Description</span>
          <input value={draft.description} onChange={(e) => set("description", e.target.value)} />
        </label>
        <label>
          <span>
            Standing instructions
            {draft.provider === "claude"
              ? " (appended to the system prompt)"
              : " (prepended to each prompt — Codex has no system-prompt flag)"}
          </span>
          <textarea
            rows={4}
            value={draft.system_prompt}
            onChange={(e) => set("system_prompt", e.target.value)}
          />
        </label>
        {draft.provider === "claude" && (
          <label>
            <span>Auto-approved tools (--allowedTools syntax)</span>
            <input
              value={draft.allowed_tools}
              onChange={(e) => set("allowed_tools", e.target.value)}
              placeholder="Read,Edit,Bash(git status *)"
            />
          </label>
        )}
        <label>
          <span>Extra CLI arguments</span>
          <input
            value={draft.extra_args}
            onChange={(e) => set("extra_args", e.target.value)}
            placeholder="--add-dir /workspaces/shared"
          />
        </label>
        <div className="row">
          <button className="primary" type="submit">
            {editingId ? "Save changes" : "Create agent"}
          </button>
          {editingId && <button onClick={reset}>Cancel</button>}
        </div>
      </form>

      {items.length === 0 ? (
        <div className="empty">No agents defined.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Provider</th>
              <th>Model</th>
              <th>Permissions</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((p) => (
              <tr key={p.id}>
                <td>
                  {p.name}
                  {p.description && (
                    <div style={{ color: "var(--text-dim)", fontSize: 12 }}>{p.description}</div>
                  )}
                </td>
                <td>{p.provider}</td>
                <td className="mono">{p.model ?? "—"}</td>
                <td className="mono">{p.permission_mode ?? "—"}</td>
                <td className="row">
                  <button onClick={() => edit(p)}>Edit</button>
                  <button className="danger" onClick={() => remove(p)}>
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
