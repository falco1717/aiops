import { useCallback, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, api } from "./api";
import Logo, { Mark } from "./components/Logo";
import type { User } from "./types";
import Account, { ChangePassword } from "./views/Account";
import Accounts from "./views/Accounts";
import Login from "./views/Login";
import Usage from "./views/Usage";
import Presets from "./views/Presets";
import Providers from "./views/Providers";
import Schedules from "./views/Schedules";
import Sessions from "./views/Sessions";
import Targets from "./views/Targets";
import Workspaces from "./views/Workspaces";

const NAV = [
  { to: "/sessions", label: "Sessions" },
  { to: "/schedules", label: "Schedules" },
  { to: "/presets", label: "Agents" },
  { to: "/accounts", label: "Accounts" },
  { to: "/workspaces", label: "Workspaces" },
  { to: "/targets", label: "Systems" },
  { to: "/usage", label: "Usage" },
  { to: "/providers", label: "Providers" },
];

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();

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

  // The drawer must not survive a navigation, or you land on the new page with
  // the menu still covering it.
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

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
        <div className="forced-change">
          <div style={{ marginBottom: 18 }}>
            <Logo size={40} />
          </div>
          <p className="subtitle">Your password must be changed before you can use AIOps.</p>
          <ChangePassword forced onChanged={refresh} />
          <button onClick={logout} style={{ marginTop: 12 }}>
            Sign out instead
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className={`app${navOpen ? " nav-open" : ""}`}>
      <header className="topbar">
        <button
          className="icon-btn"
          onClick={() => setNavOpen((v) => !v)}
          aria-label={navOpen ? "Close menu" : "Open menu"}
          aria-expanded={navOpen}
        >
          {navOpen ? "✕" : "☰"}
        </button>
        <Mark size={26} />
        <span className="topbar-title">AIOps</span>
      </header>

      <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />

      <nav className="sidebar">
        <div className="brand">
          <Logo size={34} />
        </div>
        {NAV.map((item) => (
          <NavLink key={item.to} to={item.to} className="nav-link">
            {item.label}
          </NavLink>
        ))}
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
        <Route path="/accounts" element={<Accounts me={user} />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/workspaces" element={<Workspaces />} />
        <Route path="/targets" element={<Targets me={user} />} />
        <Route path="/providers" element={<Providers />} />
        <Route path="/account" element={<Account me={user} onChanged={refresh} />} />
        <Route path="*" element={<Navigate to="/sessions" replace />} />
      </Routes>
    </div>
  );
}
