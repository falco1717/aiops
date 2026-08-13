import type * as React from "react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { User } from "../types";

export default function Account({ me, onChanged }: { me: User; onChanged: () => void }) {
  return (
    <div className="main">
      <h1>Account</h1>
      <p className="subtitle">
        Signed in as <strong>{me.username}</strong>
        {me.is_admin ? " (administrator)" : ""}.
      </p>
      <ChangePassword onChanged={onChanged} />
      {me.is_admin && <UserAdmin me={me} />}
    </div>
  );
}

export function ChangePassword({
  onChanged,
  forced = false,
}: {
  onChanged: () => void;
  forced?: boolean;
}) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < 8;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setDone(false);
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setDone(true);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="card" onSubmit={submit} style={{ maxWidth: 460 }}>
      <h2 style={{ marginTop: 0 }}>{forced ? "Set a new password" : "Change password"}</h2>
      {error && <div className="error-banner">{error}</div>}
      {done && <div className="ok-banner">Password updated.</div>}
      <label>
        <span>Current password</span>
        <input
          type="password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          autoComplete="current-password"
          required
        />
      </label>
      <label>
        <span>New password (at least 8 characters)</span>
        <input
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          autoComplete="new-password"
          required
        />
      </label>
      <label>
        <span>Confirm new password</span>
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
        disabled={busy || !current || !next || mismatch || tooShort}
      >
        {busy ? "Saving…" : "Update password"}
      </button>
    </form>
  );
}

function UserAdmin({ me }: { me: User }) {
  const [users, setUsers] = useState<User[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [mustChange, setMustChange] = useState(true);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setUsers(await api.users());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const wrap = async (fn: () => Promise<unknown>, ok?: string) => {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await fn();
      if (ok) setNotice(ok);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const create = (event: React.FormEvent) => {
    event.preventDefault();
    void wrap(async () => {
      await api.createUser({
        username,
        password,
        is_admin: isAdmin,
        must_change_password: mustChange,
      });
      setUsername("");
      setPassword("");
      setIsAdmin(false);
      setMustChange(true);
    }, "User created.");
  };

  const resetPassword = (user: User) => {
    const pw = prompt(`New password for "${user.username}" (min 8 characters):`);
    if (!pw) return;
    if (pw.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    void wrap(
      () => api.resetUserPassword(user.id, pw, true),
      `Password reset for ${user.username}. They must change it at next sign-in.`,
    );
  };

  const toggleAdmin = (user: User) =>
    void wrap(
      () => api.patchUser(user.id, { is_admin: !user.is_admin }),
      `${user.username} is now ${user.is_admin ? "a standard user" : "an administrator"}.`,
    );

  const remove = (user: User) => {
    if (!confirm(`Delete user "${user.username}"? This cannot be undone.`)) return;
    void wrap(() => api.deleteUser(user.id), `Deleted ${user.username}.`);
  };

  return (
    <>
      <h2>Users</h2>
      <p className="subtitle">
        Everyone who can sign in here can make an agent run shell commands on this server. Add
        accounts sparingly, and keep administrator rights to the people who manage the box.
      </p>
      {error && <div className="error-banner">{error}</div>}
      {notice && <div className="ok-banner">{notice}</div>}

      <form className="card" onSubmit={create}>
        <div className="grid-2">
          <label>
            <span>Username</span>
            <input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="off"
              required
            />
          </label>
          <label>
            <span>Initial password (at least 8 characters)</span>
            <input
              type="text"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="off"
              minLength={8}
              required
            />
          </label>
        </div>
        <div className="row" style={{ marginBottom: 12 }}>
          <label className="check">
            <input
              type="checkbox"
              checked={isAdmin}
              onChange={(e) => setIsAdmin(e.target.checked)}
            />
            <span>Administrator</span>
          </label>
          <label className="check">
            <input
              type="checkbox"
              checked={mustChange}
              onChange={(e) => setMustChange(e.target.checked)}
            />
            <span>Require a password change at first sign-in</span>
          </label>
        </div>
        <button className="primary" type="submit" disabled={busy || password.length < 8}>
          Create user
        </button>
      </form>

      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Role</th>
            <th>Last sign-in</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <tr key={u.id}>
              <td data-label="User">
                {u.username}
                {u.id === me.id && <span className="pill" style={{ marginLeft: 8 }}>you</span>}
                {u.must_change_password && (
                  <span className="pill cancelled" style={{ marginLeft: 8 }}>
                    must change password
                  </span>
                )}
              </td>
              <td data-label="Role">
                <span className={`pill ${u.is_admin ? "ok" : ""}`}>
                  {u.is_admin ? "administrator" : "user"}
                </span>
              </td>
              <td className="mono" data-label="Last sign-in">
                {u.last_login_at ? fmt(u.last_login_at) : "never"}
              </td>
              <td className="row actions">
                <button onClick={() => resetPassword(u)} disabled={busy}>
                  Reset password
                </button>
                <button onClick={() => toggleAdmin(u)} disabled={busy || u.id === me.id}>
                  {u.is_admin ? "Demote" : "Make admin"}
                </button>
                <button
                  className="danger"
                  onClick={() => remove(u)}
                  disabled={busy || u.id === me.id}
                >
                  Delete
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function fmt(iso: string): string {
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`).toLocaleString();
}
