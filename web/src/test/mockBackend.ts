/**
 * A deterministic in-memory stand-in for the AnalystOS API, shared by the vitest accessibility
 * suite (fetch spy) and the Playwright journeys (route interception). Fixtures are typed against
 * the client's response shapes so a shape change breaks the typecheck, not just a screenshot.
 */
import type {
  AgentSpec, Alert, Approval, Artifact, ArtifactDetail, AskInspector, AskResponse, AskThread, AskTurn, BuildDiff, BuildJob, BuildJobDetail, BuildTarget,
  CapabilityManifest, CapabilitySummary, CatalogAsset, ConsoleData, Health, Hypothesis, Insight, InsightDetail, MetricValidation, ModelHealth, Monitor, ModelsView,
  PlatformSettings, Run, RunDetail, Schedule, SemanticMetric, SkillSpec, Source, SourceKindInfo, TokenSavings, ToolSpec, Usage, User, WorkspaceDetail,
} from "../api";
import { contextRoute } from "./mockContext";
import { knowledgeReceipts, knowledgeRoute, recordQuestion, resetKnowledgeState } from "./mockKnowledge";
import { INSIGHT_VOID_ID, resetWave1, VERIFIED_STATE, voidInsight, wave1Route } from "./mockWave1";
import { wave2Approvals } from "./mockApprovals";
import { mlMonitors } from "./mockMl";
import { resetWave2, wave2Route } from "./mockWave2";
import { investigationValueRoute, resetInvestigationValue } from "./mockInvestigationValue";
import { groundingRoute, resetGrounding } from "./mockGrounding";
import { draftArtifacts, draftsRoute, resetDrafts } from "./mockDashboardDrafts";
import { reportArtifacts, reportRoute, resetReports } from "./mockReports";

export const WS = "ws_demo";
export const RUN = "run_demo";
export const INSIGHT = "ins_demo";
const T = "2026-09-25T09:00:00Z";

/** A healthy standard installation (GET /api/health); the status banner shows nothing for it. */
export const HEALTH: Health = {
  ok: true, degraded: false, problems: [], orchestrator: "temporal",
  checks: { postgres: { ok: true, state: "up" }, temporal: { ok: true, state: "up" }, worker: { ok: true, state: "up" },
    superset: { ok: true, state: "up" } },
};

export const USER: User = {
  id: "usr_admin", email: "admin@analystos.local", name: "Ada Admin", is_admin: true, active: true, attributes: {}, created_at: T,
};

export const WORKSPACE: WorkspaceDetail = {
  id: WS, name: "IT Service Management", description: "Incident and change analytics", objective: "Why are P1 resolution times rising?",
  autonomy_level: 3, status: "active", settings: {}, policy_version: 2, created_by: USER.id, created_at: T, updated_at: T,
  counts: { query: 12, dataset: 2, metric: 3, chart: 4, dashboard: 1, runs: 1, verified_insights: 1, sources: 1 },
  role: "owner",
  policy: { max_rows: 5000, pii_access: "none", publish_destinations: ["superset"], publish_requires_approval: true, run_cost_budget_usd: 2,
    run_token_budget: 200000, restricted_columns: [] },
  members: [{ user_id: USER.id, role: "owner", email: USER.email, name: USER.name }],
};

const SOURCE: Source = {
  id: "src_sn", workspace_id: WS, kind: "servicenow", name: "ServiceNow", config: {}, secret_ref: "env:SN_PASSWORD", status: "ready",
  execution_mode: "staged", staging_schema: "stg_sn", last_discovered_at: T, last_error: null, created_at: T,
};

/** A file source for uploads (Work → Prepare data); nothing is selected in it yet. */
const FILE_SOURCE: Source = {
  id: "src_files", workspace_id: WS, kind: "csv", name: "Uploaded files", config: {}, secret_ref: null, status: "registered",
  execution_mode: "staged", staging_schema: "stg_files", last_discovered_at: T, last_error: null, created_at: T,
};

const INSIGHT_ROW: Insight = {
  id: INSIGHT, workspace_id: WS, run_id: RUN, hypothesis_id: "hyp_1", code: "F1", title: "Network group drives P1 breaches",
  finding: "Network resolves P1 incidents 2.1x slower than the median group.", confidence: 0.86, population_size: 4210,
  business_impact: {}, caveats: ["Q3 only", "Data quality: 2.1% of resolved_at values are null"], evidence: [{ type: "query", id: "qry_h1" }],
  verified: true, status: "verified", narrative_source: "rule", created_at: T, verification_state: VERIFIED_STATE,
  verification: {
    verified: true,
    evaluate: [
      { check: "method_fit", passed: true, detail: "mann_whitney for numeric_by_segment; assumptions: independent samples" },
      { check: "sample_size", passed: true, detail: "n=4210, smallest group=182 (min 30)" },
      { check: "significance_after_bh", passed: true, detail: "q=0.001 alpha=0.05" },
      { check: "effect_size", passed: true, detail: "rank_biserial=0.41" },
      { check: "representative_population", passed: true, method: "time_window", detail: "time_window: 4,210 of 4,210 rows; not truncated" },
      { check: "no_overreach", passed: true, detail: "associational wording" },
      { check: "reproducible_rerun", passed: true, detail: "qry_h1: identical result hash" },
      { check: "second_method", passed: true, detail: "welch_t: p=0.0007 effect=0.38 agrees=true" },
    ],
    verify: { reproducible: true, second_method: { test: "welch_t", p_value: 0.0007, effect_size: 0.38 }, jev: { p_supports: 0.91, model: "typesafe/jev-1" },
      independent_model: { unavailable: "no model of another family allowed" }, contradictions: [] },
  },
};

/** An unverified finding on another hypothesis: its checks are not recorded, so trust reads as unknown. */
const INSIGHT_DRAFT: Insight = {
  ...INSIGHT_ROW, id: "ins_draft", hypothesis_id: "hyp_5", code: "F2", title: "Weekend P1s may close faster", verified: false,
  status: "draft", confidence: 0.4, population_size: 0, caveats: [], evidence: [], verification: {},
  finding: "Weekend P1 incidents may resolve faster; the evidence is thin.",
};

export const RUN_ROW: Run = {
  id: RUN, workspace_id: WS, objective: "Why are P1 resolution times rising?", status: "COMPLETED", autonomy_level: 3, plan_version: 1,
  plan_hash: "a1b2c3d4e5f6", policy_version: 2, instructions: [], constraints: {}, control: "run", requested_by: USER.id,
  workflow_id: null, iteration: 1, tokens: 18250, cost_usd: 0.0421, error: null,
  summary: { summary_markdown: "**One verified finding.** Network drives P1 breaches.", verified_insights: 1, published: false },
  origin: { type: "user" }, created_at: T, started_at: T, finished_at: "2026-09-25T09:04:00Z",
};

const APPROVAL: Approval = {
  id: "apr_1", workspace_id: WS, run_id: RUN, action: "publish_dashboard", risk_tier: "medium", destination: "superset",
  affected_assets: ["incident"], payload_hash: "0123456789abcdef0123", plan_hash: "a1b2c3d4e5f6", policy_version: 2, requested_by: USER.id,
  status: "pending", decided_by: null, decided_at: null, reason: null, expires_at: "2099-01-01T00:00:00Z",
  evidence: { governance_review: { ok: true, problems: [] }, jev_consequential: 0.62 }, created_at: T,
  payload: { dashboards: [{ key: "p1_resolution", title: "P1 resolution (weekly)", charts: ["mttr_by_group", "breaches"] }],
    charts: [{ key: "mttr_by_group", type: "bar" }, { key: "breaches", type: "line" }] },
};

/** The last publication approved to the same destination: the inbox diffs the pending payload against it. */
const APPROVAL_EXECUTED: Approval = {
  ...APPROVAL, id: "apr_0", status: "executed", payload_hash: "fedcba9876543210fedc", policy_version: 1, decided_by: USER.id,
  decided_at: "2026-09-18T09:30:00Z", created_at: "2026-09-18T09:00:00Z",
  payload: { dashboards: [{ key: "p1_resolution", title: "P1 resolution", charts: ["mttr_by_group"] }], charts: [{ key: "mttr_by_group", type: "bar" }] },
};

const hyp = (id: string, code: string, status: string, statement: string, extra: Partial<Hypothesis> = {}): Hypothesis => ({
  id, workspace_id: WS, run_id: RUN, code, question: "Which group is slowest?", statement, spec: {}, priority: "medium", priority_score: 0.5,
  status, methods: ["mann_whitney"], evidence: [], confidence: null, conclusion: null, parent_id: null, iteration: 1, origin: "planner",
  created_at: T, result: null, experiment_id: null, ...extra,
});

