import { useCallback, useEffect, useMemo, useState } from "react";
import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ApiError, api } from "./api";
import { displayName } from "./names";
import Logo, { Mark } from "./components/Logo";
import Working from "./components/Working";
import { workView } from "./work";
import type { ActiveRun, User } from "./types";
import Account, { ChangePassword } from "./views/Account";
import Accounts from "./views/Accounts";
import GithubAccounts from "./views/GithubAccounts";
import Login from "./views/Login";
import Nodes from "./views/Nodes";
import Usage from "./views/Usage";
import Presets from "./views/Presets";
import Providers from "./views/Providers";
import Schedules from "./views/Schedules";
import Sessions from "./views/Sessions";
import Targets from "./views/Targets";
import Teams from "./views/Teams";
import Workspaces from "./views/Workspaces";

const NAV = [
  { to: "/sessions", label: "Sessions" },
  { to: "/schedules", label: "Schedules" },
  { to: "/presets", label: "Agents" },
  { to: "/accounts", label: "Accounts" },
  { to: "/workspaces", label: "Workspaces" },
  { to: "/targets", label: "Systems" },
  { to: "/github-accounts", label: "GitHub" },
  { to: "/nodes", label: "Relay nodes" },
  { to: "/teams", label: "Teams" },
  { to: "/usage", label: "Usage" },
  { to: "/providers", label: "Providers" },
];

/**
 * Every turn in flight the signed-in user is allowed to see, kept fresh.
 *
 * Polled rather than pushed. The websocket in the chat view is per session and
 * only exists while that session is open, which is exactly the case this is
 * for — knowing something is running while you are somewhere else — so a second
 * socket would have to be opened for the whole app to carry it. Five seconds
 * is well inside the resolution of "is anything running", and the payload is
 * bounded: only unfinished turns, only the tail of each one's steps.
 */
function useActiveWork(enabled: boolean) {
  const [runs, setRuns] = useState<ActiveRun[]>([]);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled) {
      setRuns([]);
      return;
    }
    let live = true;
    const load = async () => {
      try {
        const next = await api.activeRuns();
        if (live) setRuns(next);
      } catch {
        // Deliberately not cleared. One failed poll — a redeploy under an open
        // tab, a dropped connection — is not evidence that the agent stopped,
        // and blinking to "nothing running" says it did. The next poll that
        // succeeds is the correction.
      }
    };
    void load();
    const timer = window.setInterval(load, 5000);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [enabled]);

  // The clock only ticks while there is something to time, so an idle app
  // re-renders nothing once a second.
  const busy = runs.length > 0;
  useEffect(() => {
    if (!busy) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, [busy]);

  return { runs, now };
}

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

  // Only once there is somebody to scope it to, and not while a forced password
  // change is the only thing they are allowed to do.
  const { runs, now } = useActiveWork(Boolean(user) && !user?.must_change_password);
  const work = useMemo(() => workView(runs, now, user?.id ?? -1), [runs, now, user?.id]);

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
        {/* Two copies, one visible. The indicator has to be reachable without
            opening anything, and the two layouts have no shared always-visible
            chrome: on a phone the sidebar is a drawer, and on desktop this bar
            does not exist. Placing one in each is honest about that; the
            stylesheet shows whichever belongs to the current layout, so only
            one is ever in the page's accessibility tree. */}
        <Working view={work} place="topbar" />
      </header>

      <div className="nav-scrim" onClick={() => setNavOpen(false)} aria-hidden="true" />

      <nav className="sidebar">
        <div className="brand">
          <Logo size={34} />
        </div>
        <Working view={work} place="sidebar" />
        {NAV.map((item) => (
          <NavLink key={item.to} to={item.to} className="nav-link">
            {item.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <NavLink to="/account" className="nav-link" title={user.username}>
            {displayName(user)}
            {user.is_admin && <span className="pill ok" style={{ marginLeft: 6 }}>admin</span>}
          </NavLink>
          <button onClick={logout} style={{ marginTop: 8, width: "100%" }}>
            Sign out
          </button>
        </div>
      </nav>

      <Routes>
        <Route path="/" element={<Navigate to="/sessions" replace />} />
        <Route path="/sessions" element={<Sessions me={user} />} />
        <Route path="/sessions/:sessionId" element={<Sessions me={user} />} />
        <Route path="/schedules" element={<Schedules />} />
        <Route path="/presets" element={<Presets />} />
        <Route path="/accounts" element={<Accounts me={user} />} />
        <Route path="/usage" element={<Usage />} />
        <Route path="/workspaces" element={<Workspaces me={user} />} />
        <Route path="/targets" element={<Targets me={user} />} />
        <Route path="/github-accounts" element={<GithubAccounts me={user} />} />
        <Route path="/nodes" element={<Nodes me={user} />} />
        <Route path="/teams" element={<Teams me={user} />} />
        <Route path="/providers" element={<Providers />} />
        <Route path="/account" element={<Account me={user} onChanged={refresh} />} />
        <Route path="*" element={<Navigate to="/sessions" replace />} />
      </Routes>
    </div>
  );
}
