/**
 * The information architecture (spec v4 §15, P7-18): five areas and an admin-only gear, at most
 * 20 screens.
 *
 * SCREENS is the single route manifest. App.tsx renders exactly these routes, the side nav and the
 * command palette are built from it, and tests assert the screen budget, that every concept has one
 * home (`owns`), and that the gear's platform screens are hidden from analysts and viewers. A new
 * capability arrives as a Start work job kind, an output type or a panel inside an existing screen,
 * never as a new top-level screen (P23). LEGACY_REDIRECTS keeps every older URL working.
 */

export type Area = "overview" | "data" | "work" | "outputs" | "operate" | "settings" | "access";

export const AREAS: { id: Exclude<Area, "access">; label: string; description: string }[] = [
  { id: "overview", label: "Overview", description: "What needs you, and the next step" },
  { id: "data", label: "Data", description: "Sources, the catalog and definitions: metrics, relationships and their review" },
  { id: "work", label: "Work", description: "Everything started with Start work: Ask, investigations and prepared data" },
  { id: "outputs", label: "Outputs", description: "One list, filtered by type: findings, dashboards, reports, datasets" },
  { id: "operate", label: "Operate", description: "The approval inbox, and the Autonomy Center: goals, budgets, schedules, monitors and alerts" },
  { id: "settings", label: "Settings", description: "Members and policy; for administrators, the registry, platform settings and usage" },
];

/**
 * A concept a person can edit (or, for read-only records such as audit, look up). Each has exactly
 * one home screen: the build-right study found metrics, dashboards, model configuration, audit and
 * autonomy each editable in two or three places.
 */
export const CONCEPTS = [
  "workspace", "brief", "source", "selection", "catalog", "knowledge-document", "knowledge-review", "metric",
  "relationship", "semantic-model", "definition", "question", "investigation", "recipe", "file-ingest", "dbt-build",
  "step", "branch", "notebook", "experiment", "pipeline",
  "finding", "dashboard", "report", "dataset", "chart", "model", "scoring-run", "materialization",
  "approval", "goal", "monitor", "alert", "schedule", "policy",
  "member", "autonomy", "audit", "capability", "platform-settings", "model-config", "usage",
  "request", "value-metric",
] as const;
export type Concept = (typeof CONCEPTS)[number];

/**
 * Who sees a screen: everyone, workspace owners (and platform admins), or platform admins only.
 * The server enforces the same authority; this only keeps the gear's screens out of sight.
 */
export type Audience = "everyone" | "owner" | "admin";

export type ScreenId =
  | "login"
  | "workspaces" | "overview"
  | "sources" | "catalog"
  | "work" | "ask" | "desk" | "investigation" | "investigation-console" | "value"
  | "outputs" | "finding"
  | "approvals" | "autonomy"
  | "policy" | "registry" | "platform" | "usage";

export interface Screen {
  id: ScreenId;
  area: Area;
  /** react-router pattern. */
  path: string;
  title: string;
  /** Shown in the navigation (screens with parameters other than :wsId are reached from lists). */
  nav: boolean;
  /** Needs a workspace (`:wsId`) in the URL. */
  workspace: boolean;
  audience: Audience;
  /** The concepts whose one home is this screen. */
  owns: Concept[];
  keywords?: string;
}

export const SCREEN_BUDGET = 20;