const RUN_DETAIL: RunDetail = {
  ...RUN_ROW, scope: {}, tasks: [
    { id: "tsk_1", run_id: RUN, key: "profile", agent_id: "profiler", title: "Profile incident data", status: "COMPLETED", depends_on: [],
      input: {}, output: {}, attempts: 1, plan_version: 1, seq: 1, error: null, started_at: T, finished_at: T },
  ],
  hypotheses: [
    hyp("hyp_1", "H1", "supported", "Network resolves P1s slower", { priority: "high", priority_score: 0.9, confidence: 0.86, conclusion: "Supported",
      result: { test: "mann_whitney", n: 4210, p_value: 0.0004, p_adjusted: 0.001, effect_size: 0.41, effect_label: "rank_biserial" } }),
    hyp("hyp_2", "H2", "rejected", "P1 volume rose in Q3", { result: { test: "trend", n: 26, p_value: 0.4, p_adjusted: 0.6, effect_size: 0.02 } }),
    hyp("hyp_3", "H3", "testing", "Reassignments lengthen P1 resolution"),
    hyp("hyp_4", "H4", "proposed", "Change freezes delay fixes", { priority: "low", priority_score: 0.2 }),
    hyp("hyp_5", "H5", "inconclusive", "Weekend P1s close faster"),
    hyp("hyp_0", "H0", "superseded", "An earlier framing replaced by a replan"),
  ],
  insights: [INSIGHT_ROW, INSIGHT_DRAFT],
  approvals: [APPROVAL],
  instructions: [{ kind: "redirect", text: "Exclude auto-closed tickets.", at: T, feedback_id: "fb_1" }],
  constraints: { filters: [{ asset: "incident", column: "close_code", op: "!=", value: "auto" }], focus: [] },
};

const INSIGHT_DETAIL: InsightDetail = { ...INSIGHT_ROW, queries: [{
  id: "qry_h1", workspace_id: WS, source_id: "src_sn", run_id: RUN, task_id: null, actor: "agent:data_scientist", purpose: "hypothesis.test",
  sql: "SELECT 1", executed_sql: "SELECT 1", fingerprint: "fp", status: "ok", rejected_reason: null, referenced_assets: ["incident"], row_count: 2,
  truncated: false, columns: ["g", "n"], result_hash: "9f86d081884c7d659a2feaa0c55ad015", cache_hit: false, duration_ms: 42, created_at: T,
}], experiments: [], lineage: { nodes: [], edges: [] } };

const ARTIFACTS: Artifact[] = [{
  id: "art_dash", workspace_id: WS, run_id: RUN, type: "dashboard", name: "P1 resolution", version: 1, status: "draft", platform: null,
  external_id: null, external_url: null, creator_agent: "bi", creator_user: null, content_hash: "ff00", created_at: T, updated_at: T,
  content: { title: "P1 resolution (weekly)", audience: "ops leads", charts: ["mttr_by_group"],
    layout: [{ kind: "chart", chart: "mttr_by_group", row: 0, col: 0, width: 12, height: 6 }] },
}, {
  id: "art_chart", workspace_id: WS, run_id: RUN, type: "chart", name: "mttr_by_group", version: 1, status: "draft", platform: null,
  external_id: null, external_url: null, creator_agent: "bi", creator_user: null, content_hash: "ee11", created_at: T, updated_at: T,
  content: { key: "mttr_by_group", title: "P1 MTTR by assignment group", chart_type: "bar",
    preview: { columns: ["assignment_group", "mttr_hours"], rows: [["Network", 9.4], ["Desktop", 6.1], ["Database", 5.2]] } },
}];

const artifactDetail = (a: Artifact): ArtifactDetail => ({
  ...a, versions: [{ id: 1, artifact_id: a.id, version: 1, content_hash: a.content_hash, created_by: "agent:bi", created_at: T }],
  lineage: { nodes: [], edges: [] },
});

const CATALOG: CatalogAsset[] = [{
  id: "ast_inc", fq: "servicenow.incident", source_id: SOURCE.id, name: "incident", business_name: "Incidents",
  description: "One row per incident.", description_origin: "rule", reviewed: true, selected: true, lifecycle: "active", row_count: 4210,
  role: "fact", domain: "itsm", grain: "incident", confidence: 0.9, last_crawled_at: T,
  columns: [{ name: "priority", data_type: "TEXT", business_name: "Priority", description: null, tags: [], tags_origin: "crawler",
    role: "dimension", unit: null, pii: null, glossary: null }],
}];

const KINDS: SourceKindInfo[] = [{
  kind: "servicenow", label: "ServiceNow", category: "api", required: ["username"], optional: ["instance_url"], default_port: null, docs: "",
  secret_field: "password", execution_mode: "staged", dialect: "postgres", driver_installed: true, install_hint: null, enabled: true,
}];

export const ASK: AskResponse = {
  sql: "SELECT assignment_group, COUNT(*) AS p1 FROM stg_sn.incident WHERE priority = '1' GROUP BY 1 ORDER BY 2 DESC",
  explanation: "P1 incidents by assignment group.", chart: { type: "bar", x: "assignment_group", y: "p1" }, model: "rule", attempts: [],
  result: { query_id: "qry_1", columns: ["assignment_group", "p1"], rows: [["Network", 182], ["Desktop", 110]], row_count: 2, truncated: false },
};

// ------------------------------------------------------------------------------------ Ask threads (P4-U02)
export const THREAD_OLD = "ask_old";
export const THREAD_NEW = "ask_new";

const THREAD: AskThread = { id: THREAD_OLD, workspace_id: WS, user_id: USER.id, title: "Weekly P1 volume", archived: false,
  created_at: "2026-09-20T09:00:00Z", updated_at: "2026-09-20T09:00:00Z", turn_count: 1 };

const STAGES = [
  { key: "scope", text: "Checking what you are allowed to see", at_ms: 3 },
  { key: "registry", text: "Looking for a verified answer to this question", at_ms: 9 },
  { key: "route", text: "No verified answer fits: writing new SQL", at_ms: 12 },
  { key: "context", text: "Finding the tables that answer this", at_ms: 15 },
  { key: "generate", text: "Writing the SQL", at_ms: 20 },
  { key: "execute", text: "Running it through the query gateway (read-only, within your access)", at_ms: 910 },
  { key: "done", text: "Answered", at_ms: 950 },
];

export function askTurn(id: string, question: string, over: Partial<AskTurn> = {}): AskTurn {
  return {
    id, thread_id: THREAD_NEW, workspace_id: WS, seq: 1, question, parameters: {}, status: "answered", route: "generate", answered_by: "model",
    refusal: null, sql: ASK.sql, explanation: ASK.explanation, chart: ASK.chart, result: { ...ASK.result, referenced_assets: ["stg_sn.incident"] },
    verified_query: null, model: "openrouter/auto", attempts: [], stages: STAGES, promotions: [], latency_ms: 950, created_at: T,
    decisions: [{ id: "dec_1", purpose: "ask_route", backend: "rules", value: "generate" },
      { id: "dec_2", purpose: "clarify_needed", backend: "jev", value: "answer", model: "typesafe/jev-1", probabilities: { answer: 0.88, clarify: 0.12 } }],
    provenance: { assets: [{ asset: "stg_sn.incident", asset_id: "ast_inc", business_name: "Incidents", source_id: SOURCE.id, source_name: "ServiceNow",
      source_kind: "servicenow", execution_mode: "staged", freshness_at: "2026-09-25T06:00:00Z", row_count: 4210 }],
    answered_by: "model", model: "openrouter/auto", query_id: "qry_1", cache_hit: false, result_hash: "9f86d081884c7d65", repairs: 0 },
    staleness: { state: "fresh", label: "Data as of 3 hours ago", data_as_of: "2026-09-25T06:00:00Z" },
    ...over,
  };
}

const OLD_TURN = askTurn("askt_old", "How many P1 incidents per week?", { thread_id: THREAD_OLD });

/** The clarify refusal: nothing to measure, so nothing ran. */
function clarifyTurn(question: string): AskTurn {
  return askTurn("askt_clarify", question, {
    status: "clarify", route: "generate", answered_by: null, sql: null, explanation: null, chart: null, result: null, provenance: {},
    refusal: { kind: "clarify", title: "The question needs more detail", message: "This question is too open to answer safely.",
      remedy: "Say what to measure (a count, a rate, an average), over which records and period, and how to group it.", details: { missing: [{ name: "what to measure" }] } },
    staleness: { state: "unknown", label: "Data freshness unknown", data_as_of: null },
    stages: [STAGES[0], { key: "clarify", text: "The question needs more detail before it can be answered", at_ms: 8 }],
  });
}

