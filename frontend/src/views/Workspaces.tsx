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
        Directories agents may read and write. Every path must live under the configured workspace
        root, which is the boundary keeping agents away from the rest of the server.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <form className="card" onSubmit={create}>
        <div className="grid-2">
          <label>
            <span>Name</span>
            <input value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label>
            <span>Path (absolute, or relative to the workspace root)</span>
            <input
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="my-project"
              required
            />
          </label>
        </div>
        <label>
          <span>Description</span>
          <input value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <button className="primary" type="submit">
          Add workspace
        </button>
      </form>

      {items.length === 0 ? (
        <div className="empty">No workspaces registered.</div>
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
                  <td>
                    {w.name}
                    {w.description && (
                      <div style={{ color: "var(--text-dim)", fontSize: 12 }}>{w.description}</div>
                    )}
                  </td>
                  <td className="mono">{w.path}</td>
                  <td>
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
                  <td>{st?.dirty_files.length ? `${st.dirty_files.length} file(s)` : "clean"}</td>
                  <td className="row">
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