export const SCREENS: Screen[] = [
  { id: "login", area: "access", path: "/login", title: "Sign in", nav: false, workspace: false, audience: "everyone", owns: [] },

  { id: "workspaces", area: "overview", path: "/", title: "Workspaces", nav: true, workspace: false, audience: "everyone",
    owns: ["workspace"], keywords: "home landing" },
  { id: "overview", area: "overview", path: "/w/:wsId", title: "Overview", nav: true, workspace: true, audience: "everyone",
    owns: [], keywords: "home what needs me checklist onboarding start work objective" },

  { id: "value", area: "overview", path: "/w/:wsId/value", title: "Value", nav: true, workspace: true, audience: "everyone",
    owns: ["value-metric"], keywords: "desk analyst load automation auto resolution minutes saved baseline time to answer export" },

  { id: "sources", area: "data", path: "/w/:wsId/data/sources", title: "Sources", nav: true, workspace: true, audience: "everyone",
    owns: ["source", "selection"], keywords: "connect discover crawl drift database upload" },
  { id: "catalog", area: "data", path: "/w/:wsId/data/catalog", title: "Catalog & definitions", nav: true, workspace: true, audience: "everyone",
    owns: ["brief", "catalog", "knowledge-document", "knowledge-review", "metric", "relationship", "semantic-model", "definition"],
    keywords: "brief readiness facts questions tables columns glossary aliases documents review metrics kpis relationships cardinality semantic diff definitions versions import export" },

  { id: "work", area: "work", path: "/w/:wsId/work", title: "Work", nav: true, workspace: true, audience: "everyone",
    owns: ["investigation", "recipe", "file-ingest", "dbt-build", "step", "branch", "notebook", "experiment", "pipeline"],
    keywords: "investigations data thread steps branches notebooks experiments models predict forecast prepare data recipes ingest file pipelines dry run dbt builds" },
  { id: "ask", area: "work", path: "/w/:wsId/work/ask", title: "Ask", nav: true, workspace: true, audience: "everyone",
    owns: ["question"], keywords: "question sql console explain quick" },
  { id: "desk", area: "work", path: "/w/:wsId/work/desk/:requestId?", title: "Request desk", nav: true, workspace: true,
    audience: "everyone", owns: ["request"], keywords: "requests queue escalated sla assign triage answer accept reopen data pack" },
  { id: "investigation", area: "work", path: "/w/:wsId/work/investigations/:runId", title: "Investigation", nav: false, workspace: true,
    audience: "everyone", owns: [] },
  { id: "investigation-console", area: "work", path: "/w/:wsId/work/investigations/:runId/console", title: "Agent console", nav: false,
    workspace: true, audience: "everyone", owns: [] },

  { id: "outputs", area: "outputs", path: "/w/:wsId/outputs", title: "Outputs", nav: true, workspace: true, audience: "everyone",
    owns: ["dashboard", "report", "dataset", "chart", "model", "scoring-run", "materialization"],
    keywords: "findings dashboards reports datasets charts models champion scoring tables materializations rollback publish download" },
  { id: "finding", area: "outputs", path: "/w/:wsId/outputs/findings/:insightId?", title: "Findings", nav: false, workspace: true,
    audience: "everyone", owns: ["finding"], keywords: "verified evidence why this number void" },

  { id: "approvals", area: "operate", path: "/w/:wsId/operate/approvals", title: "Approval inbox", nav: true, workspace: true,
    audience: "everyone", owns: ["approval"], keywords: "inbox publish approve reject" },
  // S11-T5: one screen for what runs on its own (merges the former Schedules and Monitors & alerts screens)
  { id: "autonomy", area: "operate", path: "/w/:wsId/operate/autonomy", title: "Autonomy Center", nav: true, workspace: true,
    audience: "everyone", owns: ["goal", "monitor", "alert", "schedule"],
    keywords: "goals runs queues budgets exceptions limits schedules recurring pins upgrade available monitors alerts thresholds noise failures void results" },

  { id: "policy", area: "settings", path: "/w/:wsId/settings/policy", title: "Members & policy", nav: true, workspace: true,
    audience: "owner", owns: ["policy", "member", "autonomy", "audit"], keywords: "roles budget limits autonomy audit log" },
  { id: "registry", area: "settings", path: "/settings/registry", title: "Capability registry", nav: true, workspace: false,
    audience: "admin", owns: ["capability"], keywords: "agents tools skills prompts plugins publish mcp servers clients grants quarantine signing" },
  { id: "platform", area: "settings", path: "/settings/platform", title: "Platform settings", nav: true, workspace: false,
    audience: "admin", owns: ["platform-settings", "model-config"], keywords: "admin llm modes presets models routing features limits" },
  { id: "usage", area: "settings", path: "/settings/usage", title: "Usage & cost", nav: true, workspace: false,
    audience: "admin", owns: ["usage"], keywords: "token savings spend" },
];