/** A distribution answered by the Ask rules from the catalog: no model, follow-up groupings offered. */
let rulesTurns = 0;
export function rulesTurn(question: string): AskTurn {
  rulesTurns += 1;
  const dim = /by contact channel/i.test(question) ? "contact_type" : "category";
  const sql = `SELECT "${dim}" AS "${dim}", COUNT(*) AS "incident_count" FROM "stg_sn"."incident" GROUP BY "${dim}" ORDER BY "incident_count" DESC NULLS LAST, "${dim}"`;
  return askTurn(`askt_rules_${rulesTurns}`, question, {
    route: "tool", answered_by: "rules", model: null, sql, chart: { type: "bar", x: dim, y: "incident_count" },
    explanation: `Number of incident records by ${dim === "category" ? "category" : "contact channel"} in stg_sn.incident (grouped by category because the domain pack declares it for incident). Built from the catalog by the Ask rules; no model call.`,
    result: { query_id: "qry_r1", columns: [dim, "incident_count"], rows: [["software", 1210], ["network", 980], ["hardware", 640]], row_count: 3,
      truncated: false, referenced_assets: ["stg_sn.incident"] },
    decisions: [{ id: "dec_r1", purpose: "ask_route", backend: "rules", value: "tool" }],
    provenance: { assets: [{ asset: "stg_sn.incident", asset_id: "ast_inc", business_name: "Incidents", source_id: SOURCE.id, source_name: "ServiceNow",
      source_kind: "servicenow", execution_mode: "staged", freshness_at: "2026-09-25T06:00:00Z", row_count: 4210 }],
    answered_by: "rules", model: null, query_id: "qry_r1", cache_hit: false, result_hash: "r1", repairs: 0,
    suggestions: dim === "category" ? ["distribution of incident by contact channel", "distribution of incident by priority"] : [] },
  });
}

/** No provider key in the API process: the refusal names the key and the restart, not "no model route". */
/** PILOT-FIX-02: an answer for a quarter the data covers only in part carries a note saying so. */
function quarterTurn(question: string): AskTurn {
  const base = askTurn("askt_1", question);
  return { ...base, provenance: { ...base.provenance, period: { start: "2026-07-01", end: "2026-10-01", label: "Q3 2026" },
    notes: ["Q3 2026 is incomplete in this data: its records run from 1 Jul 2026 to 31 Aug 2026, with none in September 2026. Read it as a partial period, not a whole one."] } };
}

function noKeyTurn(question: string): AskTurn {
  return askTurn("askt_nokey", question, {
    status: "refused", route: "generate", answered_by: null, sql: null, explanation: null, chart: null, result: null, provenance: {}, model: null,
    refusal: { kind: "no_api_key", title: "No model provider key is set for the API",
      message: "SQL generation unavailable: no API key for provider 'openrouter' (set OPENROUTER_API_KEY)",
      remedy: "Set OPENROUTER_API_KEY for the api and worker containers (docker compose reads it from your shell or the .env file at `docker compose up`), then restart them: `docker compose up -d api worker`. A key exported after the containers started is not seen by them.",
      details: { reason: "no_api_key", env: "OPENROUTER_API_KEY", provider: "openrouter" },
      business: { title: "This question needs an AI model",
        message: "This question needs an AI model, which isn't turned on for this workspace. Ask your administrator.",
        remedy: "Nothing was guessed. Questions the platform answers without a model (counts and breakdowns of a table, approved metrics, saved answers) still work." } },
    staleness: { state: "unknown", label: "Data freshness unknown", data_as_of: null },
    stages: [STAGES[0], STAGES[1], STAGES[2], STAGES[3], STAGES[4], { key: "done", text: "No model provider key is set for the API", at_ms: 30 }],
  });
}

const INSPECTOR = (turn: AskTurn): AskInspector => ({
  turn,
  decisions: [
    { id: "dec_1", purpose: "ask_route", authority: "route", backend: "rules", model: null, answer: "generate", proposal: "generate",
      probabilities: { generate: 1 }, confidence: null, fallback_reason: null, attempts: [{ backend: "rules", outcome: "answered", reason: "nothing verified matched" }],
      enforced: [], subject: `ask:${turn.id}`, latency_ms: 1, created_at: T },
    { id: "dec_2", purpose: "clarify_needed", authority: "escalate_only", backend: "jev", model: "typesafe/jev-1", answer: "answer", proposal: "answer",
      probabilities: { yes: 0.12, no: 0.88 }, confidence: 0.88, fallback_reason: null, attempts: [], enforced: [], subject: `ask:${turn.id}`, latency_ms: 240,
      created_at: T },
  ],
  model_calls: [{ id: 91, run_id: null, task_id: turn.id, agent_id: "sql", purpose: "sql_generation", profile: "chat", provider: "openrouter",
    model: "openrouter/auto", prompt_version: "sql_generation.v1@ab12", status: "ok", attempt: 1, latency_ms: 820, input_tokens: 2100, output_tokens: 90,
    cost_usd: 0.0012, error: null, created_at: T, answered_by: "llm_small",
    context_receipts: [{ kind: "glossary", id: "term_p1", title: "P1 = priority 1 (critical)", version: 3 }] }],
  query: { id: "qry_1", workspace_id: WS, source_id: SOURCE.id, run_id: null, task_id: null, actor: `user:${USER.id}`, purpose: "ask", sql: ASK.sql,
    executed_sql: `${ASK.sql} LIMIT 5001`, fingerprint: "fp_ask_1", status: "ok", rejected_reason: null, referenced_assets: ["stg_sn.incident"], row_count: 2,
    truncated: false, columns: ASK.result.columns, result_hash: "9f86d081884c7d65", cache_hit: false, duration_ms: 40, created_at: T },
  receipts: [{ kind: "glossary", id: "term_p1", title: "P1 = priority 1 (critical)", version: 3 }, ...knowledgeReceipts()],
});

const sse = (frames: [string, unknown][]): MockResponse => ({
  status: 200, contentType: "text/event-stream",
  body: frames.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join(""),
});

/** Ask thread routes: list, create, detail, streamed turn, inspector and promotions. */
function askRoute(m: string, p: string, requestBody?: string | null): MockResponse | null {
  if (m === "GET" && p === `/workspaces/${WS}/ask/threads`) return json([THREAD]);
  if (m === "POST" && p === `/workspaces/${WS}/ask/threads`) {
    return json({ id: THREAD_NEW, workspace_id: WS, user_id: USER.id, title: "New question", archived: false, created_at: T, updated_at: T, turns: [] });
  }
  if (m === "GET" && p === `/ask/threads/${THREAD_OLD}`) return json({ ...THREAD, turns: [OLD_TURN] });
  // v2 conversations over the same threads (S04): by default no branch, no state, no memory
  const conv = /^\/v2\/conversations\/([^/]+)(\/state|\/memory|\/corrections)?$/.exec(p);
  if (m === "GET" && conv) {
    const [, id, sub] = conv;
    if (sub === "/state") {
      return json({ conversation_id: id, version: 0, at_seq: 0, cause: null, message_id: null,
        state: { filters: [], sources: [], metric_bindings: {}, time_window: null, clarifications: [], result_handles: [] } });
    }
    if (sub === "/memory") return json({ items: [], overrides: [] });
    if (sub === "/corrections") return json([]);
    return json({ ...THREAD, id, tenant_id: "tnt_default", channel: "ui", owner_id: USER.id, participants: [USER.id], request_id: null,
      parent_conversation_id: null, branch_from_message_id: null, legal_hold: false, closed_at: null, summarized_at: null, revision: 1,
      state_version: 0, message_count: 1, messages: [] });
  }
  if (m === "POST" && p === `/ask/threads/${THREAD_NEW}/turns`) {
    const q = String((requestBody ? JSON.parse(requestBody) as { question?: string } : {}).question ?? "");
    recordQuestion(q);
    const turn = /about it/i.test(q) ? clarifyTurn(q) : /^distribution of/i.test(q) ? rulesTurn(q)
      : /consecutive weeks/i.test(q) ? noKeyTurn(q) : /q3 2026/i.test(q) ? quarterTurn(q) : askTurn("askt_1", q);
    return sse([...turn.stages.map((s) => ["stage", { turn_id: turn.id, ...s }] as [string, unknown]), ["turn", turn], ["end", { status: "done" }]]);
  }
  const inspect = /^\/ask\/turns\/([^/]+)\/inspector$/.exec(p);
  if (m === "GET" && inspect) return json(INSPECTOR(askTurn(inspect[1], "How many P1 incidents per assignment group?")));
  const promote = /^\/ask\/turns\/([^/]+)\/promote$/.exec(p);
  if (m === "POST" && promote) {
    const body = requestBody ? JSON.parse(requestBody) as { target?: string } : {};
    const at = T;
    switch (body.target) {
      case "verified_query": return json({ target: "verified_query", id: "vq_1", name: "p1_by_group", status: "created", at });
      case "metric": return json({ target: "metric", id: "smet_1", name: "p1_count", status: "proposed", approval_id: "apr_m1", value: 292, at });
      case "monitor": return json({ target: "monitor", id: "mon_2", name: "P1 incidents per assignment group", status: "created", kind: "metric_drift", at });
      case "dashboard": return json({ target: "dashboard", id: "apr_d1", status: "approval_required", approval_id: "apr_d1", dashboard: "Ask answers", at }, 202);
      case "draft": {
        const b = body as { title?: string; dashboard_id?: string };
        return json(b.dashboard_id
          ? { target: "draft", id: b.dashboard_id, status: "added", dashboard: "Incident operations dashboard", version: 2, chart_id: "c_p1_by_group", chart_type: "bar", at }
          : { target: "draft", id: "art_draft_ask", status: "created", dashboard: b.title ?? "Ask answers", version: 1, chart_id: "c_p1_by_group", chart_type: "bar", at });
      }
      case "investigate": return json({ target: "investigate", id: RUN, status: "started", objective: "Investigate why: P1 incidents", at });
      default: return json({ error: { code: "invalid_input", message: "unknown target", details: {} } }, 422);
    }
  }
  return null;
}

