import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { formatUtc, isFuture } from "../time";
import type { Account, Usage as UsageData } from "../types";

const fmt = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1_000 ? `${(n / 1_000).toFixed(1)}k` : String(n);

export default function Usage() {
  const [data, setData] = useState<UsageData | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [u, a] = await Promise.all([api.usage(), api.accounts().catch(() => [])]);
      setData(u);
      setAccounts(a);
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

      {/* Plan windows come from the CLI itself, so they reflect the real
          allowance rather than anything AIOps measured. */}
      {accounts.some((a) => a.limit_status) && (
        <>
          <h2 style={{ marginTop: 0 }}>Plan limits</h2>
          <div className="stat-row" style={{ marginBottom: 18 }}>
            {accounts
              .filter((a) => a.limit_status)
              .map((a) => (
                <div className="stat" key={a.id}>
                  <div className="stat-label">
                    {a.name} · {(a.limit_window ?? "window").replace(/_/g, "-")}
                  </div>
                  <div
                    className="stat-value"
                    style={{
                      fontSize: 20,
                      color: a.limit_status === "allowed" ? "var(--ok)" : "var(--warn)",
                    }}
                  >
                    {a.limit_status}
                  </div>
                  <div className="stat-sub">
                    {a.limit_resets_at
                      ? `resets ${formatUtc(a.limit_resets_at)}`
                      : "reset time not reported"}
                  </div>
                </div>
              ))}
          </div>
        </>
      )}

      {data && (
        <>
          <h2 style={{ marginTop: 0 }}>Measured by AIOps</h2>
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
                  const limited = isFuture(a.limited_until);
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
