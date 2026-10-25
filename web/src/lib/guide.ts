/**
 * The in-product guide: what AnalystOS can do (by the job a person has), guided tours over the real
 * screens, and the words the product uses. It is content only; `components/Guide.tsx` renders it and
 * `components/Tour.tsx` plays a tour. Every link goes through `routes.to`, so a moved screen breaks a
 * typecheck here rather than a tour at runtime, and a tour step's target is a `data-tour` anchor on
 * the screen it names.
 */
import { to } from "../routes";

export type Discipline = "data" | "analysis" | "engineering" | "ml" | "delivery" | "admin";

export const DISCIPLINES: { id: Discipline; label: string; blurb: string }[] = [
  { id: "data", label: "Understand your data", blurb: "Connect, crawl, profile and describe your data once, so everything else starts from facts." },
  { id: "analysis", label: "Answer questions", blurb: "Ask in plain words or run a full investigation. Every number is checked and traceable." },
  { id: "engineering", label: "Prepare and move data", blurb: "Clean, join and load data with recipes and pipelines that dry-run before they write." },
  { id: "ml", label: "Predict and forecast", blurb: "Train classical models against a baseline, with a sealed holdout and a model card." },
  { id: "delivery", label: "Share and keep watch", blurb: "Publish findings, dashboards and reports; monitor metrics; schedule re-runs." },
  { id: "admin", label: "Govern the platform", blurb: "Members, policy, AI model settings, token savings and the capability registry." },
];

export interface GuideCapability {
  id: string;
  discipline: Discipline;
  title: string;
  /** One sentence: what a person gets, not how it is built. */
  what: string;
  /** Who usually does it. */
  who: string;
  /** Where it lives; `null` when the screen needs no workspace. */
  href: (ws: string) => string;
  needsWorkspace: boolean;
  tour?: TourId;
  /** Only shown to platform administrators. */
  adminOnly?: boolean;
}

