import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useMatch, useNavigate } from "react-router-dom";
import { api, type Workspace } from "../api";
import { useAuth } from "../auth";
import { nextThemePref, setThemePref, useTheme } from "../lib/theme";
import { AREAS, canSee, fillPath, SCREENS } from "../routes";
import { CommandPalette } from "./CommandPalette";
import { ErrorBoundary } from "./ErrorBoundary";
import { GuideButton } from "./Guide";
import { NotificationBell } from "./NotificationBell";
import { StatusBanner } from "./StatusBanner";
import { TourProvider } from "./Tour";
import { LiteWorkspace } from "./LiteWorkspace";
import { to } from "../routes";

const THEME_LABEL = { system: "System theme", light: "Light theme", dark: "Dark theme" } as const;
const THEME_ICON = { system: "◐", light: "☀", dark: "☾" } as const;

export function ThemeToggle() {
  const { pref } = useTheme();
  const next = nextThemePref(pref);
  return (
    <button type="button" className="btn btn-sm btn-ghost" onClick={() => setThemePref(next)}
      title={`${THEME_LABEL[pref]} — switch to ${THEME_LABEL[next].toLowerCase()}`}>
      <span aria-hidden="true">{THEME_ICON[pref]}</span>
      <span className="sr-only">{THEME_LABEL[pref]}; switch to {THEME_LABEL[next].toLowerCase()}</span>
    </button>
  );
}

function useWorkspace(wsId: string | undefined): Workspace | null {
  const [ws, setWs] = useState<Workspace | null>(null);
  useEffect(() => {
    if (!wsId) return setWs(null);
    let alive = true;
    api.getWorkspace(wsId).then((w) => alive && setWs(w)).catch(() => alive && setWs(null));
    return () => {
      alive = false;
    };
  }, [wsId]);
  return ws;
}

/**
 * The navigation screens a person sees: the five areas, then the gear (Settings) with only the
 * screens their role allows. Built from the route manifest; the palette uses the same filter.
 */
export function visibleNav(who: { isAdmin: boolean; role?: string | null }, wsId?: string) {
  return AREAS.map((a) => ({
    area: a,
    screens: SCREENS.filter((s) => s.area === a.id && s.nav && (!s.workspace || wsId) && !(wsId && s.id === "workspaces") && canSee(s, who)),
  })).filter((g) => g.screens.length > 0);
}

/** Side nav grouped by area (spec v4 §15), built from the route manifest. */
function AreaNav({ wsId, wsName, role }: { wsId?: string; wsName?: string; role?: string | null }) {
  const { user } = useAuth();
  const groups = visibleNav({ isAdmin: !!user?.is_admin, role }, wsId);
  return (
    <>
      {wsId && (
        <div className="nav-section">
          <Link to="/" className="nav-back">← All workspaces</Link>
          <div className="nav-ws" title={wsName ?? wsId}>{wsName ?? "Workspace"}</div>
        </div>
      )}
      {groups.map(({ area, screens }) => (
        <div key={area.id} className={`nav-journey nav-area-${area.id}`} role="group" aria-labelledby={`nav-a-${area.id}`}>
          <div id={`nav-a-${area.id}`} className="nav-journey-label" title={area.description}>
            {area.id === "settings" && <span aria-hidden="true">⚙ </span>}{area.label}</div>
          <ul className="nav-list">
            {screens.map((s) => (
              <li key={s.id}>
                <NavLink to={fillPath(s.path, { wsId })} end={s.id === "overview" || s.id === "workspaces" || s.id === "work"}
                  data-tour={`nav-${s.id}`} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
                  {s.title}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

export function Layout() {
  const { user, logout } = useAuth();
  const wsId = useMatch("/w/:wsId/*")?.params.wsId;
  const ws = useWorkspace(wsId);
  const location = useLocation();
  const navigate = useNavigate();
  const [lite, setLite] = useState(() => {
    try { return sessionStorage.getItem("analystos.ui") === "lite"; } catch { return false; }
  });
  const liteEntry = !!wsId && location.pathname === to.lite(wsId);
  useEffect(() => {
    if (liteEntry) {
      setLite(true);
      try { sessionStorage.setItem("analystos.ui", "lite"); } catch { /* Storage may be disabled. */ }
    }
  }, [liteEntry]);
  const [navOpen, setNavOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  useEffect(() => setNavOpen(false), [location.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (wsId && (lite || liteEntry)) return (
    <TourProvider>
      <LiteWorkspace wsId={wsId} name={ws?.name} onExit={() => {
        setLite(false);
        try { sessionStorage.removeItem("analystos.ui"); } catch { /* Storage may be disabled. */ }
        if (liteEntry) navigate(to.ask(wsId), { replace: true });
      }}>
        <StatusBanner />
        <ErrorBoundary resetKey={location.pathname}><Outlet /></ErrorBoundary>
      </LiteWorkspace>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} wsId={wsId} role={ws?.role} />
    </TourProvider>
  );

  return (
    <TourProvider>
    <div className="shell">
      <a href="#main" className="skip-link">Skip to content</a>
      <header className="topbar">
        <button type="button" className="btn btn-ghost nav-toggle" aria-expanded={navOpen} aria-controls="sidenav"
          onClick={() => setNavOpen((o) => !o)}>
          ☰<span className="sr-only">Toggle navigation</span>
        </button>
        <Link to="/" className="brand">
          <span className="brand-mark" aria-hidden="true">◆</span>
          <span>Context2AI <strong>AnalystOS</strong></span>
        </Link>
        <div className="topbar-spacer" />
        {wsId && <Link className="btn btn-sm topbar-lite" to={to.lite(wsId)}>Lite<span className="topbar-lite-rest"> workspace</span></Link>}
        <button type="button" className="btn btn-sm palette-trigger" onClick={() => setPaletteOpen(true)} aria-haspopup="dialog"
          aria-keyshortcuts="Control+K Meta+K">
          <span>Go to…</span> <kbd className="kbd">Ctrl K</kbd>
        </button>
        <GuideButton wsId={wsId} />
        <ThemeToggle />
        {user && (
          <div className="topbar-user">
            <NotificationBell />
            <span className="user-name" title={user.email}>{user.name || user.email}</span>
            {user.is_admin && <span className="tag tag-info">admin</span>}
            <button type="button" className="btn btn-sm" onClick={logout}>Log out</button>
          </div>
        )}
      </header>
      <StatusBanner />
      <div className="body">
        <nav id="sidenav" className={`sidenav ${navOpen ? "open" : ""}`} aria-label="Main">
          <AreaNav wsId={wsId} wsName={ws?.name} role={ws?.role} />
        </nav>
        <main id="main" className="main" tabIndex={-1}>
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} wsId={wsId} role={ws?.role} />
    </div>
    </TourProvider>
  );
}