export function screen(id: ScreenId): Screen {
  const s = SCREENS.find((x) => x.id === id);
  if (!s) throw new Error(`unknown screen ${id}`);
  return s;
}

const ROLE_RANK: Record<string, number> = { viewer: 0, approver: 1, analyst: 2, editor: 3, owner: 4 };

/** The server's role order (security/auth.py ROLE_RANK). */
export function roleAtLeast(role: string | null | undefined, minimum: keyof typeof ROLE_RANK): boolean {
  return role != null && (ROLE_RANK[role] ?? -1) >= ROLE_RANK[minimum];
}

/** Why an action is not offered: null when `role` may do it (the server still decides). */
export function needsRole(role: string | null | undefined, minimum: keyof typeof ROLE_RANK, action: string): string | null {
  if (roleAtLeast(role, minimum)) return null;
  return `${action} needs the ${minimum} role in this workspace${role ? ` (yours: ${role})` : ""}.`;
}

/** Whether a person sees a screen: platform admins see everything, workspace owners the owner screens. */
export function canSee(s: Screen, who: { isAdmin: boolean; role?: string | null }): boolean {
  if (s.audience === "everyone") return true;
  if (who.isAdmin) return true;
  return s.audience === "owner" && who.role === "owner";
}

/**
 * Fill a route pattern. Optional segments without a value are dropped; a missing required value
 * (a record without its run id, say) also drops the segment, so the link lands on the parent list
 * rather than throwing during render.
 */
export function fillPath(pattern: string, params: Record<string, string | null | undefined> = {}): string {
  const segs: string[] = [];
  for (const seg of pattern.split("/")) {
    if (!seg.startsWith(":")) {
      segs.push(seg);
      continue;
    }
    const v = params[seg.slice(1).replace(/\?$/, "")];
    if (v === undefined || v === null || v === "") break;
    segs.push(encodeURIComponent(v));
  }
  return segs.join("/") || "/";
}

function withQuery(path: string, query: Record<string, string | undefined | null> = {}): string {
  const parts = Object.entries(query).filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `${path}?${parts.join("&")}` : path;
}

/** Tabs of the Data → Catalog & definitions screen. */
export type DataTab = "catalog" | "contexts" | "brief" | "documents" | "review" | "metrics" | "definitions" | "transfer" | "grounding";
/** Tabs of the Work screen. */
export type WorkTab = "investigations" | "thread" | "notebooks" | "experiments" | "prepare" | "builds" | "workflows" | "process";
/** Output types: one list, filtered (spec v4 §15). Metrics are not an output: they live in Data. */
export type OutputType = "finding" | "dashboard" | "report" | "dataset" | "chart" | "prepared" | "model" | "table" | "process" | "other";

const P = (id: ScreenId, params: Record<string, string | undefined> = {}) => fillPath(screen(id).path, params);

