import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Target, User } from "../types";

const EMPTY = {
  name: "",
  hostname: "",
  username: "",
  port: "22",
  description: "",
  auth_type: "key",
  private_key: "",
  passphrase: "",
  password: "",
  host_key_policy: "accept-new",
  known_host_key: "",
};

type Draft = typeof EMPTY;

/**
 * Systems an agent can reach by name.
 *
 * Secrets are write-only: once saved, the API reports only that one exists.
 * Editing a system therefore leaves its stored credential alone unless a new
 * one is typed, so renaming a host cannot silently wipe its key.
 */
export default function Targets({ me }: { me: User }) {
  const [items, setItems] = useState<Target[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await api.targets());
      if (me.is_admin) setUsers(await api.users().catch(() => []));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [me.is_admin]);

  useEffect(() => {
    void load();
  }, [load]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }) as Draft);

  const reset = () => {
    setDraft(EMPTY);
    setEditingId(null);
  };

  const edit = (t: Target) => {
    setEditingId(t.id);
    setDraft({
      ...EMPTY,
      name: t.name,
      hostname: t.hostname,
      username: t.username,
      port: String(t.port),
      description: t.description ?? "",
      auth_type: t.auth_type,
      host_key_policy: t.host_key_policy,
    });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    const payload: Record<string, unknown> = {
      name: draft.name,
      hostname: draft.hostname,
      username: draft.username,
      port: Number(draft.port) || 22,
      description: draft.description || null,
      auth_type: draft.auth_type,
      host_key_policy: draft.host_key_policy,
    };
    // Only send a secret the operator actually typed. An empty box on an edit
    // means "keep what is stored", not "clear it".
    if (draft.private_key.trim()) payload.private_key = draft.private_key;
    if (draft.passphrase.trim()) payload.passphrase = draft.passphrase;
    if (draft.password.trim()) payload.password = draft.password;
    if (draft.known_host_key.trim()) payload.known_host_key = draft.known_host_key;

    try {
      if (editingId) await api.updateTarget(editingId, payload);
      else await api.createTarget(payload);
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (t: Target) => {
    if (!confirm(`Delete "${t.name}"? Its stored credentials are destroyed.`)) return;
    try {
      await api.deleteTarget(t.id);
      if (editingId === t.id) reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const setGrants = async (t: Target, userId: number, granted: boolean) => {
    const next = granted
      ? [...t.allowed_user_ids, userId]
      : t.allowed_user_ids.filter((id) => id !== userId);
    try {
      await api.updateTarget(t.id, { allowed_user_ids: next });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="main">
      <h1>Systems</h1>
      <p className="subtitle">
        Machines an agent can reach by name. Credentials are encrypted before they are
        stored and are never sent back to this page — you can replace one, but not read
        it. During a run they exist only in a private directory that is deleted when the
        run ends.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {me.is_admin && (
        <form className="card" onSubmit={submit}>
          <div className="grid-2">
            <label>
              <span>Name</span>
              <input
                value={draft.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="Example Box"
                required
              />
            </label>
            <label>
              <span>Hostname or IP</span>
              <input
                value={draft.hostname}
                onChange={(e) => set("hostname", e.target.value)}
                placeholder="203.0.113.20"
                required
              />
            </label>
            <label>
              <span>Username</span>
              <input
                value={draft.username}
                onChange={(e) => set("username", e.target.value)}
                required
              />
            </label>
            <label>
              <span>Port</span>
              <input
                value={draft.port}
                onChange={(e) => set("port", e.target.value)}
                inputMode="numeric"
              />
            </label>
            <label>
              <span>Authentication</span>
              <select value={draft.auth_type} onChange={(e) => set("auth_type", e.target.value)}>
                <option value="key">SSH key</option>
                <option value="password">Password</option>
              </select>
            </label>
            <label>
              <span>Host key</span>
              <select
                value={draft.host_key_policy}
                onChange={(e) => set("host_key_policy", e.target.value)}
              >
                <option value="accept-new">Trust on first use, then pin</option>
                <option value="strict">Strict — must already be known</option>
              </select>
            </label>
          </div>

          <label>
            <span>Description</span>
            <input value={draft.description} onChange={(e) => set("description", e.target.value)} />
          </label>

          {draft.auth_type === "key" ? (
            <>
              <label>
                <span>
                  Private key{" "}
                  {editingId && <em className="hint">— leave blank to keep the stored one</em>}
                </span>
                <textarea
                  rows={5}
                  className="mono"
                  value={draft.private_key}
                  onChange={(e) => set("private_key", e.target.value)}
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                  autoComplete="off"
                  spellCheck={false}
                />
              </label>
              <label>
                <span>Key passphrase (if the key has one)</span>
                <input
                  type="password"
                  value={draft.passphrase}
                  onChange={(e) => set("passphrase", e.target.value)}
                  autoComplete="new-password"
                />
              </label>
            </>
          ) : (
            <label>
              <span>
                Password{" "}
                {editingId && <em className="hint">— leave blank to keep the stored one</em>}
              </span>
              <input
                type="password"
                value={draft.password}
                onChange={(e) => set("password", e.target.value)}
                autoComplete="new-password"
              />
            </label>
          )}

          <label>
            <span>Known host key (optional, required for strict)</span>
            <textarea
              rows={2}
              className="mono"
              value={draft.known_host_key}
              onChange={(e) => set("known_host_key", e.target.value)}
              placeholder="203.0.113.20 ssh-ed25519 AAAA..."
              spellCheck={false}
            />
          </label>

          <div className="row">
            <button className="primary" type="submit" disabled={busy || !draft.name.trim()}>
              {editingId ? "Save changes" : "Add system"}
            </button>
            {editingId && (
              <button type="button" onClick={reset}>
                Cancel
              </button>
            )}
          </div>
        </form>
      )}

      {items.length === 0 ? (
        <div className="empty">
          No systems yet.{" "}
          {me.is_admin ? "Add one above." : "An administrator can add them."}
        </div>
      ) : (
        items.map((t) => (
          <div className="card" key={t.id}>
            <div className="row">
              <h2 style={{ margin: 0, flex: 1 }}>{t.name}</h2>
              <code className="pill">ssh {t.slug}</code>
              <span className="pill">{t.auth_type}</span>
              {!t.usable_by_me && <span className="pill cancelled">no access</span>}
            </div>
            <div style={{ color: "var(--text-dim)", fontSize: 13, margin: "6px 0 10px" }}>
              {t.username}@{t.hostname}:{t.port}
              {t.description ? ` · ${t.description}` : ""}
            </div>

            <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              <span className={`pill ${credentialStored(t) ? "ok" : "failed"}`}>
                {credentialStored(t) ? "credential stored" : "no credential — will fail"}
              </span>
              <span className="pill">{t.host_key_policy}</span>
              {t.host_key_policy === "strict" && !t.has_known_host_key && (
                <span className="pill failed">strict, but no host key saved</span>
              )}
            </div>

            {me.is_admin && (
              <>
                <div className="row" style={{ marginTop: 12 }}>
                  <button onClick={() => edit(t)}>Edit</button>
                  <button className="danger" onClick={() => remove(t)}>
                    Delete
                  </button>
                </div>
                {users.length > 0 && (
                  <details style={{ marginTop: 10 }}>
                    <summary>
                      Who can use it —{" "}
                      {t.allowed_user_ids.length === 0
                        ? "everyone"
                        : `${t.allowed_user_ids.length} user(s)`}
                    </summary>
                    <p className="hint" style={{ margin: "8px 0" }}>
                      With nobody selected this system is open to every AIOps user. Selecting
                      anyone restricts it to them and to administrators.
                    </p>
                    {users.map((u) => (
                      <label key={u.id} className="row" style={{ gap: 8, margin: "4px 0" }}>
                        <input
                          type="checkbox"
                          style={{ width: 16 }}
                          checked={t.allowed_user_ids.includes(u.id)}
                          onChange={(e) => setGrants(t, u.id, e.target.checked)}
                        />
                        <span style={{ margin: 0 }}>{u.username}</span>
                      </label>
                    ))}
                  </details>
                )}
              </>
            )}
          </div>
        ))
      )}
    </div>
  );
}

function credentialStored(t: Target): boolean {
  return t.auth_type === "key" ? t.has_private_key : t.has_password;
}