export const CAPABILITIES: GuideCapability[] = [
  // Understand your data
  { id: "sources", discipline: "data", title: "Connect a database or upload files",
    what: "Postgres, MySQL, SQL Server, Snowflake, BigQuery, Databricks and more, ServiceNow, or CSV/Excel/Parquet files. Credentials stay in the environment.",
    who: "Data owner", href: (ws) => to.sources(ws), needsWorkspace: true, tour: "data" },
  { id: "crawl", discipline: "data", title: "Crawl and profile every table",
    what: "Reads structure, row counts and column profiles (nulls, distinct values, ranges, top values), detects keys, sensitive columns and drift.",
    who: "Data owner", href: (ws) => to.catalog(ws), needsWorkspace: true, tour: "data" },
  { id: "describe", discipline: "data", title: "Plain-language descriptions",
    what: "Each table and column gets a business name and description from its structure and profile; you review and correct them in one place.",
    who: "Analyst, data owner", href: (ws) => to.data(ws, "review"), needsWorkspace: true, tour: "data" },
  { id: "model", discipline: "data", title: "Relationships and data model",
    what: "Joins are measured on the data (not guessed from names), tables are classed as facts or dimensions, and a data model is proposed for approval.",
    who: "Analyst, data owner", href: (ws) => to.data(ws, "definitions"), needsWorkspace: true, tour: "data" },
  { id: "brief", discipline: "data", title: "Data brief and readiness",
    what: "What the data is about, its grain and entities, open questions, and whether it is ready for a given kind of work.",
    who: "Everyone", href: (ws) => to.data(ws, "brief"), needsWorkspace: true },
  { id: "metrics", discipline: "data", title: "Governed metrics",
    what: "Define a KPI once, get it approved, and every answer, dashboard and schedule uses the same definition.",
    who: "Analyst, approver", href: (ws) => to.data(ws, "metrics"), needsWorkspace: true },
  { id: "documents", discipline: "data", title: "Documents and glossary",
    what: "Add runbooks, definitions and business terms; they are screened and cited separately from numbers.",
    who: "Everyone", href: (ws) => to.data(ws, "documents"), needsWorkspace: true },
  { id: "context-export", discipline: "data", title: "Download the context",
    what: "Everything known about your data (or one source's) as an OKF bundle, JSON or Markdown, and exactly what the agents are sent for a task. Never secrets or rows.",
    who: "Everyone", href: (ws) => to.data(ws, "transfer"), needsWorkspace: true },

  // Answer questions
  { id: "ask", discipline: "analysis", title: "Ask a question",
    what: "Type a question; get a checked answer with its SQL and a chart. Step by step splits a “why” or “by X and Y” question into checked steps and cites them.",
    who: "Everyone", href: (ws) => to.ask(ws), needsWorkspace: true, tour: "ask" },
  { id: "investigate", discipline: "analysis", title: "Run an investigation",
    what: "Agents form hypotheses, test them with real statistics, and keep only findings that pass verification.",
    who: "Analyst", href: (ws) => to.investigations(ws), needsWorkspace: true, tour: "investigate" },
  { id: "why", discipline: "analysis", title: "“Why this number?”",
    what: "Follow any number back to its query, data version, metric definition and verification, with each link's current state.",
    who: "Everyone", href: (ws) => to.findings(ws), needsWorkspace: true },
  { id: "thread", discipline: "analysis", title: "Data Thread and notebooks",
    what: "Every step is kept with its versions: edit one and what depends on it re-runs; fork, compare and merge into a report.",
    who: "Analyst", href: (ws) => to.work(ws, "thread"), needsWorkspace: true },
  { id: "process", discipline: "analysis", title: "Process and task mining",
    what: "Point at an activity log (a ticket's created, assigned, reassigned, resolved…): see the process map, the common paths, where cases wait, rework, cancellations, handovers and steps skipped against the expected path.",
    who: "Analyst, process owner", href: (ws) => to.work(ws, "process"), needsWorkspace: true },

  // Prepare and move data
  { id: "prepare", discipline: "engineering", title: "Prepare data with recipes",
    what: "Filter, join, aggregate and clean with typed steps; preview results before anything is written.",
    who: "Data engineer", href: (ws) => to.work(ws, "prepare"), needsWorkspace: true, tour: "engineer" },
  { id: "pipelines", discipline: "engineering", title: "Pipelines with dry runs",
    what: "Incremental loads with quality gates, a rows ledger, approval before writing, and one-click rollback.",
    who: "Data engineer", href: (ws) => to.work(ws, "prepare"), needsWorkspace: true, tour: "engineer" },
  { id: "dbt", discipline: "engineering", title: "dbt projects",
    what: "Generate and build dbt models on your warehouse with column lineage.",
    who: "Data engineer", href: (ws) => to.work(ws, "builds"), needsWorkspace: true },

  // Predict and forecast
  { id: "ml", discipline: "ml", title: "Predict, classify, forecast, cluster",
    what: "Describe the target; the platform checks for leakage, trains against a baseline and only says “improved” when the holdout confirms it.",
    who: "Data scientist", href: (ws) => to.work(ws, "experiments"), needsWorkspace: true, tour: "ml" },
  { id: "scoring", discipline: "ml", title: "Approved scoring and model monitors",
    what: "Promote a champion through an approval, score new data, and watch drift and delayed labels.",
    who: "Data scientist", href: (ws) => to.outputs(ws, { type: "model" }), needsWorkspace: true },

  // Share and keep watch
  { id: "outputs", discipline: "delivery", title: "Findings, dashboards and reports",
    what: "One list of everything produced, with PDF, Excel and HTML reports and dashboards published after approval.",
    who: "Everyone", href: (ws) => to.outputs(ws), needsWorkspace: true, tour: "investigate" },
  { id: "approvals", discipline: "delivery", title: "Approval inbox",
    what: "Anything that leaves the platform waits here for a second person, bound to exactly what was reviewed.",
    who: "Approver", href: (ws) => to.approvals(ws), needsWorkspace: true },
  { id: "monitors", discipline: "delivery", title: "Monitors and alerts",
    what: "Thresholds, drift, change points and data quality, checked on a schedule; an alert can start an investigation.",
    who: "Analyst", href: (ws) => to.monitoring(ws), needsWorkspace: true },
  { id: "schedules", discipline: "delivery", title: "Schedules",
    what: "Re-run analyses and refresh data on a timetable, pinned to the exact definitions that were approved.",
    who: "Analyst", href: (ws) => to.schedules(ws), needsWorkspace: true },

  // Govern the platform
  { id: "policy", discipline: "admin", title: "Members and policy",
    what: "Who can do what in a workspace, budgets, autonomy and the audit log.",
    who: "Workspace owner", href: (ws) => to.governance(ws), needsWorkspace: true },
  { id: "platform", discipline: "admin", title: "AI model settings",
    what: "Choose when models are used (off, auto, always) per purpose; everything has a rule-based path.",
    who: "Administrator", href: () => to.settings(), needsWorkspace: false, adminOnly: true },
  { id: "usage", discipline: "admin", title: "Usage and token savings",
    what: "Spend per workspace and purpose, and the tokens saved by caching and rule-based paths.",
    who: "Administrator", href: () => to.usage(), needsWorkspace: false, adminOnly: true },
  { id: "registry", discipline: "admin", title: "Capability registry",
    what: "The agents, tools, skills and playbooks installed, with their versions and certification.",
    who: "Administrator", href: () => to.registry(), needsWorkspace: false, adminOnly: true },
];

