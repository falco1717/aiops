import type * as React from "react";
import { useState } from "react";
import { api } from "../api";
import Logo from "../components/Logo";

/**
 * The one-time "create your admin account" screen.
 *
 * Shown instead of the login screen for exactly as long as this instance has
 * never had a single user — see gate.ts for where that decision is made and
 * `backend/app/routers/setup.py` for why submitting this twice, or from two
 * tabs at once, cannot create two "first" admins.
 *
 * On success the new admin is logged straight in (the server sets the same
 * session cookie `/api/auth/login` does) rather than being sent to the login
 * screen to retype the password they just typed here.
 */
export default function Setup({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && password !== confirm;
  const tooShort = password.length > 0 && password.length < 8;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.completeSetup(username, password);
      onAuthenticated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the admin account");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <div className="login-logo">
          <Logo size={44} />
        </div>
        <p className="subtitle">
          This is a fresh AIOps instance. Create the administrator account to get started.
        </p>
        <div className="setup-banner">
          Nobody has signed in here yet, so this screen has no password to check — whoever submits
          it first becomes the administrator. It will not appear again once that happens.
        </div>
        {error && <div className="error-banner">{error}</div>}
        <label>
          <span>Username</span>
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            required
          />
        </label>
        <label>
          <span>Password (at least 8 characters)</span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        <label>
          <span>Confirm password</span>
          <input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        {mismatch && <div className="field-hint err">Passwords do not match.</div>}
        {tooShort && <div className="field-hint err">Too short — 8 characters minimum.</div>}
        <button
          className="primary"
          type="submit"
          disabled={busy || !username || !password || mismatch || tooShort}
        >
          {busy ? "Creating account…" : "Create admin account"}
        </button>
      </form>
    </div>
  );
}
