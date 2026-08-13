import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Usage as UsageData } from "../types";

const fmt = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n);

export default function Usage() {
  const [data, setData] = useState<UsageData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setData(await api.usage());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const peak = Math.max(1, ...(data?.windows.map((w) => w.total_tokens) ?? [1]));

  return (
    <div className="main">
      <h1>Usage</h1>
      <p className="subtitle">
        What AIOps has run recently, by window and by account.
      </p>
      {error && <div className="error-banner">{error}</div>}

      {data && (
        <>
          <div className="stat-row">
            {data.windows.map((w) => (
              <div className="stat" key={w.label}>
                <div className="stat-label">{w.label}</div>
                <div className="stat-value">{fmt(w.total_tokens)}</div>
                <div className="stat-sub">tokens · {w.runs} run{w.runs === 1 ? "" : "s"}</div>
                <div className="meter">
                  <span style={{ width: `${(w.total_tokens / peak) * 100}%` }} />
                </div>
                <div className="stat-sub">
                  in {fmt(w.input_tokens)} · out {fmt(w.output_tokens)}
                  {w.cache_read_tokens > 0 && ` · cached ${fmt(w.cache_read_tokens)}`}
                </div>
                <div className="stat-sub">≈ ${w.cost_usd.toFixed(4)} at API rates</div>
              </div>
            ))}
          </div>

          <div className="note-banner">{data.note}</div>

          <h2>By account (last 7 days)</h2>
          {data.by_account.length === 0 ? (
            <div className="empty">No accounts configured.</div>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Provider</th>
                  <th>Runs</th>
                  <th>Tokens</th>
                  <th>Est. cost</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {data.by_account.map((a) => {
                  const limited =
                    a.limited_until && new Date(a.limited_until + "Z") > new Date();
                  return (
                    <tr key={a.account_id}>
                      <td data-label="Account">{a.name}</td>
                      <td data-label="Provider">{a.provider}</td>
                      <td data-label="Runs">{a.runs}</td>
                      <td data-label="Tokens">{fmt(a.total_tokens)}</td>
                      <td data-label="Est. cost">${a.cost_usd.toFixed(4)}</td>
                      <td data-label="State">
                        {limited ? (
                          <span className="pill cancelled">limit hit</span>
                        ) : (
                          <span className="pill ok">available</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
