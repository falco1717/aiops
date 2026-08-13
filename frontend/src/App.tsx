import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { ApiError, api } from "./api";
import Logo from "./components/Logo";
import type { User } from "./types";
import Account, { ChangePassword } from "./views/Account";
import Login from "./views/Login";
import Presets from "./views/Presets";
import Providers from "./views/Providers";
import Schedules from "./views/Schedules";
import Sessions from "./views/Sessions";
import Workspaces from "./views/Workspaces";

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setUser(await api.me());
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) setUser(null);
      else throw err;
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  if (!ready) return <div className="empty">Loading…</div>;
  if (!user) return <Login onAuthenticated={refresh} />;

  const logout = async () => {
    await api.logout();
    setUser(null);
  };

  // An admin-forced password change blocks everything else — the API enforces
  // this too, so there is nothing to gain by routing around the UI.
  if (user.must_change_password) {
    return (
      <div className="login-wrap">
        <div style={{ width: 460 }}>
          <div style={{ marginBottom: 18 }}>
            <Logo size={40} />
          </div>
          <p className="subtitle">
            Your password must be changed before you can use AIOps.
          </p>
          <ChangePassword forced onChanged={refresh} />
          <button onClick={logout} style={{ marginTop: 12 }}>
            Sign out instead
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">
          <Logo size={34} />
        </div>
        <NavLink to="/sessions" className="nav-link">
          Sessions
        </NavLink>
        <NavLink to="/schedules" className="nav-link">
          Schedules
        </NavLink>
        <NavLink to="/presets" className="nav-link">
          Agents
        </NavLink>
        <NavLink to="/workspaces" className="nav-link">
          Workspaces
        </NavLink>
        <NavLink to="/providers" className="nav-link">
          Providers
        </NavLink>
        <div className="sidebar-foot">
          <NavLink to="/account" className="nav-link">
            {user.username}
            {user.is_admin && <span className="pill ok" style={{ marginLeft: 6 }}>admin</span>}
          </NavLink>
          <button onClick={logout} style={{ marginTop: 8, width: "100%" }}>
            Sign out
          </button>
        </div>
      </nav>
      <Routes>
        <Route path="/" element={<Navigate to="/sessions" replace />} />
        <Route path="/sessions" element={<Sessions />} />
        <Route path="/sessions/:sessionId" element={<Sessions />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/presets" element={<Presets />} />
        <Route path="/workspaces" element={<Workspaces />} />
        <Route path="/providers" element={<Providers me={user} />} />
        <Route path="/account" element={<Account me={user} onChanged={refresh} />} />
        <Route path="*" element={<Navigate to="/sessions" replace />} />
      </Routes>
    </div>
  );
}