export type TourId = "platform" | "data" | "ask" | "investigate" | "engineer" | "ml";

export interface TourStep {
  /** Where the step happens; omitted = stay on the current screen. */
  path?: (ws: string) => string;
  /** A CSS selector, normally `[data-tour="…"]`; omitted = a centred card. */
  target?: string;
  title: string;
  body: string;
}

export interface Tour {
  id: TourId;
  title: string;
  summary: string;
  minutes: number;
  steps: TourStep[];
}

const nav = (screenId: string) => `[data-tour="nav-${screenId}"]`;
const tab = (id: string) => `[data-tour="tab-${id}"]`;

export const TOURS: Tour[] = [
  {
    id: "platform", title: "The two-minute tour", minutes: 2,
    summary: "Where everything is: data, work, outputs and what needs you.",
    steps: [
      { path: (ws) => to.workspace(ws), title: "Welcome to AnalystOS",
        body: "AnalystOS understands your data, answers questions with checked numbers, prepares data and trains models. Nothing leaves the platform without an approval. This tour shows where each part lives." },
      { target: nav("overview"), title: "Overview",
        body: "Your home in a workspace. Before you start it lists the next setup step; after that it shows only what needs you." },
      { target: nav("sources"), title: "Data → Sources",
        body: "Connect a database or upload files here. You pick which tables AnalystOS may read; everything else stays invisible to it." },
      { target: nav("catalog"), title: "Data → Catalog & definitions",
        body: "What your data means: profiled tables and columns, their descriptions, how tables join, and approved metrics. Agents read this instead of guessing." },
      { target: '[data-tour="start-work"]', title: "Start work",
        body: "The one button for new work: explain a change, find drivers, prepare data, predict, forecast or monitor. It shows what the work will read and cost before it starts." },
      { target: nav("ask"), title: "Ask",
        body: "The quick box. Ask in plain words and get an answer with its SQL, a chart and where the number came from." },
      { target: nav("work"), title: "Work",
        body: "Everything you started: investigations, the Data Thread of steps, notebooks, experiments and data preparation." },
      { target: nav("outputs"), title: "Outputs",
        body: "Findings, dashboards, reports, datasets and models, in one list you can filter." },
      { target: nav("approvals"), title: "Operate",
        body: "The approval inbox, monitors and alerts, and schedules. Publishing, exporting and writing data all wait here for a second person." },
      { target: '[data-tour="guide"]', title: "Come back any time",
        body: "The Guide lists everything AnalystOS can do and has a tour for each part. Ctrl K jumps to any screen." },
    ],
  },
  {
    id: "data", title: "Connect and understand your data", minutes: 3,
    summary: "From a connection to a reviewed data model that every agent can rely on.",
    steps: [
      { path: (ws) => to.sources(ws), target: '[data-tour="page-title"]', title: "Sources",
        body: "Add a connection (a database, a warehouse, ServiceNow or files). Discover lists the tables; you select the ones to use." },
      { target: '[data-tour="source-list"]', title: "Crawls",
        body: "Each source is crawled: structure, row counts and column profiles are measured through the governed gateway. Re-crawls detect drift and keep your edits." },
      { path: (ws) => to.catalog(ws), target: '[data-tour="catalog-table"]', title: "The catalog",
        body: "Every selected table with its role (fact, dimension, event…), domain, grain, sensitive columns and how confident the classification is." },
      { target: '[data-tour="catalog-table"]', title: "Open a table",
        body: "Choose Columns on a table to see each column's profile — completeness, distinct values, range and most common values — and its description. Curate anything that is wrong; your edits are never overwritten." },
      { target: tab("brief"), title: "Brief & readiness",
        body: "A short description of the data: entities, grain, facts and open questions, and whether it is ready for each kind of work." },
      { target: tab("review"), title: "Review queue",
        body: "Proposed descriptions, domains and joins wait here. Nothing a model suggests is used until a person accepts it." },
      { target: tab("definitions"), title: "Definitions",
        body: "The data model: measured joins with their cardinality, entity and grain per table, and versioned definitions you approve." },
      { target: tab("metrics"), title: "Metrics",
        body: "Governed KPIs. Once approved, Ask, investigations, dashboards and schedules all use the same definition." },
    ],
  },
  {
    id: "ask", title: "Ask a question", minutes: 1,
    summary: "A plain-language question to a checked, explained answer.",
    steps: [
      { path: (ws) => to.ask(ws), target: '[data-tour="ask-box"]', title: "Type a question",
        body: "For example “How many P1 incidents per month?”. AnalystOS matches it to approved metrics first and only writes new SQL when none fits." },
      { target: '[data-tour="page-title"]', title: "What you get back",
        body: "The answer with its SQL, a chart and a label: governed (from an approved metric) or ad hoc. Choose Step by step for a “why did it change” or “by X and Y” question: up to four checked steps, facts computed in code, and an answer that cites each step." },
      { target: '[data-tour="ask-threads"]', title: "Threads",
        body: "Questions are kept as threads. Follow-ups reuse the context already built, so they are faster and cheaper." },
    ],
  },
  {
    id: "investigate", title: "Run an investigation", minutes: 2,
    summary: "From a business question to verified findings and a published output.",
    steps: [
      { path: (ws) => to.workspace(ws), target: '[data-tour="start-work"]', title: "Start work",
        body: "Choose Explain a change or Find drivers. Before it starts you see which tables it reads, the expected cost and what will need your approval." },
      { path: (ws) => to.investigations(ws), target: '[data-tour="page-title"]', title: "Investigations",
        body: "Agents propose hypotheses, write governed SQL, run statistics in code and verify each result. A board shows every hypothesis as supported, rejected or inconclusive." },
      { path: (ws) => to.outputs(ws), target: '[data-tour="page-title"]', title: "Outputs",
        body: "Verified findings, dashboards and reports. A finding whose data or definition changes is marked void rather than silently kept." },
      { path: (ws) => to.approvals(ws), target: '[data-tour="page-title"]', title: "Approval inbox",
        body: "Publishing a dashboard or sending a report waits for a second person, bound to exactly the content they reviewed." },
    ],
  },
  {
    id: "engineer", title: "Prepare and load data", minutes: 2,
    summary: "Recipes, pipelines, dry runs and approved writes.",
    steps: [
      { path: (ws) => to.work(ws, "prepare"), target: tab("prepare"), title: "Prepare data",
        body: "Build a recipe of typed steps (filter, join, aggregate, clean). Joins are checked for fan-out before they run." },
      { target: '[data-tour="page-title"]', title: "Pipelines",
        body: "A pipeline runs a recipe incrementally. A dry run shows the rows ledger — read, written, quarantined — before anything is written." },
      { path: (ws) => to.approvals(ws), target: '[data-tour="page-title"]', title: "Approve, then write",
        body: "Materializing writes only after approval, as a new version you can roll back in one step." },
    ],
  },
  {
    id: "ml", title: "Predict and forecast", minutes: 2,
    summary: "A model that has to beat a baseline before it is called better.",
    steps: [
      { path: (ws) => to.work(ws, "experiments"), target: tab("experiments"), title: "Experiments",
        body: "Describe what to predict. The platform checks target leakage, freezes the splits and always trains a simple baseline first." },
      { target: '[data-tour="page-title"]', title: "Honest results",
        body: "A candidate is “improved” only when a sealed holdout confirms it. Otherwise the result says no improvement, with the numbers." },
      { path: (ws) => to.outputs(ws, { type: "model" }), target: '[data-tour="page-title"]', title: "Models",
        body: "Each model has a card, versions and approved promotion. Scoring and rollback go through the approval inbox." },
    ],
  },
];

