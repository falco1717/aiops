import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Target, TargetGrant, User, UserSummary } from "../types";

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
  const [users, setUsers] = useState<UserSummary[]>([]);
  // Who this system is being shared with, edited alongside the rest of the form.
  const [grants, setGrantDraft] = useState<TargetGrant[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [keyFile, setKeyFile] = useState<string | null>(null);
  const [keyError, setKeyError] = useState<string | null>(null);

  /** Load a key file straight from disk — nobody should have to cat and paste. */
  const readKeyFile = async (file: File | undefined) => {
    if (!file) return;
    setKeyError(null);
    if (file.size > 64 * 1024) {
      setKeyError("That file is far too large to be an SSH key. Did you pick the right one?");
      return;
    }
    const text = await file.text();
    const problem = validateKey(text);
    setKeyError(problem);
    if (problem?.startsWith("That looks like a public key")) {
      // A public key is useless here and the mistake is easy to make, so do not
      // load it — otherwise it saves cleanly and only fails at connect time.
      setKeyFile(null);
      return;
    }
    set("private_key", text);
    setKeyFile(file.name);
  };

  const load = useCallback(async () => {
    try {
      setItems(await api.targets());
      setUsers(await api.userDirectory().catch(() => []));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }) as Draft);

  const reset = () => {
    setDraft(EMPTY);
    setEditingId(null);
    setKeyFile(null);
    setKeyError(null);
    setGrantDraft([]);
  };

  const edit = (t: Target) => {
    setEditingId(t.id);
    setKeyFile(null);
    setKeyError(null);
    setGrantDraft(t.grants);
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
      grants,
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

  const setGrant = (userId: number, level: "" | "use" | "manage") =>
    setGrantDraft((prev) => {
      const rest = prev.filter((g) => g.user_id !== userId);
      return level ? [...rest, { user_id: userId, level }] : rest;
    });

  const levelOf = (userId: number) => grants.find((g) => g.user_id === userId)?.level ?? "";

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
                <div className="keydrop">
                  <input
                    type="file"
                    id="keyfile"
                    className="keyfile-input"
                    onChange={(e) => readKeyFile(e.target.files?.[0])}
                  />
                  <label htmlFor="keyfile" className="keyfile-label">
                    Choose key file…
                  </label>
                  <span className="hint">
                    {keyFile ? `Loaded ${keyFile}` : "Usually ~/.ssh/id_ed25519 or id_rsa"}
                  </span>
                </div>
                {keyError && <div className="error-banner">{keyError}</div>}
                <details className="paste-key">
                  <summary>or paste it instead</summary>
                  <textarea
                    rows={5}
                    className="mono"
                    value={draft.private_key}
                    onChange={(e) => {
                      set("private_key", e.target.value);
                      setKeyFile(null);
                      setKeyError(validateKey(e.target.value));
                    }}
                    placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                    autoComplete="off"
                    spellCheck={false}
                  />
                </details>
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

          <fieldset className="sharing">
            <legend>Who else can reach it</legend>
            <p className="hint">
              This system is yours. Nobody else sees it — administrators included — until
              you name them here. <strong>Use</strong> lets their agents connect through it;
              <strong> manage</strong> also lets them edit it, replace the credential and
              share it onward.
            </p>
            {users.filter((u) => u.id !== me.id).length === 0 ? (
              <p className="hint">There is nobody else to share with yet.</p>
            ) : (
              users
                .filter((u) => u.id !== me.id)
                .map((u) => (
                  <label key={u.id} className="row share-row">
                    <span style={{ margin: 0, flex: 1 }}>{u.username}</span>
                    <select
                      value={levelOf(u.id)}
                      onChange={(e) => setGrant(u.id, e.target.value as "" | "use" | "manage")}
                    >
                      <option value="">No access</option>
                      <option value="use">Can use</option>
                      <option value="manage">Can manage</option>
                    </select>
                  </label>
                ))
            )}
          </fieldset>

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
        <div className="empty">No systems yet. Add one above.</div>
      ) : (
        items.map((t) => (
          <div className="card" key={t.id}>
            <div className="row">
              <h2 style={{ margin: 0, flex: 1 }}>{t.name}</h2>
              <code className="pill">ssh {t.slug}</code>
              <span className="pill">{t.auth_type}</span>
              {t.my_level !== "owner" && <span className="pill">{t.my_level}</span>}
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

            <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
              <span className="hint" style={{ flex: 1 }}>
                {t.owner_id === me.id
                  ? sharedWithLabel(t, users)
                  : `Shared with you by ${users.find((u) => u.id === t.owner_id)?.username ?? "another user"}`}
              </span>
              {t.my_level !== "use" && (
                <>
                  <button onClick={() => edit(t)}>Edit &amp; sharing</button>
                  <button className="danger" onClick={() => remove(t)}>
                    Delete
                  </button>
                </>
              )}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

/**
 * Catch the two mistakes that otherwise save cleanly and only surface as a
 * failed connection much later: uploading the `.pub` file, or a key whose
 * passphrase we will not be able to answer for.
 */
function validateKey(text: string): string | null {
  const value = text.trim();
  if (!value) return null;
  if (/^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-)/.test(value)) {
    return "That looks like a public key (.pub). AIOps needs the private key — the same filename without .pub.";
  }
  if (!value.includes("PRIVATE KEY")) {
    return "That does not look like an SSH private key. It should start with -----BEGIN.";
  }
  if (value.includes("ENCRYPTED") || value.includes("Proc-Type: 4,ENCRYPTED")) {
    return "This key is passphrase-protected — enter the passphrase below or it cannot be used.";
  }
  return null;
}

/** "Only you" / "Shared with alice, bob" — the owner's view of who else is in. */
function sharedWithLabel(t: Target, users: UserSummary[]): string {
  if (t.grants.length === 0) return "Only you can see this";
  const names = t.grants
    .map((g) => users.find((u) => u.id === g.user_id)?.username ?? `user ${g.user_id}`)
    .sort();
  return `Shared with ${names.slice(0, 4).join(", ")}${names.length > 4 ? ` and ${names.length - 4} more` : ""}`;
}

function credentialStored(t: Target): boolean {
  return t.auth_type === "key" ? t.has_private_key : t.has_password;
}
