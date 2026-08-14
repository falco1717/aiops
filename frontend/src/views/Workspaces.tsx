import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Workspace, WorkspaceStatus } from "../types";

export default function Workspaces() {
  const [items, setItems] = useState<Workspace[]>([]);
  const [statuses, setStatuses] = useState<Record<number, WorkspaceStatus>>({});
  const [diff, setDiff] = useState<{ id: number; text: string } | null>(null);
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await api.workspaces();
      setItems(rows);
      const entries = await Promise.all(
        rows.map(async (w) => [w.id, await api.workspaceStatus(w.id)] as const),
      );
      const next: Record<number, WorkspaceStatus> = {};
      for (const [id, status] of entries) next[id] = status;
      setStatuses(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await api.createWorkspace({ name, path, description: description || undefined });
      setName("");
      setPath("");
      setDescription("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const remove = async (w: Workspace) => {
    if (!confirm(`Unregister "${w.name}"? The directory on disk is left alone.`)) return;
    await api.deleteWorkspace(w.id);
    await load();
  };

  const showDiff = async (w: Workspace) => {
    const { diff: text } = await api.workspaceDiff(w.id);
    setDiff({ id: w.id, text: text || "(no uncommitted changes)" });
  };

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
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={create}>
        <div className="grid-2">
          <label>
            <span>Name — what you'll pick from the session's Workspace list</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Project"
              required
            />
          </label>
          <label>
            <span>Folder (absolute, or relative to the workspace root)</span>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="my-project"
              required
            />
            <span className="field-hint">
              Created if it does not exist. A git repo here is worth it: the Changes column and
              Diff button below then show what the agent did before you keep it.
            </span>
          </label>
        </div>
        <label>
          <span>Description — a note to yourself about what lives here</span>
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <button className="primary" type="submit">
          Add workspace
        </button>
      </form>

      {items.length === 0 ? (
        <div className="empty">
          No workspaces yet. Until you add one, every session runs in the workspace root with no
          project around it.
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Path</th>
              <th>Git</th>
              <th>Changes</th>
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
                  <td className="row actions">
                    {st?.is_git && <button onClick={() => showDiff(w)}>Diff</button>}
                    <button className="danger" onClick={() => remove(w)}>
                      Remove
                    </button>
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
