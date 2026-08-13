import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { ProviderInfo } from "../types";

/**
 * Read-only health page for the two agent CLIs. Signing in happens per account
 * on the Accounts page, since one provider can now have several sign-ins.
 */
export default function Providers() {
  const [items, setItems] = useState<ProviderInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await api.providers());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="main">
      <h1>Providers</h1>
      <p className="subtitle">
        The agent CLIs installed in this container and what they support. To sign in, or to run
        several subscriptions side by side, use <Link to="/accounts">Accounts</Link>.
      </p>
      {error && <div className="error-banner">{error}</div>}
      <button onClick={load} disabled={loading}>
        {loading ? "Checking…" : "Re-check"}
      </button>

      {items.map((p) => (
        <div className="card" key={p.name} style={{ marginTop: 14 }}>
          <div className="row">
            <span className={`dot ${p.available ? "ok" : "off"}`} />
            <h2 style={{ margin: 0, flex: 1 }}>{p.label}</h2>
            <span className={`pill ${p.available ? "ok" : "failed"}`}>
              {p.available ? "installed" : "not installed"}
            </span>
          </div>
          <table style={{ marginTop: 10 }}>
            <tbody>
              <tr>
                <th style={{ width: 170 }}>Binary</th>
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
                <th>{p.name === "codex" ? "Sandbox modes" : "Permission modes"}</th>
                <td className="mono">{p.permission_modes.join(", ")}</td>
              </tr>
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