export function tour(id: TourId): Tour {
  const t = TOURS.find((x) => x.id === id);
  if (!t) throw new Error(`unknown tour ${id}`);
  return t;
}

/** The words the product uses, in plain language. */
export const TERMS: { term: string; meaning: string }[] = [
  { term: "Context & mappings", meaning: "Reviewed business meanings linked to data columns, with owners and validity dates. Try a question here before investigating." },
  { term: "Hypothesis", meaning: "A possible explanation to test, not an established fact. Supported means the test found evidence; rejected means it did not support that explanation; inconclusive means it could not decide." },
  { term: "Golden question", meaning: "An owner-reviewed example used to check that the app chooses the expected meanings and columns, or asks for clarification. This does not check numerical-answer accuracy." },
  { term: "Workspace", meaning: "A team's space: its data connections, members, policy and everything produced from them." },
  { term: "Source", meaning: "A connection to a database, warehouse, application or uploaded files." },
  { term: "Crawl", meaning: "Reading a source's structure and profiling its columns. Re-crawls detect changes (drift) and keep your edits." },
  { term: "Profile", meaning: "Measured facts about a column: how complete it is, how many distinct values, its range and most common values." },
  { term: "Gateway", meaning: "The single door every query goes through. It allows read-only SQL on selected tables and withholds restricted columns." },
  { term: "Governed / ad hoc", meaning: "Governed answers use an approved metric definition; ad hoc answers were written for this question only." },
  { term: "Investigation", meaning: "A piece of work where agents test hypotheses about a question and keep only verified findings." },
  { term: "Finding", meaning: "A verified result: a claim, the numbers behind it and how it was checked." },
  { term: "Verified / void", meaning: "Verified: every check passed. Void: something it depends on (data, SQL, a definition) changed, so it must be re-checked." },
  { term: "Approval", meaning: "A second person's decision, bound to the exact content reviewed. Needed before anything leaves the platform." },
  { term: "Data Thread", meaning: "The steps of a piece of work, with versions. Editing a step re-runs what depends on it." },
  { term: "Recipe / pipeline", meaning: "A recipe is a set of data-preparation steps; a pipeline runs it on a schedule and writes only after approval." },
  { term: "Baseline", meaning: "The simple model every trained model must beat before it is called an improvement." },
  { term: "Model modes", meaning: "Off: rules only. Auto: a model only where rules are not enough. Always: a model where one is configured." },
];

const DONE_KEY = "analystos.tours.done";
const WELCOME_KEY = "analystos.guide.welcomeDismissed";

function readList(key: string): string[] {
  try {
    const v = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

export function toursDone(): TourId[] {
  return readList(DONE_KEY).filter((x): x is TourId => TOURS.some((t) => t.id === x));
}

export function markTourDone(id: TourId): void {
  try {
    const done = new Set(readList(DONE_KEY));
    done.add(id);
    window.localStorage.setItem(DONE_KEY, JSON.stringify([...done]));
  } catch {
    // storage unavailable: the tour simply is not remembered
  }
}

export function welcomeDismissed(): boolean {
  try {
    return window.localStorage.getItem(WELCOME_KEY) === "1";
  } catch {
    return false;
  }
}

export function dismissWelcome(): void {
  try {
    window.localStorage.setItem(WELCOME_KEY, "1");
  } catch {
    // storage unavailable: the welcome shows again next visit
  }
}
