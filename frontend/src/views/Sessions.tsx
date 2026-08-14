import type * as React from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type { Account, Preset, ProviderInfo, Session, Team, User, Workspace } from "../types";
import Chat from "./Chat";

/** The bucket a session is listed under: its team, or nobody's. */
const NO_TEAM = 0;

export default function Sessions({ me }: { me: User }) {
  const { sessionId } = useParams();
  const navigate = useNavigate();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [filter, setFilter] = useState("all");
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

  // Only to name the team a session belongs to; the list itself is already
  // limited to what the server says this user may see.
  useEffect(() => {
    api.teams().then(setTeams).catch(() => setTeams([]));
  }, []);

  const teamName = (id: number | null) =>
    teams.find((t) => t.id === id)?.name ?? (id === null ? "Not in a team" : "Another team");

  const groups = useMemo(() => {
    const shown = sessions.filter((s) => {
      if (filter === "all") return true;
      if (filter === "mine") return s.owner_id === me.id;
      if (filter === "shared") return s.owner_id !== me.id;
      return s.team_id === Number(filter.replace("team:", ""));
    });
    const byTeam = new Map<number, Session[]>();
    for (const s of shown) {
      const key = s.team_id ?? NO_TEAM;
      byTeam.set(key, [...(byTeam.get(key) ?? []), s]);
    }
    return [...byTeam.entries()].sort(([a], [b]) => a - b);
  }, [sessions, filter, me.id]);

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
            <select
              style={{ width: "100%", marginTop: 8 }}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="all">Everything I can see</option>
              <option value="mine">Mine</option>
              <option value="shared">Shared with me</option>
              {teams.map((t) => (
                <option key={t.id} value={`team:${t.id}`}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          {groups.length === 0 && <div className="empty">Nothing here yet.</div>}
          {groups.map(([key, items]) => (
            <div key={key}>
              <div className="session-group">{teamName(key === NO_TEAM ? null : key)}</div>
              {items.map((s) => (
                <Link
                  key={s.id}
                  to={`/sessions/${s.id}`}
                  className={`session-item${s.id === sessionId ? " active" : ""}`}
                >
                  <span className="title">{s.title}</span>
                  <span className="meta">
                    {s.provider}
                    {s.model ? ` · ${s.model}` : ""} ·{" "}
                    <span className={`pill ${s.status}`}>{s.status}</span>
                    {/* Not shown to admins: they see every session, so the
                        label would be on almost every row and would be a lie. */}
                    {s.owner_id !== me.id && !me.is_admin && (
                      <span className="pill">shared with you</span>
                    )}
                    {s.owner_id === me.id && s.shared_user_ids.length > 0 && (
                      <span className="pill">shared</span>
                    )}
                  </span>
                </Link>
              ))}
            </div>
          ))}
        </div>

        {creating ? (
          <NewSession
            teams={teams}
            onCancel={() => setCreating(false)}
            onCreated={async (session) => {
              setCreating(false);
              await load();
              navigate(`/sessions/${session.id}`);
            }}
          />
        ) : sessionId ? (
          <Chat key={sessionId} sessionId={sessionId} me={me} onChanged={load} />
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
  teams,
  onCancel,
  onCreated,
}: {
  teams: Team[];
  onCancel: () => void;
  onCreated: (session: Session) => void;
}) {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [accountId, setAccountId] = useState("");
  const [presetId, setPresetId] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      const [p, pr, w, a] = await Promise.all([
        api.providers(),
        api.presets(),
        api.workspaces(),
        api.accounts().catch(() => [] as Account[]),
      ]);
      setProviders(p);
      setPresets(pr);
      setWorkspaces(w);
      setAccounts(a);
    })();
  }, []);

  // Presets and accounts belong to one provider; clear them when it changes.
  useEffect(() => {
    setPresetId("");
    setAccountId("");
    setModel("");
  }, [provider]);

  const current = providers.find((p) => p.name === provider);
  const availablePresets = presets.filter((p) => p.provider === provider);
  const availableAccounts = accounts.filter((a) => a.provider === provider);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(
        await api.createSession({
          provider,
          model: model || null,
          account_id: accountId ? Number(accountId) : null,
          preset_id: presetId ? Number(presetId) : null,
          workspace_id: workspaceId ? Number(workspaceId) : null,
          team_id: teamId ? Number(teamId) : null,
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
            <span>Account</span>
            <select value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              <option value="">Default for {provider}</option>
              {availableAccounts.map((a) => (
                <option key={a.id} value={a.id} disabled={!a.usable_by_me}>
                  {a.name}
                  {a.signed_in ? "" : " — signed out"}
                  {a.usable_by_me ? "" : " — no access"}
                </option>
              ))}
            </select>
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
          <label>
            <span>Team</span>
            <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
              <option value="">Just me</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
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