// ------------------------------------------------------------------------------------ Build studio (P4-U05)
export const BUILD_PREV = "bld_1";
export const BUILD_NEW = "bld_2";
export const BUILD_APPROVAL = "apr_build";

/**
 * The only mutable part of the mock: a build planned in the journey and its approval decision, and
 * KPIs proposed or decided in the editor. `resetMockState` runs before every Playwright test (and in
 * the vitest cases that change it), so no test sees another's state.
 */
const state = { planned: false, buildApproval: "pending", proposed: [] as SemanticMetric[], decided: {} as Record<string, string>,
  workModes: ["analysis", "engineering", "ml"] as string[], conversation: [] as Record<string, unknown>[] };

export function resetMockState(): void {
  resetDrafts();
  resetInvestigationValue();
  resetGrounding();
  resetReports();
  state.conversation = [];
  state.workModes = ["analysis", "engineering", "ml"];
  state.planned = false;
  state.buildApproval = "pending";
  state.proposed = [];
  state.decided = {};
  resetKnowledgeState();
  resetWave1();
  resetWave2();
  resetCollections();
}

const BUILD_TARGET: BuildTarget = { id: "btg_1", workspace_id: WS, engine: "postgres:analytics", schema_name: "aos_mart",
  build_role: "aos_b_ws_demo", status: "active", provisioning: {}, created_by: USER.id, created_at: T };

const FILES_V1: Record<string, string> = {
  "dbt_project.yml": "name: analystos_p1\nversion: '1.0'\n",
  "models/p1_incidents.sql": "select sys_id, priority, assignment_group, opened_at\nfrom {{ source('stg_sn', 'incident') }}\n",
  "models/schema.yml": "version: 2\nmodels:\n  - name: p1_incidents\n",
};
const FILES_V2: Record<string, string> = {
  "dbt_project.yml": FILES_V1["dbt_project.yml"],
  "models/p1_incidents.sql": "select sys_id, priority, assignment_group, opened_at, resolved_at\nfrom {{ source('stg_sn', 'incident') }}\nwhere priority = '1'\n",
  "models/schema.yml": "version: 2\nmodels:\n  - name: p1_incidents\n    columns:\n      - name: sys_id\n        tests: [not_null, unique]\n",
  "models/semantic.yml": "semantic_models:\n  - name: p1_incidents\nmetrics:\n  - name: mttr_hours\n",
};

function buildJob(id: string, over: Partial<BuildJob>): BuildJob {
  return {
    id, workspace_id: WS, run_id: `run_${id}`, source_run_id: RUN, artifact_id: `art_tr_${id}`, approval_id: null, approval_status: null,
    engine: "postgres:analytics", runner: "dbt-core", target_schema: "aos_mart", project_name: "analystos_p1",
    project_hash: `${id}0a1b2c3d4e5f60718293a4b5c6d7e8f9`, plan_hash: "a1b2c3d4e5f6", relations: ["aos_mart.p1_incidents", "aos_mart.metricflow_time_spine"],
    dry_run: { runner: "dbt-core", ok: true, dbt_version: "1.12.5", ossie_version: "0.1.1", allowed_sources: ["stg_sn.incident"],
      tests: { candidates: [{ test: "not_null", column: "sys_id" }, { test: "unique", column: "sys_id" }, { test: "not_null", column: "resolved_at" }],
        passing: [{ test: "not_null", column: "sys_id" }, { test: "unique", column: "sys_id" }], dropped: [{ test: "not_null", column: "resolved_at" }] },
      metrics: [{ metric: "mttr_hours" }],
      skipped_metrics: [{ metric: "reopen_rate",
        reason: "not approved in the workspace semantic model (policy require_approved_metrics); approve it, then plan the build again" }],
      notes: [] },
    estimate: { rows: 4210, columns: 5, models: 2, tests: 2, approx_bytes: 336800,
      method: "COUNT(*) of the dataset through the query gateway; bytes = rows x columns x 16 (rough)", query_id: "qry_probe" },
    rollback: { strategy: "drop the relations this job creates or replaces", statements: ['DROP TABLE IF EXISTS "aos_mart"."p1_incidents" CASCADE'],
      restore: "re-run build job bld_1 (same target; needs its own approval)", previous_job_id: "bld_1" },
    status: "succeeded", run_results: {}, error: null, created_by: "agent:builder", created_at: T, started_at: null, finished_at: null,
    ...over,
  };
}

const JOB_PREV = buildJob(BUILD_PREV, {
  run_id: "run_bld1", project_hash: "0f1e2d3c4b5a69788796a5b4c3d2e1f0", created_at: "2026-09-18T09:00:00Z", finished_at: "2026-09-18T09:05:00Z",
  approval_id: "apr_b0", approval_status: "executed", run_results: { counts: { success: 2, pass: 2 } },
  rollback: { strategy: "drop the relations this job creates or replaces", statements: [],
    restore: "nothing to restore: no earlier build wrote this target", previous_job_id: null },
});

/** The job planned in the journey: waits for its approval, and (as the resumed run would) succeeds once approved. */
function jobNew(): BuildJob {
  const approved = state.buildApproval === "approved";
  return buildJob(BUILD_NEW, { approval_id: BUILD_APPROVAL, approval_status: approved ? "executed" : state.buildApproval,
    status: approved ? "succeeded" : "awaiting_approval", run_results: approved ? { counts: { success: 2, pass: 2 } } : {},
    finished_at: approved ? T : null });
}

function buildApproval(): Approval {
  const decided = state.buildApproval !== "pending";
  return { ...APPROVAL, id: BUILD_APPROVAL, run_id: `run_${BUILD_NEW}`, action: "elt_build", risk_tier: "high", destination: "postgres:analytics/aos_mart",
    affected_assets: ["aos_mart.p1_incidents", "aos_mart.metricflow_time_spine"], payload_hash: "b1d2e3f4a5b6c7d8e9f0",
    status: state.buildApproval === "approved" ? "executed" : state.buildApproval, decided_by: decided ? USER.id : null, decided_at: decided ? T : null,
    evidence: { policy: { decision: "require_approval", reasons: ["build writes to the customer's engine"] } },
    payload: { action: "elt_build", job_id: BUILD_NEW, project_hash: `${BUILD_NEW}0a1b2c3d4e5f60718293a4b5c6d7e8f9`, engine: "postgres:analytics",
      target_schema: "aos_mart", relations: ["aos_mart.metricflow_time_spine", "aos_mart.p1_incidents"], runner: "dbt-core" } };
}

function buildDetail(j: BuildJob, files: Record<string, string>): BuildJobDetail {
  const a = j.id === BUILD_NEW ? buildApproval() : null;
  return {
    ...j, project_files: files, manifest: {}, openlineage: [],
    log_tail: j.status === "succeeded" ? "Completed successfully\nDone. PASS=2 WARN=0 ERROR=0 SKIP=0 TOTAL=4" : "",
    approval: a
      ? { id: a.id, status: a.status, action: a.action, payload_hash: a.payload_hash, plan_hash: a.plan_hash, policy_version: a.policy_version,
        risk_tier: a.risk_tier, requested_by: a.requested_by, decided_by: a.decided_by, decided_at: a.decided_at, reason: a.reason, expires_at: a.expires_at }
      : { id: "apr_b0", status: "executed", action: "elt_build", payload_hash: "a0b0c0d0e0f00102", plan_hash: "a1b2c3d4e5f6", policy_version: 2,
        risk_tier: "high", requested_by: USER.id, decided_by: "usr_approver", decided_at: "2026-09-18T09:02:00Z", reason: null,
        expires_at: "2026-09-19T09:00:00Z" },
  };
}

const DIFF_NEW: BuildDiff = {
  job_id: BUILD_NEW, project_hash: `${BUILD_NEW}0a1b2c3d4e5f60718293a4b5c6d7e8f9`, basis: "previous_job_same_target", identical: false,
  against: { job_id: BUILD_PREV, status: "succeeded", project_hash: JOB_PREV.project_hash, created_at: JOB_PREV.created_at },
  summary: { added: 1, removed: 0, modified: 2, unchanged: 1, lines_added: 9, lines_removed: 1 },
  files: [
    { path: "dbt_project.yml", status: "unchanged", lines_added: 0, lines_removed: 0, diff: "", truncated: false },
    { path: "models/p1_incidents.sql", status: "modified", lines_added: 2, lines_removed: 1, truncated: false,
      diff: "--- a/models/p1_incidents.sql\n+++ b/models/p1_incidents.sql\n@@ -1,2 +1,3 @@\n-select sys_id, priority, assignment_group, opened_at\n"
        + "+select sys_id, priority, assignment_group, opened_at, resolved_at\n from {{ source('stg_sn', 'incident') }}\n+where priority = '1'" },
    { path: "models/schema.yml", status: "modified", lines_added: 3, lines_removed: 0, truncated: false,
      diff: "--- a/models/schema.yml\n+++ b/models/schema.yml\n@@ -1,3 +1,6 @@\n version: 2\n models:\n   - name: p1_incidents\n"
        + "+    columns:\n+      - name: sys_id\n+        tests: [not_null, unique]" },
    { path: "models/semantic.yml", status: "added", lines_added: 4, lines_removed: 0, truncated: false,
      diff: "--- /dev/null\n+++ b/models/semantic.yml\n@@ -0,0 +1,4 @@\n+semantic_models:\n+  - name: p1_incidents\n+metrics:\n+  - name: mttr_hours" },
  ],
};

