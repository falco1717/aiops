import type * as React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Account, LoginFlow, ProviderInfo, User } from "../types";

export default function Accounts({ me }: { me: User }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [name, setName] = useState("");
  const [provider, setProvider] = useState("claude");
  const [description, setDescription] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [a, p] = await Promise.all([api.accounts(), api.providers()]);
      setAccounts(a);
      setProviders(p);
      if (me.is_admin) setUsers(await api.users().catch(() => []));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [me.is_admin]);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    try {
      await api.createAccount({ name, provider, description: description || null });
      setName("");
      setDescription("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="main">
      <h1>Accounts</h1>
      <p className="subtitle">
        Each account is a separate sign-in with its own credential directory, so several
        subscriptions live side by side — one per person, or one per plan. Give an account a
        fallback and AIOps moves work over automatically when the first hits its usage limit.
      </p>
      {error && <div className="error-banner">{error}</div>}

      <div className="row" style={{ marginBottom: 14 }}>
        <button onClick={load} disabled={loading}>
          {loading ? "Checking…" : "Re-check sign-in status"}
        </button>
      </div>

      {me.is_admin && (
        <form className="card" onSubmit={create}>
          <div className="grid-2">
            <label>
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Jordan's Claude"
                required
              />
            </label>
            <label>
              <span>Provider</span>
              <select value={provider} onChange={(e) => setProvider(e.target.value)}>
                {providers.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label>
            <span>Description</span>
            <input value={description} onChange={(e) => setDescription(e.target.value)} />
          </label>
          <button className="primary" type="submit" disabled={!name.trim()}>
            Add account
          </button>
        </form>
      )}

      {accounts.length === 0 ? (
        <div className="empty">No accounts yet.</div>
      ) : (
        accounts.map((a) => (
          <AccountCard
            key={a.id}
            account={a}
            accounts={accounts}
            users={users}
            me={me}
            onChanged={load}
          />
        ))
      )}
    </div>
  );
}

function AccountCard({
  account,
  accounts,
  users,
  me,
  onChanged,
}: {
  account: Account;
  accounts: Account[];
  users: User[];
  me: User;
  onChanged: () => void;
}) {
  const [flow, setFlow] = useState<LoginFlow | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [popupBlocked, setPopupBlocked] = useState(false);
  const [showAccess, setShowAccess] = useState(false);
  const pollRef = useRef<number | undefined>(undefined);

  const stop = () => {
    window.clearInterval(pollRef.current);
    pollRef.current = undefined;
  };

  useEffect(() => {
    const active = flow && ["starting", "awaiting_user", "completing"].includes(flow.status);
    if (!active) {
      stop();
      return;
    }
    if (pollRef.current) return;
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.accountLoginStatus(account.id);
        setFlow(next);
        if (next.status === "success") {
          stop();
          onChanged();
        } else if (["failed", "expired", "cancelled"].includes(next.status)) {
          stop();
        }
      } catch {
        /* transient */
      }
    }, 2000);
    return stop;
  }, [flow, account.id, onChanged]);

  useEffect(() => stop, []);

  const openTab = (url: string, prepared?: Window | null) => {
    const target = prepared && !prepared.closed ? prepared : window.open("", "_blank");
    if (!target || target.closed) {
      setPopupBlocked(true);
      return;
    }
    try {
      target.opener = null;
    } catch {
      /* opener may be read-only */
    }
    target.location.href = url;
    setPopupBlocked(false);
  };

  const start = async () => {
    // Opened inside the click; a window.open after the await is blocked.
    const prepared = window.open("", "_blank");
    setBusy(true);
    setError(null);
    setPopupBlocked(false);
    setCode("");
    try {
      const next = await api.startAccountLogin(account.id);
      setFlow(next);
      if (next.verification_url) openTab(next.verification_url, prepared);
      else prepared?.close();
    } catch (err) {
      prepared?.close();
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const wrap = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
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
      setError("Clipboard blocked — select and copy manually.");
    }
  };

  const active = flow && ["starting", "awaiting_user", "completing"].includes(flow.status);
  const limited = account.limited_until ? new Date(account.limited_until + "Z") : null;
  const isLimited = limited !== null && limited > new Date();
  const siblings = accounts.filter((a) => a.provider === account.provider && a.id !== account.id);
  const fallback = accounts.find((a) => a.id === account.fallback_account_id);

  return (
    <div className="card account-card">
      <div className="row">
        <span className={`dot ${account.signed_in ? "ok" : "off"}`} />
        <h2 style={{ margin: 0, flex: 1 }}>{account.name}</h2>
        <span className="pill">{account.provider}</span>
        {account.is_default && <span className="pill">default</span>}
        <span className={`pill ${account.signed_in ? "ok" : "failed"}`}>
          {account.signed_in ? "signed in" : "signed out"}
        </span>
        {isLimited && <span className="pill cancelled">limit hit</span>}
        {!account.usable_by_me && <span className="pill">no access</span>}
      </div>

      {account.description && <p className="subtitle" style={{ margin: "8px 0 0" }}>{account.description}</p>}
      {account.account_detail && (
        <div className="mono" style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 6 }}>
          {account.account_detail}
        </div>
      )}
      {error && <div className="error-banner" style={{ marginTop: 12 }}>{error}</div>}
      {flow?.status === "success" && <div className="ok-banner">Signed in successfully.</div>}
      {flow && ["failed", "expired"].includes(flow.status) && (
        <div className="error-banner" style={{ marginTop: 12 }}>{flow.message}</div>
      )}

      {account.limit_status && (
        <div className={account.limit_status === "allowed" ? "note-banner" : "warn-banner"}>
          <strong>Plan window</strong>{" "}
          {account.limit_window ? account.limit_window.replace(/_/g, "-") : "unknown"}:{" "}
          {account.limit_status}
          {account.limit_resets_at &&
            ` · resets ${new Date(account.limit_resets_at + "Z").toLocaleString()}`}
          <div style={{ color: "var(--text-dim)", fontSize: 12, marginTop: 4 }}>
            Reported by the CLI on the last run.
          </div>
        </div>
      )}

      {isLimited && (
        <div className="warn-banner">
          Held out of rotation until {limited!.toLocaleString()}.{" "}
          {fallback ? `Work moves to ${fallback.name}.` : "No fallback is set, so runs will fail."}
        </div>
      )}

      {me.is_admin && !active && (
        <div className="row" style={{ marginTop: 12 }}>
          {account.signed_in ? (
            <button
              className="danger"
              disabled={busy}
              onClick={() =>
                confirm(`Sign ${account.name} out?`) && wrap(() => api.accountLogout(account.id))
              }
            >
              Sign out
            </button>
          ) : (
            <button className="primary" onClick={start} disabled={busy}>
              {busy ? "Starting…" : "Sign in"}
            </button>
          )}
          {isLimited && (
            <button disabled={busy} onClick={() => wrap(() => api.clearAccountLimit(account.id))}>
              Clear limit
            </button>
          )}
          {!account.is_default && (
            <button
              disabled={busy}
              onClick={() => wrap(() => api.patchAccount(account.id, { is_default: true }))}
            >
              Make default
            </button>
          )}
          <button onClick={() => setShowAccess((v) => !v)}>
            {showAccess ? "Hide settings" : "Settings"}
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() =>
              confirm(
                `Remove "${account.name}"? Credentials on disk are left in place.`,
              ) && wrap(() => api.deleteAccount(account.id))
            }
          >
            Remove
          </button>
        </div>
      )}

      {active && (
        <div className="login-flow">
          <ol>
            <li>
              <div className="step-head">Approve the sign-in for {account.name}</div>
              {flow!.verification_url ? (
                <>
                  <div className="row">
                    <button className="primary big" onClick={() => openTab(flow!.verification_url!)}>
                      Open sign-in page ↗
                    </button>
                    <button onClick={() => copy(flow!.verification_url!, "url")}>
                      {copied === "url" ? "Copied" : "Copy link"}
                    </button>
                  </div>
                  {popupBlocked && (
                    <div className="error-banner" style={{ margin: "10px 0 0" }}>
                      Your browser blocked the new tab — use the link below or allow pop-ups.
                    </div>
                  )}
                  <details className="linkfallback">
                    <summary>Open it somewhere else</summary>
                    <a className="linkbox" href={flow!.verification_url} target="_blank" rel="noreferrer noopener">
                      {flow!.verification_url}
                    </a>
                  </details>
                </>
              ) : (
                <span className="subtitle">Waiting for the CLI to produce a link…</span>
              )}
            </li>
            {flow!.user_code && (
              <li>
                <div className="step-head">Enter this one-time code</div>
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
                <form
                  className="row"
                  onSubmit={async (e) => {
                    e.preventDefault();
                    setBusy(true);
                    try {
                      setFlow(await api.submitAccountLoginCode(account.id, code));
                      setCode("");
                    } catch (err) {
                      setError(err instanceof Error ? err.message : String(err));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <input
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    placeholder="Authorization code"
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
            <button
              onClick={async () => {
                await api.cancelAccountLogin(account.id).catch(() => {});
                setFlow(null);
                stop();
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {showAccess && me.is_admin && (
        <div className="settings-block">
          <label>
            <span>When this account hits its limit, move work to</span>
            <select
              value={account.fallback_account_id ?? ""}
              onChange={(e) =>
                wrap(() =>
                  api.patchAccount(account.id, {
                    fallback_account_id: e.target.value ? Number(e.target.value) : null,
                  }),
                )
              }
            >
              <option value="">Nothing — fail instead</option>
              {siblings.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>

          <div>
            <span className="field-label">
              Who can use it{account.allowed_user_ids.length === 0 ? " — currently everyone" : ""}
            </span>
            <div className="checkgrid">
              {users.map((u) => (
                <label className="check" key={u.id}>
                  <input
                    type="checkbox"
                    checked={account.allowed_user_ids.includes(u.id)}
                    onChange={(e) => {
                      const next = e.target.checked
                        ? [...account.allowed_user_ids, u.id]
                        : account.allowed_user_ids.filter((x) => x !== u.id);
                      void wrap(() => api.patchAccount(account.id, { allowed_user_ids: next }));
                    }}
                  />
                  <span>
                    {u.username}
                    {u.is_admin && " (admin)"}
                  </span>
                </label>
              ))}
            </div>
            <div className="field-hint">
              Leave every box unticked to let everyone use it. Admins always have access.
            </div>
          </div>

          <div className="mono" style={{ color: "var(--text-dim)", fontSize: 11 }}>
            {account.config_dir}
          </div>
        </div>
      )}
    </div>
  );
}
