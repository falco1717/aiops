import { useEffect, useState } from "react";
import { api } from "../api";
import type { ProviderInfo } from "../types";

const LOGIN_HINT: Record<string, string> = {
  claude: "docker compose exec -it app claude auth login",
  codex: "docker compose exec -it app codex login",
};

export default function Providers() {
  const [items, setItems] = useState<ProviderInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      setItems(await api.providers());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  return (
    <div className="main">
      <h1>Providers</h1>
      <p className="subtitle">
        AIOps drives the real agent CLIs, so each one has to be installed and signed in on the
        server. Sign in once per provider; the credentials persist in the mounted home volumes.
      </p>
      {error && <div className="error-banner">{error}</div>}
      <button onClick={load} disabled={loading}>
        {loading ? "Checking…" : "Re-check"}
      </button>

      {items.map((p) => (
        <div className="card" key={p.name} style={{ marginTop: 14 }}>
          <div className="row">
            <h2 style={{ margin: 0, flex: 1 }}>{p.name}</h2>
            <span className={`pill ${p.available ? "ok" : "failed"}`}>
              {p.available ? "installed" : "not installed"}
            </span>
            {p.available && (
              <span className={`pill ${p.authenticated ? "ok" : "failed"}`}>
                {p.authenticated ? "signed in" : "signed out"}
              </span>
            )}
          </div>
          <table style={{ marginTop: 10 }}>
            <tbody>
              <tr>
                <th style={{ width: 160 }}>Binary</th>
                <td className="mono">{p.binary}</td>
              </tr>
              <tr>
                <th>Version</th>
                <td className="mono">{p.version ?? "—"}</td>
              </tr>
              <tr>
                <th>Models offered</th>
                <td className="mono">{p.models.join(", ")}</td>
              </tr>
              <tr>
                <th>Permission modes</th>
                <td className="mono">{p.permission_modes.join(", ")}</td>
              </tr>
              {!p.authenticated && (
                <tr>
                  <th>Sign in with</th>
                  <td className="mono">{LOGIN_HINT[p.name] ?? `${p.binary} login`}</td>
                </tr>
              )}
            </tbody>
          </table>
          {p.detail && (
            <details style={{ marginTop: 10 }}>
              <summary style={{ color: "var(--text-dim)", cursor: "pointer" }}>CLI output</summary>
              <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
                {p.detail}
              </pre>
            </details>
          )}
        </div>
      ))}
    </div>
  );
}