/** Typed links into the IA. Every in-app link goes through here, so a route move is one edit. */
export const to = {
  workspaces: () => "/",
  workspace: (ws: string) => P("overview", { wsId: ws }),
  ask: (ws: string) => P("ask", { wsId: ws }),
  /** Work → Request desk; with a request id its detail is open. */
  desk: (ws: string, request?: string, q: { view?: "mine" | "queue" | "all" } = {}) =>
    withQuery(P("desk", { wsId: ws, requestId: request }), { view: q.view }),
  /** Overview → Value: the desk's FR-053 metrics. */
  value: (ws: string) => P("value", { wsId: ws }),
  work: (ws: string, tab?: WorkTab, q: Record<string, string | undefined> = {}) =>
    withQuery(P("work", { wsId: ws }), { tab: tab === "investigations" ? undefined : tab, ...q }),
  investigations: (ws: string) => P("work", { wsId: ws }),
  run: (ws: string, run: string) => P("investigation", { wsId: ws, runId: run }),
  runConsole: (ws: string, run: string) => P("investigation-console", { wsId: ws, runId: run }),
  findings: (ws: string, insight?: string) => P("finding", { wsId: ws, insightId: insight }),
  sources: (ws: string) => P("sources", { wsId: ws }),
  catalog: (ws: string) => P("catalog", { wsId: ws }),
  /** Data → Catalog & definitions; `doc` is a context receipt's document id, opened in Documents. */
  data: (ws: string, tab?: DataTab, q: { pack?: string; path?: string; doc?: string; kpi?: string; definition?: string } = {}) =>
    withQuery(P("catalog", { wsId: ws }), { tab: tab === "catalog" ? undefined : tab, ...q }),
  /** Older name for `data` (Knowledge studio tabs); `graph` is now inside Definitions. */
  knowledge: (ws: string, tab?: "catalog" | "documents" | "review" | "graph" | "metrics" | "transfer",
    q: { pack?: string; path?: string; doc?: string } = {}) => to.data(ws, tab === "graph" ? "definitions" : tab, q),
  /** Work → Data Thread of an investigation, an Ask thread or a notebook (`container` = "run:<id>", "ask_thread:<id>"). */
  thread: (ws: string, type: "run" | "ask_thread", id: string, q: { branch?: string; step?: string } = {}) =>
    to.work(ws, "thread", { container: `${type}:${id}`, ...q }),
  outputs: (ws: string, q: { type?: OutputType; artifact?: string; dashboard?: string; model?: string; table?: string; analysis?: string } = {}) =>
    withQuery(P("outputs", { wsId: ws }), q),
  studio: (ws: string, artifact?: string) => to.outputs(ws, { artifact }),
  /** The former Build studio tabs, each at its one home: dbt builds in Work, KPIs in Data, dashboards in Outputs. */
  build: (ws: string, tab: "builds" | "kpis" | "dashboards", q: { job?: string; kpi?: string; dashboard?: string } = {}) =>
    tab === "builds" ? to.work(ws, "builds", { job: q.job })
      : tab === "kpis" ? to.data(ws, "metrics", { kpi: q.kpi })
        : to.outputs(ws, { type: "dashboard", dashboard: q.dashboard }),
  reports: (ws: string, artifact?: string) => to.outputs(ws, { type: "report", artifact }),
  approvals: (ws: string) => P("approvals", { wsId: ws }),
  /** The Autonomy Center; `view` picks its section (overview, goals, schedules, monitors). */
  autonomy: (ws: string, view?: "overview" | "goals" | "schedules" | "monitors", q: Record<string, string | undefined> = {}) =>
    withQuery(P("autonomy", { wsId: ws }), { view: view === "overview" ? undefined : view, ...q }),
  monitoring: (ws: string, q: { tab?: string; alert?: string } = {}) => to.autonomy(ws, "monitors", { tab: q.tab, alert: q.alert }),
  schedules: (ws: string, schedule?: string) => to.autonomy(ws, "schedules", { schedule }),
  governance: (ws: string) => P("policy", { wsId: ws }),
  registry: () => screen("registry").path,
  settings: () => screen("platform").path,
  usage: () => screen("usage").path,
};

export interface LegacyRedirect {
  from: string;
  to: string;
  /** An old `?tab=` value whose content now lives elsewhere → that target (its own query kept, `tab` dropped). */
  tabs?: Record<string, string>;
}

