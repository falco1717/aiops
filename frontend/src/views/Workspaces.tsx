import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fullName, nameById } from "../names";
import type { User, UserSummary, Workspace, WorkspaceGrant, WorkspaceStatus } from "../types";

const EMPTY = { name: "", path: "", description: "" };

type Draft = typeof EMPTY;

/**
 * Project folders on the server that agents may run inside.
 *
 * Owned and shared exactly as a stored system is: a workspace is its contents,
 * so it is private to whoever registered it — administrators included — until
 * they name somebody here.
 */
export default function Workspaces({ me }: { me: User }) {
  const [items, setItems] = useState<Workspace[]>([]);
  const [statuses, setStatuses] = useState<Record<number, WorkspaceStatus>>({});
  const [users, setUsers] = useState<UserSummary[]>([]);
  const [diff, setDiff] = useState<{ id: number; text: string } | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  // Who this workspace is being shared with, edited alongside the rest of the form.
  const [grants, setGrantDraft] = useState<WorkspaceGrant[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const rows = await api.workspaces();
      setItems(rows);
      setUsers(await api.userDirectory().catch(() => []));
      const entries = await Promise.all(
        rows.map(async (w) => [w.id, await api.workspaceStatus(w.id)] as const),
      );
      const next: Record<number, WorkspaceStatus> = {};
      for (const [id, status] of entries) next[id] = status;
      setStatuses(next);
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
    setGrantDraft([]);
    setEditingId(null);
  };

  const edit = (w: Workspace) => {
    setEditingId(w.id);
    // A grant to the owner is a no-op the sharing list cannot show, since it
    // never lists you against your own workspace. Drop it rather than carry it.
    setGrantDraft(w.grants.filter((g) => g.user_id !== w.owner_id));
    setDraft({ name: w.name, path: w.path, description: w.description ?? "" });
  };

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      if (editingId) {
        // No path: re-pointing a registered workspace would change what every
        // session already using it is looking at, so the API does not take one.
        await api.updateWorkspace(editingId, {
          name: draft.name,
          description: draft.description || null,
          grants,
        });
      } else {
        await api.createWorkspace({
          name: draft.name,
          path: draft.path,
          description: draft.description || undefined,
          grants,
        });
      }
      reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (w: Workspace) => {
    if (!confirm(`Unregister "${w.name}"? The directory on disk is left alone.`)) return;
    try {
      await api.deleteWorkspace(w.id);
      if (editingId === w.id) reset();
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const showDiff = async (w: Workspace) => {
    try {
      const { diff: text } = await api.workspaceDiff(w.id);
      setDiff({ id: w.id, text: text || "(no uncommitted changes)" });
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
      <h1>Workspaces</h1>
      <p className="subtitle">
        A workspace is a project folder on this server. Point a session at one and that folder
        becomes the agent's working directory: it starts there, reads and edits the files in it,
        and runs its commands from it — so "fix the login bug in this repo" has a repo to mean.
      </p>
      <p className="subtitle">
        Register one per repo or project you want agents to work on. A session with no workspace
        starts in the workspace root instead, with no project around it — it can still answer
        questions and reach your stored systems, but it has no code in front of it. Every path
        must live under the configured root, which is the boundary keeping agents away from the
        rest of the server.
      </p>
      <p className="subtitle">
        A workspace you register is yours. Nobody else can see it, work in it or read its diff —
        administrators included — until you share it below, because a workspace is whatever is
        checked out in it.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={submit}>
        <div className="grid-2">
          <label>
            <span>Name — what you'll pick from the session's Workspace list</span>
            <input
              value={draft.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="My Project"
              required
            />
          </label>
          <label>
            <span>Folder (absolute, or relative to the workspace root)</span>
            <input
              value={draft.path}
              onChange={(e) => set("path", e.target.value)}
              placeholder="my-project"
              required
              disabled={editingId !== null}
            />
            <span className="field-hint">
              {editingId !== null
                ? "The folder cannot be moved once registered — sessions already point at it. " +
                  "Unregister this workspace and add it again to change where it looks."
                : "Created if it does not exist. A git repo here is worth it: the Changes column " +
                  "and Diff button below then show what the agent did before you keep it."}
            </span>
          </label>
        </div>
        <label>
          <span>Description — a note to yourself about what lives here</span>
          <input value={draft.description} onChange={(e) => set("description", e.target.value)} />
        </label>

        <fieldset className="sharing">
          <legend>Who else can work in it</legend>
          <p className="hint">
            This workspace is yours. Nobody else sees it — administrators included — until you
            name them here. <strong>Use</strong> lets them point their own sessions at it and run
            turns in it; <strong>manage</strong> also lets them rename it, edit this description
            and share it onward.
          </p>
          {users.filter((u) => u.id !== me.id).length === 0 ? (
            <p className="hint">There is nobody else to share with yet.</p>
          ) : (
            users
              .filter((u) => u.id !== me.id)
              .map((u) => (
                <label key={u.id} className="row share-row">
                  {/* Both names: this hands somebody a checkout of your work, and
                      display names are deliberately not unique. */}
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
          <button className="primary" type="submit" disabled={busy || !draft.name.trim()}>
            {editingId ? "Save changes" : "Add workspace"}
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
          No workspaces yet. Until you add one — or somebody shares one with you — every session
          runs in the workspace root with no project around it.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Path</th>
              <th>Git</th>
              <th>Changes</th>
              <th>Access</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {items.map((w) => {
              const st = statuses[w.id];
              return (
                <tr key={w.id}>
                  <td data-label="Name">
                    {w.name}
                    {w.description && (
                      <div style={{ color: "var(--text-dim)", fontSize: 12 }}>{w.description}</div>
                    )}
                  </td>
                  <td className="mono" data-label="Path">
                    {w.path}
                  </td>
                  <td data-label="Git">
                    {!st ? (
                      "…"
                    ) : !st.exists ? (
                      <span className="pill failed">missing</span>
                    ) : st.is_git ? (
                      <span className="mono">
                        {st.branch} @ {st.head}
                      </span>
                    ) : (
                      <span className="pill">not a repo</span>
                    )}
                  </td>
                  <td data-label="Changes">
                    {st?.dirty_files.length ? `${st.dirty_files.length} file(s)` : "clean"}
                  </td>
                  <td data-label="Access">
                    {w.my_level !== "owner" && (
                      <span className="pill" style={{ marginRight: 6 }}>
                        {w.my_level}
                      </span>
                    )}
                    <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
                      {w.owner_id === me.id
                        ? sharedWithLabel(w, users)
                        : `Shared with you by ${nameById(users, w.owner_id, "another user")}`}
                    </span>
                  </td>
                  <td className="row actions">
                    {st?.is_git && <button onClick={() => showDiff(w)}>Diff</button>}
                    {w.my_level !== "use" && (
                      <>
                        <button onClick={() => edit(w)}>Edit &amp; sharing</button>
                        <button className="danger" onClick={() => remove(w)}>
                          Remove
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {diff && (
        <>
          <h2>Uncommitted diff</h2>
          <div className="diff">
            <pre>{diff.text}</pre>
          </div>
          <button style={{ marginTop: 10 }} onClick={() => setDiff(null)}>
            Close
          </button>
        </>
      )}
    </div>
  );
}

/** "Only you" / "Shared with alice, bob" — the owner's view of who else is in. */
function sharedWithLabel(w: Workspace, users: UserSummary[]): string {
  const shared = w.grants.filter((g) => g.user_id !== w.owner_id);
  if (shared.length === 0) return "Only you can see this";
  const names = shared.map((g) => nameById(users, g.user_id, `user ${g.user_id}`)).sort();
  return `Shared with ${names.slice(0, 4).join(", ")}${
    names.length > 4 ? ` and ${names.length - 4} more` : ""
  }`;
}
