import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fullName, nameById } from "../names";
import type { GithubAccount, GithubAccountGrant, User, UserSummary } from "../types";

const EMPTY = { label: "", token: "" };

type Draft = typeof EMPTY;

/**
 * GitHub personal access tokens, stored so agents can clone, push, pull and
 * open pull requests on your behalf.
 *
 * Owned and shared exactly as a stored system is: a GitHub account is a
 * bearer credential, so it is private to whoever added it — administrators
 * included — until they name somebody here. The token itself is write-only:
 * once saved, this page (and the API behind it) only ever reports whether
 * one is stored, never its value.
 */
export default function GithubAccounts({ me }: { me: User }) {
  const [items, setItems] = useState<GithubAccount[]>([]);
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [grants, setGrantDraft] = useState<GithubAccountGrant[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setItems(await api.githubAccounts());
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
    setGrantDraft([]);
  };

  const edit = (a: GithubAccount) => {
    setEditingId(a.id);
    // A grant to the owner is a no-op the sharing list cannot show, since it
    // never lists you against your own account. Drop it rather than carry it.
    setGrantDraft(a.grants.filter((g) => g.user_id !== a.owner_id));
    setDraft({ label: a.label, token: "" });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (editingId) {
        const payload: { label: string; grants: GithubAccountGrant[]; token?: string } = {
          label: draft.label,
          grants,
        };
        // Only send a token the operator actually typed. An empty box on an
        // edit means "keep what is stored", not "clear it".
        if (draft.token.trim()) payload.token = draft.token;
        await api.updateGithubAccount(editingId, payload);
      } else {
        await api.createGithubAccount({ label: draft.label, token: draft.token, grants });
      }
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (a: GithubAccount) => {
    if (
      !confirm(
        `Remove "${a.label}"? Any workspace linked to it keeps working — it simply loses ` +
          `GitHub push/pull and pull-request access.`,
      )
    )
      return;
    try {
      await api.deleteGithubAccount(a.id);
      if (editingId === a.id) reset();
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
      <h1>GitHub accounts</h1>
      <p className="subtitle">
        A personal access token, pasted once, so agents can clone a repository into a new
        workspace, push and pull inside one you already have, and open pull requests — all as
        you. Paste a token with the scopes you want it to actually have: <code>repo</code> for a
        private repository, at minimum.
      </p>
      <p className="subtitle">
        The token is encrypted before it is stored and is never sent back to this page — you can
        replace one, but not read it. A turn only ever uses the token of the account linked to the
        workspace it runs in, and only when whoever sent that turn has been given access to that
        account here — a shared session does not lend out its owner's GitHub token.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={submit}>
        <label>
          <span>Label — what you'll recognise it by</span>
          <input
            value={draft.label}
            onChange={(e) => set("label", e.target.value)}
            placeholder="Jordan's GitHub"
            required
          />
        </label>
        <label>
          <span>
            Personal access token{" "}
            {editingId && <em className="hint">— leave blank to keep the stored one</em>}
          </span>
          <input
            type="password"
            value={draft.token}
            onChange={(e) => set("token", e.target.value)}
            placeholder="ghp_…"
            autoComplete="new-password"
            required={!editingId}
          />
          <span className="field-hint">
            Create one at github.com → Settings → Developer settings → Personal access tokens.
          </span>
        </label>

        <fieldset className="sharing">
          <legend>Who else can use it</legend>
          <p className="hint">
            This account is yours. Nobody else can clone, push, pull or open a pull request with
            it — administrators included — until you name them here. <strong>Use</strong> lets
            their turns act as this account; <strong>manage</strong> also lets them replace the
            token and share it onward.
          </p>
          {users.filter((u) => u.id !== me.id).length === 0 ? (
            <p className="hint">There is nobody else to share with yet.</p>
          ) : (
            users
              .filter((u) => u.id !== me.id)
              .map((u) => (
                <label key={u.id} className="row share-row">
                  <span style={{ margin: 0, flex: 1 }}>{fullName(u)}</span>
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
          <button className="primary" type="submit" disabled={busy || !draft.label.trim()}>
            {editingId ? "Save changes" : "Add account"}
          </button>
          {editingId && (
            <button type="button" onClick={reset}>
              Cancel
            </button>
          )}
        </div>
      </form>

      {items.length === 0 ? (
        <div className="empty">
          No GitHub accounts yet. Add one above, then clone a repository into a new workspace from
          the Workspaces page.
        </div>
      ) : (
        items.map((a) => (
          <div className="card" key={a.id}>
            <div className="row">
              <h2 style={{ margin: 0, flex: 1 }}>{a.label}</h2>
              <span className={`pill ${a.has_token ? "ok" : "failed"}`}>
                {a.has_token ? "token stored" : "no token — will fail"}
              </span>
              {a.my_level !== "owner" && <span className="pill">{a.my_level}</span>}
            </div>
            <div className="row" style={{ marginTop: 12, flexWrap: "wrap" }}>
              <span className="hint" style={{ flex: 1 }}>
                {a.owner_id === me.id
                  ? sharedWithLabel(a, users)
                  : `Shared with you by ${nameById(users, a.owner_id, "another user")}`}
              </span>
              {a.my_level !== "use" && (
                <>
                  <button onClick={() => edit(a)}>Edit &amp; sharing</button>
                  <button className="danger" onClick={() => remove(a)}>
                    Remove
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

/** "Only you" / "Shared with alice, bob" — the owner's view of who else is in. */
function sharedWithLabel(a: GithubAccount, users: UserSummary[]): string {
  const shared = a.grants.filter((g) => g.user_id !== a.owner_id);
  if (shared.length === 0) return "Only you can use this";
  const names = shared.map((g) => nameById(users, g.user_id, `user ${g.user_id}`)).sort();
  return `Shared with ${names.slice(0, 4).join(", ")}${
    names.length > 4 ? ` and ${names.length - 4} more` : ""
  }`;
}
