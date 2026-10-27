import { Suspense, type ReactElement, type ReactNode } from "react";
import { lazyWithReload } from "./lib/lazyReload";
import { BrowserRouter, Navigate, Route, Routes, useLocation, useParams } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { EmptyState, Loading, StateView } from "./components/ui";
import { AdminPage } from "./pages/Admin";
import { ApprovalsPage } from "./pages/Approvals";
import { AskPage } from "./pages/Ask";
import { ConsolePage } from "./pages/Console";
import { DeskPage } from "./pages/Desk";
import { GovernancePage } from "./pages/Governance";
import { LoginPage } from "./pages/Login";
import { MonitoringPage } from "./pages/Monitoring";
import { WorkPage } from "./pages/Runs";
import { RunViewPage } from "./pages/RunView";
import { SchedulesPage } from "./pages/Schedules";
import { SourcesPage } from "./pages/Sources";
import { ValuePage } from "./pages/Value";
import { OutputsPage } from "./pages/Studio";
import { WorkspaceHomePage } from "./pages/WorkspaceHome";
import { WorkspacesPage } from "./pages/Workspaces";
import { legacyTarget, LEGACY_REDIRECTS, SCREENS, type LegacyRedirect as Legacy, type Screen, type ScreenId } from "./routes";

// The knowledge studio (catalog, documents, review queue, graph, import/export) loads on first visit.
const CatalogPage = lazyWithReload(() => import("./pages/Catalog").then((m) => ({ default: m.CatalogPage })));

function RequireAuth({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  return <>{children}</>;
}

/** One element per screen in the manifest: a screen without an element is a type error. */
const ELEMENTS: Record<ScreenId, ReactElement> = {
  login: <LoginPage />,
  workspaces: <WorkspacesPage />,
  overview: <WorkspaceHomePage />,
  ask: <AskPage />,
  desk: <DeskPage />,
  value: <ValuePage />,
  work: <WorkPage />,
  investigation: <RunViewPage />,
  "investigation-console": <ConsolePage />,
  outputs: <OutputsPage />,
  finding: <OutputsPage />,
  sources: <SourcesPage />,
  catalog: <Suspense fallback={<div className="page"><Loading /></div>}><CatalogPage /></Suspense>,
  approvals: <ApprovalsPage />,
  monitoring: <MonitoringPage />,
  schedules: <SchedulesPage />,
  policy: <GovernancePage />,
  registry: <AdminPage key="registry" section="registry" />,
  platform: <AdminPage key="settings" section="settings" />,
  usage: <AdminPage key="usage" section="usage" />,
};

/**
 * Platform-admin screens render a not-entitled state for everyone else (the nav already hides
 * them; the API refuses their writes either way). Owner screens decide per workspace themselves.
 */
function Entitled({ screen, children }: { screen: Screen; children: ReactElement }) {
  const { user } = useAuth();
  if (screen.audience === "admin" && !user?.is_admin) {
    return (
      <div className="page">
        <StateView kind="not-entitled" title={`${screen.title} is for platform administrators`}>
          Ask an administrator if you need a change here.
        </StateView>
      </div>
    );
  }
  return children;
}

/** Old URL → new screen, keeping path parameters and the query string (?tab=, ?artifact=, …). */
function LegacyRedirect({ redirect }: { redirect: Legacy }) {
  const params = useParams();
  const { search, hash } = useLocation();
  return <Navigate replace to={`${legacyTarget(redirect, params as Record<string, string>, search)}${hash}`} />;
}

export function AppRoutes() {
  const screens = SCREENS.filter((s) => s.id !== "login");
  return (
    <Routes>
      <Route path="/login" element={ELEMENTS.login} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        {screens.map((s) => <Route key={s.id} path={s.path} element={<Entitled screen={s}>{ELEMENTS[s.id]}</Entitled>} />)}
        {LEGACY_REDIRECTS.map((r) => <Route key={r.from} path={r.from} element={<LegacyRedirect redirect={r} />} />)}
        <Route path="*" element={<div className="page"><EmptyState title="Page not found" /></div>} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </AuthProvider>
  );
}
