import type * as React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { LoginFlow, ProviderInfo, User } from "../types";

export default function Providers({ me }: { me: User }) {
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
        AIOps drives the real agent CLIs, so each one has to be signed in on the server. You can do
        that from here — you authenticate on the provider's own site and AIOps only relays the link
        and code, so no account password ever passes through it.
      </p>
      {error && <div className="error-banner">{error}</div>}
      <button onClick={load} disabled={loading}>
        {loading ? "Checking…" : "Re-check"}
      </button>

      {items.map((p) => (
        <ProviderCard key={p.name} provider={p} canManage={me.is_admin} onChanged={load} />
      ))}
    </div>
  );
}

function ProviderCard({
  provider,
  canManage,
  onChanged,
}: {
  provider: ProviderInfo;
  canManage: boolean;
  onChanged: () => void;
}) {
  const [flow, setFlow] = useState<LoginFlow | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const pollRef = useRef<number | undefined>(undefined);

  const stopPolling = () => {
    window.clearInterval(pollRef.current);
    pollRef.current = undefined;
  };

  // Poll while a sign-in is in flight; the CLI completes on its own schedule.
  useEffect(() => {
    const active = flow && ["starting", "awaiting_user", "completing"].includes(flow.status);
    if (!active) {
      stopPolling();
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.providerLoginStatus(provider.name);
        setFlow(next);
        if (next.status === "success") {
          stopPolling();
          onChanged();
        } else if (["failed", "expired", "cancelled"].includes(next.status)) {
          stopPolling();
        }
      } catch {
        /* transient; keep polling */
      }
    }, 2000);
    return stopPolling;
  }, [flow, provider.name, onChanged]);

  useEffect(() => stopPolling, []);

  const start = async () => {
    setBusy(true);
    setError(null);
    setCode("");
    try {
      setFlow(await api.startProviderLogin(provider.name));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const submitCode = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setFlow(await api.submitProviderLoginCode(provider.name, code));
      setCode("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    await api.cancelProviderLogin(provider.name).catch(() => {});
    setFlow(null);
    stopPolling();
  };

  const signOut = async () => {
    if (!confirm(`Sign ${provider.label} out on the server?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.providerLogout(provider.name);
      setFlow(null);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const copy = async (value: string, what: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(what);
      window.setTimeout(() => setCopied(null), 1500);
    } catch {
      setError("Clipboard blocked by the browser — select and copy manually.");
    }
  };

  const active = flow && ["starting", "awaiting_user", "completing"].includes(flow.status);

  return (
    <div className="card provider-card">
      <div className="row">
        <h2 style={{ margin: 0, flex: 1 }}>{provider.label}</h2>
        <span className={`pill ${provider.available ? "ok" : "failed"}`}>
          {provider.available ? "installed" : "not installed"}
        </span>
        {provider.available && (
          <span className={`pill ${provider.authenticated ? "ok" : "failed"}`}>
            {provider.authenticated ? "signed in" : "signed out"}
          </span>
        )}
        {provider.available && canManage && !active && (
          provider.authenticated ? (
            <button className="danger" onClick={signOut} disabled={busy}>
              Sign out
            </button>
          ) : (
            <button className="primary" onClick={start} disabled={busy}>
              {busy ? "Starting…" : "Sign in"}
            </button>
          )
        )}
      </div>

      {!canManage && !provider.authenticated && provider.available && (
        <p className="subtitle" style={{ margin: "10px 0 0" }}>
          Signing providers in requires an administrator account.
        </p>
      )}

      {error && <div className="error-banner" style={{ marginTop: 12 }}>{error}</div>}

      {flow && flow.status === "success" && (
        <div className="ok-banner">Signed in successfully.</div>
      )}
      {flow && ["failed", "expired"].includes(flow.status) && (
        <div className="error-banner" style={{ marginTop: 12 }}>
          {flow.message ?? "Sign-in did not complete."}
        </div>
      )}

      {active && (
        <div className="login-flow">
          <ol>
            <li>
              <div className="step-head">Open this link and approve the sign-in</div>
              {flow!.verification_url ? (
                <div className="row">
                  <a
                    className="linkbox"
                    href={flow!.verification_url}
                    target="_blank"
                    rel="noreferrer noopener"
                  >
                    {flow!.verification_url.length > 78
                      ? flow!.verification_url.slice(0, 78) + "…"
                      : flow!.verification_url}
                  </a>
                  <button onClick={() => copy(flow!.verification_url!, "url")}>
                    {copied === "url" ? "Copied" : "Copy link"}
                  </button>
                </div>
              ) : (
                <span className="subtitle">Waiting for the CLI to produce a link…</span>
              )}
            </li>

            {flow!.user_code && (
              <li>
                <div className="step-head">Enter this one-time code on that page</div>
                <div className="row">
                  <code className="devicecode">{flow!.user_code}</code>
                  <button onClick={() => copy(flow!.user_code!, "code")}>
                    {copied === "code" ? "Copied" : "Copy code"}
                  </button>
                </div>
              </li>
            )}

            {flow!.needs_code && (
              <li>
                <div className="step-head">Paste the code you are given back here</div>
                <form className="row" onSubmit={submitCode}>
                  <input
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    placeholder="Authorization code from the browser"
                    autoComplete="off"
                    spellCheck={false}
                    style={{ maxWidth: 420 }}
                  />
                  <button className="primary" type="submit" disabled={busy || !code.trim()}>
                    Submit
                  </button>
                </form>
              </li>
            )}
          </ol>

          <div className="row" style={{ marginTop: 6 }}>
            <span className="pill running">
              {flow!.status === "completing" ? "verifying…" : "waiting for you"}
            </span>
            {flow!.expires_in > 0 && (
              <span className="subtitle" style={{ margin: 0 }}>
                expires in {Math.floor(flow!.expires_in / 60)}m {flow!.expires_in % 60}s
              </span>
            )}
            <button onClick={cancel}>Cancel</button>
          </div>
        </div>
      )}

      <table style={{ marginTop: 12 }}>
        <tbody>
          <tr>
            <th style={{ width: 170 }}>Binary</th>
            <td className="mono">{provider.binary}</td>
          </tr>
          <tr>
            <th>Version</th>
            <td className="mono">{provider.version ?? "—"}</td>
          </tr>
          {provider.account && (
            <tr>
              <th>Account</th>
              <td className="mono">{provider.account}</td>
            </tr>
          )}
          <tr>
            <th>Models offered</th>
            <td className="mono">{provider.models.join(", ")}</td>
          </tr>
          <tr>
            <th>Permission modes</th>
            <td className="mono">{provider.permission_modes.join(", ")}</td>
          </tr>
        </tbody>
      </table>

      {provider.detail && (
        <details style={{ marginTop: 10 }}>
          <summary style={{ color: "var(--text-dim)", cursor: "pointer" }}>CLI output</summary>
          <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
            {provider.detail}
          </pre>
        </details>
      )}
    </div>
  );
}