/** Older URLs → their new home. Query strings are carried over by the redirect. */
export const LEGACY_REDIRECTS: LegacyRedirect[] = [
  // pre-increment-4
  { from: "/admin", to: "/settings/registry" },
  { from: "/w/:wsId/sources", to: "/w/:wsId/data/sources" },
  { from: "/w/:wsId/catalog", to: "/w/:wsId/data/catalog" },
  { from: "/w/:wsId/runs", to: "/w/:wsId/work" },
  { from: "/w/:wsId/runs/:runId", to: "/w/:wsId/work/investigations/:runId" },
  { from: "/w/:wsId/runs/:runId/console", to: "/w/:wsId/work/investigations/:runId/console" },
  { from: "/w/:wsId/insights", to: "/w/:wsId/outputs/findings" },
  { from: "/w/:wsId/insights/:insightId", to: "/w/:wsId/outputs/findings/:insightId" },
  { from: "/w/:wsId/studio", to: "/w/:wsId/outputs" },
  { from: "/w/:wsId/reports", to: "/w/:wsId/outputs?type=report" },
  { from: "/w/:wsId/schedules", to: "/w/:wsId/operate/autonomy?view=schedules" },
  { from: "/w/:wsId/monitoring", to: "/w/:wsId/operate/autonomy?view=monitors" },
  { from: "/w/:wsId/governance", to: "/w/:wsId/settings/policy" },
  { from: "/w/:wsId/operate", to: "/w/:wsId/operate/approvals" },
  // increment-4 six-journey IA (spec v3 §9)
  { from: "/operate", to: "/settings/registry" },
  { from: "/operate/registry", to: "/settings/registry" },
  { from: "/operate/settings", to: "/settings/platform" },
  { from: "/operate/usage", to: "/settings/usage" },
  { from: "/w/:wsId/ask", to: "/w/:wsId/work/ask" },
  { from: "/w/:wsId/investigate", to: "/w/:wsId/work" },
  { from: "/w/:wsId/investigate/:runId", to: "/w/:wsId/work/investigations/:runId" },
  { from: "/w/:wsId/investigate/:runId/console", to: "/w/:wsId/work/investigations/:runId/console" },
  { from: "/w/:wsId/investigate/findings", to: "/w/:wsId/outputs/findings" },
  { from: "/w/:wsId/investigate/findings/:insightId", to: "/w/:wsId/outputs/findings/:insightId" },
  { from: "/w/:wsId/knowledge", to: "/w/:wsId/data/catalog" },
  { from: "/w/:wsId/knowledge/sources", to: "/w/:wsId/data/sources" },
  { from: "/w/:wsId/knowledge/catalog", to: "/w/:wsId/data/catalog", tabs: { graph: "/w/:wsId/data/catalog?tab=definitions" } },
  { from: "/w/:wsId/build", to: "/w/:wsId/outputs" },
  { from: "/w/:wsId/build/studio", to: "/w/:wsId/outputs", tabs: {
    artifacts: "/w/:wsId/outputs",
    builds: "/w/:wsId/work?tab=builds",
    kpis: "/w/:wsId/data/catalog?tab=metrics",
    dashboards: "/w/:wsId/outputs?type=dashboard",
  } },
  { from: "/w/:wsId/build/reports", to: "/w/:wsId/outputs?type=report" },
  { from: "/w/:wsId/operate/governance", to: "/w/:wsId/settings/policy" },
  // S11-T5: Schedules and Monitors & alerts are sections of the Autonomy Center
  { from: "/w/:wsId/operate/schedules", to: "/w/:wsId/operate/autonomy?view=schedules" },
  { from: "/w/:wsId/operate/monitoring", to: "/w/:wsId/operate/autonomy?view=monitors" },
];

/**
 * Where an old URL lands: the target pattern filled with the path parameters, the old `tab` mapped
 * when its content moved, and the rest of the old query kept (the target's own query first).
 */
export function legacyTarget(r: LegacyRedirect, params: Record<string, string | undefined>, search: string): string {
  const old = new URLSearchParams(search);
  let target = r.to;
  const tab = old.get("tab");
  if (tab && r.tabs?.[tab]) {
    target = r.tabs[tab];
    old.delete("tab");
  }
  const [pattern, ownQuery = ""] = target.split("?");
  const merged = new URLSearchParams(ownQuery);
  old.forEach((v, k) => { if (!merged.has(k)) merged.append(k, v); });
  const q = merged.toString();
  return `${fillPath(pattern, params)}${q ? `?${q}` : ""}`;
}
