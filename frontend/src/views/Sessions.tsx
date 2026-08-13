import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { Preset, ProviderInfo, Session, Workspace } from "../types";
import Chat from "./Chat";

export default function Sessions() {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setSessions(await api.sessions());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(load, 8000);
    return () => clearInterval(timer);
  }, [load]);

  return (
    <div className="main flush">
      {/* On a phone the list and the conversation are separate screens; the
          class tells the stylesheet which one is in front. */}
      <div className={`sessions${sessionId || creating ? " showing-detail" : ""}`}>
        <div className="session-list">
          <div className="session-list-head">
            <button className="primary" style={{ width: "100%" }} onClick={() => setCreating(true)}>
              + New session
            </button>
          </div>
          {sessions.length === 0 && <div className="empty">No sessions yet.</div>}
          {sessions.map((s) => (
            <Link
              key={s.id}
              to={`/sessions/${s.id}`}
              className={`session-item${s.id === sessionId ? " active" : ""}`}
            >
              <span className="title">{s.title}</span>
              <span className="meta">
                {s.provider}
                {s.model ? ` · ${s.model}` : ""} · <span className={`pill ${s.status}`}>{s.status}</span>
              </span>
            </Link>
          ))}
        </div>

        {creating ? (
          <NewSession
            onCancel={() => setCreating(false)}
            onCreated={async (session) => {
              setCreating(false);
              await load();
              navigate(`/sessions/${session.id}`);
            }}
          />
        ) : sessionId ? (
          <Chat key={sessionId} sessionId={sessionId} onChanged={load} />
        ) : (
          <div className="empty">
            {error ? <div className="error-banner">{error}</div> : "Select a session, or create one."}
          </div>
        )}
      </div>
    </div>
  );
}

function NewSession({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (session: Session) => void;
}) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [presetId, setPresetId] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      const [p, pr, w] = await Promise.all([api.providers(), api.presets(), api.workspaces()]);
      setProviders(p);
      setPresets(pr);
      setWorkspaces(w);
    })();
  }, []);

  // A preset belongs to one provider; clear it when the provider changes.
  useEffect(() => {
    setPresetId("");
    setModel("");
  }, [provider]);

  const current = providers.find((p) => p.name === provider);
  const availablePresets = presets.filter((p) => p.provider === provider);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await api.createSession({
          provider,
          model: model || null,
          preset_id: presetId ? Number(presetId) : null,
          workspace_id: workspaceId ? Number(workspaceId) : null,
          prompt: prompt.trim() || undefined,
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  return (
    <div className="chat">
      <div className="chat-head">
        <h1>New session</h1>
        <button onClick={onCancel}>Cancel</button>
      </div>
      <form className="chat-body" onSubmit={submit}>
        {error && <div className="error-banner">{error}</div>}
        {current && !current.available && (
          <div className="error-banner">
            The <code>{current.binary}</code> CLI isn't installed in this container — runs will fail.
          </div>
        )}
        {current?.available && current.authenticated === false && (
          <div className="error-banner">
            {provider} is installed but not signed in. See the Providers page for the login command.
          </div>
        )}
        <div className="grid-2">
          <label>
            <span>Provider</span>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              {providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Model (blank = preset or CLI default)</span>
            <input
              list="model-options"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder={current?.models[0] ?? ""}
            />
            <datalist id="model-options">
              {current?.models.map((m) => (
                <option key={m} value={m} />
              ))}
            </datalist>
          </label>
          <label>
            <span>Agent preset</span>
            <select value={presetId} onChange={(e) => setPresetId(e.target.value)}>
              <option value="">None</option>
              {availablePresets.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Workspace</span>
            <select value={workspaceId} onChange={(e) => setWorkspaceId(e.target.value)}>
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
          <span>First task (optional)</span>
          <textarea rows={6} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </label>
        <div className="row">
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create session"}
          </button>
        </div>
      </form>
    </div>
  );
}