const DIFF_PREV: BuildDiff = {
  job_id: BUILD_PREV, project_hash: JOB_PREV.project_hash, basis: "none", identical: false, against: null,
  summary: { added: 3, removed: 0, modified: 0, unchanged: 0, lines_added: 7, lines_removed: 0 },
  files: Object.entries(FILES_V1).map(([path, text]) => ({
    path, status: "added" as const, lines_added: text.split("\n").length - 1, lines_removed: 0, truncated: false,
    diff: ["--- /dev/null", `+++ b/${path}`, ...text.trimEnd().split("\n").map((l) => `+${l}`)].join("\n"),
  })),
};

const semMetric = (name: string, version: number, status: string, expression: string, over: Partial<SemanticMetric> = {}): SemanticMetric => ({
  id: `smet_${name}_${version}`, workspace_id: WS, name, version, status, expression, normalized_expression: expression.toLowerCase(),
  definition: { name, expressions: [{ dialect: "ANSI_SQL", expression }], description: `${name.replace(/_/g, " ")} for P1 incidents`, format: "hours",
    grain: "week", dimensions: ["assignment_group"], filters: [] },
  display_name: name.replace(/_/g, " "), owner_id: "usr_analyst", proposed_by: "usr_analyst", proposed_via: "user", run_id: null,
  approval_id: status === "proposed" ? `apr_${name}_${version}` : null, decided_by: status === "approved" ? USER.id : null,
  decided_at: status === "approved" ? T : null, reason: null, content_hash: `c0ffee${version}${name.length}abcdef`, created_at: T, updated_at: T, ...over,
});

function kpiRows(): SemanticMetric[] {
  const base = [
    semMetric("mttr_hours", 1, "approved", "AVG(resolution_hours)"),
    semMetric("mttr_hours", 2, "proposed", "AVG(resolution_hours) FILTER (WHERE priority = '1')"),
    semMetric("reopen_rate", 1, "proposed", "AVG(CASE WHEN reopen_count > 0 THEN 1.0 ELSE 0 END)"),
  ];
  return [...base, ...state.proposed].map((m) => (state.decided[m.id] ? { ...m, status: state.decided[m.id], decided_by: USER.id, decided_at: T } : m));
}

/** The mock's stand-in for the server's KPI checks: the name pattern, one aggregate, no subquery, conflicts. */
function validateKpi(body: { name?: unknown; expression?: unknown }): MetricValidation {
  const name = String(body.name ?? "");
  const expr = String(body.expression ?? "").trim();
  const fail = (field: string, message: string): MetricValidation =>
    ({ ok: false, problems: [{ field, message }], conflicts: [], normalized_expression: null, existing: null });
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return fail("name", "use letters, digits and underscores, starting with a letter or underscore (max 120)");
  if (!expr) return fail("expression", "an expression is required");
  if (/\bselect\b/i.test(expr)) return fail("expression", "subqueries are not allowed in metric expressions");
  if (!/\b(count|sum|avg|min|max|percentile_cont)\s*\(/i.test(expr)) return fail("expression", "not an aggregate expression");
  const live = kpiRows().filter((m) => m.status === "approved" || m.status === "proposed");
  const norm = expr.toLowerCase();
  const dup = [...new Set(live.filter((m) => m.normalized_expression === norm && m.name !== name).map((m) => m.name))];
  const competing = live.filter((m) => m.name === name && m.normalized_expression !== norm);
  const same = live.find((m) => m.name === name && m.normalized_expression === norm);
  return {
    ok: true, problems: [], normalized_expression: norm, existing: same ? { version: same.version, status: same.status } : null,
    conflicts: [
      ...(dup.length ? [{ kind: "duplicate_expression" as const, names: [name, ...dup],
        detail: `${dup.join(", ")} already compute this expression; reuse that name or keep one` }] : []),
      ...(competing.length ? [{ kind: "conflicting_definition" as const, names: [name],
        detail: `${name} already has ${competing.length} live definition(s); approving this one deprecates the approved version` }] : []),
    ],
  };
}

/** Build jobs and targets, the job diff, the semantic layer (KPI editor) and dashboard publication. */
function buildRoute(m: string, p: string, requestBody?: string | null): MockResponse | null {
  const W = `/workspaces/${WS}`;
  const body = requestBody ? JSON.parse(requestBody) as Record<string, unknown> : {};
  if (m === "GET" && p === `${W}/build-targets`) return json([BUILD_TARGET]);
  if (m === "POST" && p === `${W}/builds`) {
    if (body.target_schema !== BUILD_TARGET.schema_name) {
      return json({ error: { code: "forbidden", message: `schema ${String(body.target_schema)} is not a designated build target`, details: {} } }, 403);
    }
    state.planned = true;
    return json({ run_id: `run_${BUILD_NEW}`, status: "PENDING", playbook: "playbook.elt_build" });
  }
  if (m === "GET" && p === `${W}/builds`) return json(state.planned ? [jobNew(), JOB_PREV] : [JOB_PREV]);
  if (m === "GET" && p === `/builds/${BUILD_PREV}`) return json(buildDetail(JOB_PREV, FILES_V1));
  if (m === "GET" && p === `/builds/${BUILD_PREV}/diff`) return json(DIFF_PREV);
  if (state.planned && m === "GET" && p === `/builds/${BUILD_NEW}`) return json(buildDetail(jobNew(), FILES_V2));
  if (state.planned && m === "GET" && p === `/builds/${BUILD_NEW}/diff`) return json(DIFF_NEW);
  const decided = /^\/approvals\/([^/]+)\/(approve|reject)$/.exec(p);
  if (m === "POST" && decided && decided[1] === BUILD_APPROVAL && state.planned) {
    state.buildApproval = decided[2] === "approve" ? "approved" : "rejected";
    return json({ ...buildApproval(), status: state.buildApproval, payload: undefined });
  }
  if (m === "GET" && p === `${W}/semantic`) {
    const rows = kpiRows();
    const latest = new Map<string, SemanticMetric>();
    for (const r of rows) latest.set(r.name, r);
    return json({ model: { name: "itsm" }, metrics: [...latest.values()], approved: rows.filter((r) => r.status === "approved").map((r) => r.name),
      conflicts: [{ kind: "conflicting_definition", names: ["mttr_hours"], detail: "mttr_hours has 2 competing definitions; approve one, reject the others" }],
      ossie_version: "0.1.1" });
  }
  if (m === "POST" && p === `${W}/semantic/metrics/validate`) return json(validateKpi(body));
  if (m === "POST" && p === `${W}/semantic/metrics`) {
    const v = validateKpi(body);
    if (!v.ok) {
      return json({ error: { code: "invalid_input", message: `metric ${String(body.name)}: ${v.problems[0].message}`, details: { problems: v.problems } } }, 422);
    }
    const name = String(body.name);
    const version = kpiRows().filter((r) => r.name === name).length + 1;
    const row = semMetric(name, version, "proposed", String(body.expression), { proposed_by: USER.id, owner_id: USER.id,
      display_name: (body.display_name as string | null) ?? null });
    state.proposed.push(row);
    return json({ metric: row, created: true, conflicts: v.conflicts });
  }
  const kpi = new RegExp(`^${W}/semantic/metrics/([^/]+?)(/approve|/reject)?$`).exec(p);
  if (kpi && m === "GET" && !kpi[2]) return json(kpiRows().filter((r) => r.name === decodeURIComponent(kpi[1])));
  if (kpi && m === "POST" && kpi[2]) {
    const pending = kpiRows().filter((r) => r.name === decodeURIComponent(kpi[1]) && r.status === "proposed");
    const row = pending.find((r) => r.version === body.version) ?? pending[pending.length - 1];
    if (!row) return json({ error: { code: "conflict", message: "no pending proposal", details: {} } }, 409);
    if (row.proposed_by === USER.id) {
      return json({ error: { code: "forbidden", message: "separation of duties: the proposer of a metric cannot approve or reject it", details: {} } }, 403);
    }
    state.decided[row.id] = kpi[2] === "/approve" ? "approved" : "rejected";
    return json({ ...row, status: state.decided[row.id], decided_by: USER.id, decided_at: T });
  }
  const art = /^\/artifacts\/(art_dash|art_chart)$/.exec(p);
  if (m === "GET" && art) return json(artifactDetail(ARTIFACTS.find((a) => a.id === art[1])!));
  if (m === "POST" && p === "/artifacts/art_dash/publish") return json({ ...APPROVAL, payload: undefined });
  return collectionsRoute(m, p, requestBody);
}

/** BI-VIEWS-02: the demo run's dashboards published one collection at a time; existing BI dashboards grouped by name. */
const collections = { requests: [] as { approval_id: string; status: string; collection: string[]; publication: { id: string; status: string; error: null } | null }[],
  renamed: false, grouping: null as string | null };

export function resetCollections(): void {
  collections.requests = [];
  collections.renamed = false;
  collections.grouping = null;
}

/** Tests flip a request's status the way an approver's decision in the inbox would. */
export function decideCollection(status: string): void {
  for (const r of collections.requests) r.status = status;
  if (collections.grouping) collections.grouping = status;
}

const GROUPING_OLD = "Executive overview: Why are P1 incidents slow?";

function collectionsRoute(m: string, p: string, requestBody?: string | null): MockResponse | null {
  const W = `/v2/workspaces/${WS}`;
  const keys = (): string[] => requestBody ? [...(JSON.parse(requestBody) as { keys: string[] }).keys].sort() : [];
  if (m === "GET" && p === `/v2/runs/${RUN}/collections`) {
    return json({ run_id: RUN, workspace_id: WS, destination: "superset", destination_note: null, dashboards: [
      { key: "P1 resolution", title: "P1 resolution (weekly)", kind: "operational", audience: "operational", charts: 1,
        artifact_id: "art_dash", status: collections.requests.some((r) => r.status === "executed") ? "published" : "draft",
        external_url: collections.requests.some((r) => r.status === "executed") ? "http://localhost:8088/superset/dashboard/41/" : null },
      { key: "topic_delivery", title: "Demo / Delivery reliability", kind: "topic", audience: "operational", charts: 2,
        artifact_id: "art_topic", status: "draft", external_url: null },
    ], requests: collections.requests.map((r) => ({ ...r, destination: "superset", payload_hash: `hash-${r.collection.join("+")}`,
      risk_tier: "high", created_at: T, expires_at: "2099-01-01T00:00:00Z", reason: null })) });
  }
  if (m === "POST" && p === `/v2/runs/${RUN}/collections/publish-request`) {
    const collection = keys();
    const approval_id = `apr_col_${collections.requests.length + 1}`;
    collections.requests.unshift({ approval_id, status: "pending", collection, publication: null });
    return json({ approval_id, status: "pending", risk_tier: "high", destination: "superset", payload_hash: `hash-${collection.join("+")}`,
      collection, expires_at: "2099-01-01T00:00:00Z" }, 202);
  }
  if (m === "POST" && p === "/v2/collections/publish") {
    const id = requestBody ? (JSON.parse(requestBody) as { approval_id: string }).approval_id : "";
    const r = collections.requests.find((x) => x.approval_id === id);
    if (!r || r.status !== "approved") {
      return json({ error: { code: "approval_required", message: `approval is ${r?.status ?? "missing"}`, details: {} } }, 409);
    }
    r.status = "executed";
    r.publication = { id: "pub_col_1", status: "succeeded", error: null };
    return json({ status: "published", publication_id: "pub_col_1", collection: r.collection, destination: "superset",
      urls: Object.fromEntries(r.collection.map((k, i) => [k, `http://localhost:8088/superset/dashboard/${41 + i}/`])) });
  }
  const change = { id: 16, slug: "aos-ws-demo-executive", from: GROUPING_OLD, to: `Demo / ${GROUPING_OLD}` };
  if (m === "GET" && p === `${W}/dashboard-grouping`) {
    return json({ workspace_id: WS, group: "Demo", separator: " / ", available: true, reason: null, to_change: collections.renamed ? 0 : 1,
      dashboards: [
        { ...change, from: collections.renamed ? change.to : change.from, url: "http://localhost:8088/superset/dashboard/16/", published: true, grouped: collections.renamed },
        { id: 17, slug: "aos-ws-demo-operational", from: "Demo / Operations deep-dive", to: "Demo / Operations deep-dive",
          url: "http://localhost:8088/superset/dashboard/17/", published: true, grouped: true },
      ] });
  }
  if (m === "POST" && p === `${W}/dashboard-grouping/request`) {
    collections.grouping = "pending";
    return json({ approval_id: "apr_group", status: "pending", risk_tier: "medium", payload_hash: "hash-group", changes: [change],
      expires_at: "2099-01-01T00:00:00Z" }, 202);
  }
  if (m === "POST" && p === `${W}/dashboard-grouping/apply`) {
    if (collections.grouping !== "approved") {
      return json({ error: { code: "approval_required", message: `approval is ${collections.grouping ?? "missing"}`, details: {} } }, 409);
    }
    collections.grouping = "executed";
    collections.renamed = true;
    return json({ status: "applied", approval_id: "apr_group", renamed: [change] });
  }
  return null;
}

/** The open grouping request as the approvals list shows it (with its payload). */
export function groupingApproval(): Approval | null {
  if (!collections.grouping) return null;
  return { ...APPROVAL, id: "apr_group", run_id: null, action: "dashboard.grouping_update", risk_tier: "medium",
    status: collections.grouping, payload_hash: "hash-group", evidence: {},
    payload: { workspace_id: WS, destination: "superset", group: "Demo",
      changes: [{ id: 16, slug: "aos-ws-demo-executive", from: GROUPING_OLD, to: `Demo / ${GROUPING_OLD}` }] } };
}

const SETTINGS: PlatformSettings = {
  llm: {
    purpose_modes: { planning: "auto" }, routing_overrides: {}, profile_models: {}, disabled_models: [], cache_enabled: true, cache_ttl_hours: 168,
    cacheable_purposes: ["planning"], max_prompt_tokens: 16000, downgrade_below_budget_fraction: 0.25, compact_prompts: true,
    catalog_max_columns_per_table: 30, catalog_max_tables: 12,
  },
  analysis: { min_sample_size: 100 }, crawl: { default_mode: "incremental" }, monitors: { default_z_threshold: 3 },
  sources: { enabled_kinds: [], allow_pushdown: true, staged_max_rows: 1000000 }, features: { reports: true },
};

const MODELS: ModelsView = {
  allowlist: ["openrouter/auto"], profiles: { chat: { models: ["openrouter/auto"], provider: "openrouter", temperature: 0, max_tokens: 2000,
    timeout_seconds: 60, exclude_families: [] } },
  routing: { planning: "chat" }, providers: { openrouter: { kind: "chat", base_url: "https://openrouter.ai/api/v1" } }, available: { planning: true },
  effective: { planning: { profile: "chat", models: ["openrouter/auto"], mode: "auto", available: true, deterministic_path: true, decision_model: false } },
};

/** No key in the API process, a credit cooldown after HTTP 402, and today's spend near the daily cap. */
export const MODEL_HEALTH: ModelHealth = {
  checked_at: T, counters_available: true,
  spend_today: { usd: 1.72, cap_usd: 2, source: "counter", fraction: 0.86, alert_fraction: 0.8, resets_at: "2026-09-27T00:00:00+00:00" },
  providers: [{
    provider: "openrouter", type: "openrouter", kind: "chat", base_url: "https://openrouter.ai/api/v1", api_key_env: "OPENROUTER_API_KEY",
    key_required: true, key_present: false, last_success_at: T, last_error: { at: T, model: "openai/gpt-5.4-mini", error: "HTTP 402: insufficient credits" },
    cooldown: { remaining_seconds: 42, reason: "model provider refused for credits HTTP 402" }, spend_today_usd: 1.72, calls_today: 58,
    message: "No API key in this process — set OPENROUTER_API_KEY for the api and worker containers and restart them.",
  }],
};

const MONITOR: Monitor = {
  id: "mon_1", workspace_id: WS, name: "P1 MTTR", kind: "metric_threshold", config: { metric: "mttr_hours", op: ">", value: 8 }, enabled: true,
  auto_investigate: false, state: "ok", last_evaluated_at: T, last_result: {}, created_by: USER.id, created_at: T,
};

const ALERT: Alert = {
  id: "alr_1", workspace_id: WS, monitor_id: MONITOR.id, severity: "warning", title: "P1 MTTR above 8h", message: "MTTR 9.4h > 8h",
  data: { severity: "warning", triage: { p_material: 0.64, model: "typesafe/jev-1" } }, dedupe_key: "k", status: "open", investigation_run_id: null, acknowledged_by: null, created_at: T, resolved_at: null,
};

const SCHEDULE: Schedule = {
  id: "sch_1", workspace_id: WS, name: "Weekly re-analysis", kind: "reanalysis", cron: "0 7 * * 1", timezone: "UTC", config: {},
  enabled: true, owner_id: USER.id, next_run_at: "2026-09-28T07:00:00Z", last_run_at: null, created_at: T, recent_runs: [],
};

const CONSOLE: ConsoleData = { messages: [], tool_calls: [], model_calls: [], queries: [],
  cost: { usd: 0.0421, tokens: 18250, model_calls: 3, jev_calls: 1, failed_calls: 0, cache_hits: 2, deterministic_skips: 5, tokens_saved: 9100 } };

// ------------------------------------------------------------------------------------ capability registry
const cap = (m: Partial<CapabilitySummary> & Pick<CapabilitySummary, "id" | "kind" | "summary">): CapabilitySummary => ({
  version: "1.0.0", ref: `${m.id}@${m.version ?? "1.0.0"}`, source: "builtin", entry: null, determinism: "deterministic", side_effect: "none",
  cost_class: "free", certification: { status: "certified", evidence: "tests/unit" }, autonomous_ok: true, needs_approval: false, tags: [],
  enabled: null, ...m,
});

/** A plugin method installed from a Python entry point: the registry must show and run it with no UI change. */
export const PLUGIN_METHOD: CapabilityManifest = {
  apiVersion: "analystos/v1", kind: "Method", id: "method.acme_funnel", version: "0.3.0",
  summary: "Funnel conversion by segment with a chi-square test", entry: "python:acme_methods.funnel:Funnel",
  input_schema: {
    type: "object", required: ["asset", "steps"],
    properties: {
      asset: { type: "string", title: "Asset", description: "Table holding one row per event" },
      steps: { type: "array", items: { type: "string" }, minItems: 2, description: "Funnel steps, in order" },
      segment: { anyOf: [{ type: "string" }, { type: "null" }], default: null, description: "Optional column to compare" },
      alpha: { type: "number", minimum: 0, maximum: 0.5, default: 0.05 },
      window_days: { type: "integer", minimum: 1, default: 30 },
      method: { enum: ["chi_square", "fisher"], default: "chi_square" },
      include_nulls: { type: "boolean", default: false },
    },
  },
  output_schema: { $ref: "schemas/stat_result.json" }, determinism: "deterministic", side_effect: "read_source", cost_class: "query",
  permissions: ["scope:read"], requires: ["engine:sql"], certification: { status: "tested", evidence: "acme_methods/tests/test_funnel.py" },
  ui: { form: "auto", renderer: "renderer.stat_result" }, tags: ["statistical", "plugin"], spec: {}, source: "entrypoint:acme-methods",
};

export const FUNNEL_RESULT = {
  test: "chi_square", n: 1830, p_value: 0.0021, p_adjusted: 0.0042, effect_size: 0.12, effect_label: "cramers_v", supported: true,
  groups: [{ segment: "web", conversion: 0.31, n: 1200 }, { segment: "mobile", conversion: 0.22, n: 630 }], warnings: [],
};

export const CAPABILITIES: CapabilitySummary[] = [
  cap({ id: "playbook.investigate", kind: "Playbook", summary: "The v1 investigation: context → hypotheses → tests → verified findings" }),
  cap({ id: "agent.data_scientist", kind: "Agent", summary: "Tests hypotheses with compiled analysis specs", determinism: "seeded" }),
  cap({ id: "method.trend", kind: "Method", summary: "Volume or numeric outcome over time", side_effect: "read_source", cost_class: "query" }),
  cap({ id: PLUGIN_METHOD.id, kind: "Method", version: PLUGIN_METHOD.version, summary: PLUGIN_METHOD.summary, source: PLUGIN_METHOD.source!,
    entry: PLUGIN_METHOD.entry!, side_effect: "read_source", cost_class: "query", certification: { status: "tested", evidence: null },
    autonomous_ok: false, tags: ["statistical", "plugin"] }),
  cap({ id: "tool.sql_query", kind: "Tool", summary: "Governed SQL through the query gateway", side_effect: "read_source", cost_class: "query" }),
  cap({ id: "publisher.superset", kind: "Publisher", summary: "Publishes dashboards to Apache Superset", side_effect: "write_external",
    needs_approval: true, certification: { status: "tested" }, autonomous_ok: false }),
  cap({ id: "connector.servicenow", kind: "Connector", summary: "ServiceNow table API", side_effect: "read_source" }),
];

function capabilityList(workspace: string | null) {
  const enabledIds = new Set(["playbook.investigate", "agent.data_scientist", "method.trend", "tool.sql_query", "connector.servicenow"]);
  return { digest: "d1e2f3a4b5c6d7e8", capabilities: CAPABILITIES.map((c) => ({ ...c, enabled: workspace ? enabledIds.has(c.id) : null })) };
}

function manifestFor(id: string): CapabilityManifest | null {
  if (id === PLUGIN_METHOD.id) return PLUGIN_METHOD;
  const c = CAPABILITIES.find((x) => x.id === id);
  if (!c) return null;
  return { apiVersion: "analystos/v1", kind: c.kind, id: c.id, version: c.version, summary: c.summary, entry: c.entry, input_schema: {},
    output_schema: {}, determinism: c.determinism, side_effect: c.side_effect, cost_class: c.cost_class, permissions: [], requires: [],
    certification: c.certification, ui: { form: "auto", renderer: null }, tags: c.tags, spec: {}, source: c.source };
}

const AGENTS: AgentSpec[] = [{ id: "planner", version: "1.0", name: "Planner", description: "Plans analyses", capabilities: [], skills: [],
  tools: [], model_profile: "chat", phase: "plan", knowledge: { sections: ["glossary"], budget_chars: null },
  output_contract: [{ type: "narrative", schema: null }], enabled: true }];
const TOOLS: ToolSpec[] = [{ tool_id: "sql.query", name: "SQL query", version: "1.0", description: "Governed SQL", category: "data", risk: "low",
  approval_policy: "none", side_effects: "none", runtime: "python", min_role: "analyst", enabled: true }];
const SKILLS: SkillSpec[] = [{ id: "stats.mann_whitney", category: "statistics", description: "Two-sample test", tools: [], deterministic: true, enabled: true }];

const SAVINGS: TokenSavings = {
  days: 30,
  totals: { calls: 12, tokens_used: 40000, tokens_saved: 26000, cost_usd: 0.12, cache_hits: 4, deterministic_skips: 9, refused: 0, saved_share: 0.39 },
  by_purpose: { planning: { calls: 12, tokens_used: 40000, tokens_saved: 26000, cost_usd: 0.12, by_status: { ok: 3, cache_hit: 4, skipped: 9 } } },
};
const USAGE: Usage = { models: [], queries: [] };

export interface MockResponse {
  status: number;
  body: string;
  contentType: string;
}

const json = (body: unknown, status = 200): MockResponse => ({ status, body: JSON.stringify(body), contentType: "application/json" });

/** A fetch init's headers as a lower-cased record (for `If-Match` on revisioned edits). */
export function headersOf(init: RequestInit | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  new Headers(init?.headers ?? {}).forEach((v, k) => { out[k.toLowerCase()] = v; });
  return out;
}

/**
 * Route one request. `path` is the URL path after the host, with its query string; `headers` are
 * lower-cased (the revisioned wave-2 edits read `if-match`).
 */
export function mockBackend(method: string, path: string, requestBody?: string | null, headers: Record<string, string> = {}): MockResponse {
  const url = new URL(path, "http://mock.local");
  const p = url.pathname.replace(/^\/api/, "");
  const m = method.toUpperCase();
  const W = `/workspaces/${WS}`;
  const value = investigationValueRoute(m, p, requestBody, WS, RUN_ROW, headers);
  if (value) return json(value.body, value.status);
  const drafted = draftsRoute(m, p, requestBody, WS);
  if (drafted) return json(drafted.body, drafted.status);
  const grounding = groundingRoute(m, p, requestBody, WS, headers);
  if (grounding) return json(grounding.body, grounding.status);
  const reported = reportRoute(m, p, requestBody, WS);  // CONV-04 report documents
  if (reported) return { status: reported.status, body: JSON.stringify(reported.body), contentType: reported.contentType ?? "application/json" };
  const wave2 = wave2Route(m, p, url, WS, requestBody, headers);
  if (wave2) return wave2.status === 204 ? { status: 204, body: "", contentType: "application/json" } : json(wave2.body, wave2.status);

  // Goal API v2 run view (S06): the demo run was started without a goal, so no goal and no budget.
  if (m === "GET" && p === `/v2/runs/${RUN}`) {
    return json({ run_id: RUN, goal_id: null, workspace_id: WS, state: RUN_ROW.status, control: RUN_ROW.control,
      objective: RUN_ROW.objective, autonomy_level: RUN_ROW.autonomy_level, plan_version: RUN_ROW.plan_version,
      plan_hash: RUN_ROW.plan_hash, playbook: null, error: null, complexity: null, budget: null, limitations: null,
      steps: [], events_url: `/v2/runs/${RUN}/events`, created_at: RUN_ROW.created_at, finished_at: RUN_ROW.finished_at });
  }
  if (m === "POST" && p === "/auth/login") {
    const body = requestBody ? JSON.parse(requestBody) as { password?: string } : {};
    if (body.password !== "ChangeMe123!") return json({ error: { code: "unauthorized", message: "invalid credentials", details: {} } }, 401);
    return json({ access_token: "mock-token", token_type: "bearer", user: USER });
  }
  // playbook editor (S06-T6, expert tools in Work → Workflows): options and a template; nothing is drafted here
  if (m === "GET" && p === `${W}/playbook-editor`) {
    return json({ agents: [], checks: [], replan_reasons: [], fixtures: [{ name: "supervised", description: "A person approves the plan" }],
      schema: {}, template: "apiVersion: analystos/v1\nkind: Playbook\nid: playbook.my_review\nversion: 1.0.0\nspec:\n  steps: []\n" });
  }
  // capability registry: list (with enablement per workspace), manifest, enable toggle, reload, run
  if (m === "GET" && p === "/capabilities") return json(capabilityList(url.searchParams.get("workspace_id")));
  if (p === `${W}/work-modes` || p === `${W}/work-modes/preview`) {
    const requested = requestBody ? (JSON.parse(requestBody) as { modes: string[] }).modes : state.workModes;
    const previous = [...state.workModes];
    if (m === "PUT" && p === `${W}/work-modes`) state.workModes = [...requested];
    return json({ workspace_id: WS, current: m === "PUT" ? requested : previous, selected: requested,
      capabilities: [], note: "Modes configure shortcuts and playbooks; grants remain separate." });
  }
  const capId = /^\/capabilities\/([^/]+)$/.exec(p);
  if (m === "GET" && capId) {
    const man = manifestFor(decodeURIComponent(capId[1]));
    return man ? json(man) : json({ error: { code: "not_found", message: "capability not found", details: {} } }, 404);
  }
  const wsCap = new RegExp(`^${W}/capabilities/([^/]+)(/invoke)?$`).exec(p);
  if (wsCap && m === "PUT" && !wsCap[2]) {
    const body = requestBody ? JSON.parse(requestBody) as { enabled?: boolean } : {};
    return json({ capability_id: decodeURIComponent(wsCap[1]), enabled: !!body.enabled });
  }
  if (wsCap && m === "POST" && wsCap[2]) {
    return decodeURIComponent(wsCap[1]) === PLUGIN_METHOD.id
      ? json({ status: "ok", capability: PLUGIN_METHOD.id, side_effect: "read_source", result: FUNNEL_RESULT })
      : json({ error: { code: "invalid_input", message: "cannot be run on its own", details: { reason: "not_invocable" } } }, 422);
  }
  if (m === "POST" && p === "/admin/capabilities/reload") {
    return json({ digest: "d1e2f3a4b5c6d7e8", previous_digest: "d1e2f3a4b5c6d7e8", count: CAPABILITIES.length, problems: [] });
  }
  if (m === "GET" && p === `${W}/mcp/capabilities`) return json([]);
  if (m === "POST" && /^\/insights\/[^/]+\/outcome$/.test(p)) {
    const body = requestBody ? JSON.parse(requestBody) as { signal?: string } : {};
    return json({ insight: p.split("/")[2], signal: body.signal, labelled_decisions: 1 });
  }
  if (m === "POST" && p === `${W}/analysis/${RUN}/feedback`) {
    const body = requestBody ? JSON.parse(requestBody) as { kind?: string | null; text?: string } : {};
    if (body.kind === "question") {
      const answer = { mode: "saved_evidence", topic: "summary", recorded_at: T,
        paragraphs: ["One finding is currently verified. Network resolves P1 incidents 2.1x slower than the median group."],
        references: [{ id: INSIGHT, code: "F1", title: INSIGHT_ROW.title }] };
      const feedback_id = `fb_question_${state.conversation.length}`;
      state.conversation.push({ text: body.text, kind: "question", at: T, feedback_id, answer });
      return json({ kind: "question", classified_by: "user", consequential_p: null, feedback_id, answer });
    }
    return json({ kind: body.kind || "redirect", classified_by: body.kind ? "user" : "jev:typesafe/jev-1", consequential_p: 0.04,
      interpretation: body.kind === "reject_finding" ? undefined : { filters: [], focus: ["Network"], summary: "Focus on the Network group.", interpreted_by: "rule" },
      replan: { plan_version: 2, tasks_reset: [], tasks_removed: [], approvals_invalidated: [] }, feedback_id: "fb_2" });
  }
  if (m === "GET" && p === `${W}/approvals`) {
    const status = url.searchParams.get("status");
    const grouping = groupingApproval();
    const all = [...wave2Approvals(), ...(grouping ? [grouping] : []),
      ...(state.planned ? [buildApproval(), APPROVAL, APPROVAL_EXECUTED] : [APPROVAL, APPROVAL_EXECUTED])];
    return json(all.filter((a) => !status || a.status === status));
  }
  const wave = wave1Route(m, p, WS, requestBody);
  if (wave) return json(wave.body, wave.status);
  const asked = askRoute(m, p, requestBody);
  if (asked) return asked;
  const known = knowledgeRoute(m, p, url, requestBody, WS, kpiRows) ?? contextRoute(m, p, url, WS);
  if (known) {
    return known.contentType ? { status: known.status, body: String(known.body), contentType: known.contentType } : json(known.body, known.status);
  }
  const built = buildRoute(m, p, requestBody);
  if (built) return built;
  if (m === "GET" && p === `${W}/artifacts`) {
    const type = url.searchParams.get("type");
    const run = url.searchParams.get("run_id");
    return json([...draftArtifacts(WS), ...ARTIFACTS, ...reportArtifacts(WS)].filter((a) => (!type || a.type === type) && (!run || a.run_id === run)));
  }
  if (p.endsWith("/events")) {
    return { status: 200, body: `event: end\ndata: {"status":"COMPLETED"}\n\n`, contentType: "text/event-stream" };
  }
  const routes: [string, string, unknown][] = [
    ["GET", "/health", HEALTH],
    ["GET", "/auth/me", USER],
    ["GET", "/auth/providers", { password: true, oidc: { enabled: false, name: "SSO", login_url: null } }],
    ["GET", "/workspaces", [WORKSPACE]],
    ["GET", W, WORKSPACE],
    ["GET", `${W}/sources`, [SOURCE, FILE_SOURCE]],
    ["GET", `${W}/analysis`, [RUN_ROW]],
    ["GET", `${W}/analysis/${RUN}`, { ...RUN_DETAIL, instructions: [...RUN_DETAIL.instructions, ...state.conversation] }],
    ["GET", `${W}/analysis/${RUN}/console`, CONSOLE],
    ["GET", `${W}/insights`, [INSIGHT_ROW, INSIGHT_DRAFT, voidInsight(INSIGHT_ROW)]],
    ["GET", `/insights/${INSIGHT}`, INSIGHT_DETAIL],
    ["GET", `/insights/${INSIGHT_VOID_ID}`, { ...INSIGHT_DETAIL, ...voidInsight(INSIGHT_ROW), queries: [], experiments: [] }],
    ["GET", `${W}/alerts`, [ALERT]],
    ["GET", `${W}/monitors`, [MONITOR, ...mlMonitors()]],
    ["GET", `${W}/schedules`, [SCHEDULE]],
    ["GET", `${W}/catalog`, CATALOG],
    ["GET", `${W}/crawls`, []],
    ["GET", `${W}/assets`, []],
    ["GET", `${W}/relationships`, []],
    ["GET", `${W}/audit`, []],
    ["GET", "/source-kinds", KINDS],
    ["GET", "/notifications", []],
    ["POST", `${W}/ask`, ASK],
    ["POST", `${W}/analysis`, RUN_ROW],
    ["GET", "/agents", AGENTS],
    ["GET", "/tools", TOOLS],
    ["GET", "/skills", SKILLS],
    ["GET", "/admin/models", MODELS],
    ["GET", "/admin/models/health", MODEL_HEALTH],
    ["GET", "/admin/usage", USAGE],
    ["GET", "/admin/audit", []],
    ["GET", "/admin/prompts", []],
    ["GET", "/admin/token-savings", SAVINGS],
    ["GET", "/admin/settings", { version: 3, settings: SETTINGS, defaults: SETTINGS, presets: { balanced: { planning: "always" } }, schema: {} }],
    ["GET", "/admin/settings/history", [{ version: 3, note: "initial", created_by: USER.id, created_at: T }]],
    ["GET", `${W}/pilot-readiness`, { workspace_id: WS, workspace_name: WORKSPACE.name, verdict: "not_ready", missing: 1, failing: 0,
      generated_at: T, checks: [{ check: "owner.business", subject: "workspace", status: "missing", reason: "no named business owner",
        remediation: null, evidence: null }] }],
  ];
  for (const [rm, rp, body] of routes) if (rm === m && rp === p) return json(body);
  const notFound = json({ error: { code: "not_found", message: `mock: no route for ${m} ${p}`, details: {} } }, 404);
  // Unknown records are 404s, as on the server; other unlisted collection reads are empty lists.
  const foreignWorkspace = p.startsWith("/workspaces/") && !p.startsWith(W);
  const recordRead = /\/(ws|run|ins|art|sch|mon|alr|apr|src|crl|tsk)_[^/]*$/.test(p);
  if (m === "GET" && !foreignWorkspace && !recordRead) return json([]);
  return notFound;
}
