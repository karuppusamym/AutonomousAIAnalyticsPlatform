/**
 * Typed client for the AnalystOS FastAPI backend (src/analystos/api/routers/*.py).
 *
 * Every call sends `Authorization: Bearer <token>`. Errors come back as
 * {"error": {code, message, details, retryable}} and are raised as ApiError.
 */
import type { components, paths } from "./generated/openapi";
import { readSSE, type SSEMessage } from "./lib/sse";

export type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
export type Dict = Record<string, unknown>;

// ----------------------------------------------------------------------------------- response shapes
/*
 * Response shapes. The FastAPI routers return untyped dicts (no `response_model`), so the
 * OpenAPI schema says nothing about response bodies and these are maintained by hand against
 * the routers. Request bodies, paths and query parameters are generated (see "generated
 * contract" below); where a request type is named here it is an alias of the generated schema.
 */
export interface User {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
  active: boolean;
  attributes: Dict;
  created_at: string;
}

export interface UserSummary {
  id: string;
  email: string;
  name: string;
  is_admin: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

/** What the login screen offers (GET /api/auth/providers). */
export interface AuthProviders {
  password: boolean;
  oidc: { enabled: boolean; name: string; login_url: string | null };
}

export interface WorkspaceCounts {
  query: number;
  dataset: number;
  metric: number;
  chart: number;
  dashboard: number;
  runs: number;
  verified_insights: number;
  sources: number;
}

export interface Workspace {
  id: string;
  name: string;
  description: string;
  objective: string;
  autonomy_level: number;
  status: string;
  settings: Dict;
  policy_version: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  counts?: WorkspaceCounts;
  role?: string;
  owners?: Owners;
}

/** Named business and technical owner of a pilot workspace or source (P4-09): people, not platform roles. */
export type NamedOwner = Schemas["NamedOwner"];
export interface Owners {
  business?: NamedOwner;
  technical?: NamedOwner;
}
export type PilotCheck = Schemas["PilotCheck"];
export type PilotReadiness = Schemas["PilotReadiness"];

export interface Member {
  user_id: string;
  role: string;
  email: string;
  name: string;
}

export interface WorkspaceDetail extends Workspace {
  counts: WorkspaceCounts;
  role: string;
  policy: WorkspacePolicy;
  members: Member[];
}

export type WorkMode = "analysis" | "engineering" | "ml";
export interface WorkModesPlan {
  workspace_id: string;
  current: WorkMode[];
  selected: WorkMode[];
  capabilities: { capability_id: string; mode: WorkMode; enabled: boolean; available: boolean; changed: boolean }[];
  note: string;
}

export interface WorkspacePolicy {
  max_rows?: number;
  query_timeout_seconds?: number;
  max_queries_per_run?: number;
  run_token_budget?: number;
  run_cost_budget_usd?: number;
  workspace_monthly_cost_budget_usd?: number;
  allowed_models?: string[];
  allowed_providers?: string[];
  restricted_columns?: string[];
  pii_columns?: string[];
  pii_access?: "none" | "restricted" | "allowed";
  tool_denylist?: string[];
  publish_destinations?: string[];
  publish_requires_approval?: boolean;
  separation_of_duties?: boolean;
  approval_ttl_hours?: number;
  max_iterations?: number;
  alpha?: number;
  [k: string]: unknown;
}

export interface Source {
  id: string;
  workspace_id: string;
  kind: string;
  name: string;
  config: Dict;
  secret_ref: string | null;
  status: string;
  execution_mode: string;
  staging_schema: string | null;
  last_discovered_at: string | null;
  last_error: string | null;
  created_at: string;
  owners?: Owners;
}

export interface TopValue {
  value: unknown;
  count: number;
}

export interface ColumnProfile {
  null_rate?: number;
  distinct?: number;
  top_values?: TopValue[];
  min?: unknown;
  max?: unknown;
  mean?: number | null;
  references?: unknown;
  [k: string]: unknown;
}

export interface SourceColumn {
  id: number;
  asset_id: string;
  name: string;
  ordinal: number;
  data_type: string;
  semantic_type: string | null;
  nullable: boolean;
  is_key: boolean;
  business_name: string | null;
  description: string | null;
  tags: string[];
  profile: ColumnProfile;
}

export interface Asset {
  id: string;
  source_id: string;
  workspace_id: string;
  schema_name: string;
  name: string;
  source_name: string;
  kind: string;
  selected: boolean;
  row_count: number | null;
  freshness_at: string | null;
  business_name: string | null;
  description: string | null;
  stats: Dict;
  created_at: string;
  fq: string;
  columns: SourceColumn[];
}

export interface DiscoveredAsset {
  source_name: string;
  name: string;
  schema_name: string | null;
  kind: string;
  row_count: number | null;
  description: string | null;
  business_name: string | null;
  columns: { name: string; data_type: string }[];
}

export interface DiscoverResponse {
  assets: DiscoveredAsset[];
  test: Dict;
  crawl_id?: string;
  stats?: CrawlStats;
  changes?: CrawlChanges;
}

export interface Relationship {
  id: string;
  workspace_id: string;
  from_asset_id: string;
  from_column: string;
  to_asset_id: string;
  to_column: string;
  cardinality: string;
  confidence: number;
  validated: boolean;
  evidence: Dict;
  origin: string;
  from_asset: string | null;
  to_asset: string | null;
}

export interface RunTask {
  id: string;
  run_id: string;
  key: string;
  agent_id: string;
  title: string;
  status: string;
  depends_on: string[];
  input: Dict;
  output: Dict;
  attempts: number;
  plan_version: number;
  seq: number;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface HypothesisResult {
  test?: string | null;
  n?: number | null;
  p_value?: number | null;
  p_adjusted?: number | null;
  effect_size?: number | null;
  effect_label?: string | null;
  highlights?: Dict | null;
  groups?: Dict[] | null;
  warnings?: string[] | null;
  /** P8-15: how far a supported result clears its method's bars (`evidence/strength.py`). */
  strength?: FindingStrength | null;
}

/** P8-15 `contracts/evidence.py Strength`: weak when the effect is under 1.5x the method's minimum or q is within 10x of alpha. */
export interface FindingStrength {
  label: "weak" | "moderate" | "strong";
  effect?: number | null;
  effect_label?: string | null;
  threshold?: number | null;
  margin?: number | null;
  q?: number | null;
  alpha?: number | null;
  reasons?: string[];
}

/** P8-15 `contracts/evidence.py HoldoutCheck`: the claim locked, then tested once on rows the discovery never read. */
export interface HoldoutCheck {
  evaluated: boolean;
  reason?: string | null;
  partition?: string | null;
  claim?: Dict | null;
  claim_locked_at?: string | null;
  partition_accessed_at?: string | null;
  supported?: boolean | null;
  top?: string | null;
  direction?: string | null;
  test?: string | null;
  p_value?: number | null;
  p_one_sided?: number | null;
  alpha?: number | null;
  effect_size?: number | null;
  effect_label?: string | null;
  n?: number | null;
  contrast?: string[] | null;
  experiment_id?: string | null;
  confirmed?: boolean | null;
}

export interface Hypothesis {
  provenance?: { rationale?: string; priority_by?: string; mapping_receipts?: {id:string;term?:string; revision?:number; columns:string[]}[] };
  id: string;
  workspace_id: string;
  run_id: string;
  code: string;
  question: string;
  statement: string;
  spec: Dict;
  priority: string;
  priority_score: number;
  status: string;
  methods: string[];
  evidence: string[];
  confidence: number | null;
  conclusion: string | null;
  parent_id: string | null;
  iteration: number;
  origin: string;
  created_at: string;
  result: HypothesisResult | null;
  experiment_id: string | null;
  /** P8-15: the held-out confirmation of this hypothesis's finding, when one was considered. */
  holdout?: HoldoutCheck | null;
}

export interface EvidenceRef {
  type: string;
  id: string;
  label?: string;
}

export interface VerificationCheck {
  check: string;
  passed: boolean;
  detail: string;
  /** representative_population (staging/snapshots.py): how the analysed rows were chosen. */
  method?: string;
  sampling?: Dict;
  truncated?: boolean;
}

export interface Verification {
  reason?: { claim?: string; question?: string; required_evidence?: string[] };
  evaluate?: VerificationCheck[];
  verify?: {
    reproducible?: boolean;
    second_method?: Dict | null;
    independent_model?: { model?: string; review?: Dict; unavailable?: string } | null;
    jev?: { p_supports: number; model: string } | null;
    contradictions?: string[];
  };
  verified?: boolean;
  note?: string;
}

export interface Insight {
  id: string;
  workspace_id: string;
  run_id: string;
  hypothesis_id: string | null;
  code: string;
  title: string;
  finding: string;
  confidence: number;
  population_size: number;
  business_impact: Dict;
  caveats: string[];
  evidence: EvidenceRef[];
  verified: boolean;
  verification: Verification;
  status: string;
  narrative_source: string;
  created_at: string;
  /** The verification record's state (P7-01); a void one carries its cause. */
  verification_state?: VerificationState;
  /** Evidence state (P4-03): exploratory | confirmed | replicated | inconclusive | ... */
  validation?: string | null;
  /** P8-15: strength of a verified finding and its held-out confirmation record. */
  strength?: FindingStrength | null;
  holdout?: HoldoutCheck | null;
}

/** `evidence/verification.py state_of`: what the UI shows about a finding's verdict. */
export interface VerificationState {
  state: "ACTIVE" | "PENDING" | "VOID" | "SUPERSEDED" | "LEGACY" | null | string;
  badge: "verified" | "pending" | "void" | "superseded" | "legacy" | "failed" | "unverified" | string;
  record_id: string | null;
  verdict?: string | null;
  verifier?: string | null;
  fingerprint?: string | null;
  dependencies?: unknown[];
  created_at?: string | null;
  void: { kind: string | null; reason: string | null; at: string | null; detail?: unknown } | null;
  flags?: { by: string; reason: string; at: string }[];
  prior_verdicts?: Dict[];
}

export type WhyLinkState = "ok" | "changed" | "void" | "failed" | "broken" | "unknown" | "not_applicable";

/** One of the six links a displayed number resolves through (docs/10-architecture/03-evidence-model.md §8). */
export interface WhyLink {
  link: "fact" | "step" | "query_receipt" | "data_version" | "semantic_version" | "verdict" | string;
  state: WhyLinkState | string;
  reason?: string | null;
  detail?: Dict;
}

export interface WhyNumber {
  text: string;
  value: number | null;
  unit: string | null;
  state: WhyLinkState | string;
  links: WhyLink[];
}

/** GET /api/insights/{id}/why (evidence/why.py). */
export interface WhyResponse {
  subject: { type: string; id: string; code?: string; run_id?: string; title?: string; finding?: string; status?: string };
  verification_state: VerificationState;
  state: WhyLinkState | string;
  numbers: WhyNumber[];
}

/** POST /api/verification/{record_id}/reverify (evidence/reverify.py): a replay run to follow, or the new verdict. */
export interface ReverifyResult {
  record_id: string;
  subject_type: string;
  subject_id: string;
  status: "started" | "reverified" | string;
  run_id?: string;
  message?: string;
  [k: string]: unknown;
}

/** GET /api/workspaces/{ws}/analysis/{run}/why. */
export interface WhyRunResponse {
  run_id: string;
  findings: WhyResponse[];
  numbers: number;
  by_state: Record<string, number>;
}

/** A measured result a claim cites (contracts/citations.py, N-8): only these back verified numbers. */
export interface QuantitativeCitation {
  kind: "quantitative";
  id: string;
  label: string;
  query_id: string;
  result_hash: string | null;
  query_hash?: string | null;
  step?: number | null;
  fact_ids: string[];
  values: number[];
  verified: boolean;
}

/** A number as a document states it: never a verified metric. */
export interface DocumentNumber {
  text: string;
  value: number;
  unit: "percent" | "value";
  sentence: string;
  source: "document";
  verified: false;
}

/** A knowledge section a claim cites, with the receipts to check it was not edited. */
export interface DocumentCitation {
  kind: "document";
  id: string;
  document_id: string;
  path: string;
  anchor: string | null;
  heading: string | null;
  pack: string | null;
  section: string | null;
  document_sha256: string;
  section_sha256: string | null;
  excerpt: string;
  numbers: DocumentNumber[];
  trusted: boolean;
}

export interface NarrativeNumber {
  text: string;
  source: "quantitative" | "document" | "unbound";
  citation_id: string | null;
}

/** A document number that disagrees with a measured fact; measured data wins. */
export interface EvidenceConflict {
  document_citation_id: string;
  path: string;
  anchor: string | null;
  fact_id: string;
  metric: string;
  subject: string | null;
  document_text: string;
  document_value: number;
  measured_value: number;
  unit: "fraction" | "percent" | "value";
  relative_difference: number;
  sentence: string;
  resolution: "measured_data_wins";
}

/** GET /api/insights/{id}/citations and /api/ask/turns/{id}/citations (services/citations.py). */
export interface CitationsResponse {
  version: string;
  subject_type: "insight" | "ask_turn";
  subject_id: string;
  quantitative: QuantitativeCitation[];
  documents: DocumentCitation[];
  narrative_numbers: NarrativeNumber[];
  conflicts: EvidenceConflict[];
  summary: { quantitative: number; documents: number; conflicts: number; document_sourced_numbers: number; unbound_numbers: number };
  recorded: boolean;
}

export interface QueryExecution {
  id: string;
  workspace_id: string;
  source_id: string | null;
  run_id: string | null;
  task_id: string | null;
  actor: string;
  purpose: string;
  sql: string;
  executed_sql: string | null;
  fingerprint: string | null;
  status: string;
  rejected_reason: string | null;
  referenced_assets: string[];
  row_count: number;
  truncated: boolean;
  columns: string[];
  result_hash: string | null;
  result_preview?: unknown[];
  cache_hit: boolean;
  duration_ms: number;
  created_at: string;
}

export interface Experiment {
  id: string;
  hypothesis_id: string | null;
  run_id: string;
  method: string;
  params: Dict;
  result: Dict;
  query_ids: string[];
  role: string;
  created_at: string;
}

export interface LineageNode {
  type: string;
  id: string;
}

export interface LineageEdge {
  from: [string, string];
  relation: string;
  to: [string, string];
}

export interface Lineage {
  nodes: LineageNode[];
  edges: LineageEdge[];
}

/** Every edge one run recorded; `truncated` when the run has more edges than the route returns. */
export interface RunLineage extends Lineage {
  truncated: boolean;
}

export interface InsightDetail extends Insight {
  queries: QueryExecution[];
  experiments: Experiment[];
  lineage: Lineage;
}

/** Existing-dashboard mode (N-2, BI-011/012): a BI dashboard this workspace may import. */
export interface BiDashboardCandidate {
  id: number | string;
  title: string | null;
  slug: string | null;
  published: boolean;
  url: string;
  last_import: { id: string; version: number; status: string } | null;
}

export interface BiChartVerification {
  chart_id: number | string;
  name: string | null;
  viz_type: string | null;
  status: "verified" | "mismatch" | "governed_only" | "not_reproducible" | "unmapped" | "refused" | "filtered";
  reason: string | null;
  sql: string | null;
  comparison?: { compared: number; mismatches: { key: unknown[]; metric: string; governed: unknown; bi: unknown; delta: number | null }[] };
}

export interface BiDashboardFinding { id: string; kind: string; severity: "high" | "medium" | "low" | "info"; message: string }

export interface BiDashboardProposal {
  id: string;
  kind: string;
  title: string;
  write_back: boolean;
  platform: boolean;
  status: string;
}

export interface BiDashboardImport {
  id: string;
  destination: string;
  external_id: string;
  version: number;
  title: string;
  status: "verified" | "attention" | "unverified";
  drift: boolean;
  url?: string | null;
  verification?: BiChartVerification[];
  verification_counts: Record<string, number>;
  mapping?: { datasets: { dataset_id: string; name: string; assets: string[]; mapped: boolean; reason: string | null }[];
    metrics: { label: string; status: string; semantic_metric: string | null }[] };
  findings: BiDashboardFinding[];
  proposals: BiDashboardProposal[];
  applied: { approval_id: string; status: string; proposals: string[]; changed?: string[]; error?: string }[];
  created_at: string | null;
}

export interface Approval {
  id: string;
  workspace_id: string;
  run_id: string | null;
  action: string;
  risk_tier: string;
  destination: string | null;
  affected_assets: string[];
  payload_hash: string;
  plan_hash: string | null;
  policy_version: number;
  requested_by: string;
  status: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  expires_at: string;
  evidence: Dict;
  /** The proposal the approval binds to (list endpoint only; decisions return it without). */
  payload?: Dict;
  created_at: string;
}

export interface Publication {
  status?: string;
  urls?: Record<string, string>;
  external_ids?: Dict;
  publication_id?: string;
  reused?: boolean;
}

export interface RunSummary {
  summary_markdown?: string;
  summary_source?: string;
  verified_insights?: number;
  published?: boolean;
  publication?: Publication | null;
  cancel_outcome?: string;
  changes?: RunChanges | null;
  report_artifact_id?: string | null;
  [k: string]: unknown;
}

export interface Run {
  id: string;
  workspace_id: string;
  objective: string;
  status: string;
  autonomy_level: number;
  plan_version: number;
  plan_hash: string | null;
  policy_version: number;
  instructions: Dict[];
  constraints: Dict;
  control: string;
  requested_by: string;
  workflow_id: string | null;
  iteration: number;
  tokens: number;
  cost_usd: number;
  error: string | null;
  summary: RunSummary;
  origin?: RunOrigin | null;
  capabilities?: Dict;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDetail extends Run {
  scope: Dict;
  plan?: Dict;
  tasks: RunTask[];
  hypotheses: Hypothesis[];
  insights: Insight[];
  approvals: Approval[];
  /** N-14: purposes answered by rules because no model route was allowed (absent on older servers). */
  model_fallbacks?: ModelFallback[];
}

/** One purpose answered by a deterministic rung for want of an allowed model (`model.fallback` events, folded). */
export interface ModelFallback {
  purpose: string;
  cause: string;
  detail: string;
  rung: string;
  code: string;
  message: string;
  count: number;
  tasks: string[];
}

export interface RunEvent {
  id: number;
  type: string;
  payload: Dict;
  actor: string | null;
  created_at: string;
}

export interface SavedRunAnswer {
  mode: "saved_evidence";
  topic: string;
  paragraphs: string[];
  references: { id: string; code: string; title: string }[];
  recorded_at?: string;
}

export interface FeedbackResponse {
  kind: string;
  classified_by: string;
  consequential_p: number | null;
  note?: string;
  interpretation?: { filters: Dict[]; focus: string[]; summary: string; interpreted_by: string };
  replan?: { plan_version: number; tasks_reset: unknown; tasks_removed: unknown; approvals_invalidated: unknown };
  feedback_id: string;
  answer?: SavedRunAnswer;
}

export interface AgentMessage {
  id: number;
  run_id: string;
  task_id: string | null;
  agent_id: string;
  kind: string;
  content: string;
  data: Dict;
  created_at: string;
}

export interface ToolExecution {
  id: number;
  run_id: string | null;
  task_id: string | null;
  agent_id: string | null;
  tool_id: string;
  status: string;
  input: Dict;
  output: Dict;
  decision: Dict;
  latency_ms: number;
  error: string | null;
  created_at: string;
}

export interface ModelCall {
  id: number;
  run_id: string | null;
  task_id: string | null;
  agent_id: string | null;
  purpose: string;
  profile: string;
  provider: string;
  model: string;
  prompt_version: string | null;
  status: string;
  attempt: number;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  error: string | null;
  created_at: string;
  /** Execution-ladder rung that answered (P4-T01) and the context compiler's receipts (P4-T03). */
  answered_by?: string | null;
  context_receipts?: Dict[] | null;
  tokens_saved?: number;
}

export type AgentRunDetail = RunTask & { messages: AgentMessage[]; tool_calls: ToolExecution[]; model_calls: ModelCall[]; queries: QueryExecution[] };

export interface ConsoleData {
  messages: AgentMessage[];
  tool_calls: ToolExecution[];
  model_calls: ModelCall[];
  queries: QueryExecution[];
  cost: ConsoleCost;
}

/** Run cost block (analysis.py console). Rung counts arrive with the execution ladder (P4-T*); render when present. */
export interface ConsoleCost {
  usd: number;
  tokens: number;
  model_calls: number;
  jev_calls: number;
  failed_calls: number;
  cache_hits?: number;
  deterministic_skips?: number;
  tokens_saved?: number;
  by_rung?: Record<string, RungSpend> | null;
}

/** Spend on one rung of the deterministic-first ladder (spec v3 §4.1): L0 cache … L5 strong model. */
export interface RungSpend {
  cost_complete?: boolean;
  calls?: number;
  tokens_used?: number;
  tokens_saved?: number;
  cost_usd?: number;
}

export interface QueryResult {
  query_id: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  cache_hit?: boolean;
  fingerprint?: string;
  result_hash?: string;
  duration_ms?: number;
  referenced_assets?: string[];
  sql?: string;
}

export type ChartHint = { type?: string; x?: string | null; y?: string | null } | null;

export interface AskResponse {
  sql: string;
  explanation: string | null;
  chart: ChartHint;
  model: string | null;
  attempts: { sql: string; error: string }[];
  result: QueryResult;
  status?: string;
  answered_by?: string | null;
  route?: string | null;
}

// ----------------------------------------------------------------------------------- Ask threads (P4-U02, services/ask.py)
/** One plain-language step of an Ask, streamed while it runs. */
export interface AskStage {
  key: string;
  text: string;
  at_ms: number;
  turn_id?: string;
  data?: Dict;
}

/** The refusal kinds of services/ask.py REFUSALS: one state each, with a remedy. */
export type AskRefusalKind = "needs_input" | "clarify" | "sql_rejected" | "policy_denied" | "budget_exceeded" | "spend_cap" | "no_model"
  | "no_scope" | "timeout" | "unavailable" | "failed"
  // why no model could write the SQL: one kind per cause (services/ask.py MODEL_REFUSALS)
  | "mode_off" | "no_api_key" | "provider_cooldown" | "policy_blocked" | "residency_blocked" | "approval_required"
  | "model_budget" | "cap_reached" | "context_over_budget" | "invalid_output";

export interface AskRefusal {
  kind: AskRefusalKind | string;
  title: string;
  message: string;
  remedy: string;
  details: { missing?: { name: string; type?: string; values?: string[] }[]; verified_query?: Dict | null; parameters?: Dict; [k: string]: unknown };
  /** The business view of a refusal only an administrator can fix (no model turned on): plain words, no setup steps. */
  business?: { title: string; message: string; remedy: string };
}

export interface AskProvenanceAsset {
  asset: string;
  asset_id: string | null;
  business_name: string | null;
  source_id: string | null;
  source_name: string | null;
  source_kind: string | null;
  execution_mode: string | null;
  freshness_at: string | null;
  row_count: number | null;
}

/** A governed metric query (ADR-0019): names and literal values only; the compiler writes the SQL. */
export type SemanticQueryBody = Schemas["SemanticQuery"];

// ----------------------------------------------------------------------------------- what-if scenarios (N-9)
export type ValueBasis = "observed" | "simulated";
export interface LabelledValue { value: number | null; basis: ValueBasis }
export type ScenarioAdjustment = Schemas["ScenarioAdjustment"];
export type ScenarioSpecBody = Schemas["ScenarioSpec"];
export interface ScenarioCell {
  metric: string;
  observed: LabelledValue;
  simulated: LabelledValue;
  change: LabelledValue;
  change_pct: LabelledValue;
  adjusted_by: number[];
}
/** A recorded scenario: every number labelled; `publishable` is always false. */
export interface Scenario {
  id: string;
  workspace_id: string;
  name: string;
  scenario_version: string;
  label: "simulated";
  publishable: false;
  spec: ScenarioSpecBody;
  spec_hash: string;
  assumptions: string[];
  assumptions_hash: string;
  baseline: { basis: "observed"; query_id: string; sql_hash: string; result_hash: string | null; row_count: number; truncated: boolean;
    semantic: { model_version?: number; compiler_version?: string } };
  remeasured: { basis: "simulated"; query_id: string; row_count: number } | null;
  columns: string[];
  rows: { key: Record<string, unknown>; cells: ScenarioCell[] }[];
  totals: ScenarioCell[];
  summary: string;
  guard: { ok: boolean; problems: string[] };
  result_hash: string;
  ask_turn_id: string | null;
  created_by: string;
  created_at: string | null;
}

export interface AskProvenance {
  parent_turn_id?: string | null;
  governance?: "governed" | "ad_hoc";
  semantic?: { model_id: string; model_version: number; compiler_version: string;
    metrics: { id: string; name: string; version: number; hash: string }[]; query?: SemanticQueryBody } | null;
  assets?: AskProvenanceAsset[];
  answered_by?: string | null;
  verified_query?: { id: string; name: string; pattern?: string; score?: number } | null;
  model?: string | null;
  query_id?: string | null;
  cache_hit?: boolean;
  result_hash?: string | null;
  repairs?: number;
  /** Follow-up questions the Ask rules offer (e.g. the other groupings of a distribution). */
  suggestions?: string[];
  rules?: Dict;
  /** What a reader must know beside the numbers: an incomplete period, an approved metric not used. */
  notes?: string[];
  /** The period the question named, [start, end). */
  period?: { start: string; end: string; label: string } | null;
  /** What a follow-up added to the previous answer (PILOT-FIX-02). */
  followup?: { mode: "include" | "exclude"; column: string | null; values?: unknown[]; period?: { start: string; end: string; label: string } } | null;
}

export interface AskStaleness {
  state: "fresh" | "aging" | "stale" | "changed" | "unknown";
  label: string;
  data_as_of: string | null;
}

export interface AskDecisionSummary {
  id: string | null;
  purpose: string;
  backend: string;
  value: unknown;
  p?: number | null;
  model?: string | null;
  fallback_reason?: string | null;
  probabilities?: Record<string, number>;
  note?: string | null;
}

export interface AskPromotion {
  target: "verified_query" | "metric" | "monitor" | "dashboard" | "draft" | "investigate" | "report" | "schedule" | string;
  id: string;
  status: string;
  name?: string;
  approval_id?: string | null;
  /** While `status` is approval_required: the approval's current status (pending | approved | rejected | expired | ...). */
  approval_status?: string;
  destination?: string;
  dashboard?: string;
  url?: string | null;
  note?: string;
  expires_at?: string;
  cron?: string;
  value?: unknown;
  at?: string;
  [k: string]: unknown;
}

export interface AskTurn {
  evidence_status?: { state: "recorded" | "changed" | "ad_hoc"; reasons: string[] };
  id: string;
  thread_id: string;
  workspace_id: string;
  seq: number;
  question: string;
  parameters: Dict;
  status: "running" | "answered" | "needs_input" | "clarify" | "refused" | string;
  route: string | null;
  answered_by: string | null;
  refusal: AskRefusal | null;
  sql: string | null;
  explanation: string | null;
  chart: ChartHint;
  result: QueryResult | null;
  verified_query: Dict | null;
  model: string | null;
  attempts: { sql: string; error: string }[];
  stages: AskStage[];
  decisions: AskDecisionSummary[];
  provenance: AskProvenance;
  promotions: AskPromotion[];
  latency_ms: number;
  created_at: string;
  staleness: AskStaleness;
  /** Analyst mode only (null for a quick answer): the plan, the steps and the synthesis. */
  analysis?: AskAnalysis | null;
  /** What answering it cost, from the model-call receipts recorded against the turn (S11-T2). */
  cost?: AnswerCost;
}

/** Spend and model use of one answer: calls by ladder rung and model, cache hits, calls a rule or cache avoided. */
export interface AnswerCost {
  usd: number;
  model_calls: number;
  cache_hits: number;
  avoided: number;
  tokens_saved: number;
  rungs: string[];
  models: string[];
}

export type AskMode = "quick" | "analyst";

export interface AnalystMeasureFact {
  column: string; count: number; total?: number | null; avg?: number | null; min?: number | null; min_label?: string | null;
  max?: number | null; max_label?: string | null; pre_aggregated?: boolean;
}

export interface AnalystStep {
  n: number;
  kind?: string;
  goal: string;
  question: string;
  status: "answered" | "clarify" | "needs_input" | "refused" | "skipped" | string;
  answered_by?: string | null;
  governance?: string | null;
  sql?: string | null;
  explanation?: string | null;
  chart?: ChartHint;
  result?: QueryResult | null;
  facts?: { row_count?: number; truncated?: boolean; measures?: AnalystMeasureFact[]; statements?: string[]; [k: string]: unknown } | null;
  series?: {
    time_column: string; column: string; points: number; first_period?: string; last_period?: string; direction?: string;
    slope_per_period?: number | null; slope_share_of_mean?: number | null; anomalies?: { period: string; value: number; z: number; direction: string }[];
    missing_periods?: string[]; warnings?: string[];
  } | null;
  comparison?: { previous?: number | null; current?: number | null; change?: number | null; pct_change?: number | null;
    previous_period?: string; current_period?: string; [k: string]: unknown } | null;
  drivers?: {
    members: number;
    drivers: { member: string; previous: number; current: number; change: number; pct_change: number | null; share_of_change: number | null }[];
    offsets: { member: string; previous: number; current: number; change: number; pct_change: number | null; share_of_change: number | null }[];
    appeared?: string[]; disappeared?: string[];
  } | null;
  checks?: { code: string; status: "pass" | "suspect" | string; note: string }[];
  refusal?: AskRefusal | null;
  note?: string;
  rerun?: { at: string; edited: boolean } | null;
}

export interface AskAnalysis {
  mode: "analyst";
  plan: { approach: string; origin: "rules" | "model" | string; assumptions?: string[]; steps: { n: number; goal: string; question: string }[] };
  steps: AnalystStep[];
  synthesis: {
    text: string; answer?: string; evidence?: { step: number; text: string }[]; caveats?: string[]; citations?: number[];
    origin: "template" | "model" | string; rejected?: string | null; stale?: boolean; stale_steps?: number[];
  };
  follow_ups: string[];
  headline_step: number;
}

export interface SuggestedTable {
  asset_id: string; fq: string; name: string; business_name: string | null; role: string | null; entity: string | null; grain: string | null;
  primary_key: { columns: string[]; evidence: "approved" | "declared" | "measured_unique" | "profile_unique" | "none" | string; unique: boolean | null };
  time_column: string | null; measures: string[]; dimensions: string[]; confidence: number | null; issues: { code: string; message: string }[];
}

export interface SuggestedRelationship {
  from: { asset_id: string; fq: string; columns: string[] }; to: { asset_id: string; fq: string; columns: string[] };
  cardinality: string; status: "validated" | "pending" | "proposed" | string; confidence: number | null; evidence: Dict;
  candidate_id: string | null; relationship_id?: string | null;
}

export interface ModelSuggestion {
  generated_at: string;
  tables: SuggestedTable[];
  relationships: SuggestedRelationship[];
  metrics: { name: string; label: string; expression: string; table_fq: string; reason: string }[];
  star_schemas: { fact: string; dimensions: string[] }[];
  issues: { code: string; message: string; asset_id?: string | null }[];
  summary: { tables: number; facts: number; dimensions: number; relationships_validated: number; relationships_pending: number; keys_measured: number };
}

export interface ModelValidation {
  tables: { asset_id: string; rows: number; distinct_keys: number; unique: boolean }[];
  joins: { from: string; to: string; from_columns: string[]; to_columns: string[]; rows_before: number; rows_after: number; fans_out: boolean }[];
  queries: number;
  skipped: { kind: string; reason: string; [k: string]: unknown }[];
}

export interface AskThread {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  archived: boolean;
  created_at: string;
  updated_at: string;
  turn_count?: number;
  /** S04: an Ask thread is a v2 conversation; a branch names its parent and the message it continues after. */
  channel?: string;
  parent_conversation_id?: string | null;
  branch_from_message_id?: string | null;
  legal_hold?: boolean;
  closed_at?: string | null;
}

export interface AskThreadDetail extends AskThread {
  turns: AskTurn[];
}

// ----------------------------------------------------------------------------------- v2 conversations (S04, S11-T2)
/** GET /api/v2/conversations/{id} without messages (conversations/service.py `view`). */
export interface Conversation {
  id: string;
  workspace_id: string;
  tenant_id: string | null;
  channel: string;
  title: string;
  owner_id: string;
  participants: string[];
  request_id: string | null;
  parent_conversation_id: string | null;
  branch_from_message_id: string | null;
  legal_hold: boolean;
  archived: boolean;
  closed_at: string | null;
  summarized_at: string | null;
  created_at: string;
  updated_at: string;
  revision: number;
  state_version: number;
  message_count: number;
}

/** A message: the question and its answer (the Ask turn without the fields the message already names). */
export interface ConversationMessage {
  id: string;
  conversation_id: string;
  seq: number;
  /** Shown from the conversation this one branched from, up to the branch point. */
  inherited: boolean;
  author_id: string;
  text: string;
  mode: "ask" | "investigate";
  status: string;
  created_at: string;
  answer: Omit<AskTurn, "id" | "thread_id" | "question" | "user_id" | "seq" | "workspace_id" | "created_at">;
}

export interface ConversationDetail extends Conversation {
  messages: ConversationMessage[];
}

export interface ConversationBranch extends Conversation {
  restored_state: { conversation_id: string; version: number };
}

export interface StateFilter { column: string; op: string; value: unknown }

export type CorrectionScope = "session" | "personal" | "workspace_proposed";

export interface Clarification {
  term: string;
  term_key: string;
  meaning: string;
  correction_id: string;
  scope: CorrectionScope;
}

/** T1 session state, one immutable version per change (GET /api/v2/conversations/{id}/state). */
export interface ConversationState {
  conversation_id: string;
  version: number;
  at_seq: number;
  cause: "created" | "message" | "correction" | "edit" | "branch" | null;
  message_id: string | null;
  state: {
    filters: StateFilter[];
    sources: string[];
    metric_bindings: Record<string, string>;
    time_window: string | Dict | null;
    clarifications: Clarification[];
    result_handles: Dict[];
  };
}

export interface Correction {
  id: string;
  conversation_id: string | null;
  message_id: string | null;
  user_id: string;
  term: string;
  meaning: string;
  scope: CorrectionScope;
  /** session/personal: active | superseded | expired | revoked; team: proposed | approved | rejected | not_proposed */
  status: string;
  promoted_from: string | null;
  suggestion_id: string | null;
  expires_at: string | null;
  created_at: string | null;
}

/** What the next message uses: one meaning per term, session first, then personal, then the approved team one. */
export interface ConversationMemory {
  items: {
    term: string; term_key: string; meaning: string; correction_id: string | null; scope: "session" | "personal" | "workspace";
    /** A session item's scope as applied here: a "remember" or team proposal made in this conversation. */
    saved_as?: "session" | "personal" | "workspace_proposed";
  }[];
  overrides: { term: string; used: string; meaning: string; team_meaning: string; team_correction_id: string }[];
}

/** A `decision` row (decisions/store.py) recorded against the turn. */
export interface DecisionRow {
  id: string;
  purpose: string;
  authority: string;
  backend: string;
  model: string | null;
  answer: unknown;
  proposal: unknown;
  probabilities: Record<string, number>;
  confidence: number | null;
  fallback_reason: string | null;
  attempts: { backend: string; outcome: string; reason?: string | null }[];
  enforced: string[];
  subject: string | null;
  latency_ms: number;
  created_at: string;
}

export interface AskInspector {
  turn: AskTurn;
  decisions: DecisionRow[];
  model_calls: ModelCall[];
  query: QueryExecution | null;
  receipts: ContextReceipt[];
}

/**
 * One item the context compiler put in a prompt (P4-T03/K05). Pack sections carry their document,
 * path, anchor and hashes; `source` is `review:<origin>` for a document approved in the review queue.
 */
export interface ContextReceipt {
  id?: string;
  section?: string;
  kind?: string;
  name?: string;
  title?: string;
  source?: string;
  score?: number;
  trusted?: boolean;
  sha256?: string;
  document_id?: string;
  path?: string;
  anchor?: string;
  section_sha256?: string;
  rank?: number;
  via?: string;
  version?: number;
  [k: string]: unknown;
}

// ----------------------------------------------------------------------------------- knowledge studio (P4-U04)
export type KnowledgePackKind = "platform" | "workspace" | "imported";

export interface KnowledgePackInfo {
  id: string;
  kind: KnowledgePackKind;
  slug: string;
  title: string;
  read_only: boolean;
  /** True only for the workspace's own pack and an editor or owner. */
  writable: boolean;
  head_revision: number | null;
  okf_root: string;
  okf_version: string;
  git_remote: string | null;
  git_branch: string;
  origin: Dict;
  files: number;
  content_digest: string | null;
  updated_at: string;
}

export interface KnowledgeDocSummary {
  path: string;
  document_id: string;
  sha256: string;
  size: number;
  markdown: boolean;
  reserved: boolean;
  type: string | null;
  title: string;
  status?: string;
  trust_tier?: "unverified" | "machine-confirmed" | "human-reviewed" | string;
  stale?: boolean;
  kind?: string | null;
  /** `review_queue`: written by the review queue, which may replace it; `owner`: a person's content. */
  authorship?: "review_queue" | "owner";
  tags?: string[];
  problem?: string;
}

export interface KnowledgeDocList {
  pack_id: string;
  revision: number | null;
  documents: KnowledgeDocSummary[];
}

export interface VerifiedEntry {
  by: string;
  at?: string;
  [k: string]: unknown;
}

export interface KnowledgeTrust {
  tier: string;
  verified: VerifiedEntry[];
  status: string;
  stale_after: string | null;
  stale: boolean;
  trusted: boolean | null;
}

export interface KnowledgeDocument extends KnowledgeDocSummary {
  pack_id: string;
  revision: number | null;
  text: string;
  frontmatter: Dict | null;
  body: string | null;
  trust: KnowledgeTrust | null;
  sections: { anchor: string; heading: string }[];
  links: { raw: string; kind: string; target: string | null; exists: boolean }[];
}

export type KnowledgeSaveBody = Schemas["DocumentSaveIn"];

export interface KnowledgeSaveResult {
  changed: boolean;
  revision: number | null;
  document: KnowledgeDocument;
}

export interface KnowledgeRevisionInfo {
  number: number;
  parent: number | null;
  author: string;
  reason: string;
  origin: string;
  files: number;
  content_digest: string;
  created_at: string;
  added: string[];
  changed: string[];
  removed: string[];
  sha256: string | null;
  conformance_problems: number;
}

export interface SuggestionField {
  value: unknown;
  confidence: number;
  provenance: Dict;
  before?: unknown;
}

export interface KnowledgeSuggestion {
  id: string;
  kind: string;
  subject: string;
  title: string;
  path: string;
  fields: Record<string, SuggestionField>;
  confidence: number;
  origin: string;
  proposed_by: string;
  batch: string | null;
  status: "pending" | "approved" | "rejected" | "superseded";
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  revision: number | null;
  created_at: string;
}

export type ReviewDecisionBody = Schemas["ReviewDecision"];

export interface ReviewResult {
  revision: number | null;
  approved: { id: string; path: string | null; catalog?: string }[];
  rejected: { id: string; path: string | null; catalog?: string }[];
  errors: { id: string; error: string; [k: string]: unknown }[];
}

/** Stream D: the workspace context as one download (OKF zip, versioned JSON, or one Markdown file). */
export type ContextExportFormat = "okf" | "json" | "markdown";

export interface ContextPurpose {
  purpose: string;
  label: string;
  description: string;
}

export interface ContextPreviewSection {
  name: string;
  part: "stable" | "volatile";
  items: number;
  chars: number;
  no_match: boolean;
}

/** What one model purpose's prompt would carry (GET /context/preview); no model is called. */
export interface ContextPreview {
  purpose: string;
  label: string;
  question: string;
  scope: { assets: string[]; source_ids: string[]; denied_columns: number; source_id: string | null };
  system_prompt: string;
  system_text: string;
  preamble_text: string;
  volatile_text: string;
  stable_keys: string[];
  omitted: Dict[];
  no_match: string[];
  refused: string | null;
  trimmed: boolean;
  receipts: number;
  estimated_tokens: { stable: number; volatile: number; total: number };
  budget_chars: number;
  cache: { key: string | null; kind: string; hit: boolean; shared: boolean; enabled: boolean; ttl_seconds: number };
  knowledge_version: string | null;
  sections: ContextPreviewSection[];
}

export interface ContextCacheCounts {
  hits: number;
  misses: number;
  chars_reused: number;
}

/** The workspace's shared context cache (GET /context/cache, editor). */
export interface ContextCacheStats {
  workspace_id: string;
  shared: boolean;
  enabled: boolean;
  ttl_seconds: number;
  entries: Record<string, number>;
  total_entries: number;
  by_kind: Record<string, Record<string, ContextCacheCounts>>;
  totals: ContextCacheCounts;
}

/** `POST …/knowledge/glossary/scan`: what one glossary scan queued (Stream E). */
export interface GlossaryScanResult {
  glossary_terms: number;
  description_questions: number;
  candidates: number;
  by_rule: Record<string, number>;
  skipped_known: number;
  skipped_decided: number;
  assets: number;
  model: { called: boolean; filled: number; skipped?: string };
}

/** Pending review drafts per kind; `questions` = glossary terms and descriptions a person should answer. */
export interface SuggestionSummary {
  pending: Record<string, number>;
  questions: number;
  total: number;
}

export interface KnowledgeImportReport {
  pack_id: string;
  slug: string;
  format: "atlas" | "okf" | string;
  okf_root: string;
  revision: number | null;
  changed: boolean;
  files: number;
  documents: number;
  ignored: string[];
  conformance: { code: string; path: string; detail?: string }[];
  dangling_links: number;
  verified_claims: number;
  attested_computations: number;
  manifest: Dict;
  warnings: string[];
}

export type GraphNodeKind = "table" | "dataset" | "metric" | "document" | "suggestion";

export interface KnowledgeGraphNode {
  id: string;
  kind: GraphNodeKind;
  label: string;
  status?: string;
  path?: string;
  pack_id?: string;
  pack_kind?: KnowledgePackKind;
  trust_tier?: string;
  confidence?: number;
  suggestion_id?: string;
  [k: string]: unknown;
}

export interface KnowledgeGraphEdge {
  source: string;
  target: string;
  kind: string;
  /** Governed edges are drawn solid, inferred ones dashed. */
  governed: boolean;
  why: string;
  label: string;
}

export interface KnowledgeGraph {
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  truncated: boolean;
  governed: number;
  inferred: number;
}

export type AskPromoteBody = Schemas["AskPromoteIn"];

export interface AskStreamCallbacks {
  onStage: (s: AskStage) => void;
}

export interface Artifact {
  id: string;
  workspace_id: string;
  run_id: string | null;
  type: string;
  name: string;
  version: number;
  status: string;
  platform: string | null;
  external_id: string | null;
  external_url: string | null;
  creator_agent: string | null;
  creator_user: string | null;
  content: Dict;
  content_hash: string;
  /** S07-T7: set while a finding this published artifact presents is VOID; cleared once re-verified. */
  banner?: ArtifactBanner | null;
  created_at: string;
  updated_at: string;
}

export interface ArtifactBanner {
  state: string;
  reason: string;
  request_id: string | null;
  raised_at: string;
  findings: { insight_id: string; code: string | null; record_id: string; kind: string | null; reason: string | null }[];
}

export interface ArtifactVersion {
  id: number;
  artifact_id: string;
  version: number;
  content_hash: string;
  created_by: string | null;
  created_at: string;
}

export interface ArtifactDetail extends Artifact {
  versions: ArtifactVersion[];
  lineage: Lineage;
}

export interface AgentSpec {
  id: string;
  version: string;
  name: string;
  description: string;
  capabilities: string[];
  skills: string[];
  tools: string[];
  model_profile: string;
  phase: string;
  knowledge?: { sections: string[]; budget_chars: number | null } | null;
  output_contract?: { type: string; schema: string | Record<string, unknown> | null }[];
  enabled: boolean;
  [k: string]: unknown;
}

export interface ToolSpec {
  tool_id: string;
  name: string;
  version: string;
  description: string;
  category: string;
  risk: string;
  approval_policy: string;
  side_effects: string;
  runtime: string;
  min_role: string;
  enabled: boolean;
  [k: string]: unknown;
}

export interface SkillSpec {
  id: string;
  category: string;
  description: string;
  function?: string;
  runtime?: string;
  /** Legacy seeded skill records may omit this; the API fills it with []. */
  tools?: string[];
  deterministic: boolean;
  enabled: boolean;
  [k: string]: unknown;
}

export interface ModelProfile {
  models: string[];
  provider: string;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  exclude_families: string[];
  /** Cheap first, escalate: the large tier, called only after a failed deterministic check (or first under always_large). */
  escalation_models?: string[];
}

export type EscalationPolicy = "never" | "on_validation_failure" | "always_large";

export interface EffectiveRoute {
  profile: string;
  models: string[];
  mode: LLMMode;
  available: boolean;
  unavailable_reason?: string | null;
  unavailable_code?: string | null;
  /** A rule-based path exists: "off"/"auto" still produce a result without a model. */
  deterministic_path: boolean;
  decision_model: boolean;
  escalation?: EscalationPolicy;
  escalation_models?: string[];
}

/** GET /api/admin/models/health: never carries a key, only whether one is set in the server process. */
export interface ProviderHealth {
  provider: string;
  type: string;
  kind: string;
  base_url: string;
  api_key_env: string | null;
  key_required: boolean;
  key_present: boolean;
  last_success_at: string | null;
  last_error: { at: string; model: string; error: string } | null;
  cooldown: { remaining_seconds: number; reason: string } | null;
  spend_today_usd: number;
  calls_today: number;
  message: string | null;
  probe?: { probed: boolean; ok?: boolean; model?: string; latency_ms?: number; cost_usd?: number; code?: string; error?: string;
    detail?: string; remedy?: string };
}

/** GET /api/health: every dependency labelled up | down | off; the down ones listed with what that means for users. */
export interface HealthCheck {
  ok: boolean;
  state?: "up" | "down" | "off";
  error?: string;
  status?: string;
  [detail: string]: unknown;
}

export interface HealthProblem {
  dependency: string;
  severity: "critical" | "warning" | "info";
  message: string;
  detail?: string | null;
}

export interface Health {
  ok: boolean;
  degraded?: boolean;
  problems?: HealthProblem[];
  orchestrator?: string;
  checks?: Record<string, HealthCheck>;
}

export interface ModelHealth {
  checked_at: string;
  counters_available: boolean;
  /** The store that proves hard spend caps: Redis, or Postgres in the lite profile. False = billable calls are refused. */
  spend_counters?: { store: string; available: boolean };
  spend_today: { usd: number; cap_usd: number | null; source: "counter" | "database"; fraction: number | null;
    alert_fraction: number; resets_at: string };
  providers: ProviderHealth[];
}

export interface ModelsView {
  allowlist: string[];
  profiles: Record<string, ModelProfile>;
  routing: Record<string, string>;
  providers: Record<string, { kind: string; base_url: string }>;
  available: Record<string, boolean>;
  /** Families the owner blocked: never routed. */
  blocked_families?: string[];
  effective?: Record<string, EffectiveRoute>;
}

// ----------------------------------------------------------------------------- model gateway admin (S05)
export type ModelLayer = "file" | "platform" | "tenant" | "workspace";

/** GET /api/admin/models/effective: file ⊕ platform ⊕ tenant ⊕ workspace, each value with its source layer. */
export interface EffectiveModels {
  tenant_id: string;
  workspace_id: string | null;
  scope: "tenant" | "workspace";
  version: number;
  allowlist: string[];
  /** Families no route may use (owner decision): always shown as blocked. */
  blocked_families: string[];
  residency: string[] | null;
  budgets: { per_run_usd: number | null; per_day_usd: number | null; per_user_day_usd: number | null };
  purposes: Record<string, { ladder: string[]; mode: string; profile: string; provider: string; models: string[];
    rules_fallback: boolean; model_unavailable?: { purpose: string; reason: string } }>;
  profile_models: Record<string, string[]>;
  sources: Record<string, ModelLayer>;
  excluded_models: Record<string, { layer: ModelLayer; reason: string }>;
  violations: { layer: string; path: string; message: string }[];
  layers: { layer: string; version?: number | string; present?: boolean }[];
}

export interface ModelOverrideDoc {
  scope: "tenant" | "workspace";
  tenant_id: string;
  workspace_id: string | null;
  version: number;
  document: Dict;
  history: { version: number; note: string; created_by: string; created_at: string; document: Dict }[];
}

export interface ProviderView extends ProviderHealth {
  egress: "internet" | "internal";
  region: string | null;
  base_url_env: string | null;
  capabilities: { json_mode?: boolean; tools?: boolean; context_window?: number; vision?: boolean; prompt_cache?: boolean };
  models: string[] | null;
  profiles: string[];
  stats_24h: { calls: number; errors: number; error_rate: number | null; latency_ms_p50: number | null; latency_ms_p95: number | null };
}

export interface ProvidersView {
  checked_at: string;
  spend_today: ModelHealth["spend_today"];
  providers: ProviderView[];
}

export interface ProviderTest {
  provider: string;
  profile?: string;
  probed: boolean;
  ok?: boolean;
  model?: string;
  latency_ms?: number;
  cost_usd?: number;
  code?: string;
  error?: string;
  detail?: string;
}

export interface ModelEvaluation {
  id: string;
  suite: string;
  suite_version: number;
  purpose: string;
  profile: string;
  model: string;
  status: "scored" | "unavailable";
  score: number | null;
  cases: number;
  passed: number;
  cost_usd: number;
  created_at: string | null;
  reused: boolean;
  reason?: string;
}

export interface ModelEvaluations {
  suite_version: number;
  tolerance: number;
  min_score: number;
  suites: Record<string, number>;
  purpose_suites: Record<string, string>;
  default_suite: string;
  items: ModelEvaluation[];
}

/** The model-switch gate's verdicts (409 details.model_switch_gate when blocked). */
export interface SwitchGateResult {
  gated: boolean;
  blocked?: boolean;
  overridden?: boolean;
  tolerance?: number;
  evaluations: { purpose: string; profile: string; from: string | null; to: string; verdict: "passed" | "blocked";
    reason: string | null; new: ModelEvaluation; current: ModelEvaluation | null }[];
}

export interface AirgapPosture {
  air_gapped: boolean;
  ok: boolean;
  checks: { name: string; ok: boolean; detail: string; severity: "ok" | "warning" | "error" }[];
  purposes: Record<string, { mode: string; rules_fallback: boolean; served_locally: boolean; reason: string | null }>;
}

export interface Usage {
  models: { purpose: string; model: string; provider: string; calls: number; cost_usd: number; avg_latency_ms: number; failed: number; cost_complete?: boolean; missing_price_calls?: number }[];
  queries: { status: string; count: number; avg_ms: number }[];
}

export interface AuditEvent {
  id: number;
  workspace_id: string | null;
  run_id: string | null;
  actor: string;
  action: string;
  target: string | null;
  decision: string | null;
  reasons: string[];
  details: Dict;
  created_at: string;
}

// ----------------------------------------------------------------------------------- continuous (phase 3)
export type ScheduleKind = "reanalysis" | "dataset_refresh" | "report" | "monitor" | "crawl";
export type ReportKind = "executive" | "operational" | "statistical" | "exception" | "weekly_summary";
export type ReportFormat = "md" | "html" | "pdf" | "xlsx" | "pptx" | "docx";

export interface ReportSpec {
  kind?: ReportKind | string;
  formats?: (ReportFormat | string)[];
}

/** Kind-specific schedule config (see services/schedules.py). */
export interface ScheduleConfig {
  objective?: string;
  refresh_first?: boolean;
  publish?: "skip" | "propose";
  report?: ReportSpec | null;
  kind?: ReportKind | string;
  formats?: (ReportFormat | string)[];
  run_id?: string;
  monitor_ids?: string[];
  source_ids?: string[];
  mode?: "full" | "incremental";
  include?: string[];
  exclude?: string[];
  /** N-3: approved delivery destinations a produced report is sent to. */
  deliver_to?: string[];
  [k: string]: unknown;
}

export interface ScheduleRunResult {
  run_id?: string;
  previous_run_id?: string | null;
  report_artifact_id?: string | null;
  changes?: { new?: number; persisting?: number; changed?: number; resolved?: number };
  /** crawl schedules: per source id, the crawl id and headline counts, or an error. */
  crawls?: Record<string, { crawl_id?: string; new?: number; changed?: number; deprecated?: number; profiled?: number; error?: string }>;
  [k: string]: unknown;
}

export interface ScheduleRun {
  id: string;
  schedule_id: string;
  workspace_id: string;
  fire_key: string;
  scheduled_for: string;
  trigger: string;
  status: string;
  result: ScheduleRunResult;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Schedule {
  id: string;
  workspace_id: string;
  name: string;
  kind: ScheduleKind | string;
  cron: string;
  timezone: string;
  config: ScheduleConfig;
  enabled: boolean;
  owner_id: string;
  next_run_at: string | null;
  last_run_at: string | null;
  created_at: string;
  recent_runs?: ScheduleRun[];
  revision?: number;
  pins?: Dict | null;
}

/** An external email/webhook target (N-3): sends only while an approval bound to its hash stands. */
export interface DeliveryDestination {
  id: string;
  workspace_id: string;
  name: string;
  kind: "email" | "webhook" | string;
  /** The secret reference is not returned; `has_secret` says whether a webhook has one. */
  config: { recipients?: string[]; attach_formats?: string[]; url?: string };
  has_secret?: boolean;
  content_kinds: string[];
  destination_hash: string;
  status: "pending" | "authorized" | "rejected" | "revoked" | "lapsed" | string;
  approval_id: string | null;
  authorized_until: string | null;
  authorized: boolean;
  summary: string;
  approval: { id: string; status: string } | null;
  revision: number;
}

export interface Delivery {
  id: string;
  destination_id: string;
  subject_type: "report" | "alert" | string;
  subject_id: string;
  status: "queued" | "sending" | "retrying" | "delivered" | "dead_letter" | "refused" | string;
  attempts: number;
  max_attempts: number;
  next_attempt_at: string | null;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export type PinState = "current" | "upgrade_available" | "deprecated" | "blocked" | "unpinned";

/** One pinned dependency of a schedule against what is current now (contracts/definition.py PinItem). */
export interface PinItem {
  type: "definition" | "capability" | "method" | "metric" | "semantic_model" | string;
  id: string;
  pinned: string | null;
  current: string | null;
  state: "current" | "newer" | "deprecated" | "retired" | "rejected" | string;
  reason: string | null;
  diff: { path: string; from: unknown; to: unknown }[];
}

export interface PinStatus {
  state: PinState | string;
  revision: number;
  items: PinItem[];
  warnings: string[];
  blocking: string[];
  upgrade_hash: string | null;
  checked_at: string | null;
}

/** GET /api/workspaces/{ws}/schedules/{id}: the schedule with its pin status computed now. */
export interface ScheduleDetail extends Schedule {
  revision: number;
  pin_status: PinStatus;
}

export interface ScheduleUpgradeResult {
  schedule_id: string;
  revision: number;
  pin_revision: number;
  status: PinStatus;
  added: string[];
  removed: string[];
}

// ----------------------------------------------------------------------------------- definitions (P7-03)
export type DefinitionStatus = "draft" | "published" | "deprecated" | "retired";

/** services/definitions.py `out`: one version of an executable definition. */
export interface DefinitionVersion {
  id: string;
  workspace_id: string;
  kind: string;
  key: string;
  version: number;
  status: DefinitionStatus | string;
  title: string | null;
  content_hash: string;
  revision: number;
  created_by: string;
  published_by: string | null;
  retired_by: string | null;
  reason: string | null;
  published_at: string | null;
  retired_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  spec?: Dict;
  /** Agent definitions: the form the manifest was built from (null when it uses fields the form cannot show). */
  form?: AgentFormInput | null;
}

/** An agent form (P7-19): compiled by the server into the Agent manifest the YAML path loads. */
export type AgentFormInput = Schemas["AgentForm"];

/** GET .../agent-form: what an owner may grant here, with the reason a capability is not grantable. */
export interface AgentFormCapability {
  id: string;
  ref: string;
  summary: string;
  side_effect: string;
  tool: string | null;
  certification: string;
  enabled: boolean;
  grantable: boolean;
  reason: string | null;
  default_input: Dict | null;
  default_reason: string | null;
}

export interface AgentFormOptions {
  capabilities: AgentFormCapability[];
  knowledge_sections: string[];
  output_types: string[];
  limits: { llm_calls: number; usd: number; queries: number; max_rows: number; max_steps: number; pii_access: string[] };
}

export interface DefinitionRef {
  kind: string;
  key: string;
  version: number | string | null;
  content_hash: string | null;
  source: "workspace" | "builtin";
  status: string | null;
  id: string | null;
}

export interface DefinitionPage {
  items: DefinitionVersion[];
  next_cursor: string | null;
  builtin?: DefinitionRef[];
}

/** A playbook's signing state (autonomy/playbook_signing.state): signed versions run; drafts wait for a sign-off. */
export interface PlaybookSignature {
  state: "unsigned" | "requested" | "approved" | "rejected" | "invalidated" | "stale" | "signed" | string;
  approval_id?: string | null;
  content_hash?: string | null;
  requested_by?: string | null;
  requested_at?: string | null;
  reason?: string | null;
  key_id?: string | null;
  signed_at?: string | null;
}

/** A playbook definition as the editor sees it: the version, its YAML, signing state and diff to the published one. */
export interface PlaybookDraft extends DefinitionVersion {
  yaml: string;
  signature?: PlaybookSignature;
  test_evidence?: { kind?: string; content_hash?: string; simulations?: { fixture: string; outcome: string; digest: string }[] } | null;
  diff?: { against: string | null; changes: { path: string; from: unknown; to: unknown }[] };
}

/** POST .../playbook-editor/validate: every problem at once, or the normalized manifest. */
export interface PlaybookCheck {
  ok: boolean;
  key?: string | null;
  problems: string[];
  warnings?: string[];
  content_hash?: string;
  manifest?: Dict;
  steps?: { key: string; use: string; behaviour: string | null; type: string; after: string[]; optional: boolean; when: string | null }[];
  approval_gates?: string[];
  diff?: { against: string | null; changes: { path: string; from: unknown; to: unknown }[] };
}

export interface SimulationEvent {
  seq: number;
  key: string;
  agent: string | null;
  behaviour: string | null;
  type: string;
  status: string;
  detail: string;
  plan_version: number;
  wave?: number;
  approval?: string;
  budget: Record<string, number>;
}

/** autonomy/simulator.simulate: a dry run against a fixture workspace (no SQL, no model call, no side effect). */
export interface SimulationReport {
  outcome: "completed" | "completed_with_limitations" | "rejected" | "failed" | "refused" | string;
  reason: string | null;
  playbook: { id: string; version: string; ref: string; content_hash: string };
  fixture: Dict & { name: string };
  timeline: SimulationEvent[];
  waves: string[][];
  approvals: { id: string; gate: string; payload: string; for: string | null; decision: string; status: string; binds: string; plan_version: number }[];
  approval_gates: { key: string; payload: string | null; approval_for: string | null; type: string }[];
  replans: Dict[];
  budget: { limits: Record<string, number>; used: Record<string, number>; remaining: Record<string, number>; stopped_by: string | null };
  warnings: string[];
  mocks: Record<string, string>;
  plan_version: number;
  plan_hash: string;
  digest: string;
}

export interface PlaybookEditorOptions {
  agents: { id: string; version: string; summary: string; default_entry: boolean; behaviours: string[]; certification: string; enabled: boolean }[];
  checks: string[];
  replan_reasons: string[];
  fixtures: { name: string; description: string }[];
  schema: Dict;
  template: string;
}

/** Governed training result; a refusal remains visible in Work with its reason. */
export interface DefinitionDiff {
  from: DefinitionVersion;
  to: DefinitionVersion | null;
  changes: { path: string; from: unknown; to: unknown }[];
}

// ----------------------------------------------------------------------------------- recipes and file ingest (P6-04..07)
export interface Recipe {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
  status: "draft" | "published" | string;
  spec: Dict;
  spec_hash: string;
  created_by: string;
  created_at: string;
  published_at: string | null;
}

/** services/recipes.py `run_view`. */
export interface RecipeRun {
  id: string;
  workspace_id: string;
  recipe_id: string;
  recipe_name: string;
  recipe_version: number;
  spec_hash: string;
  mode: "preview" | "materialize" | string;
  engine: string | null;
  status: "running" | "succeeded" | "blocked" | "refused" | "failed" | string;
  plan?: Dict | null;
  preflight?: Dict[] | null;
  snapshots?: Dict | null;
  /** Per output: its gate outcome (recipes/gates.py `GateOutcome.summary`). */
  gates?: Record<string, RecipeGateOutcome> | null;
  schema_changes?: unknown;
  outputs?: Dict | null;
  lineage?: unknown;
  query_ids?: string[];
  error: string | null;
  created_by: string;
  created_at: string;
  finished_at: string | null;
  /** Preview mode only: the rows each output would have; nothing was written. */
  preview?: Record<string, { columns: string[]; rows: unknown[][]; row_count: number; truncated: boolean; dropped_rows: number; would_block: boolean }>;
}

export interface RecipeGateOutcome {
  blocked: boolean;
  kept_rows: number;
  dropped_rows: number;
  gates: { gate: string; type: string; severity: string; columns: string[]; checked_rows: number; failed_rows: number; status: string; note?: string }[];
  warnings: string[];
}

/** POST …/sources/{id}/ingest (services/file_ingest.py). */
export interface IngestResult {
  asset: string;
  asset_id: string;
  mode: string;
  format: string;
  [k: string]: unknown;
}

// ----------------------------------------------------------------------------------- relationship review (P7-09)
export interface RelationshipCandidate {
  id: string;
  workspace_id: string;
  source_id: string | null;
  from_asset: string;
  from_columns: string[];
  to_asset: string;
  to_columns: string[];
  /** Measured, never guessed: one_to_one | many_to_one | one_to_many | many_to_many. */
  cardinality: string;
  containment: number;
  confidence: number;
  evidence: Dict;
  assessment: Dict;
  origin: string;
  status: "pending" | "accepted" | "rejected" | "superseded" | string;
  proposed_by: string;
  approval_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  relationship_name: string | null;
  measured_at: string;
  content_hash: string;
  created_at: string;
}

/** GET …/semantic/model/diff: a structure version against the approved one. */
export interface SemanticModelDiff {
  version: number;
  status: string;
  base_version: number | null;
  has_changes: boolean;
  entries: { field: string; change: "added" | "removed" | "changed"; before: unknown; after: unknown }[];
}

/** Request body of POST …/schedules: the generated ScheduleIn with the typed kind-specific config. */
export type ScheduleInput = Schemas["ScheduleIn"] & { timezone: string; config: ScheduleConfig };

export type MonitorKind = "metric_threshold" | "metric_drift" | "change_point" | "forecast_deviation" | "data_quality";

export interface MonitorConfig {
  metric?: string;
  grain?: "day" | "week" | "month";
  op?: ">" | ">=" | "<" | "<=";
  value?: number;
  severity?: string;
  lookback?: number;
  z_threshold?: number;
  recent_periods?: number;
  /** forecast_deviation: interval width in standard deviations, fitted history length, optional season length. */
  z?: number;
  history?: number;
  seasonal_periods?: number;
  assets?: string[];
  [k: string]: unknown;
}

export interface MonitorResult {
  alert?: boolean;
  message?: string;
  reason?: string;
  error?: string;
  period?: string;
  value?: number;
  baseline_median?: number | null;
  z?: number | null;
  threshold?: string;
  series_tail?: [string, number][] | null;
  alert_id?: string | null;
  [k: string]: unknown;
}

export interface Monitor {
  id: string;
  workspace_id: string;
  name: string;
  kind: MonitorKind | string;
  config: MonitorConfig;
  enabled: boolean;
  auto_investigate: boolean;
  state: string;
  last_evaluated_at: string | null;
  last_result: MonitorResult;
  created_by: string;
  created_at: string;
}

export interface MonitorSeries {
  label: string;
  expression?: string;
  grain: string;
  points: [string, number][];
  query_id?: string;
}

export interface Alert {
  id: string;
  workspace_id: string;
  monitor_id: string | null;
  severity: string;
  title: string;
  message: string;
  data: Dict & { triage?: { p_material?: number; model?: string } | null };
  dedupe_key: string;
  status: string;
  investigation_run_id: string | null;
  acknowledged_by: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface NotificationLink {
  type?: "alert" | "artifact" | "run" | "schedule" | string;
  id?: string;
}

export interface AppNotification {
  id: number;
  workspace_id: string;
  user_id: string | null;
  kind: string;
  title: string;
  body: string;
  link: NotificationLink;
  read_by: string[];
  read: boolean;
  created_at: string;
}

export interface RunOrigin {
  type?: "user" | "schedule" | "alert" | string;
  schedule_id?: string;
  schedule_run_id?: string;
  previous_run_id?: string | null;
  alert_id?: string;
  publish?: "skip" | "propose" | string;
  automatic?: boolean;
  [k: string]: unknown;
}

export interface ChangedFinding {
  code?: string;
  title?: string;
  finding?: string;
  id?: string;
  effect?: number | null;
  previous_effect?: number | null;
  [k: string]: unknown;
}

export interface MetricDelta {
  name: string;
  value: number | null;
  previous_value: number | null;
  pct_change: number | null;
}

export interface RunChanges {
  previous_run_id?: string;
  new?: ChangedFinding[];
  persisting?: ChangedFinding[];
  changed?: ChangedFinding[];
  resolved?: ChangedFinding[];
  metrics?: MetricDelta[];
}

export interface ReportFile {
  sha256: string;
  bytes: number;
  mime: string;
  ext: string;
  path?: string;
}

export interface ReportContent {
  kind?: string;
  title?: string;
  report_data_hash?: string;
  files?: Record<string, ReportFile>;
  insights?: number;
  metrics?: number;
  alerts?: number;
  /** Formats asked for that this installation cannot render, with the reason (services/reports.py). */
  unavailable_formats?: Record<string, string>;
  /** An Ask answer saved as a report (kind `ask_answer`). */
  origin?: { type: string; turn_id?: string; thread_id?: string };
  [k: string]: unknown;
}

export interface DownloadedFile {
  blob: Blob;
  filename: string;
}


// ----------------------------------------------------------------------------------- catalog & crawls (increment 3)
export type SourceCategory = "database" | "warehouse" | "lakehouse" | "engine" | "file" | "api";

/** One connectable kind (GET /api/source-kinds, from config/source_kinds.yaml). */
export interface SourceKindInfo {
  kind: string;
  label: string;
  category: SourceCategory | string;
  required: string[];
  optional: string[];
  default_port: number | null;
  docs: string;
  /** The credential's name; it is always supplied through secret_ref, never in config. */
  secret_field: string | null;
  execution_mode: "pushdown" | "staged" | string;
  dialect: string;
  driver_installed: boolean;
  install_hint: string | null;
  enabled: boolean;
}

/** Request body of POST …/sources (generated). */
export type SourceInput = Schemas["SourceIn"];
export type SourceUpdatePatch = Schemas["SourceUpdateIn"];

export interface RetypedColumn {
  name: string;
  previous_type: string;
  current_type: string;
}

export interface AssetChange {
  key: string;
  previous_fingerprint?: string | null;
  fingerprint?: string;
  added: string[];
  removed: string[];
  retyped: RetypedColumn[];
  attributes_changed: boolean;
}

export interface RenameCandidate {
  previous_key: string;
  current_key: string;
  similarity: number;
}

export interface CrawlChanges {
  new?: string[];
  changed?: AssetChange[];
  missing?: string[];
  deprecated?: string[];
  rename_candidates?: RenameCandidate[];
}

export interface CrawlStats {
  discovered?: number;
  in_scope?: number;
  truncated?: number;
  new?: number;
  changed?: number;
  unchanged?: number;
  missing?: number;
  deprecated?: number;
  renamed?: number;
  profiled?: number;
  tokens_saved?: number;
  model_calls?: number;
  [k: string]: unknown;
}

export interface CrawlLogEntry {
  at: string;
  stage: string;
  message: string;
  [k: string]: unknown;
}

export interface Crawl {
  id: string;
  source_id: string;
  workspace_id: string;
  mode: "full" | "incremental" | string;
  trigger: string;
  status: "running" | "succeeded" | "failed" | string;
  stage: string | null;
  options: { include?: string[]; exclude?: string[]; profile?: boolean; enrich?: boolean; [k: string]: unknown };
  stats: CrawlStats;
  changes: CrawlChanges;
  log: CrawlLogEntry[];
  error: string | null;
  started_by: string | null;
  started_at: string;
  finished_at: string | null;
}

/** Request body of POST …/sources/{id}/crawl (generated). */
export type CrawlInput = Schemas["CrawlIn"];

export interface PiiInfo {
  category: string | null;
  sensitivity: string;
  confidence: number;
  reasons: string[];
}

export interface GlossaryLink {
  term_id: string;
  term: string | null;
  score?: number;
  reason?: string;
}

/** A column profile as measured by the crawler (skills/profiling.py); sensitive columns arrive without values. */
export interface MeasuredProfile {
  type_family?: string;
  semantic_type?: string;
  non_null?: number;
  null_rate?: number;
  distinct?: number;
  distinct_ratio?: number;
  min?: unknown;
  max?: unknown;
  mean?: number | null;
  stddev?: number | null;
  percentiles?: Record<string, number | null>;
  true_count?: number | null;
  avg_length?: number | null;
  max_length?: number | null;
  top_values?: { value: unknown; count: number; share?: number | null }[];
  histogram?: { bin: number; low: number; high: number; count: number }[];
  monthly_counts?: { month: string; count: number }[];
  outliers?: { low_count?: number; high_count?: number; low_fence?: number; high_fence?: number };
  values?: unknown[];
  values_complete?: boolean;
  has_blanks?: boolean;
  patterns?: { mask: string; share: number }[];
  [k: string]: unknown;
}

export interface ProfileMeta {
  profiled_at?: string | null;
  rows_profiled?: number | null;
  source?: "full" | "snapshot" | string;
  truncated?: boolean;
  sampled?: boolean;
  reused?: boolean;
  [k: string]: unknown;
}

export interface CatalogColumn {
  name: string;
  data_type: string;
  semantic_type?: string | null;
  is_key?: boolean;
  profile?: MeasuredProfile | null;
  business_name: string | null;
  description: string | null;
  tags: string[];
  tags_origin: "crawler" | "user" | string;
  role: string | null;
  unit: string | null;
  pii: PiiInfo | null;
  glossary: GlossaryLink | null;
}

export type DescriptionOrigin = "source" | "rule" | "model" | "user";

export interface CatalogAsset {
  id: string;
  fq: string;
  source_id: string;
  name: string;
  business_name: string | null;
  business_name_origin?: string | null;
  description: string | null;
  description_origin: DescriptionOrigin | string | null;
  reviewed: boolean;
  selected: boolean;
  lifecycle: "active" | "deprecated" | string;
  row_count: number | null;
  role: string | null;
  domain: string | null;
  grain: string | null;
  confidence: number | null;
  last_crawled_at: string | null;
  entity?: string | null;
  time_column?: string | null;
  profile_meta?: ProfileMeta | null;
  snapshot?: Dict | null;
  columns: CatalogColumn[];
}

/** Query of GET …/catalog (generated parameters). */
export type CatalogFilter = NonNullable<paths["/api/workspaces/{workspace_id}/catalog"]["get"]["parameters"]["query"]>;

/** Request body of PATCH /assets/{id}/metadata (generated). */
export type AssetMetadataPatch = Schemas["AssetMetadataIn"];

export interface CuratedAsset {
  id: string;
  business_name: string | null;
  description: string | null;
  description_origin: string | null;
  reviewed: boolean;
}

export interface SqlExplanation {
  statement?: string;
  read_only?: boolean;
  summary: string;
  tables?: string[];
  ctes?: string[];
  outputs?: string[];
  joins?: { kind: string; table: string; on: string | null }[];
  filter?: string | null;
  group_by?: string[];
  aggregations?: string[];
  having?: string | null;
  order_by?: string[];
  limit?: string | null;
  window_functions?: number;
  distinct?: boolean;
  gateway: { accepted: boolean; code?: string; reason?: string };
  /** The source's plan through the gateway (EXPLAIN, never executed); only when the validator accepts. */
  plan?: { available: boolean; reason?: string; node?: string | null; estimated_rows?: number | null; total_cost?: number | null;
    nodes?: string[]; relations?: string[] };
}

// ----------------------------------------------------------------------------------- platform settings (admin)
export type LLMMode = "off" | "auto" | "always";

/** contracts/platform.py PlatformSettings: the effective admin document. */
export interface PlatformSettings {
  llm: {
    purpose_modes: Record<string, LLMMode>;
    routing_overrides: Record<string, string>;
    profile_models: Record<string, string[]>;
    disabled_models: string[];
    cache_enabled: boolean;
    cache_ttl_hours: number;
    cacheable_purposes: string[];
    max_prompt_tokens: number;
    downgrade_below_budget_fraction: number;
    compact_prompts: boolean;
    catalog_max_columns_per_table: number;
    catalog_max_tables: number;
    [k: string]: unknown;
  };
  analysis: Record<string, number | boolean | string>;
  crawl: Record<string, number | boolean | string>;
  monitors: Record<string, number | boolean | string>;
  sources: { enabled_kinds: string[]; allow_pushdown: boolean; staged_max_rows: number; [k: string]: unknown };
  features: Record<string, boolean>;
  [k: string]: unknown;
}

export interface SettingsDocument {
  version: number;
  settings: PlatformSettings;
  defaults: PlatformSettings;
  presets: Record<string, Record<string, LLMMode>>;
  schema: Dict;
}

export interface SettingsChange {
  path: string;
  from: unknown;
  to: unknown;
}

export interface SettingsUpdateResult {
  version: number;
  changes?: SettingsChange[];
  rolled_back_to?: number;
}

export interface SettingsVersion {
  version: number;
  note: string | null;
  created_by: string | null;
  created_at: string;
}

export interface PromptTemplate {
  name: string;
  version: string;
  text: string;
}

export interface TokenSavingsRow {
  cost_complete?: boolean;
  calls: number;
  tokens_used: number;
  tokens_saved: number;
  cost_usd: number;
  by_status: Record<string, number>;
}

export interface TokenSavings {
  days: number;
  totals: {
    calls: number;
    tokens_used: number;
    tokens_saved: number;
    cost_usd: number;
    cache_hits: number;
    deterministic_skips: number;
    refused: number;
    saved_share: number;
    /** Input tokens the provider served from its prompt cache (billed at a discount). */
    input_tokens?: number;
    cached_input_tokens?: number;
    cached_input_share?: number | null;
    cached_input_saved_usd?: number | null;
  };
  by_purpose: Record<string, TokenSavingsRow>;
  /** Compiled context and knowledge retrieval reused instead of rebuilt, per kind and purpose. */
  context_cache?: { shared: boolean; by_kind: Record<string, Record<string, { hits: number; misses: number; chars_reused: number }>> } | null;
  /** Optional until the ladder records `answered_by` per call (spec v3 §4.1). */
  by_rung?: Record<string, RungSpend> | null;
  by_model?: Record<string, RungSpend> | null;
  missing_price?: { model: string; calls: number; tokens: number }[];
  prices_version?: string;
  cost_complete?: boolean;
  /** Large-tier calls made because a small-tier answer failed validation, by purpose. */
  escalations?: Record<string, { calls: number; cost_usd: number; by_model: Record<string, number>; by_reason: Record<string, number> }>;
}

// ----------------------------------------------------------------------------------- capabilities
export type CapabilityKind = "Agent" | "Skill" | "Tool" | "Method" | "Connector" | "Engine" | "Publisher" | "DecisionPurpose"
  | "Detector" | "Crawler" | "KnowledgePack" | "Playbook" | "Renderer";
export type SideEffect = "none" | "read_source" | "write_internal" | "write_external";
export type CertStatus = "draft" | "tested" | "certified" | "deprecated";

/** One row of GET /api/capabilities (capabilities.py `_out`). Kinds and values are open: plugins add new ones. */
export interface CapabilitySummary {
  id: string;
  kind: CapabilityKind | string;
  version: string;
  ref: string;
  summary: string;
  source: string;
  entry: string | null;
  determinism: string;
  side_effect: SideEffect | string;
  cost_class: string;
  certification: { status: CertStatus | string; evidence?: string | null };
  autonomous_ok: boolean;
  needs_approval: boolean;
  tags: string[];
  /** Enablement in the requested workspace; null without `workspace_id`. */
  enabled: boolean | null;
  /** False when an optional extra is missing (ADR-0025): shown with the reason, never as a broken button. */
  available?: boolean;
  unavailable_reason?: string | null;
  bindings?: {
    capabilities: { id: string; kind: string; enabled: boolean | null; determinism: string; certification: string }[];
    tools: string[];
    model_purposes: string[];
    playbooks: string[];
    default_actions: string[];
  };
}

export interface WorkspaceInventory {
  workspace_id: string;
  status: string;
  archived: boolean;
  control_rows: Record<string, number>;
  control_tables_checked: number;
  staged_schemas: string[];
  build_targets: { engine: string; schema: string; role: string; status: string }[];
  local_paths: { path: string; exists: boolean }[];
  publications: { id: string; destination: string; status: string; external_ids: Dict }[];
  verification: string;
}

export interface CapabilityList {
  digest: string;
  capabilities: CapabilitySummary[];
}

/** The full manifest (GET /api/capabilities/{id}, contracts/capability.py). */
export interface CapabilityManifest {
  apiVersion?: string;
  kind: CapabilityKind | string;
  id: string;
  version: string;
  summary: string;
  entry?: string | null;
  input_schema?: Dict;
  output_schema?: Dict;
  determinism?: string;
  side_effect?: SideEffect | string;
  cost_class?: string;
  permissions?: string[];
  requires?: string[];
  certification?: { status: CertStatus | string; evidence?: string | null };
  ui?: { form?: "auto" | "none" | "custom" | string; renderer?: string | null };
  tags?: string[];
  spec?: Dict;
  source?: string;
}

export interface CapabilityReload {
  digest: string;
  previous_digest: string;
  count: number;
  problems: string[];
}

/** Invocation result: MCP invoke (mcp.py) today; the generic capability invoke shares the shape. */
export interface CapabilityInvocation {
  status: "ok" | "error" | "approval_required" | string;
  capability?: string;
  side_effect?: string;
  approval_id?: string;
  result?: unknown;
  [k: string]: unknown;
}

// ----------------------------------------------------------------------------------- tool registry (S10)
/** A registry tool version as listed (GET /v2/tools): toolreg/store.py `summary`. */
export interface RegistryTool {
  tool_id: string;
  version: string;
  kind: "data_product" | "playbook" | "external_mcp" | "builtin" | "agent" | string;
  owner: string;
  status: "certified" | "quarantined" | "refused" | "deprecated" | "retired" | string;
  side_effect: "none" | "write_internal" | "write_external" | string;
  title: string;
  /** Screened text from the source or a third-party server: shown as data, never as instructions. */
  description: string;
  exposure: { rest?: boolean; mcp?: boolean; grants_required?: string[] };
  data_classification_max?: string | null;
}

/** The signed v2 manifest (contracts/tool_manifest.py). */
export interface ToolManifestV2 {
  apiVersion: string;
  tool_id: string;
  version: string;
  kind: string;
  owner: string;
  title: string;
  description: string;
  input_schema: Dict;
  output_schema: Dict;
  side_effect: string;
  permissions: string[];
  data_classification_max: string;
  execution: { via: string; source?: string | null; ref: Dict };
  limits: { timeout_s: number; max_rows: number; rate_per_min: number };
  exposure: { rest: boolean; mcp: boolean; grants_required: string[] };
  signature: { alg: string; key_id: string; value: string } | null;
}

export interface ToolHealth {
  status: string;
  /** "valid", or why the runtime refuses the manifest: unsigned | tampered | key | invalid */
  signature: string;
  source_current: boolean | null;
  calls_24h: number;
  error_rate_24h: number | null;
  last_ok_at: string | null;
  invocable: boolean;
}

export interface ToolVersionRow {
  id: string;
  tool_id: string;
  version: string;
  kind: string;
  owner: string;
  status: string;
  status_reason: string | null;
  side_effect: string;
  manifest: ToolManifestV2;
  manifest_hash: string;
  source: Dict;
  screening: { flags?: string[]; screened_at?: string; rescreen?: boolean; classified_by?: string; read_only_hint?: boolean | null } & Dict;
  created_by: string;
  created_at: string | null;
}

export interface RegistryToolDetail extends ToolVersionRow {
  health: ToolHealth;
  versions: { version: string; status: string; status_reason: string | null; created_at: string | null }[];
}

export interface ToolPublished extends ToolVersionRow {
  created: boolean;
}

export interface ToolUsage {
  tool_id: string;
  days: number;
  calls: number;
  by_status: Record<string, number>;
  by_channel: Record<string, number>;
  top_callers: [string, number][];
  latency_ms_p50: number | null;
  cost_usd: number;
  recent: { at: string | null; version: string | null; caller: string; channel: string; status: string; reason: string | null;
    latency_ms: number; row_count: number | null; query_id: string | null }[];
}

export interface ToolInvocationResult {
  tool_id: string;
  version: string;
  kind: string;
  status: "ok" | "error" | "approval_required" | string;
  deprecated?: boolean;
  approval_id?: string;
  result?: unknown;
  [k: string]: unknown;
}

export interface ToolSigningKey {
  key_id: string;
  alg: string;
  public_key: string;
  status: "active" | "retired" | "revoked" | string;
  created_at: string | null;
  retired_at: string | null;
}

export type ToolPublishBody = Schemas["ToolPublishIn"];

/** A registered external MCP server (GET /api/workspaces/{ws}/mcp/servers, mcp.py `_server_out`). */
export interface McpServerRow {
  id: string;
  workspace_id: string;
  name: string;
  url: string;
  transport: string;
  status: "registered" | "ready" | "error" | string;
  allowed: boolean;
  has_secret: boolean;
  config: Dict;
  tools: { name: string; description: string; refused: boolean; flags: string[]; hash: string; read_only_hint: boolean | null }[];
  last_refreshed_at: string | null;
  last_error: string | null;
}

/** What one admission did (POST /v2/mcp-servers/{id}:admit). */
export interface McpAdmission {
  server: string;
  server_id: string;
  quarantined: string[];
  rescreened: string[];
  refused: string[];
  retired: string[];
  unchanged: string[];
}

export interface McpGrantRow {
  workspace_id: string;
  role: "viewer" | "analyst" | string;
  tools: string[];
  quotas: Record<string, number>;
  tool_patterns: string[];
  rate_per_min: number | null;
  expires_at: string | null;
}

/** An MCP / API client of the platform (GET /api/admin/mcp/clients). */
export interface McpClientRow {
  client_id: string;
  name: string;
  status: "active" | "revoked" | string;
  service_user_id: string;
  created_at: string | null;
  last_used_at: string | null;
  grants: McpGrantRow[];
}

export type McpGrantBody = Schemas["GrantIn"];

// ----------------------------------------------------------------------------------- build (P4-E04/E06, P4-U05)
export interface BuildTarget {
  id: string;
  workspace_id: string;
  engine: string;
  schema_name: string;
  build_role: string;
  status: string;
  provisioning: Dict;
  created_by: string;
  created_at: string;
}

export type BuildStatus = "planned" | "awaiting_approval" | "running" | "succeeded" | "failed" | "refused" | string;

export interface BuildTest {
  column: string;
  test: string;
}

export interface BuildDryRun {
  runner?: string;
  command?: string[] | string;
  ok?: boolean;
  dbt_version?: string | null;
  ossie_version?: string | null;
  tests?: { candidates?: BuildTest[]; passing?: BuildTest[]; dropped?: BuildTest[] };
  allowed_sources?: string[];
  metrics?: { metric: string; [k: string]: unknown }[];
  skipped_metrics?: { metric: string; reason: string }[];
  notes?: string[];
  [k: string]: unknown;
}

export interface BuildEstimate {
  rows?: number | null;
  columns?: number | null;
  models?: number | null;
  tests?: number | null;
  approx_bytes?: number | null;
  method?: string;
  query_id?: string | null;
}

export interface BuildRollback {
  strategy?: string;
  statements?: string[];
  restore?: string;
  previous_job_id?: string | null;
}

/** GET /api/workspaces/{id}/builds: a job without its files, manifest or logs, plus its approval status. */
export interface BuildJob {
  id: string;
  workspace_id: string;
  run_id: string;
  source_run_id: string;
  artifact_id: string | null;
  approval_id: string | null;
  approval_status?: string | null;
  engine: string;
  runner: string;
  target_schema: string;
  project_name: string;
  project_hash: string;
  plan_hash: string | null;
  relations: string[];
  dry_run: BuildDryRun;
  estimate: BuildEstimate;
  rollback: BuildRollback;
  status: BuildStatus;
  run_results: { counts?: Record<string, number>; [k: string]: unknown };
  error: string | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface BuildApprovalState {
  id: string;
  status: string;
  action: string;
  payload_hash: string;
  plan_hash: string | null;
  policy_version: number;
  risk_tier: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  expires_at: string | null;
}

export interface BuildJobDetail extends BuildJob {
  project_files: Record<string, string>;
  manifest: Dict;
  openlineage: Dict[];
  log_tail: string;
  approval: BuildApprovalState | null;
}

export interface BuildFileDiff {
  path: string;
  status: "added" | "removed" | "modified" | "unchanged";
  lines_added: number;
  lines_removed: number;
  diff: string;
  truncated: boolean;
}

export interface BuildDiff {
  job_id: string;
  project_hash: string;
  basis: "previous_job_same_target" | "requested" | "none";
  against: { job_id: string; status: string; project_hash: string; created_at: string | null } | null;
  identical: boolean;
  files: BuildFileDiff[];
  summary: { added: number; removed: number; modified: number; unchanged: number; lines_added: number; lines_removed: number };
}

// ----------------------------------------------------------------------------------- semantic layer (P4-K03)
export type MetricProposal = Schemas["MetricProposalIn"];

export interface SemanticMetric {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
  status: "draft" | "proposed" | "approved" | "deprecated" | "rejected" | string;
  definition: Dict;
  expression: string;
  normalized_expression: string;
  display_name: string | null;
  owner_id: string | null;
  proposed_by: string;
  proposed_via: string;
  run_id: string | null;
  approval_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  reason: string | null;
  content_hash: string;
  created_at: string;
  updated_at: string;
}

export interface SemanticConflictRow {
  kind: "duplicate_expression" | "conflicting_definition";
  names: string[];
  metrics?: { name: string; version: number; status: string; expression: string }[];
  detail: string;
}

export interface SemanticModelView {
  model: Dict | null;
  metrics: SemanticMetric[];
  approved: string[];
  conflicts: SemanticConflictRow[];
  ossie_version: string;
}

export interface MetricProblem {
  field: string;
  message: string;
}

export interface MetricValidation {
  ok: boolean;
  problems: MetricProblem[];
  conflicts: SemanticConflictRow[];
  normalized_expression: string | null;
  existing: { version: number; status: string } | null;
}

export interface MetricProposalResult {
  metric: SemanticMetric;
  created: boolean;
  conflicts: SemanticConflictRow[];
}

// ----------------------------------------------------------------------------------- brief, readiness, job kinds (P4-04)
/** contracts/brief.py (docs/20-contracts/04-brief-steps-api.md §1–3). */
export type AssertionOrigin = "source" | "rule" | "model" | "user";
export type ReviewState = "suggested" | "reviewed" | "validated" | "rejected";
export type BriefGroup = "decision" | "domain" | "data_semantics" | "time_measures" | "ml_objective" | "constraints" | "knowledge";

export interface BriefEvidence {
  kind: string;
  ref: string;
  detail?: Dict | null;
}

export interface Assertion {
  key: string;
  group: BriefGroup | string;
  field: string;
  subject?: string | null;
  value: unknown;
  origin: AssertionOrigin | string;
  evidence: BriefEvidence[];
  review_state: ReviewState | string;
  version: number;
  confidence?: number | null;
  note?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
}

export interface WorkspaceBrief {
  schema_version?: number;
  workspace_id: string;
  /** 0 = no brief yet; the ETag and If-Match are this number. */
  version: number;
  content_hash: string | null;
  created_by: string | null;
  created_at: string | null;
  reason: string | null;
  assertions: Assertion[];
}

export interface BriefVersion {
  version: number;
  content_hash: string | null;
  reason: string | null;
  created_by: string | null;
  created_at: string | null;
  assertions: number | Assertion[];
}

export interface BriefPatchResult extends WorkspaceBrief {
  changed?: string[];
  impact?: { readiness_assessments_outdated?: number };
}

export interface BriefRefreshResult extends WorkspaceBrief {
  added?: string[];
  updated?: string[];
  new_version?: boolean;
}

export type BriefPatchBody = Schemas["BriefPatch"];

export type JobKindKey = "explain" | "compare" | "forecast" | "predict" | "prepare" | "monitor";

export interface JobKindReason {
  code: "no_executor" | "no_ml_spec" | "not_registered" | "capability_unusable" | "role" | "no_data" | "work_mode" | string;
  message: string;
  remediation: string;
}

/** capabilities/job_kinds.py `availability` (GET /workspaces/{ws}/capabilities). */
export interface JobKindAvailability {
  key: JobKindKey;
  label: string;
  /** The workspace work mode the kind belongs to; a kind whose mode is not selected carries a `work_mode` reason. */
  mode: WorkMode;
  work_order_kind: string;
  available: boolean;
  reasons: JobKindReason[];
  capabilities: { id: string; ref: string; kind: string; usable: boolean; reason: string | null; certification: string }[];
  entry: { type: "work_order" | "recipe" | "monitor" | string; payload_type?: string; route?: string };
  readiness_checks: string[];
  min_role: string;
}

export interface WorkspaceJobKinds {
  workspace_id: string;
  digest: string;
  job_kinds: JobKindAvailability[];
}

export type ReadinessStatus = "ready" | "needs_input" | "blocked" | "unsupported";
export type ReadinessCheckStatus = "pass" | "fail" | "needs_input" | "warn" | "not_applicable" | "unsupported";

export interface ReadinessCheck {
  check: string;
  status: ReadinessCheckStatus | string;
  required: boolean;
  reason: string;
  remediation?: string | null;
  subject?: string | null;
  evidence?: BriefEvidence[];
}

export interface ReadinessAssessment {
  id: string | null;
  workspace_id: string;
  job_kind: string;
  status: ReadinessStatus | string;
  brief_version: number;
  work_order_id?: string | null;
  work_order_revision?: number | null;
  checks: ReadinessCheck[];
  inputs: Dict;
  inputs_hash: string | null;
  alternatives: { job_kind: string; requires_explicit_choice?: boolean; note?: string }[];
  created_at?: string | null;
}

export type ReadinessInput = Schemas["ReadinessIn"];

// ----------------------------------------------------------------------------------- steps, branches, notebooks (P7-04/05/12)
export type StepKind = "plan" | "query" | "method" | "recipe" | "train" | "chart" | "claim";
export type StepStatus = "pending" | "ok" | "flagged" | "failed" | "unsupported" | "recorded";
export type ContainerType = "run" | "ask_thread" | "notebook";

export interface StepCheck {
  check: string;
  passed: boolean;
  severity: "error" | "warning" | "info" | string;
  detail: string;
  evidence?: Dict;
  corrected?: boolean;
}

export interface StepCorrection {
  check: string;
  reason: string;
  from_sql?: string | null;
  to_sql?: string | null;
  round?: number;
}

/** contracts/step.py `Step`: one version of a step. */
export interface Step {
  schema_version?: number;
  id: string;
  version: number;
  current_version: number;
  kind: StepKind | string;
  title: string;
  status: StepStatus | string;
  spec: Dict;
  spec_hash: string;
  receipts: Dict[];
  result_snapshot: { kind: "artifact"; id: string; version: number; content_hash: string; media_type: string } | null;
  chart_spec?: { type?: string; x?: string; y?: string } | null;
  checks: StepCheck[];
  corrections: StepCorrection[];
  verification_record: VerificationState | null;
  depends_on: string[];
  inputs: Record<string, string>;
  branch_id: string;
  container: { type: ContainerType | string; id: string };
  seq: number;
  origin: Dict;
  forked_from?: { step_id?: string; version?: number } | null;
  reason: "created" | "edited" | "rerun" | "upstream_changed" | "forked" | "ingested" | string;
  error?: string | null;
  inherited: boolean;
  created_by?: string | null;
  created_at?: string | null;
}

/** A step's stored result (services/steps.py `to_table`, plus `stat` for a method). */
export interface StepResult {
  columns?: string[];
  rows?: unknown[][];
  row_count?: number;
  truncated?: boolean;
  stat?: Dict;
  text?: string;
  [k: string]: unknown;
}

export interface StepWithResult extends Step {
  result: StepResult;
}

export interface Branch {
  id: string;
  name: string;
  container: { type: string; id: string };
  parent_branch_id: string | null;
  forked_from: { step_id?: string; version?: number } | null;
  base?: Dict[];
  status: "open" | "merged" | string;
  merged_into: string[];
  created_by?: string | null;
  created_at?: string | null;
}

export interface ThreadView {
  branch: Branch;
  steps: Step[];
  branches?: Branch[];
}

export interface StepVersions {
  step_id: string;
  current_version: number;
  versions: Step[];
}

export interface StepRevision {
  step: Step;
  rerun: Step[];
  voided_records: string[];
}

export type PinBody = Schemas["PinIn"];

export interface PinResult {
  status: "approval_required" | "pinned" | string;
  approval_id?: string;
  payload_hash?: string;
  frozen?: Dict;
  pin?: Dict & { id: string };
}

export interface ForkResult {
  branch: Branch;
  steps: Step[];
  edited?: Step | null;
}

export type BranchMatch = "shared" | "equivalent" | "diverged" | "only_a" | "only_b";

export interface BranchCompareRow {
  match: BranchMatch | string;
  root: string | null;
  a: Step | null;
  b: Step | null;
  spec_diff?: { path: string; a: unknown; b: unknown }[] | Dict | null;
  numbers?: {
    same_result?: boolean;
    headline?: { a: number | null; b: number | null; delta: number | null } | null;
    row_count?: { a: number | null; b: number | null } | null;
    cells?: { key: string; column: string; a: unknown; b: unknown; delta: number | null }[];
    stat?: Dict | null;
  } | null;
  verdicts?: { a: VerificationState | null; b: VerificationState | null };
}

export interface BranchCompare {
  a: Branch;
  b: Branch;
  steps: BranchCompareRow[];
  summary: Dict;
}

export interface MergeResult {
  report: { id: string; version: number; name: string; content: { kind: "data_thread"; title: string; sections: Dict[]; branches: Dict[] } };
  branch: Branch;
  /** services/branches.py `merge`: how many sections the report took from the branch (a count, not ids). */
  included: number;
  excluded: Dict[];
}

export type CellType = "markdown" | "sql" | "python";

export interface NotebookCell extends Step {
  cell: CellType | string;
  source: string;
}

export interface NotebookSummary {
  id: string;
  title: string;
  revision: number;
  branch_id?: string;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  cells?: number | NotebookCell[];
}

export interface Notebook {
  id: string;
  title: string;
  revision: number;
  branch_id: string;
  cells: NotebookCell[];
  executed?: string[];
}

// ----------------------------------------------------------------------------------- governed ML (P5-01..06)
export type MLTask = "classify" | "regress" | "forecast" | "cluster" | "anomaly";

/** contracts/work.py `MLSpec` (the fields the form edits; the rest travel through as they came). */
export interface MLSpecDoc {
  type?: "ml";
  task: MLTask | string;
  dataset?: { asset: string; source_id?: string | null; version?: string | null } | null;
  target?: string | null;
  positive_class?: string | number | boolean | null;
  target_unit?: string | null;
  entity_keys?: string[];
  group_keys?: string[];
  time_column?: string | null;
  cutoff_column?: string | null;
  prediction_cutoff?: string | null;
  outcome_time_column?: string | null;
  label_horizon?: number | null;
  horizon?: number | null;
  season_length?: number | null;
  features?: { column: string; type?: string | null; available_at?: "cutoff" | "known_in_advance" | "after_outcome" | string }[];
  split?: { strategy: string; holdout_fraction?: number; validation_folds?: number; embargo_periods?: number;
    independence_justification?: string | null } | null;
  estimators?: string[];
  search?: { max_trials?: number; max_seconds?: number; max_rows?: number };
  objective_metric?: string | null;
  min_improvement?: number;
  guardrails?: Dict[];
  [k: string]: unknown;
}

export interface MLProposal {
  proposal: MLSpecDoc | null;
  problems: string[];
  source: string;
}

export type ProposalInput = Schemas["ProposalIn"];

export interface MLReadinessCheck {
  check: string;
  status?: string;
  outcome?: string;
  reason?: string;
  rows?: Dict | null;
  [k: string]: unknown;
}

export interface MLExperiment {
  id: string;
  workspace_id: string;
  run_id: string | null;
  definition_id: string;
  definition_key: string;
  definition_version: number;
  task: MLTask | string;
  spec_hash: string;
  status: "running" | "succeeded" | "failed" | "refused" | string;
  verdict: "improved" | "no_improvement" | "guardrail_failed" | "invalid" | null | string;
  dataset_asset: string;
  dataset_source_id?: string | null;
  dataset_version: string | null;
  split_id: string | null;
  manifest_hash: string | null;
  selection_hash: string | null;
  evaluation_seal: string | null;
  package_hash: string | null;
  code_digest?: string | null;
  environment_digest?: string | null;
  readiness: { status?: string; checks?: MLReadinessCheck[]; excluded?: unknown; [k: string]: unknown } | null;
  artifacts: Record<string, string> | null;
  verification_record_id: string | null;
  query_ids?: string[];
  reproduction_of?: string | null;
  error: string | null;
  created_by: string;
  created_at: string | null;
  finished_at: string | null;
  summary: {
    metric?: string;
    baseline?: Dict;
    candidate?: Dict;
    decision?: { improved?: boolean; gain?: number | null; min_improvement?: number; reason?: string };
    estimator?: string;
    trials_run?: number;
    stopped?: string | null;
    seconds?: number;
    rows_read?: number;
    [k: string]: unknown;
  };
  verification?: VerificationState;
  model_version?: ModelVersion;
}

export type MLRecordType = "ml_spec" | "ml_split_manifest" | "ml_trials" | "ml_model" | "ml_evaluation" | "ml_model_card";

export interface MLRecord<T = Dict> {
  artifact_id: string;
  type: MLRecordType | string;
  version: number;
  content_hash: string;
  content: T;
}

/** ml/splits.py manifest. */
export interface SplitManifest {
  version?: string;
  dataset_version?: string;
  strategy: string;
  seed?: number;
  holdout_fraction?: number | null;
  embargo_periods?: number;
  independence_justification?: string | null;
  time_column?: string | null;
  group_columns?: string[];
  rows: { usable: number; train: number; holdout: number; folds: { train: number; validation: number }[] };
  boundaries?: { holdout_from?: string; embargo_from?: string | null; holdout_periods?: number; distinct_periods?: number } | null;
  groups?: { columns?: string[]; train_groups?: number; holdout_groups?: number; overlap?: number } | null;
  excluded?: unknown;
}

export interface MLTrial {
  trial: number;
  role: "baseline" | "candidate" | string;
  estimator: string;
  params: Dict;
  status: string;
  error: string | null;
  folds: (number | null)[] | null;
  mean: number | null;
}

export interface ModelVersion {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
  experiment_id: string;
  task: string;
  package_hash: string;
  status: "candidate" | "challenger" | "champion" | "retired" | string;
  feature_schema: Dict[];
  metrics: { metric?: string; candidate?: Dict; baseline?: Dict };
  approval_id: string | null;
  previous_champion_id: string | null;
  promoted_by: string | null;
  reason: string | null;
  created_by: string;
  promoted_at: string | null;
  retired_at: string | null;
  created_at: string | null;
}

export interface ApprovalStep {
  status: "approval_required" | "promoted" | "rolled_back" | "succeeded" | "duplicate" | string;
  approval_id?: string;
  payload_hash?: string;
  expires_at?: string | null;
  model_version?: ModelVersion;
  retired?: ModelVersion;
  scoring_run_id?: string;
  scoring_run?: ScoringRun;
}

export interface ScoringRun {
  id: string;
  workspace_id: string;
  definition_id: string;
  definition_key: string;
  definition_version: number;
  model_version_id: string;
  package_hash: string;
  input_asset: string;
  input_version: string | null;
  status: string;
  approval_id: string | null;
  rows_input: number | null;
  rows_scored: number | null;
  rows_rejected: number | null;
  output_table: string | null;
  rejected_table: string | null;
  details?: Dict | null;
  error: string | null;
  created_by: string;
  created_at: string | null;
  finished_at: string | null;
}

// ----------------------------------------------------------------------------------- pipelines (P6-01..03)
export interface Pipeline {
  id: string;
  workspace_id: string;
  name: string;
  version: number;
  status: "draft" | "published" | string;
  spec: Dict;
  spec_hash: string;
  created_by: string;
  created_at: string | null;
  published_at: string | null;
}

/** pipelines/dryrun.py checks: every one carries `ok`. */
export interface PipelineCheck {
  check: string;
  ok: boolean;
  [k: string]: unknown;
}

export interface PipelineReconciliation {
  inputs: Record<string, number>;
  input_rows: number;
  output_rows: number;
  rejected_rows: number;
  dropped_rows: number;
  blocked: boolean;
  /** Rows that arrived behind the watermark's late window: a count only when measured (the dry run of an
   * incremental pipeline); null with `late_rows_reason` otherwise; absent from older servers. Never a silent 0. */
  late_rows?: number | null;
  late_rows_reason?: string | null;
  unmatched: Record<string, { unmatched_left_rows: number; unmatched_left_pct: number; unmatched_left_keys: number; unmatched_right_keys: number;
    row_multiplication: number | null }>;
  aggregates: { name: string; func: string; input: { node: string; column: string | null; value: number }; output: { column: string | null; value: number };
    difference: number; difference_pct: number; tolerance_pct: number; ok: boolean }[];
}

export interface PipelineRun {
  id: string;
  workspace_id: string;
  pipeline_id: string;
  pipeline_name: string;
  pipeline_version: number;
  spec_hash: string;
  mode: string;
  status: "running" | "succeeded" | "blocked" | "awaiting_approval" | "failed" | string;
  recipes: Dict | null;
  plan: Dict | null;
  manifest: { scope_hash?: string; engine?: string; cross_source?: boolean; join_strategy?: string; sources?: Record<string, Dict> } | null;
  sql: { output?: string; dialect?: string; preflight?: Record<string, string> } | null;
  checks: PipelineCheck[] | null;
  reconciliation: PipelineReconciliation | null;
  candidate: { snapshot: string; row_count: number; columns: { name: string; type?: string }[]; keys: string[]; preview: unknown[][];
    gates?: { blocked: boolean; kept_rows: number; dropped_rows: number; gates: Dict[]; warnings: string[] } } | null;
  recipe_run_ids?: string[] | null;
  query_ids?: string[] | null;
  plan_hash: string | null;
  approval_id: string | null;
  error: string | null;
  created_by: string;
  created_at: string | null;
  finished_at: string | null;
}

export interface Materialization {
  id: string;
  workspace_id: string;
  destination_id: string;
  schema_name: string;
  table_name: string;
  version: number;
  version_table: string;
  pipeline_id: string;
  pipeline_run_id: string;
  approval_id: string | null;
  status: "promoted" | "superseded" | "rolled_back" | "failed" | "staged" | string;
  row_count: number | null;
  content_fingerprint: string | null;
  previous_id: string | null;
  checkpoint?: Dict | null;
  error: string | null;
  created_by: string;
  created_at: string | null;
  promoted_at: string | null;
}

export interface WriterDestination {
  id: string;
  workspace_id: string;
  engine?: string;
  schema_name: string;
  tables: string[] | null;
  status: string;
}

// ----------------------------------------------------------------------------------- errors
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Dict;

  constructor(status: number, code: string, message: string, details: Dict = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/** Turn any thrown value into a message suitable for display. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    const extra = err.code === "invalid_input" && Array.isArray((err.details as { errors?: unknown[] }).errors)
      ? ": " + ((err.details as { errors: { loc?: unknown[]; msg?: string }[] }).errors
        .map((e) => `${(e.loc ?? []).slice(1).join(".")} ${e.msg ?? ""}`.trim()).join("; "))
      : "";
    return `${err.message}${extra}`;
  }
  if (err instanceof Error) return err.message;
  return String(err);
}

// ----------------------------------------------------------------------------------- token store
const TOKEN_KEY = "analystos.token";
const USER_KEY = "analystos.user";

function storageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: keep the session in memory only */
  }
}

let token: string | null = storageGet(TOKEN_KEY);
let unauthorizedHandler: (() => void) | null = null;

export const session = {
  get token(): string | null {
    return token;
  },
  get user(): User | null {
    const raw = storageGet(USER_KEY);
    if (!raw) return null;
    try {
      return JSON.parse(raw) as User;
    } catch {
      return null;
    }
  },
  set(t: string, user: User) {
    token = t;
    storageSet(TOKEN_KEY, t);
    storageSet(USER_KEY, JSON.stringify(user));
  },
  clear() {
    token = null;
    storageSet(TOKEN_KEY, null);
    storageSet(USER_KEY, null);
  },
  onUnauthorized(fn: (() => void) | null) {
    unauthorizedHandler = fn;
  },
};

// ----------------------------------------------------------------------------------- transport
export const API_BASE = "/api";

function authHeaders(): Record<string, string> {
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parseError(resp: Response): Promise<ApiError> {
  let code = `http_${resp.status}`;
  let message = resp.statusText || `request failed (${resp.status})`;
  let details: Dict = {};
  try {
    const body = (await resp.json()) as { error?: { code?: string; message?: string; details?: Dict }; detail?: unknown };
    if (body?.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      details = body.error.details ?? {};
    } else if (body?.detail) {
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    }
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(resp.status, code, message, details);
}

export async function request<T>(method: string, path: string, body?: unknown, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...authHeaders() };
  let payload: BodyInit | undefined;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, { ...init, method, headers: { ...headers, ...(init.headers as Record<string, string> | undefined) }, body: payload });
  } catch (err) {
    throw new ApiError(0, "network_error", `API unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!resp.ok) {
    const err = await parseError(resp);
    if (resp.status === 401 && !path.startsWith("/auth/login")) unauthorizedHandler?.();
    throw err;
  }
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

// ----------------------------------------------------------------------------------- dashboard builder (S08)
export type BuilderOp = { op: string } & Dict;
export interface BuilderField { name: string; type: "temporal" | "quantitative" | "nominal" | "ordinal" | "boolean"; label?: string; sensitivity?: string }
export interface BuilderChart {
  id: string; title: string; type: string; fields: BuilderField[]; labels?: string[]; sensitivity?: string;
  dataset_ref: { kind: string; name?: string; version?: number; handle_id?: string };
  encoding: { x?: string | null; y: string[]; series?: string | null; color?: string | null; aggregation?: string };
  query?: { version: number; compare: string };
  preview?: { columns?: string[]; rows?: unknown[][]; truncated?: boolean; row_count?: number };
  description?: string;
  provenance?: { sources: string[]; query_ids?: string[]; created_by: string; created_via?: string; turn_id?: string | null };
}
export interface BuilderDocument {
  schema_version?: "dashboard.v2"; id: string; title: string; description?: string; audience?: string; sensitivity?: string;
  layout: { columns?: number; items: { ref: string; x: number; y: number; w: number; h: number; tab?: string | null }[] };
  charts: BuilderChart[];
  kpis: { id: string; title: string; metric: { name: string; version: number }; value?: number | null; unit?: string; labels?: string[] }[];
  text_blocks: { id: string; markdown: string }[];
  filters: { id: string; field: string; op: string; value: unknown; scope?: string[]; resolved?: Dict }[];
  tabs?: { id: string; title: string }[];
}
export interface BuilderSummary {
  id: string; workspace_id: string; name: string; title?: string; version: number; status: string;
  platform: string | null; external_url: string | null; content_hash: string; updated_at: string | null;
}
export interface BuilderDashboard extends BuilderSummary { document: BuilderDocument }
/** CONV-06: a session view filter (a click or a chip). The server validates the field and value; never a predicate. */
export interface ViewFilter { field: string; op: "eq" | "in"; value: unknown; source?: string | null; label?: string; via?: string; note?: string }
export type ViewTileStatus = "filtered" | "not_filtered" | "source" | "saved_filter" | "refused" | "busy" | "error" | "skipped";
export interface ViewTile {
  id: string; kind: "chart" | "kpi"; status: ViewTileStatus; reason?: string; filtered_by?: string[];
  preview?: { columns: string[]; rows: unknown[][]; truncated?: boolean; row_count?: number }; value?: number | null;
  query_id?: string; cache_hit?: boolean;
}
export interface DashboardView {
  version: number; filters: ViewFilter[]; tiles: ViewTile[]; apply: BuilderOp[];
  limits: { max_concurrent: number; max_queries: number };
}
export interface DrillLevel { field: string; label: string; grain?: string }
export interface DrillPlan { kind: "time" | "dimension" | "none"; field?: string; levels?: DrillLevel[]; reason?: string }
export interface DashboardExplore {
  version: number;
  charts: { id: string; filter_fields: string[]; drill: DrillPlan }[];
  kpis: { id: string; dimensions: string[] }[];
  limits: { max_filters: number; max_concurrent: number; detail_rows: number };
}
export interface DrillStep { field: string; value: unknown; label?: string; grain?: string }
export interface DrillResult {
  chart_id: string; kind: DrillPlan["kind"]; depth: number; levels: DrillLevel[]; path: DrillStep[]; level: DrillLevel | null;
  filtered_by: string[]; preview?: ViewTile["preview"]; query_id?: string; status?: ViewTileStatus; reason?: string;
  detail?: { columns?: string[]; rows?: unknown[][]; hidden_columns?: number; truncated?: boolean; unavailable?: string; query_id?: string };
}
export interface EmbedToken { token: string; dashboard_uuid: string; superset_url: string | null; rls_clauses: number }

/** BI-VIEWS-02: an investigation's dashboards that can be published on their own, and the requests made so far. */
export interface CollectionDashboard {
  key: string; title: string; kind: "executive" | "operational" | "topic" | "other"; audience: string; charts: number;
  artifact_id: string; status: string; external_url: string | null;
}
export interface CollectionRequest {
  approval_id: string; status: string; collection: string[]; destination: string | null; payload_hash: string; risk_tier: string;
  created_at: string | null; expires_at: string | null; reason: string | null;
  publication: { id: string; status: string; error: string | null } | null;
}
export interface RunCollections {
  run_id: string; workspace_id: string; destination: string | null; destination_note: string | null;
  dashboards: CollectionDashboard[]; requests: CollectionRequest[];
}
export interface CollectionRequested {
  approval_id: string; status: string; risk_tier: string; destination: string | null; payload_hash: string; collection: string[]; expires_at: string;
}
export interface CollectionPublished { status: string; publication_id: string; collection: string[]; destination: string; urls: Record<string, string> }
/** An existing BI dashboard this workspace published, with the title grouping would give it. */
export interface GroupingChange { id: number; slug: string | null; from: string; to: string }
export interface GroupingItem extends GroupingChange { url: string | null; published: boolean; grouped: boolean }
export interface GroupingPlan {
  workspace_id: string; group: string; separator: string; available: boolean; reason: string | null;
  dashboards: GroupingItem[]; to_change: number;
}
export interface GroupingRequested { approval_id: string; status: string; risk_tier: string; payload_hash: string; changes: GroupingChange[]; expires_at: string }
/** A question the platform could not ground: the clarifying reply and questions it can answer instead (CONV-01). */
export interface BuilderClarify { clarify: string; kind?: string; examples?: string[] }
export interface BuilderRefusal { index: number; op: string; reason: string; suggestion?: BuilderOp | BuilderClarify }
export interface BuilderTurn {
  status: "applied" | "refused" | "unchanged" | "proposed" | "no_ops";
  dashboard: BuilderDashboard;
  proposal?: { ops: BuilderOp[]; answered_by: string; notes: string[]; model: string | null };
  refused?: BuilderRefusal[];
  deferred?: BuilderOp[];
  /** The applied ops; a chart from a question carries what answered it in `result`. */
  applied?: (BuilderOp & { result?: Partial<DraftTile> })[];
  forked_from?: string;
  approval?: { id: string; status: string; risk_tier: string };
  clarify?: BuilderClarify;
}
/** One planned tile of a draft (CONV-02): built, or listed with the reason it could not be. */
export interface DraftTile { tile: string; kind: "kpi" | "chart"; title: string; question?: string; reason?: string; clarify?: string;
  answered_by?: string | null; governance?: string; query_id?: string | null; chart_type?: string; rationale?: string; element?: string }
export interface BuilderDraft {
  status: "created" | "clarify";
  dashboard?: BuilderDashboard;
  clarify?: string;
  examples?: string[];
  plan: { title: string; audience: string; grounded_on: string; ranked_by: string; topic: string[] };
  built: DraftTile[];
  failed: DraftTile[];
}
export interface BuilderVersion { version: number; diff: BuilderOp[]; content_hash: string; created_by: string | null; created_at: string | null }

// ----------------------------------------------------------------------------------- report documents (CONV-04) and stories (CONV-05)
export type ReportOp = { op: string } & Dict;
export interface ReportSource { label: string; assets?: string[]; version?: number | null; content_hash?: string; labels?: string[] }
export type ReportDocBlock =
  | { kind: "text"; id: string; markdown: string; held?: string | null; generated?: string | null }
  | { kind: "chart"; id: string; title: string; chart_type: string; columns: string[]; rows: unknown[][]; caption?: string; sensitivity?: string; source: ReportSource; ref: Dict }
  | { kind: "kpi"; id: string; title: string; value: number | null; previous_value?: number | null; format?: string; unit?: string; sensitivity?: string; source: ReportSource; ref: Dict }
  | { kind: "finding"; id: string; title: string; text: string; confidence: number; verified: boolean; caveats?: string[]; sensitivity?: string; source: ReportSource; ref: Dict };
export interface ReportDocSection { id: string; title: string; blocks: ReportDocBlock[] }
export interface ReportDocDocument {
  schema_version?: "report_doc.v1"; id: string; title: string; objective?: string; sections: ReportDocSection[];
  sensitivity?: string; schedule_id?: string | null; origin?: Dict; derived?: Dict[];
}
export interface ReportDocSummary { id: string; workspace_id: string; name: string; version: number; status: string; content_hash: string; updated_at: string | null; title?: string; schedule_id?: string | null }
export interface ReportDocView extends ReportDocSummary { document: ReportDocDocument }
export interface ReportDocRefusal { index: number; op: string; reason: string; suggestion?: ReportOp }
export interface ReportExportRequest {
  status: "approval_required"; format: string; snapshot_hash: string; sensitivity: string;
  approval: { id: string; status: string; risk_tier: string };
}
export interface ReportDocTurn {
  status: "applied" | "refused" | "unchanged" | "proposed" | "no_ops";
  report: ReportDocView;
  proposal?: { ops: ReportOp[]; answered_by: string; notes: string[]; model: string | null };
  refused?: ReportDocRefusal[];
  deferred?: ReportOp[];
  forked_from?: string;
  export?: ReportExportRequest;
}
export interface ReportDocVersion { version: number; diff: ReportOp[]; content_hash: string; created_by: string | null; created_at: string | null }
export interface ReportContentItem { ref: Dict; title: string; kind: "chart" | "kpi" | "finding"; where: string }
export interface StoryFact { kind: string; text: string; score: number; chart_id: string | null; values: number[]; how: string }
export interface DashboardStory {
  dashboard_id: string; version: number; previous_version: number | null; title: string; shows: string;
  facts: StoryFact[]; changes: StoryFact[]; wording: "rules" | "llm_small"; notes: string[]; story_hash: string; markdown: string;
}
export type ReportDocFormat = "pdf" | "docx" | "pptx" | "html";

// ----------------------------------------------------------------------------------- generated contract
/*
 * Paths, path parameters, query parameters and request bodies come from the generated OpenAPI
 * types (src/generated/openapi.ts, `npm run gen:api` from the committed web/openapi.json). A
 * renamed route, a removed parameter or a changed request body is a typecheck error here rather
 * than a 404 or 422 at runtime. Response bodies are not in the schema yet: the routers return
 * untyped dicts (no `response_model`), so each call names its response shape from the
 * "Response shapes" section above.
 */
export type Schemas = components["schemas"];
type Method = "get" | "post" | "put" | "patch" | "delete";
type Operation<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;

/** Every OpenAPI path that declares method M. */
export type ApiPath<M extends Method> = {
  [P in keyof paths]: [NonNullable<paths[P][M]>] extends [never] ? never : P;
}[keyof paths];

type PathParams<O> = O extends { parameters: { path: infer X } } ? X : never;
type QueryParams<O> = O extends { parameters: { query?: infer X } } ? X : never;
type JsonBody<O> = O extends { requestBody?: infer R }
  ? NonNullable<R> extends { content: { "application/json": infer B } } ? B
    : NonNullable<R> extends { content: { "multipart/form-data": unknown } } ? FormData : never
  : never;

export type CallOptions<O> = ([PathParams<O>] extends [never] ? { path?: undefined } : { path: PathParams<O> })
  & { query?: QueryParams<O>; body?: JsonBody<O> };

function qs(params: object | undefined): string {
  if (!params) return "";
  const parts = Object.entries(params as Record<string, unknown>)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

/**
 * Build the request path (relative to API_BASE) for a typed OpenAPI route: fills `{name}`
 * segments (URL-encoded) and appends non-empty query parameters in the caller's order.
 */
export function apiPath<M extends Method, P extends ApiPath<M>>(_method: M, path: P, opts: CallOptions<Operation<P, M>>): string {
  const params = (opts.path ?? {}) as Record<string, string | number>;
  const filled = (path as string).replace(/\{(\w+)\}/g, (_m, k: string) => {
    if (params[k] === undefined) throw new Error(`missing path parameter ${k} for ${path}`);
    return encodeURIComponent(String(params[k]));
  });
  const rel = filled.startsWith(API_BASE) ? filled.slice(API_BASE.length) : filled;
  return rel + qs(opts.query as object | undefined);
}

/**
 * One typed call. The response is `unknown` because the schema does not describe it; each
 * endpoint below states its response shape. POSTs without a body send `{}`, as the routers
 * expect a JSON object.
 */
export function call<M extends Method, P extends ApiPath<M>>(method: M, path: P, opts: CallOptions<Operation<P, M>>): Promise<unknown> {
  const body = opts.body !== undefined ? opts.body : method === "post" ? {} : undefined;
  return request<unknown>(method.toUpperCase(), apiPath(method, path, opts), body);
}

const get = <P extends ApiPath<"get">>(path: P, opts: CallOptions<Operation<P, "get">>) => call("get", path, opts);
const post = <P extends ApiPath<"post">>(path: P, opts: CallOptions<Operation<P, "post">>) => call("post", path, opts);
const put = <P extends ApiPath<"put">>(path: P, opts: CallOptions<Operation<P, "put">>) => call("put", path, opts);
const patch = <P extends ApiPath<"patch">>(path: P, opts: CallOptions<Operation<P, "patch">>) => call("patch", path, opts);
const del = <P extends ApiPath<"delete">>(path: P, opts: CallOptions<Operation<P, "delete">>) => call("delete", path, opts);

/** Filename from a Content-Disposition header (attachment; filename="x.pdf"). */
export function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(header);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ""));
    } catch {
      /* fall through */
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1].trim() : fallback;
}

/** GET a binary file with the bearer header (an <a href> cannot send it). */
export async function downloadFile(path: string, fallbackName: string): Promise<DownloadedFile> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, { method: "GET", headers: { ...authHeaders() } });
  } catch (err) {
    throw new ApiError(0, "network_error", `API unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!resp.ok) {
    const err = await parseError(resp);
    if (resp.status === 401) unauthorizedHandler?.();
    throw err;
  }
  const blob = await resp.blob();
  return { blob, filename: filenameFromDisposition(resp.headers.get("Content-Disposition"), fallbackName) };
}

/** Hand a downloaded blob to the browser as a file save, via a short-lived object URL. */
export function saveBlob(file: DownloadedFile): void {
  const url = URL.createObjectURL(file.blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = file.filename;
  a.rel = "noopener";
  a.style.display = "none";
  document.body.appendChild(a);
  try {
    a.click();
  } finally {
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30_000);
  }
}

// ----------------------------------------------------------------------------------- endpoints
const W = (ws: string) => ({ workspace_id: ws });
/** The `If-Match` of an edit: the revision (version) the person was looking at. */
const ifMatch = (revision: number): RequestInit => ({ headers: { "If-Match": `"${revision}"` } });

export const api = {
  // auth
  login: (email: string, password: string) => post("/api/auth/login", { body: { email, password } }) as Promise<LoginResponse>,
  me: () => get("/api/auth/me", {}) as Promise<User>,
  authProviders: () => get("/api/auth/providers", {}) as Promise<AuthProviders>,
  users: () => get("/api/users", {}) as Promise<UserSummary[]>,

  // workspaces
  listWorkspaces: () => get("/api/workspaces", {}) as Promise<Workspace[]>,
  createWorkspace: (body: Schemas["WorkspaceIn"]) => post("/api/workspaces", { body }) as Promise<Workspace>,
  getWorkspace: (ws: string) =>
    get("/api/workspaces/{workspace_id}", { path: W(ws) }) as Promise<WorkspaceDetail>,

  // request desk (S13): intake, queue, detail, actions; value metrics (FR-053)
  listRequests: (q: { view?: "mine" | "queue" | "all"; workspace_id?: string; state?: RequestState; class?: RequestClass; limit?: number } = {}) =>
    get("/api/requests", { query: q }) as Promise<DeskRequest[]>,
  getRequest: (id: string) => get("/api/requests/{request_id}", { path: { request_id: id } }) as Promise<DeskRequest>,
  createRequest: (body: Schemas["RequestIn"]) => post("/api/requests", { body }) as Promise<CreatedRequest>,
  requestAction: (id: string, type: RequestActionType, payload: Dict = {}) =>
    post("/api/requests/{request_id}/actions", { path: { request_id: id }, body: { type, payload } }) as Promise<DeskRequest>,
  value: (ws: string, q: { from?: string; to?: string } = {}) =>
    get("/api/value", { query: { workspace: ws, ...q } }) as Promise<ValueReport>,
  refreshValue: (ws: string, q: { from?: string; to?: string } = {}) =>
    post("/api/value/rollup", { query: { workspace: ws, ...q } }) as Promise<{ workspace_id: string; from: string; to: string; rows: number }>,
  exportValue: (ws: string, q: { from?: string; to?: string } = {}) =>
    downloadFile(apiPath("get", "/api/value/export", { query: { workspace: ws, ...q } }), `desk-value-${ws}.csv`),
  valueBaselines: (ws: string) => get("/api/value/baselines", { query: { workspace: ws } }) as Promise<ClassBaseline[]>,
  putValueBaseline: (body: Schemas["BaselineIn"]) => put("/api/value/baselines", { body }) as Promise<ClassBaseline>,
  getWorkModes: (ws: string) =>
    get("/api/workspaces/{workspace_id}/work-modes", { path: W(ws) }) as Promise<WorkModesPlan>,
  previewWorkModes: (ws: string, modes: WorkMode[]) =>
    post("/api/workspaces/{workspace_id}/work-modes/preview", { path: W(ws), body: { modes } }) as Promise<WorkModesPlan>,
  setWorkModes: (ws: string, modes: WorkMode[]) =>
    put("/api/workspaces/{workspace_id}/work-modes", { path: W(ws), body: { modes } }) as Promise<WorkModesPlan>,
  workspaceInventory: (ws: string) =>
    get("/api/workspaces/{workspace_id}/inventory", { path: W(ws) }) as Promise<WorkspaceInventory>,
  updateWorkspace: (ws: string, body: Schemas["WorkspacePatch"]) =>
    patch("/api/workspaces/{workspace_id}", { path: W(ws), body }) as Promise<Workspace>,
  setWorkspaceStatus: (ws: string, status: "active" | "disabled") =>
    patch("/api/workspaces/{workspace_id}", { path: W(ws), body: { status } }) as Promise<Workspace>,
  putPolicy: (ws: string, policy: Dict) =>
    put("/api/workspaces/{workspace_id}/policy", { path: W(ws), body: policy }) as Promise<{ policy_version: number }>,
  addMember: (ws: string, email: string, role: string) =>
    post("/api/workspaces/{workspace_id}/members", { path: W(ws), body: { email, role } }) as Promise<{ user_id: string; role: string }>,
  removeMember: (ws: string, userId: string) =>
    del("/api/workspaces/{workspace_id}/members/{user_id}", { path: { workspace_id: ws, user_id: userId } }) as Promise<{ removed: boolean }>,
  putWorkspaceOwners: (ws: string, body: Schemas["OwnersIn"]) =>
    put("/api/workspaces/{workspace_id}/owners", { path: W(ws), body }) as Promise<{ workspace_id: string; owners: Owners }>,
  putSourceOwners: (ws: string, sourceId: string, body: Schemas["OwnersIn"]) =>
    put("/api/workspaces/{workspace_id}/sources/{source_id}/owners", { path: { workspace_id: ws, source_id: sourceId }, body }) as
      Promise<{ source_id: string; owners: Owners }>,
  pilotReadiness: (ws: string) =>
    get("/api/workspaces/{workspace_id}/pilot-readiness", { path: W(ws) }) as Promise<PilotReadiness>,
  workspaceAudit: (ws: string, limit = 200) =>
    get("/api/workspaces/{workspace_id}/audit", { path: W(ws), query: { limit } }) as Promise<AuditEvent[]>,
  activity: (ws: string, afterId = 0, limit = 100) =>
    get("/api/workspaces/{workspace_id}/activity", { path: W(ws), query: { after_id: afterId, limit } }) as Promise<RunEvent[]>,

  // sources
  listSources: (ws: string) => get("/api/workspaces/{workspace_id}/sources", { path: W(ws) }) as Promise<Source[]>,
  addSource: (ws: string, body: SourceInput) =>
    post("/api/workspaces/{workspace_id}/sources", { path: W(ws), body }) as Promise<Source>,
  /** Rename a source or correct its connection (host, tables, other config, secret reference); only the
    * fields sent change. The kind cannot change — add a new source for a different connector. */
  updateSource: (ws: string, sourceId: string, body: SourceUpdatePatch) =>
    patch("/api/workspaces/{workspace_id}/sources/{source_id}", { path: { workspace_id: ws, source_id: sourceId }, body }) as Promise<Source>,
  sourceKinds: () => get("/api/source-kinds", {}) as Promise<SourceKindInfo[]>,
  discover: (ws: string, sourceId: string) =>
    post("/api/workspaces/{workspace_id}/sources/{source_id}/discover", { path: { workspace_id: ws, source_id: sourceId } }) as Promise<DiscoverResponse>,
  selectAssets: (ws: string, sourceId: string, assets: string[]) =>
    put("/api/workspaces/{workspace_id}/sources/{source_id}/selection", { path: { workspace_id: ws, source_id: sourceId }, body: { assets } }) as Promise<{ selected: string[]; loaded: Dict[] }>,
  upload: (ws: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return post("/api/workspaces/{workspace_id}/uploads", { path: W(ws), body: fd }) as Promise<{ path: string; bytes: number }>;
  },
  listAssets: (ws: string) => get("/api/workspaces/{workspace_id}/assets", { path: W(ws) }) as Promise<Asset[]>,
  tagColumn: (assetId: string, column: string, tags: string[]) =>
    put("/api/assets/{asset_id}/columns/{column}/tags", { path: { asset_id: assetId, column }, body: { tags } }) as Promise<SourceColumn>,
  relationships: (ws: string) =>
    get("/api/workspaces/{workspace_id}/relationships", { path: W(ws) }) as Promise<Relationship[]>,

  // metadata crawls & catalog
  startCrawl: (ws: string, sourceId: string, body: CrawlInput = {}) =>
    post("/api/workspaces/{workspace_id}/sources/{source_id}/crawl", { path: { workspace_id: ws, source_id: sourceId }, body }) as Promise<Crawl>,
  listCrawls: (ws: string, sourceId?: string) =>
    get("/api/workspaces/{workspace_id}/crawls", { path: W(ws), query: { source_id: sourceId } }) as Promise<Crawl[]>,
  getCrawl: (id: string) => get("/api/crawls/{crawl_id}", { path: { crawl_id: id } }) as Promise<Crawl>,
  catalog: (ws: string, filter: CatalogFilter = {}) =>
    get("/api/workspaces/{workspace_id}/catalog", {
      path: W(ws),
      query: { q: filter.q, domain: filter.domain, role: filter.role, include_deprecated: filter.include_deprecated ? true : undefined },
    }) as Promise<CatalogAsset[]>,
  curateAsset: (assetId: string, body: AssetMetadataPatch) =>
    patch("/api/assets/{asset_id}/metadata", { path: { asset_id: assetId }, body }) as Promise<CuratedAsset>,

  // analysis
  startRun: (ws: string, body: Schemas["RunIn"]) =>
    post("/api/workspaces/{workspace_id}/analysis", { path: W(ws), body }) as Promise<Run>,
  listRuns: (ws: string) => get("/api/workspaces/{workspace_id}/analysis", { path: W(ws) }) as Promise<Run[]>,
  getRun: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/analysis/{run_id}", { path: { workspace_id: ws, run_id: run } }) as Promise<RunDetail>,
  controlRun: (ws: string, run: string, action: "pause" | "resume" | "cancel") =>
    post(`/api/workspaces/{workspace_id}/analysis/{run_id}/${action}`, { path: { workspace_id: ws, run_id: run } }) as Promise<Run>,
  feedback: (ws: string, run: string, body: Schemas["FeedbackIn"]) =>
    post("/api/workspaces/{workspace_id}/analysis/{run_id}/feedback", { path: { workspace_id: ws, run_id: run }, body }) as Promise<FeedbackResponse>,
  console: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/analysis/{run_id}/console", { path: { workspace_id: ws, run_id: run } }) as Promise<ConsoleData>,
  agentRun: (taskId: string) =>
    get("/api/agent-runs/{task_id}", { path: { task_id: taskId } }) as Promise<AgentRunDetail>,
  patchHypothesis: (id: string, body: Schemas["HypothesisPatch"]) =>
    patch("/api/hypotheses/{hypothesis_id}", { path: { hypothesis_id: id }, body }) as Promise<Hypothesis>,
  ask: (ws: string, question: string) =>
    post("/api/workspaces/{workspace_id}/ask", { path: W(ws), body: { question } }) as Promise<AskResponse>,
  query: (ws: string, sql: string, maxRows?: number) =>
    post("/api/workspaces/{workspace_id}/query", { path: W(ws), body: { sql, max_rows: maxRows ?? null } }) as Promise<QueryResult>,
  explainQuery: (ws: string, sql: string, maxRows?: number) =>
    post("/api/workspaces/{workspace_id}/query/explain", { path: W(ws), body: { sql, max_rows: maxRows ?? null } }) as Promise<SqlExplanation>,

  // artifacts, insights, approvals
  listArtifacts: (ws: string, filter: { type?: string; run_id?: string } = {}) =>
    get("/api/workspaces/{workspace_id}/artifacts", { path: W(ws), query: { type: filter.type, run_id: filter.run_id } }) as Promise<Artifact[]>,
  getArtifact: (id: string) => get("/api/artifacts/{artifact_id}", { path: { artifact_id: id } }) as Promise<ArtifactDetail>,
  getQuery: (id: string) => get("/api/queries/{query_id}", { path: { query_id: id } }) as Promise<QueryExecution>,
  listInsights: (ws: string) => get("/api/workspaces/{workspace_id}/insights", { path: W(ws) }) as Promise<Insight[]>,
  getInsight: (id: string) => get("/api/insights/{insight_id}", { path: { insight_id: id } }) as Promise<InsightDetail>,
  runLineage: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/lineage", { path: W(ws), query: { run_id: run } }) as Promise<RunLineage>,
  listApprovals: (ws: string, status?: string) =>
    get("/api/workspaces/{workspace_id}/approvals", { path: W(ws), query: { status } }) as Promise<Approval[]>,
  approve: (id: string, reason?: string) =>
    post("/api/approvals/{approval_id}/approve", { path: { approval_id: id }, body: { reason: reason || null } }) as Promise<Approval>,
  reject: (id: string, reason?: string) =>
    post("/api/approvals/{approval_id}/reject", { path: { approval_id: id }, body: { reason: reason || null } }) as Promise<Approval>,
  rollback: (publicationId: string) =>
    post("/api/publications/{publication_id}/rollback", { path: { publication_id: publicationId } }) as Promise<{ removed: unknown; run_id: string | null }>,

  // schedules (§37)
  listSchedules: (ws: string) => get("/api/workspaces/{workspace_id}/schedules", { path: W(ws) }) as Promise<Schedule[]>,
  createSchedule: (ws: string, body: ScheduleInput) =>
    post("/api/workspaces/{workspace_id}/schedules", { path: W(ws), body }) as Promise<Schedule>,
  updateSchedule: (id: string, body: Schemas["SchedulePatch"]) =>
    patch("/api/schedules/{schedule_id}", { path: { schedule_id: id }, body }) as Promise<Schedule>,
  deleteSchedule: (id: string) =>
    del("/api/schedules/{schedule_id}", { path: { schedule_id: id } }) as Promise<{ deleted: boolean }>,
  runScheduleNow: (id: string) =>
    post("/api/schedules/{schedule_id}/run", { path: { schedule_id: id } }) as Promise<ScheduleRun>,

  // approved external delivery (N-3): authorization is decided in the approvals inbox
  listDeliveryDestinations: (ws: string) =>
    get("/api/workspaces/{workspace_id}/delivery-destinations", { path: W(ws) }) as Promise<DeliveryDestination[]>,
  createDeliveryDestination: (ws: string, body: Schemas["DeliveryDestinationIn"]) =>
    post("/api/workspaces/{workspace_id}/delivery-destinations", { path: W(ws), body }) as Promise<DeliveryDestination>,
  reauthorizeDeliveryDestination: (id: string) =>
    post("/api/delivery-destinations/{destination_id}/reauthorize", { path: { destination_id: id } }) as Promise<DeliveryDestination>,
  revokeDeliveryDestination: (id: string) =>
    post("/api/delivery-destinations/{destination_id}/revoke", { path: { destination_id: id } }) as Promise<DeliveryDestination>,
  listDeliveries: (ws: string) =>
    get("/api/workspaces/{workspace_id}/deliveries", { path: W(ws) }) as Promise<Delivery[]>,
  redriveDelivery: (id: string) =>
    post("/api/deliveries/{delivery_id}/redrive", { path: { delivery_id: id } }) as Promise<Delivery>,

  // monitors & alerts (§38)
  listMonitors: (ws: string) => get("/api/workspaces/{workspace_id}/monitors", { path: W(ws) }) as Promise<Monitor[]>,
  createMonitor: (ws: string, body: Schemas["MonitorIn"] & { config: MonitorConfig }) =>
    post("/api/workspaces/{workspace_id}/monitors", { path: W(ws), body }) as Promise<Monitor>,
  updateMonitor: (id: string, body: Schemas["MonitorPatch"]) =>
    patch("/api/monitors/{monitor_id}", { path: { monitor_id: id }, body }) as Promise<Monitor>,
  evaluateMonitor: (id: string) =>
    post("/api/monitors/{monitor_id}/evaluate", { path: { monitor_id: id } }) as Promise<MonitorResult>,
  monitorSeries: (id: string) =>
    get("/api/monitors/{monitor_id}/series", { path: { monitor_id: id } }) as Promise<MonitorSeries>,
  listAlerts: (ws: string, status?: string) =>
    get("/api/workspaces/{workspace_id}/alerts", { path: W(ws), query: { status } }) as Promise<Alert[]>,
  alertAction: (id: string, action: "acknowledge" | "resolve") =>
    post("/api/alerts/{alert_id}/{action}", { path: { alert_id: id, action } }) as Promise<Alert>,
  investigateAlert: (id: string) =>
    post("/api/alerts/{alert_id}/{action}", { path: { alert_id: id, action: "investigate" } }) as Promise<{ run_id: string | null }>,

  // notifications
  notifications: (unread = false) =>
    get("/api/notifications", { query: { unread: unread ? true : undefined } }) as Promise<AppNotification[]>,
  markNotificationsRead: (ids: number[]) =>
    post("/api/notifications/read", { body: { ids } }) as Promise<{ marked: number }>,

  // reports (§43)
  createReport: (ws: string, body: { run_id?: string | null; kind: string; formats: string[] }) =>
    post("/api/workspaces/{workspace_id}/reports", { path: W(ws), body: { ...body, run_id: body.run_id || null } }) as Promise<Artifact>,
  downloadReport: (id: string, format: string) =>
    downloadFile(apiPath("get", "/api/artifacts/{artifact_id}/download", { path: { artifact_id: id }, query: { format } }), `report.${format}`),

  // admin / registry
  agents: () => get("/api/agents", {}) as Promise<AgentSpec[]>,
  patchAgent: (id: string, enabled: boolean) =>
    patch("/api/agents/{agent_id}", { path: { agent_id: id }, body: { enabled } }) as Promise<AgentSpec>,
  tools: () => get("/api/tools", {}) as Promise<ToolSpec[]>,
  patchTool: (id: string, enabled: boolean) =>
    patch("/api/tools/{tool_id}", { path: { tool_id: id }, body: { enabled } }) as Promise<ToolSpec>,
  skills: () => get("/api/skills", {}) as Promise<SkillSpec[]>,
  models: () => get("/api/admin/models", {}) as Promise<ModelsView>,
  /** Dependency health for the status banner; `signal` lets the caller give up on a hung API. */
  health: (signal?: AbortSignal) => request<Health>("GET", apiPath("get", "/api/health", {}), undefined, { signal }),
  modelHealth: (probe = false) =>
    get("/api/admin/models/health", { query: probe ? { probe: true } : {} }) as Promise<ModelHealth>,
  usage: () => get("/api/admin/usage", {}) as Promise<Usage>,
  modelsEffective: (workspace?: string) =>
    get("/api/admin/models/effective", { query: workspace ? { workspace } : {} }) as Promise<EffectiveModels>,
  modelOverride: (scope: "tenant" | "workspace", workspace?: string) =>
    get("/api/admin/models/config", { query: { scope, ...(workspace ? { workspace } : {}) } }) as Promise<ModelOverrideDoc>,
  /** A new version of the scope's override; If-Match is the version the person edited ("0" before the first). */
  putModelOverride: (scope: "tenant" | "workspace", version: number, document: Dict, note: string,
    workspace?: string, switchOverrideReason?: string) =>
    request<{ scope: string; version: number; changed: boolean; model_switch_gate?: SwitchGateResult }>("PUT",
      apiPath("put", "/api/admin/models/config", { query: { scope, ...(workspace ? { workspace } : {}) },
        body: { document, note, switch_override_reason: switchOverrideReason ?? null } }),
      { document, note, switch_override_reason: switchOverrideReason ?? null }, ifMatch(version)),
  rollbackModelOverride: (scope: "tenant" | "workspace", version: number, workspace?: string, switchOverrideReason?: string) =>
    post("/api/admin/models/config/rollback", { body: { scope, version, workspace: workspace ?? null,
      switch_override_reason: switchOverrideReason ?? null } }) as Promise<{ version: number; rolled_back_to: number }>,
  modelProviders: () => get("/api/admin/models/providers", {}) as Promise<ProvidersView>,
  /** One live, billable ping through the router (recorded as a model call). */
  testModelProvider: (provider: string) =>
    post("/api/admin/models/providers/{provider_id}/test", { path: { provider_id: provider } }) as Promise<ProviderTest>,
  modelEvaluations: (purpose?: string) =>
    get("/api/admin/models/evaluations", { query: purpose ? { purpose } : {} }) as Promise<ModelEvaluations>,
  runModelEvaluation: (purpose: string, model?: string) =>
    post("/api/admin/models/evaluations/run", { body: { purpose, model: model ?? null } }) as Promise<ModelEvaluation>,
  modelsAirgap: () => get("/api/admin/models/airgap", {}) as Promise<AirgapPosture>,
  audit: (limit = 300) => get("/api/admin/audit", { query: { limit } }) as Promise<AuditEvent[]>,

  // platform settings (admin only)
  adminSettings: () => get("/api/admin/settings", {}) as Promise<SettingsDocument>,
  updateSettings: (patchDoc: Dict, note: string) =>
    put("/api/admin/settings", { body: { patch: patchDoc, note } }) as Promise<SettingsUpdateResult>,
  applyPreset: (preset: string, switchOverrideReason?: string) =>
    post("/api/admin/settings/preset", { body: switchOverrideReason ? { preset, switch_override_reason: switchOverrideReason } : { preset } }) as Promise<SettingsUpdateResult>,
  settingsHistory: () => get("/api/admin/settings/history", {}) as Promise<SettingsVersion[]>,
  rollbackSettings: (version: number) =>
    post("/api/admin/settings/rollback", { body: { version } }) as Promise<SettingsUpdateResult>,
  prompts: () => get("/api/admin/prompts", {}) as Promise<PromptTemplate[]>,
  tokenSavings: (days = 30) => get("/api/admin/token-savings", { query: { days } }) as Promise<TokenSavings>,

  // "Why this number?" (P7-08)
  whyInsight: (id: string, q: { number?: string; fact_id?: string } = {}) =>
    get("/api/insights/{insight_id}/why", { path: { insight_id: id }, query: q }) as Promise<WhyResponse>,
  whyRun: (ws: string, run: string) =>
    get("/api/workspaces/{workspace_id}/analysis/{run_id}/why", { path: { workspace_id: ws, run_id: run } }) as Promise<WhyRunResponse>,
  /** An Ask answer's numbers, each traced fact -> query receipt -> data version -> definition -> verdict. */
  whyAskTurn: (turnId: string) => get("/api/ask/turns/{turn_id}/why", { path: { turn_id: turnId } }) as Promise<WhyResponse>,
  /** Measured and document evidence as separate citation kinds, with document/data conflicts (N-8). */
  insightCitations: (id: string) =>
    get("/api/insights/{insight_id}/citations", { path: { insight_id: id } }) as Promise<CitationsResponse>,
  askTurnCitations: (turnId: string) =>
    get("/api/ask/turns/{turn_id}/citations", { path: { turn_id: turnId } }) as Promise<CitationsResponse>,
  /** A new verdict by the subject's own deterministic path; the old (VOID) record stays readable. */
  reverify: (recordId: string) =>
    post("/api/verification/{record_id}/reverify", { path: { record_id: recordId } }) as Promise<ReverifyResult>,

  // schedule pins (P7-03)
  getSchedule: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/schedules/{schedule_id}", { path: { workspace_id: ws, schedule_id: id } }) as Promise<ScheduleDetail>,
  /** Accept the upgrade the owner reviewed: bound to its revision (If-Match) and `upgrade_hash`. */
  upgradeSchedule: (ws: string, id: string, revision: number, upgradeHash: string | null) =>
    request<ScheduleUpgradeResult>("POST", apiPath("post", "/api/workspaces/{workspace_id}/schedules/{schedule_id}/upgrade",
      { path: { workspace_id: ws, schedule_id: id } }), { upgrade_hash: upgradeHash } satisfies Schemas["UpgradeIn"],
    { headers: { "If-Match": `"${revision}"` } }),

  // definitions: drafts, publish, deprecate, retire, diff (P7-03)
  listDefinitions: (ws: string, q: { kind?: string; status?: string; include_builtin?: boolean; cursor?: string } = {}) =>
    get("/api/workspaces/{workspace_id}/definitions", { path: W(ws), query: q }) as Promise<DefinitionPage>,
  getDefinition: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/definitions/{definition_id}", { path: { workspace_id: ws, definition_id: id } }) as Promise<DefinitionVersion>,
  updateDefinition: (ws: string, id: string, revision: number, body: { title: string; spec: Dict }) =>
    request<DefinitionVersion>("PATCH", apiPath("patch", "/api/workspaces/{workspace_id}/definitions/{definition_id}",
      { path: { workspace_id: ws, definition_id: id } }), body, { headers: { "If-Match": `"${revision}"` } }),
  publishDefinition: (ws: string, id: string, revision: number) =>
    request<DefinitionVersion>("POST", apiPath("post", "/api/workspaces/{workspace_id}/definitions/{definition_id}/publish",
      { path: { workspace_id: ws, definition_id: id } }), {}, { headers: { "If-Match": `"${revision}"` } }),
  changeDefinitionStatus: (ws: string, id: string, action: "deprecate" | "retire", revision: number, reason?: string) =>
    request<DefinitionVersion>("POST", apiPath("post", "/api/workspaces/{workspace_id}/definitions/{definition_id}/{action}",
      { path: { workspace_id: ws, definition_id: id, action } }), { reason: reason ?? null } satisfies Schemas["ReasonIn"],
    { headers: { "If-Match": `"${revision}"` } }),
  agentFormOptions: (ws: string) =>
    get("/api/workspaces/{workspace_id}/agent-form", { path: W(ws) }) as Promise<AgentFormOptions>,
  createAgentFromForm: (ws: string, body: AgentFormInput) =>
    post("/api/workspaces/{workspace_id}/agent-form", { path: W(ws), body }) as Promise<DefinitionVersion>,
  updateAgentFromForm: (ws: string, id: string, revision: number, body: AgentFormInput) =>
    request<DefinitionVersion>("PUT", apiPath("put", "/api/workspaces/{workspace_id}/agent-form/{definition_id}",
      { path: { workspace_id: ws, definition_id: id } }), body, { headers: { "If-Match": `"${revision}"` } }),
  definitionDiff: (ws: string, id: string, against?: string) =>
    get("/api/workspaces/{workspace_id}/definitions/{definition_id}/diff",
      { path: { workspace_id: ws, definition_id: id }, query: { against } }) as Promise<DefinitionDiff>,

  // playbook editor (S06-T6): YAML drafts, dry-run simulation, signing; publishing is publishDefinition
  playbookEditorOptions: (ws: string) =>
    get("/api/workspaces/{workspace_id}/playbook-editor", { path: W(ws) }) as Promise<PlaybookEditorOptions>,
  validatePlaybook: (ws: string, yaml: string) =>
    post("/api/workspaces/{workspace_id}/playbook-editor/validate", { path: W(ws), body: { yaml } }) as Promise<PlaybookCheck>,
  simulatePlaybook: (ws: string, body: { yaml?: string; definition_id?: string; fixture?: string | Dict }) =>
    post("/api/workspaces/{workspace_id}/playbook-editor/simulate", { path: W(ws), body }) as Promise<SimulationReport>,
  createPlaybookDraft: (ws: string, yaml: string, title?: string) =>
    post("/api/workspaces/{workspace_id}/playbook-editor/drafts", { path: W(ws), body: { yaml, title: title || null } }) as Promise<PlaybookDraft>,
  getPlaybookDraft: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}",
      { path: { workspace_id: ws, definition_id: id } }) as Promise<PlaybookDraft>,
  updatePlaybookDraft: (ws: string, id: string, revision: number, yaml: string, title?: string) =>
    request<PlaybookDraft>("PUT", apiPath("put", "/api/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}",
      { path: { workspace_id: ws, definition_id: id } }), { yaml, title: title || null }, ifMatch(revision)),
  simulatePlaybookDraft: (ws: string, id: string, revision: number, fixtures?: string[]) =>
    request<PlaybookDraft>("POST", apiPath("post", "/api/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}/simulate",
      { path: { workspace_id: ws, definition_id: id } }), { fixtures: fixtures?.length ? fixtures : null }, ifMatch(revision)),
  requestPlaybookSignature: (ws: string, id: string, revision: number) =>
    request<{ definition: PlaybookDraft; approval: Approval }>("POST", apiPath("post",
      "/api/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}/request-signature",
      { path: { workspace_id: ws, definition_id: id } }), {}, ifMatch(revision)),

  // recipes and file ingestion: Work → Prepare data (P6-04..07)
  listRecipes: (ws: string) => get("/api/workspaces/{workspace_id}/recipes", { path: W(ws) }) as Promise<Recipe[]>,
  saveRecipe: (ws: string, spec: Dict) =>
    post("/api/workspaces/{workspace_id}/recipes", { path: W(ws), body: { spec } }) as Promise<Recipe>,
  publishRecipe: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/recipes/{recipe_id}/publish", { path: { workspace_id: ws, recipe_id: id } }) as Promise<Recipe>,
  runRecipe: (ws: string, id: string, body: Schemas["RecipeRunIn"]) =>
    post("/api/workspaces/{workspace_id}/recipes/{recipe_id}/runs", { path: { workspace_id: ws, recipe_id: id }, body }) as Promise<RecipeRun>,
  listRecipeRuns: (ws: string, recipe?: string) =>
    get("/api/workspaces/{workspace_id}/recipe-runs", { path: W(ws), query: { recipe } }) as Promise<RecipeRun[]>,
  ingestFile: (ws: string, sourceId: string, body: Schemas["IngestSpec"]) =>
    post("/api/workspaces/{workspace_id}/sources/{source_id}/ingest", { path: { workspace_id: ws, source_id: sourceId }, body }) as Promise<IngestResult>,

  // relationship review queue and semantic diff (P7-09, P4-05)
  relationshipCandidates: (ws: string, status?: string) =>
    get("/api/workspaces/{workspace_id}/semantic/relationships/candidates", { path: W(ws), query: { status } }) as Promise<RelationshipCandidate[]>,
  decideRelationship: (ws: string, id: string, decision: "accept" | "reject", reason?: string) =>
    post(`/api/workspaces/{workspace_id}/semantic/relationships/candidates/{candidate_id}/${decision}`,
      { path: { workspace_id: ws, candidate_id: id }, body: { reason: reason ?? null } }) as Promise<RelationshipCandidate>,
  measureRelationship: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/semantic/relationships/candidates/{candidate_id}/measure",
      { path: { workspace_id: ws, candidate_id: id } }) as Promise<RelationshipCandidate>,
  /** The data model suggested from the catalog, keys and measured joins (deterministic, no model). */
  modelSuggestion: (ws: string) =>
    get("/api/workspaces/{workspace_id}/semantic/model/suggestion", { path: W(ws) }) as Promise<ModelSuggestion>,
  /** Measure key uniqueness and join fan-out through the gateway, as the caller (bounded). */
  validateModelSuggestion: (ws: string) =>
    post("/api/workspaces/{workspace_id}/semantic/model/suggestion/validate", { path: W(ws) }) as Promise<ModelValidation>,
  /** Turn the suggestion into a proposed model version and queue its joins for review; a different person approves. */
  proposeModelSuggestion: (ws: string) =>
    post("/api/workspaces/{workspace_id}/semantic/model/suggestion/propose", { path: W(ws) }) as
      Promise<{ model_version: number | null; status: "proposed" | "unchanged" | string; candidates_queued: number }>,
  /** Measure relationship candidates (single-column and composite) and queue them for review. */
  discoverRelationships: (ws: string) =>
    post("/api/workspaces/{workspace_id}/semantic/relationships/discover", { path: W(ws), body: {} }) as Promise<RelationshipCandidate[]>,
  semanticModelDiff: (ws: string, version?: number) =>
    get("/api/workspaces/{workspace_id}/semantic/model/diff", { path: W(ws), query: { version } }) as Promise<SemanticModelDiff>,
  decideSemanticModel: (ws: string, version: number, decision: "approve" | "reject", reason?: string) =>
    post(`/api/workspaces/{workspace_id}/semantic/model/${decision}`, { path: W(ws), body: { version, reason: reason ?? null } }) as Promise<Dict>,

  // capability registry (P4-X01)
  listCapabilities: (filter: { kind?: string; workspace_id?: string } = {}) =>
    get("/api/capabilities", { query: filter }) as Promise<CapabilityList>,
  getCapability: (id: string) =>
    get("/api/capabilities/{capability_id}", { path: { capability_id: id } }) as Promise<CapabilityManifest>,
  setCapabilityEnabled: (ws: string, id: string, enabled: boolean) =>
    put("/api/workspaces/{workspace_id}/capabilities/{capability_id}", { path: { workspace_id: ws, capability_id: id }, body: { enabled } }) as
      Promise<Dict>,
  reloadCapabilities: () => post("/api/admin/capabilities/reload", {}) as Promise<CapabilityReload>,
  mcpCapabilities: (ws: string) => get("/api/workspaces/{workspace_id}/mcp/capabilities", { path: W(ws) }) as Promise<CapabilityManifest[]>,
  invokeMcpTool: (ws: string, server: string, tool: string, args: Dict) =>
    post("/api/workspaces/{workspace_id}/mcp/servers/{server_name}/tools/{tool_name}/invoke",
      { path: { workspace_id: ws, server_name: server, tool_name: tool }, body: { arguments: args } }) as Promise<CapabilityInvocation>,
  /** Built-in or plugin capability (capabilities/invoke.py): read-only runs now; a side effect answers 202 + approval. */
  invokeCapability: (ws: string, id: string, args: Dict, approvalId?: string) =>
    post("/api/workspaces/{workspace_id}/capabilities/{capability_id}/invoke",
      { path: { workspace_id: ws, capability_id: id }, body: approvalId ? { arguments: args, approval_id: approvalId } : { arguments: args } }) as Promise<CapabilityInvocation>,
  // tool, agent and data-product registry (S10): v2 API, toolreg/
  listRegistryTools: (ws: string, filter: { kind?: string; q?: string; include_quarantined?: boolean } = {}) =>
    get("/v2/tools", { query: { workspace_id: ws, ...filter } }) as Promise<RegistryTool[]>,
  getRegistryTool: (ws: string, toolId: string) =>
    get("/v2/tools/{tool_id}", { path: { tool_id: toolId }, query: W(ws) }) as Promise<RegistryToolDetail>,
  registryToolUsage: (ws: string, toolId: string, days = 7) =>
    get("/v2/tools/{tool_id}/usage", { path: { tool_id: toolId }, query: { workspace_id: ws, days } }) as Promise<ToolUsage>,
  publishTool: (body: ToolPublishBody) => post("/v2/tools:publish", { body }) as Promise<ToolPublished>,
  invokeRegistryTool: (ws: string, ref: string, args: Dict, approvalId?: string) =>
    post("/v2/tools/{tool_id}:invoke", { path: { tool_id: ref }, body: { workspace_id: ws, arguments: args, approval_id: approvalId ?? null } }) as
      Promise<ToolInvocationResult>,
  deprecateRegistryTool: (ws: string, toolId: string, opts: { version?: string; reason?: string; retire?: boolean } = {}) =>
    post("/v2/tools/{tool_id}:deprecate", { path: { tool_id: toolId }, body: { workspace_id: ws, version: opts.version ?? null,
      reason: opts.reason ?? null, retire: !!opts.retire } }) as Promise<{ tool_id: string; status: string; versions: string[] }>,
  classifyRegistryTool: (ws: string, toolId: string, sideEffect: string) =>
    post("/v2/tools/{tool_id}:classify", { path: { tool_id: toolId }, body: { workspace_id: ws, side_effect: sideEffect } }) as
      Promise<ToolVersionRow>,
  toolSigningKeys: (ws: string) => get("/v2/tool-keys", { query: W(ws) }) as Promise<ToolSigningKey[]>,
  rotateToolSigningKey: (ws: string, revokePrevious: boolean) =>
    post("/v2/tool-keys:rotate", { body: { workspace_id: ws, revoke_previous: revokePrevious, resign: true } }) as
      Promise<{ key_id: string; public_key: string; resigned: number }>,
  // external MCP servers (P4-X05) and their admission into the registry (S10-T4)
  listMcpServers: (ws: string) => get("/api/workspaces/{workspace_id}/mcp/servers", { path: W(ws) }) as Promise<McpServerRow[]>,
  registerMcpServer: (ws: string, body: Schemas["ServerIn"]) =>
    post("/api/workspaces/{workspace_id}/mcp/servers", { path: W(ws), body }) as Promise<McpServerRow>,
  allowMcpServer: (ws: string, serverId: string, allowed: boolean) =>
    post("/api/workspaces/{workspace_id}/mcp/servers/{server_id}/allow", { path: { workspace_id: ws, server_id: serverId }, body: { allowed } }) as
      Promise<McpServerRow>,
  admitMcpServer: (ws: string, serverId: string) =>
    post("/v2/mcp-servers/{server_id}:admit", { path: { server_id: serverId }, body: W(ws) }) as Promise<McpAdmission>,
  // MCP / API clients of the platform and their grants (P4-X06, S10-T3), platform admins only
  listMcpClients: () => get("/api/admin/mcp/clients", {}) as Promise<McpClientRow[]>,
  createMcpClient: (name: string) =>
    post("/api/admin/mcp/clients", { body: { name } }) as Promise<{ client_id: string; name: string; token: string; note: string }>,
  revokeMcpClient: (clientId: string) =>
    post("/api/admin/mcp/clients/{client_id}/revoke", { path: { client_id: clientId } }) as Promise<{ client_id: string; status: string }>,
  setMcpGrant: (clientId: string, ws: string, body: McpGrantBody) =>
    put("/api/admin/mcp/clients/{client_id}/grants/{workspace_id}", { path: { client_id: clientId, workspace_id: ws }, body }) as
      Promise<McpGrantRow & { client_id: string }>,
  deleteMcpGrant: (clientId: string, ws: string) =>
    del("/api/admin/mcp/clients/{client_id}/grants/{workspace_id}", { path: { client_id: clientId, workspace_id: ws } }) as Promise<void>,
  /** P4-T09: accept or dismiss a finding (labels the decisions behind it for calibration). */
  findingOutcome: (insightId: string, signal: "accept" | "dismiss") =>
    post("/api/insights/{insight_id}/outcome", { path: { insight_id: insightId }, body: { signal } }) as
      Promise<{ insight: string; signal: string; labelled_decisions: number }>,

  // build (P4-E04/E06): targets, elt_build runs and their jobs; approval goes through the inbox
  buildTargets: (ws: string) => get("/api/workspaces/{workspace_id}/build-targets", { path: W(ws) }) as Promise<BuildTarget[]>,
  designateBuildTarget: (ws: string, schema: string) =>
    post("/api/workspaces/{workspace_id}/build-targets", { path: W(ws), body: { schema_name: schema } }) as Promise<BuildTarget>,
  startBuild: (ws: string, fromRunId: string, targetSchema: string) =>
    post("/api/workspaces/{workspace_id}/builds", { path: W(ws), body: { from_run_id: fromRunId, target_schema: targetSchema } }) as
      Promise<{ run_id: string; status: string; playbook: string }>,
  listBuilds: (ws: string) => get("/api/workspaces/{workspace_id}/builds", { path: W(ws) }) as Promise<BuildJob[]>,
  getBuild: (id: string) => get("/api/builds/{job_id}", { path: { job_id: id } }) as Promise<BuildJobDetail>,
  buildDiff: (id: string, against?: string) =>
    get("/api/builds/{job_id}/diff", { path: { job_id: id }, query: { against } }) as Promise<BuildDiff>,

  // semantic layer (P4-K03): KPI proposals, validation and the separation-of-duties approve route
  semanticModel: (ws: string) => get("/api/workspaces/{workspace_id}/semantic", { path: W(ws) }) as Promise<SemanticModelView>,
  metricVersions: (ws: string, name: string) =>
    get("/api/workspaces/{workspace_id}/semantic/metrics/{name}", { path: { workspace_id: ws, name } }) as Promise<SemanticMetric[]>,
  validateMetric: (ws: string, body: MetricProposal) =>
    post("/api/workspaces/{workspace_id}/semantic/metrics/validate", { path: W(ws), body }) as Promise<MetricValidation>,
  proposeMetric: (ws: string, body: MetricProposal) =>
    post("/api/workspaces/{workspace_id}/semantic/metrics", { path: W(ws), body }) as Promise<MetricProposalResult>,
  decideMetric: (ws: string, name: string, approve: boolean, version?: number, reason?: string) =>
    post(approve ? "/api/workspaces/{workspace_id}/semantic/metrics/{name}/approve" : "/api/workspaces/{workspace_id}/semantic/metrics/{name}/reject",
      { path: { workspace_id: ws, name }, body: { version: version ?? null, reason: reason || null } }) as Promise<SemanticMetric>,

  // knowledge studio (P4-U04): packs, documents, revisions, review queue, import/export, graph
  knowledgePacks: (ws: string) => get("/api/workspaces/{workspace_id}/knowledge/packs", { path: W(ws) }) as Promise<KnowledgePackInfo[]>,
  knowledgeDocuments: (ws: string, pack: string, revision?: number) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/documents", { path: { workspace_id: ws, pack_id: pack }, query: { revision } }) as
      Promise<KnowledgeDocList>,
  knowledgeDocument: (ws: string, pack: string, path: string, revision?: number) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document", { path: { workspace_id: ws, pack_id: pack }, query: { path, revision } }) as
      Promise<KnowledgeDocument>,
  saveKnowledgeDocument: (ws: string, pack: string, body: KnowledgeSaveBody) =>
    put("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/document", { path: { workspace_id: ws, pack_id: pack }, body }) as
      Promise<KnowledgeSaveResult>,
  knowledgeRevisions: (ws: string, pack: string, path?: string) =>
    get("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/revisions", { path: { workspace_id: ws, pack_id: pack }, query: { path } }) as
      Promise<KnowledgeRevisionInfo[]>,
  locateKnowledge: (ws: string, documentId: string) =>
    get("/api/workspaces/{workspace_id}/knowledge/locate", { path: W(ws), query: { document_id: documentId } }) as
      Promise<{ document_id: string; pack_id: string; path: string; title: string; revision: number }>,
  knowledgeSuggestions: (ws: string, status: string = "pending") =>
    get("/api/workspaces/{workspace_id}/knowledge/suggestions", { path: W(ws), query: { status } }) as Promise<KnowledgeSuggestion[]>,
  reviewSuggestions: (ws: string, decisions: ReviewDecisionBody[]) =>
    post("/api/workspaces/{workspace_id}/knowledge/suggestions/review", { path: W(ws), body: { decisions } }) as Promise<ReviewResult>,
  suggestionSummary: (ws: string) =>
    get("/api/workspaces/{workspace_id}/knowledge/suggestions/summary", { path: W(ws) }) as Promise<SuggestionSummary>,
  glossaryScan: (ws: string, body: { use_model?: boolean; include_ask?: boolean; include_descriptions?: boolean } = {}) =>
    post("/api/workspaces/{workspace_id}/knowledge/glossary/scan", { path: W(ws), body }) as Promise<GlossaryScanResult>,
  importKnowledge: (ws: string, file: File, slug: string) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("slug", slug);
    return post("/api/workspaces/{workspace_id}/knowledge/import", { path: W(ws), body: fd }) as Promise<KnowledgeImportReport>;
  },
  exportKnowledge: (ws: string, pack: string, slug: string, revision?: number) =>
    downloadFile(apiPath("get", "/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/export", { path: { workspace_id: ws, pack_id: pack },
      query: { revision } }), `${slug}.okf.zip`),
  requestKnowledgePush: (ws: string, pack: string) =>
    post("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push/request", { path: { workspace_id: ws, pack_id: pack } }) as Promise<Approval>,
  pushKnowledge: (ws: string, pack: string, approvalId: string) =>
    post("/api/workspaces/{workspace_id}/knowledge/packs/{pack_id}/push", { path: { workspace_id: ws, pack_id: pack }, body: { approval_id: approvalId } }) as
      Promise<{ revision: number; commit: string; remote: string; branch: string }>,
  knowledgeGraph: (ws: string) => get("/api/workspaces/{workspace_id}/knowledge/graph", { path: W(ws) }) as Promise<KnowledgeGraph>,

  // workspace context: download it, see what the agents see, and its shared cache (Stream D)
  exportContext: (ws: string, format: ContextExportFormat, sourceId?: string | null) =>
    downloadFile(apiPath("get", "/api/workspaces/{workspace_id}/context/export", { path: W(ws), query: { format, source_id: sourceId || undefined } }),
      `context.${format === "okf" ? "okf.zip" : format === "json" ? "json" : "md"}`),
  contextPurposes: (ws: string) => get("/api/workspaces/{workspace_id}/context/purposes", { path: W(ws) }) as Promise<ContextPurpose[]>,
  contextPreview: (ws: string, purpose: string, question?: string, sourceId?: string | null) =>
    get("/api/workspaces/{workspace_id}/context/preview", { path: W(ws), query: { purpose, question: question || undefined,
      source_id: sourceId || undefined } }) as Promise<ContextPreview>,
  downloadContextPreview: (ws: string, purpose: string, question?: string, sourceId?: string | null) =>
    downloadFile(apiPath("get", "/api/workspaces/{workspace_id}/context/preview", { path: W(ws), query: { purpose, question: question || undefined,
      source_id: sourceId || undefined, download: true } }), `context-preview-${purpose}.txt`),
  contextCache: (ws: string) => get("/api/workspaces/{workspace_id}/context/cache", { path: W(ws) }) as Promise<ContextCacheStats>,
  clearContextCache: (ws: string) => del("/api/workspaces/{workspace_id}/context/cache", { path: W(ws) }) as Promise<{ cleared: number }>,

  // dashboards: publishing is proposal-based (returns the pending, hash-bound proposal; decided in the inbox)
  publishDashboard: (artifactId: string) =>
    post("/api/artifacts/{artifact_id}/publish", { path: { artifact_id: artifactId } }) as Promise<Approval>,

  // BI-VIEWS-02: publish a chosen collection of an investigation's dashboards; group existing BI dashboards by workspace
  runCollections: (runId: string) =>
    get("/api/v2/runs/{run_id}/collections", { path: { run_id: runId } }) as Promise<RunCollections>,
  requestCollection: (runId: string, keys: string[]) =>
    post("/api/v2/runs/{run_id}/collections/publish-request", { path: { run_id: runId }, body: { keys } }) as Promise<CollectionRequested>,
  publishCollection: (approvalId: string) =>
    post("/api/v2/collections/publish", { body: { approval_id: approvalId } }) as Promise<CollectionPublished>,
  dashboardGrouping: (ws: string) =>
    get("/api/v2/workspaces/{workspace_id}/dashboard-grouping", { path: W(ws) }) as Promise<GroupingPlan>,
  requestGrouping: (ws: string, dashboardIds: number[]) =>
    post("/api/v2/workspaces/{workspace_id}/dashboard-grouping/request", { path: W(ws), body: { dashboard_ids: dashboardIds } }) as Promise<GroupingRequested>,
  applyGrouping: (ws: string, approvalId: string) =>
    post("/api/v2/workspaces/{workspace_id}/dashboard-grouping/apply", { path: W(ws), body: { approval_id: approvalId } }) as Promise<{ status: string; approval_id: string; renamed: GroupingChange[] }>,

  // Dashboard builder (S08): typed ops, conversation, versions, publish lifecycle, exports
  builderDashboards: (ws: string) =>
    get("/api/v2/workspaces/{workspace_id}/dashboards", { path: W(ws) }) as Promise<BuilderSummary[]>,
  createBuilderDashboard: (ws: string, document: BuilderDocument) =>
    post("/api/v2/workspaces/{workspace_id}/dashboards", { path: W(ws), body: { document: document as unknown as Dict } }) as Promise<BuilderDashboard>,
  builderDraft: (ws: string, body: { prompt: string; title?: string | null }) =>
    post("/api/v2/workspaces/{workspace_id}/dashboards/draft", { path: W(ws), body }) as Promise<BuilderDraft>,
  builderDashboard: (id: string) =>
    get("/api/v2/dashboards/{dashboard_id}", { path: { dashboard_id: id } }) as Promise<BuilderDashboard>,
  builderMessage: (id: string, body: { text: string; focus?: string | null; base_version?: number | null; apply?: boolean }) =>
    post("/api/v2/dashboards/{dashboard_id}/messages", { path: { dashboard_id: id }, body }) as Promise<BuilderTurn>,
  builderOps: (id: string, ops: BuilderOp[], baseVersion?: number) =>
    post("/api/v2/dashboards/{dashboard_id}/ops", { path: { dashboard_id: id }, body: { ops, base_version: baseVersion ?? null } }) as Promise<BuilderTurn>,
  builderRevert: (id: string, toVersion: number, baseVersion?: number) =>
    post("/api/v2/dashboards/{dashboard_id}/revert", { path: { dashboard_id: id }, body: { to_version: toVersion, base_version: baseVersion ?? null } }) as Promise<BuilderTurn>,
  builderVersions: (id: string) =>
    get("/api/v2/dashboards/{dashboard_id}/versions", { path: { dashboard_id: id } }) as Promise<BuilderVersion[]>,
  builderVersion: (id: string, n: number) =>
    get("/api/v2/dashboards/{dashboard_id}/versions/{n}", { path: { dashboard_id: id, n } }) as Promise<BuilderVersion & { document: BuilderDocument }>,
  builderValidate: (id: string) =>
    post("/api/v2/dashboards/{dashboard_id}/validate", { path: { dashboard_id: id } }) as Promise<{ valid: boolean; errors: string[]; dashboard: BuilderDashboard }>,
  builderPublishRequest: (id: string, body: { destination: "superset" | "preview"; audience: string[] }) =>
    post("/api/v2/dashboards/{dashboard_id}/publish-request", { path: { dashboard_id: id }, body }) as Promise<{ approval_id: string; status: string; risk_tier: string; payload_hash: string }>,
  builderPublish: (id: string, approvalId: string) =>
    post("/api/v2/dashboards/{dashboard_id}/publish", { path: { dashboard_id: id }, body: { approval_id: approvalId } }) as Promise<{ status: string; url: string | null; publication_id: string }>,
  /** A Superset guest token for this viewer; the server re-checks access and adds the viewer's row filters. */
  builderEmbedToken: (id: string) =>
    post("/api/v2/dashboards/{dashboard_id}/embed-token", { path: { dashboard_id: id } }) as Promise<EmbedToken>,
  builderExportRequest: (id: string, format: "csv" | "xlsx" | "pdf") =>
    post("/api/v2/dashboards/{dashboard_id}/exports", { path: { dashboard_id: id }, body: { format } }) as Promise<{ status: "allowed" | "approval_required"; sensitivity: string; approval?: { id: string; status: string } }>,
  builderExport: (id: string, format: "csv" | "xlsx" | "pdf", approvalId?: string) =>
    downloadFile(apiPath("get", "/api/v2/dashboards/{dashboard_id}/exports/{fmt}", { path: { dashboard_id: id, fmt: format }, query: { approval_id: approvalId } }), `dashboard.${format}`),
  /** CONV-06: what a click on each chart filters by or drills into (no query runs). */
  dashboardExplore: (id: string) =>
    get("/api/v2/dashboards/{dashboard_id}/explore", { path: { dashboard_id: id } }) as Promise<DashboardExplore>,
  /** A cross-filtered view (not a new version): each affected tile re-queried through its governed path as this viewer. */
  dashboardView: (id: string, body: { filters: ViewFilter[]; base_version?: number | null; refresh_saved?: boolean }, signal?: AbortSignal) =>
    request<DashboardView>("POST", apiPath("post", "/api/v2/dashboards/{dashboard_id}/view", { path: { dashboard_id: id } }),
      { ...body, filters: body.filters.map(({ field, op, value, source }) => ({ field, op, value, source: source ?? null })) }, { signal }),
  /** One drill level for a chart, and optionally the rows behind the chosen member (only columns the viewer may see). */
  dashboardDrill: (id: string, body: { chart_id: string; path: DrillStep[]; filters: ViewFilter[]; base_version?: number | null; detail?: boolean; preview?: boolean },
    signal?: AbortSignal) =>
    request<DrillResult>("POST", apiPath("post", "/api/v2/dashboards/{dashboard_id}/drill", { path: { dashboard_id: id } }), {
      ...body, path: body.path.map(({ field, value }) => ({ field, value })),
      filters: body.filters.map(({ field, op, value, source }) => ({ field, op, value, source: source ?? null })),
    }, { signal }),
  /** CONV-05: what a builder dashboard shows, its notable facts and what changed since the previous version. */
  dashboardStory: (id: string) =>
    get("/api/v2/dashboards/{dashboard_id}/story", { path: { dashboard_id: id } }) as Promise<DashboardStory>,
  storyToReport: (id: string, title?: string) =>
    post("/api/v2/dashboards/{dashboard_id}/story/report", { path: { dashboard_id: id }, body: { title: title ?? null } }) as Promise<ReportDocView>,

  // Report documents (CONV-04): typed ops by conversation, versions, preview, approval-bound exports
  reportDocs: (ws: string) =>
    get("/api/v2/workspaces/{workspace_id}/report-documents", { path: W(ws) }) as Promise<ReportDocSummary[]>,
  createReportDoc: (ws: string, body: { title: string; objective?: string }) =>
    post("/api/v2/workspaces/{workspace_id}/report-documents", { path: W(ws), body }) as Promise<ReportDocView>,
  reportContent: (ws: string) =>
    get("/api/v2/workspaces/{workspace_id}/report-content", { path: W(ws) }) as Promise<ReportContentItem[]>,
  reportDoc: (id: string) =>
    get("/api/v2/report-documents/{doc_id}", { path: { doc_id: id } }) as Promise<ReportDocView>,
  reportDocMessage: (id: string, body: { text: string; focus?: string | null; base_version?: number | null; apply?: boolean }) =>
    post("/api/v2/report-documents/{doc_id}/messages", { path: { doc_id: id }, body }) as Promise<ReportDocTurn>,
  reportDocOps: (id: string, ops: ReportOp[], baseVersion?: number) =>
    post("/api/v2/report-documents/{doc_id}/ops", { path: { doc_id: id }, body: { ops, base_version: baseVersion ?? null } }) as Promise<ReportDocTurn>,
  reportDocRevert: (id: string, toVersion: number, baseVersion?: number) =>
    post("/api/v2/report-documents/{doc_id}/revert", { path: { doc_id: id }, body: { to_version: toVersion, base_version: baseVersion ?? null } }) as Promise<ReportDocTurn>,
  reportDocVersions: (id: string) =>
    get("/api/v2/report-documents/{doc_id}/versions", { path: { doc_id: id } }) as Promise<ReportDocVersion[]>,
  reportDocPreview: (id: string) =>
    get("/api/v2/report-documents/{doc_id}/preview", { path: { doc_id: id } }) as Promise<{ html: string; snapshot_hash: string; version: number }>,
  reportDocExportRequest: (id: string, format: ReportDocFormat) =>
    post("/api/v2/report-documents/{doc_id}/exports", { path: { doc_id: id }, body: { format } }) as Promise<ReportExportRequest>,
  reportDocExport: (id: string, format: ReportDocFormat, approvalId: string) =>
    downloadFile(apiPath("get", "/api/v2/report-documents/{doc_id}/exports/{fmt}", { path: { doc_id: id, fmt: format }, query: { approval_id: approvalId } }), `report.${format}`),

  // Existing-dashboard mode (N-2, BI-011/012)
  biDashboardCandidates: (ws: string, destination = "superset") =>
    get("/api/workspaces/{workspace_id}/bi/dashboards/candidates", { path: W(ws), query: { destination } }) as Promise<BiDashboardCandidate[]>,
  biDashboardImports: (ws: string) =>
    get("/api/workspaces/{workspace_id}/bi/dashboards/imports", { path: W(ws) }) as Promise<BiDashboardImport[]>,
  biDashboardImport: (ws: string, importId: string) =>
    get("/api/workspaces/{workspace_id}/bi/dashboards/imports/{import_id}", { path: { ...W(ws), import_id: importId } }) as Promise<BiDashboardImport>,
  importBiDashboard: (ws: string, dashboardId: string, destination = "superset") =>
    post("/api/workspaces/{workspace_id}/bi/dashboards/imports", { path: W(ws), body: { dashboard_id: dashboardId, destination } }) as Promise<BiDashboardImport>,
  requestBiDashboardUpdate: (ws: string, importId: string, proposalIds: string[]) =>
    post("/api/workspaces/{workspace_id}/bi/dashboards/imports/{import_id}/update-requests",
      { path: { ...W(ws), import_id: importId }, body: { proposal_ids: proposalIds } }) as Promise<{ status: string; approval_id: string; expires_at: string }>,
  applyBiDashboardUpdate: (ws: string, importId: string, approvalId: string) =>
    post("/api/workspaces/{workspace_id}/bi/dashboards/imports/{import_id}/updates",
      { path: { ...W(ws), import_id: importId }, body: { approval_id: approvalId } }) as Promise<{ status: string; changed?: string[]; error?: string }>,
  proposeBiDashboardMetric: (ws: string, importId: string, proposalId: string) =>
    post("/api/workspaces/{workspace_id}/bi/dashboards/imports/{import_id}/semantic-proposals",
      { path: { ...W(ws), import_id: importId }, body: { proposal_id: proposalId } }) as Promise<{ status: string; metric: string; approval_id: string | null }>,

  // Ask threads (P4-U02)
  askThreads: (ws: string, q?: string) =>
    get("/api/workspaces/{workspace_id}/ask/threads", { path: W(ws), query: { q: q || undefined } }) as Promise<AskThread[]>,
  createAskThread: (ws: string, title?: string) =>
    post("/api/workspaces/{workspace_id}/ask/threads", { path: W(ws), body: { title: title ?? null } }) as Promise<AskThreadDetail>,
  askThread: (id: string) => get("/api/ask/threads/{thread_id}", { path: { thread_id: id } }) as Promise<AskThreadDetail>,
  // v2 conversations over the same threads (S04): branches, state, typed corrections and memory
  conversation: (id: string) =>
    get("/api/v2/conversations/{conversation_id}", { path: { conversation_id: id } }) as Promise<ConversationDetail>,
  branchConversation: (id: string, fromMessageId: string | null) =>
    post("/api/v2/conversations/{conversation_id}/branch", { path: { conversation_id: id }, body: { from_message_id: fromMessageId } }) as
      Promise<ConversationBranch>,
  conversationState: (id: string) =>
    get("/api/v2/conversations/{conversation_id}/state", { path: { conversation_id: id } }) as Promise<ConversationState>,
  patchConversationState: (id: string, body: Schemas["ConversationStatePatch"]) =>
    patch("/api/v2/conversations/{conversation_id}/state", { path: { conversation_id: id }, body }) as Promise<ConversationState>,
  conversationMemory: (id: string) =>
    get("/api/v2/conversations/{conversation_id}/memory", { path: { conversation_id: id } }) as Promise<ConversationMemory>,
  conversationCorrections: (id: string) =>
    get("/api/v2/conversations/{conversation_id}/corrections", { path: { conversation_id: id } }) as Promise<Correction[]>,
  addCorrection: (id: string, body: Schemas["ConversationCorrectionIn"]) =>
    post("/api/v2/conversations/{conversation_id}/corrections", { path: { conversation_id: id }, body }) as
      Promise<{ correction: Correction; state_version: number }>,
  promoteCorrection: (id: string, correctionId: string, to: "personal" | "workspace_proposed") =>
    post("/api/v2/conversations/{conversation_id}/corrections/{correction_id}/promote",
      { path: { conversation_id: id, correction_id: correctionId }, body: { to } }) as Promise<{ correction: Correction; state_version: number }>,
  forgetCorrection: (correctionId: string) =>
    del("/api/v2/memory/corrections/{correction_id}", { path: { correction_id: correctionId } }) as Promise<Correction>,
  patchAskThread: (id: string, body: Schemas["AskThreadPatch"]) =>
    patch("/api/ask/threads/{thread_id}", { path: { thread_id: id }, body }) as Promise<AskThread>,
  askTurn: (threadId: string, question: string, parameters?: Dict, mode: AskMode = "quick") =>
    post("/api/ask/threads/{thread_id}/turns", { path: { thread_id: threadId }, body: { question, parameters: parameters ?? null, mode } }) as
      Promise<AskTurn>,
  /** Analyst mode: re-run one step (edited SQL, or its saved SQL) through the gateway; the synthesis becomes stale. */
  rerunAskStep: (turnId: string, step: number, sql?: string) =>
    post("/api/ask/turns/{turn_id}/steps/{n}/rerun", { path: { turn_id: turnId, n: step }, body: sql ? { sql } : {} }) as Promise<AskTurn>,
  /** Analyst mode: rewrite the answer from the steps' current facts. */
  resynthesizeAsk: (turnId: string) => post("/api/ask/turns/{turn_id}/synthesize", { path: { turn_id: turnId } }) as Promise<AskTurn>,
  askInspector: (turnId: string) => get("/api/ask/turns/{turn_id}/inspector", { path: { turn_id: turnId } }) as Promise<AskInspector>,
  rerunAsk: (turnId: string, sql?: string) => request<AskTurn>("POST", `/ask/turns/${encodeURIComponent(turnId)}/rerun`, { sql }),
  scheduleAsk: (turnId: string, body: { name: string; cron: string; timezone: string; approval_id?: string }) =>
    request<{ status: string; approval_id?: string; expires_at?: string; id?: string }>("POST", `/ask/turns/${encodeURIComponent(turnId)}/schedule`, body),
  promoteTurn: (turnId: string, body: AskPromoteBody) =>
    post("/api/ask/turns/{turn_id}/promote", { path: { turn_id: turnId }, body }) as Promise<AskPromotion>,
  // what-if scenarios (N-9): observed baseline through the gateway, simulated values by deterministic arithmetic
  runScenario: (ws: string, body: ScenarioSpecBody) =>
    post("/api/workspaces/{workspace_id}/scenarios", { path: W(ws), body }) as Promise<Scenario>,
  scenarios: (ws: string, askTurnId?: string) =>
    get("/api/workspaces/{workspace_id}/scenarios", { path: W(ws), query: { ask_turn_id: askTurnId } }) as Promise<Scenario[]>,
  scenario: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/scenarios/{scenario_id}", { path: { workspace_id: ws, scenario_id: id } }) as Promise<Scenario>,

  // brief, readiness and Start-work job kinds (P4-04)
  jobKinds: (ws: string) => get("/api/workspaces/{workspace_id}/capabilities", { path: W(ws) }) as Promise<WorkspaceJobKinds>,
  brief: (ws: string, version?: number) =>
    get("/api/workspaces/{workspace_id}/brief", { path: W(ws), query: { version } }) as Promise<WorkspaceBrief>,
  briefVersions: (ws: string) => get("/api/workspaces/{workspace_id}/brief/versions", { path: W(ws) }) as Promise<BriefVersion[]>,
  refreshBrief: (ws: string) => post("/api/workspaces/{workspace_id}/brief/suggestions", { path: W(ws) }) as Promise<BriefRefreshResult>,
  /** `version` is the brief the edit was made against: a newer one answers 412 (someone changed it). */
  patchBrief: (ws: string, version: number, body: BriefPatchBody) =>
    request<BriefPatchResult>("PATCH", apiPath("patch", "/api/workspaces/{workspace_id}/brief", { path: W(ws), body }), body, ifMatch(version)),
  assessReadiness: (ws: string, body: ReadinessInput) =>
    post("/api/workspaces/{workspace_id}/readiness", { path: W(ws), body }) as Promise<ReadinessAssessment>,

  // steps and the Data Thread (P7-04/05)
  thread: (ws: string, type: ContainerType, id: string) =>
    get("/api/workspaces/{workspace_id}/threads/{container_type}/{container_id}",
      { path: { workspace_id: ws, container_type: type, container_id: id } }) as Promise<ThreadView>,
  ingestThread: (ws: string, type: "run" | "ask_thread", id: string) =>
    post("/api/workspaces/{workspace_id}/threads/{container_type}/{container_id}/ingest",
      { path: { workspace_id: ws, container_type: type, container_id: id } }) as Promise<Dict>,
  branch: (ws: string, branch: string) =>
    get("/api/workspaces/{workspace_id}/branches/{branch_id}", { path: { workspace_id: ws, branch_id: branch } }) as Promise<ThreadView>,
  addStep: (ws: string, branch: string, body: Schemas["StepIn"]) =>
    post("/api/workspaces/{workspace_id}/branches/{branch_id}/steps", { path: { workspace_id: ws, branch_id: branch }, body }) as Promise<Step>,
  step: (ws: string, step: string, version?: number) =>
    get("/api/workspaces/{workspace_id}/steps/{step_id}", { path: { workspace_id: ws, step_id: step }, query: { version } }) as Promise<StepWithResult>,
  stepVersions: (ws: string, step: string) =>
    get("/api/workspaces/{workspace_id}/steps/{step_id}/versions", { path: { workspace_id: ws, step_id: step } }) as Promise<StepVersions>,
  /** `version` is the step's current version the edit was made against (412 when someone edited it since). */
  editStep: (ws: string, step: string, version: number, body: Schemas["StepEdit"]) =>
    request<StepRevision>("PATCH", apiPath("patch", "/api/workspaces/{workspace_id}/steps/{step_id}",
      { path: { workspace_id: ws, step_id: step }, body }), body, ifMatch(version)),
  rerunStep: (ws: string, step: string) =>
    post("/api/workspaces/{workspace_id}/steps/{step_id}/runs", { path: { workspace_id: ws, step_id: step } }) as Promise<StepRevision>,
  pinStep: (ws: string, step: string, body: PinBody) =>
    post("/api/workspaces/{workspace_id}/steps/{step_id}/pins", { path: { workspace_id: ws, step_id: step }, body }) as Promise<PinResult>,
  fork: (ws: string, branch: string, body: Schemas["ForkIn"]) =>
    post("/api/workspaces/{workspace_id}/branches/{branch_id}/forks", { path: { workspace_id: ws, branch_id: branch }, body }) as Promise<ForkResult>,
  compareBranches: (ws: string, a: string, b: string) =>
    get("/api/workspaces/{workspace_id}/branches/{branch_id}/compare",
      { path: { workspace_id: ws, branch_id: a }, query: { with: b } }) as Promise<BranchCompare>,
  mergeBranch: (ws: string, branch: string, body: Schemas["MergeIn"]) =>
    post("/api/workspaces/{workspace_id}/branches/{branch_id}/merge", { path: { workspace_id: ws, branch_id: branch }, body }) as Promise<MergeResult>,

  // notebooks (P7-12)
  notebooks: (ws: string) => get("/api/workspaces/{workspace_id}/notebooks", { path: W(ws) }) as Promise<NotebookSummary[]>,
  createNotebook: (ws: string, title: string) =>
    post("/api/workspaces/{workspace_id}/notebooks", { path: W(ws), body: { title } }) as Promise<NotebookSummary>,
  notebook: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/notebooks/{notebook_id}", { path: { workspace_id: ws, notebook_id: id } }) as Promise<Notebook>,
  addCell: (ws: string, id: string, body: Schemas["CellIn"]) =>
    post("/api/workspaces/{workspace_id}/notebooks/{notebook_id}/cells", { path: { workspace_id: ws, notebook_id: id }, body }) as Promise<NotebookCell>,
  editCell: (ws: string, id: string, step: string, version: number, body: Schemas["CellEdit"]) =>
    request<StepRevision>("PATCH", apiPath("patch", "/api/workspaces/{workspace_id}/notebooks/{notebook_id}/cells/{step_id}",
      { path: { workspace_id: ws, notebook_id: id, step_id: step }, body }), body, ifMatch(version)),
  runNotebook: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/notebooks/{notebook_id}/runs", { path: { workspace_id: ws, notebook_id: id } }) as Promise<Notebook>,

  // governed ML (P5-01..06)
  createDefinition: (ws: string, body: Schemas["DefinitionDraftIn"]) =>
    post("/api/workspaces/{workspace_id}/definitions", { path: W(ws), body }) as Promise<DefinitionVersion>,
  mlPropose: (ws: string, body: ProposalInput) =>
    post("/api/workspaces/{workspace_id}/ml/proposals", { path: W(ws), body }) as Promise<MLProposal>,
  startExperiment: (ws: string, definition: string | { key: string; version: number }) =>
    post("/api/workspaces/{workspace_id}/ml/experiments", { path: W(ws), body: { definition } }) as Promise<MLExperiment>,
  experiments: (ws: string, definition?: string) =>
    get("/api/workspaces/{workspace_id}/ml/experiments", { path: W(ws), query: { definition } }) as Promise<MLExperiment[]>,
  experiment: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/ml/experiments/{experiment_id}", { path: { workspace_id: ws, experiment_id: id } }) as Promise<MLExperiment>,
  experimentRecord: <T = Dict>(ws: string, id: string, record: MLRecordType) =>
    get("/api/workspaces/{workspace_id}/ml/experiments/{experiment_id}/records/{record}",
      { path: { workspace_id: ws, experiment_id: id, record } }) as Promise<MLRecord<T>>,
  modelVersions: (ws: string, name?: string) =>
    get("/api/workspaces/{workspace_id}/ml/models", { path: W(ws), query: { name } }) as Promise<ModelVersion[]>,
  promoteModel: (ws: string, versionId: string, approvalId?: string) =>
    post("/api/workspaces/{workspace_id}/ml/models/{version_id}/promote",
      { path: { workspace_id: ws, version_id: versionId }, body: { approval_id: approvalId ?? null } }) as Promise<ApprovalStep>,
  rollbackModel: (ws: string, name: string, approvalId?: string) =>
    post("/api/workspaces/{workspace_id}/ml/model-names/{name}/rollback",
      { path: { workspace_id: ws, name }, body: { approval_id: approvalId ?? null } }) as Promise<ApprovalStep>,
  planScoring: (ws: string, definition: string | { key: string; version: number }) =>
    post("/api/workspaces/{workspace_id}/ml/scoring", { path: W(ws), body: { definition } }) as Promise<ApprovalStep>,
  executeScoring: (ws: string, id: string, approvalId?: string) =>
    post("/api/workspaces/{workspace_id}/ml/scoring/{scoring_id}/execute",
      { path: { workspace_id: ws, scoring_id: id }, body: { approval_id: approvalId ?? null } }) as Promise<ApprovalStep>,
  scoringRuns: (ws: string) => get("/api/workspaces/{workspace_id}/ml/scoring", { path: W(ws) }) as Promise<ScoringRun[]>,

  // pipelines, managed writer, materializations (P6-01..03)
  pipelines: (ws: string) => get("/api/workspaces/{workspace_id}/pipelines", { path: W(ws) }) as Promise<Pipeline[]>,
  savePipeline: (ws: string, spec: Dict) => post("/api/workspaces/{workspace_id}/pipelines", { path: W(ws), body: { spec } }) as Promise<Pipeline>,
  publishPipeline: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/pipelines/{pipeline_id}/publish", { path: { workspace_id: ws, pipeline_id: id } }) as Promise<Pipeline>,
  dryRunPipeline: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/pipelines/{pipeline_id}/dry-run", { path: { workspace_id: ws, pipeline_id: id }, body: {} }) as Promise<PipelineRun>,
  pipelineRuns: (ws: string, pipeline?: string) =>
    get("/api/workspaces/{workspace_id}/pipeline-runs", { path: W(ws), query: { pipeline } }) as Promise<PipelineRun[]>,
  pipelineRun: (ws: string, id: string) =>
    get("/api/workspaces/{workspace_id}/pipeline-runs/{run_id}", { path: { workspace_id: ws, run_id: id } }) as Promise<PipelineRun>,
  materialize: (ws: string, runId: string, approvalId: string) =>
    post("/api/workspaces/{workspace_id}/pipeline-runs/{run_id}/materialize",
      { path: { workspace_id: ws, run_id: runId }, body: { approval_id: approvalId } }) as Promise<Materialization>,
  materializations: (ws: string, table?: string) =>
    get("/api/workspaces/{workspace_id}/materializations", { path: W(ws), query: { table } }) as Promise<Materialization[]>,
  rollbackMaterialization: (ws: string, id: string) =>
    post("/api/workspaces/{workspace_id}/materializations/{materialization_id}/rollback",
      { path: { workspace_id: ws, materialization_id: id } }) as Promise<Materialization>,
  writerDestinations: (ws: string) =>
    get("/api/workspaces/{workspace_id}/writer-destinations", { path: W(ws) }) as Promise<WriterDestination[]>,
  designateDestination: (ws: string, body: Schemas["DestinationIn"]) =>
    post("/api/workspaces/{workspace_id}/writer-destinations", { path: W(ws), body }) as Promise<WriterDestination>,

  // process and task mining (Work → Process)
  processCandidates: (ws: string) =>
    get("/api/workspaces/{workspace_id}/process/candidates", { path: W(ws) }) as Promise<{ version: string; candidates: ProcessCandidate[] }>,
  analyzeProcess: (ws: string, body: ProcessAnalyzeInput) =>
    post("/api/workspaces/{workspace_id}/process/analyze", { path: W(ws), body }) as Promise<ProcessAnalysis>,
  processAnalyses: (ws: string) =>
    get("/api/workspaces/{workspace_id}/process/analyses", { path: W(ws) }) as Promise<SavedProcessAnalysis[]>,
  /** Turn an event log into the workspace tables `<log>_cases` and `<log>_transitions` for Ask, investigations and dashboards. */
  buildProcessTables: (ws: string, body: Schemas["ProcessTablesIn"]) =>
    post("/api/workspaces/{workspace_id}/process/tables", { path: W(ws), body }) as Promise<ProcessTablesResult>,

  // what the selected tables are good for (Overview card)
  dataShape: (ws: string) =>
    get("/api/workspaces/{workspace_id}/data-shape", { path: W(ws) }) as Promise<DataShape>,
  markDataShape: (ws: string, body: Schemas["ShapeMarkIn"]) =>
    post("/api/workspaces/{workspace_id}/data-shape/mark", { path: W(ws), body }),
  proposeDataShape: (ws: string) =>
    post("/api/workspaces/{workspace_id}/data-shape/propose", { path: W(ws) }) as Promise<DataShapeProposal>,
};

/** api/routers/data_shape.py: the patterns of the selected tables, from the catalog's own measurements. */
export type ShapeKind = "event_log" | "time_series" | "ml_candidate" | "fact" | "dimension" | "bridge" | "reference";
export interface ShapePattern {
  kind: ShapeKind;
  confidence: number;
  reasons: string[];
  detail: Record<string, unknown>;
  next_step: { action: "process_analysis" | "investigation" | "experiment" | "ask"; label: string } | null;
  origin: "rules" | "model";
  state: "suggested" | "proposed" | "confirmed";
}
export interface ShapeTable {
  asset_id: string; fq: string; name: string; business_name: string | null; role: string; row_count: number | null;
  patterns: ShapePattern[]; unexplained: boolean;
}
export interface DataShape {
  version: string;
  workspace: { kind: string; label: string; reasons: string[]; tables: string[] }[];
  tables: ShapeTable[];
  summary: Partial<Record<ShapeKind, number>>;
  generated_at: string;
}
export interface DataShapeProposal { called: boolean; considered: number; proposed: number; rejected: { table: string; why: string }[]; skipped?: string }

export interface ProcessTablesResult {
  source_id: string;
  event_log: { asset_id: string; fq: string };
  tables: { kind: "cases" | "transitions" | string; name: string; fq: string; asset_id: string; rows: number | null; business_name: string | null }[];
  segments: { segment: string | null; cases: number; expected_path: string[]; expected_path_source: string }[];
  cases: number;
  transitions: number;
  truncated: boolean;
}

// ----------------------------------------------------------------------------------- run events (SSE)
export interface EventStreamHandle {
  close: () => void;
  /** Skip the backoff wait and reconnect at once (a person pressed "Reconnect now"). */
  reconnectNow: () => void;
}

/** Where a reconnecting stream stands: which attempt, when it retries, and the last event it has. */
export interface StreamInfo {
  attempt: number;
  /** Milliseconds until the next attempt (only while reconnecting). */
  retryInMs?: number;
  /** The persisted cursor it resumes from (`after_id`). */
  lastEventId: number;
  /** True when this `open` follows a drop: the caller should re-read state it may have missed. */
  resumed?: boolean;
}

export interface EventStreamCallbacks {
  onEvent: (ev: RunEvent) => void;
  onEnd?: (status: string | null) => void;
  onStatus?: (state: "connecting" | "open" | "reconnecting" | "closed", error?: string, info?: StreamInfo) => void;
}

/**
 * Subscribe to GET /api/workspaces/{ws}/analysis/{run}/events with the bearer header.
 * Reconnects with ?after_id=<last seen id> (exponential backoff, max 15 s) until the server sends
 * `event: end` (terminal run) or the caller closes the handle. `onStatus` reports the attempt, the
 * retry delay and the cursor, and marks the first `open` after a drop as `resumed`.
 */
export function subscribeRunEvents(ws: string, run: string, cb: EventStreamCallbacks, afterId = 0): EventStreamHandle {
  const controller = new AbortController();
  let last = afterId;
  let ended = false;
  let attempt = 0;
  let dropped = false;
  let closedReason: string | undefined;
  let wake: (() => void) | null = null;

  const handle = (m: SSEMessage) => {
    if (m.event === "expired" || m.event === "revoked") {
      // The server re-authorizes open streams: an expired token signs out, lost access closes for good.
      ended = true;
      if (m.event === "expired") unauthorizedHandler?.();
      closedReason = m.event === "expired" ? "Your session expired" : "Access to this run was revoked";
      return;
    }
    if (m.event === "end") {
      ended = true;
      let status: string | null = null;
      try {
        status = (JSON.parse(m.data) as { status?: string }).status ?? null;
      } catch {
        /* ignore */
      }
      cb.onEnd?.(status);
      return;
    }
    if (!m.data) return;
    try {
      const ev = JSON.parse(m.data) as RunEvent;
      if (typeof ev.id === "number") {
        if (ev.id <= last) return; // duplicate after reconnect
        last = ev.id;
      }
      cb.onEvent(ev);
    } catch {
      /* malformed payload: skip */
    }
  };

  const loop = async () => {
    while (!controller.signal.aborted && !ended) {
      cb.onStatus?.(attempt === 0 && !dropped ? "connecting" : "reconnecting", undefined, { attempt, lastEventId: last });
      let opened = false;
      try {
        await readSSE({
          url: API_BASE + apiPath("get", "/api/workspaces/{workspace_id}/analysis/{run_id}/events", {
            path: { workspace_id: ws, run_id: run }, query: { after_id: last } }),
          headers: authHeaders(),
          signal: controller.signal,
          onOpen: () => {
            opened = true;
            attempt = 0;
            cb.onStatus?.("open", undefined, { attempt: 0, lastEventId: last, resumed: dropped });
          },
          onMessage: handle,
        });
        if (ended || controller.signal.aborted) break;
      } catch (err) {
        if (controller.signal.aborted) break;
        const status = (err as { status?: number }).status;
        if (status === 401) {
          unauthorizedHandler?.();
          break;
        }
        if (status === 403 || status === 404) {
          cb.onStatus?.("closed", err instanceof Error ? err.message : String(err));
          return;
        }
        const next = attempt + 1;
        cb.onStatus?.("reconnecting", err instanceof Error ? err.message : String(err),
          { attempt: next, retryInMs: Math.min(15000, 500 * 2 ** Math.min(next, 5)), lastEventId: last });
      }
      dropped = true;
      // A stream that opened and then dropped reconnects quickly; repeated failures back off.
      attempt = opened ? 1 : attempt + 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(attempt, 5));
      await new Promise<void>((r) => {
        const t = setTimeout(r, delay);
        wake = () => { clearTimeout(t); r(); };
      });
      wake = null;
    }
    cb.onStatus?.("closed", closedReason);
  };
  void loop();
  return { close: () => controller.abort(), reconnectNow: () => wake?.() };
}

/**
 * Ask in a thread with the stages streamed (POST + `Accept: text/event-stream`): `stage` events as
 * the Ask runs, then `turn` (the persisted answer or refusal). Rejects with ApiError on an `error`
 * event, a non-2xx status or a stream that ends without the answer.
 */
export async function streamAskTurn(threadId: string, question: string, parameters: Dict | undefined, cb: AskStreamCallbacks,
  signal?: AbortSignal, mode: AskMode = "quick"): Promise<AskTurn> {
  let turn: AskTurn | null = null;
  let failure: ApiError | null = null;
  try {
    await readSSE({
      url: API_BASE + apiPath("post", "/api/ask/threads/{thread_id}/turns", { path: { thread_id: threadId } }),
      method: "POST", body: { question, parameters: parameters ?? null, mode }, headers: authHeaders(),
      signal: signal ?? new AbortController().signal,
      onMessage: (m) => {
        let data: unknown = null;
        try {
          data = m.data ? JSON.parse(m.data) : null;
        } catch {
          return;
        }
        if (m.event === "stage") cb.onStage(data as AskStage);
        else if (m.event === "turn") turn = data as AskTurn;
        else if (m.event === "expired") {
          unauthorizedHandler?.();
          failure = new ApiError(401, "token_expired", "Your session expired; sign in again");
        } else if (m.event === "revoked") failure = new ApiError(403, "access_revoked", "Access to this workspace was revoked");
        else if (m.event === "error") {
          const e = (data as { error?: { code?: string; message?: string; details?: Dict } }).error ?? {};
          failure = new ApiError(422, e.code ?? "error", e.message ?? "The question could not be asked", e.details ?? {});
        }
      },
    });
  } catch (err) {
    const status = (err as { status?: number }).status;
    if (status === 401) unauthorizedHandler?.();
    throw new ApiError(status ?? 0, status ? `http_${status}` : "network_error", err instanceof Error ? err.message : String(err));
  }
  if (failure) throw failure;
  if (!turn) throw new ApiError(0, "stream_incomplete", "The answer stream ended before the answer arrived");
  return turn;
}

// ----------------------------------------------------------------------------------- process mining
/** api/routers/process.py: an event log detected in the catalog, with the suggested mapping. */
export interface ProcessMapping {
  case_column: string;
  activity_column: string;
  timestamp_column: string;
  resource_column: string | null;
}
export interface ProcessSegmentValue {
  value: string | number | boolean;
  count?: number | null;
  label?: string | null;
  reference_path?: string[] | null;
}
export interface ProcessCandidate {
  asset_id: string;
  asset: string;
  name: string;
  business_name: string | null;
  row_count: number | null;
  role: string | null;
  declared_by: string | null;
  mapping: ProcessMapping;
  segments: { column: string; values: ProcessSegmentValue[] }[];
  score: number;
  reasons: string[];
  columns: string[];
}
export type ProcessAnalyzeInput = Schemas["ProcessAnalyzeIn"];
export interface ProcessEdge {
  source: string; target: string; count: number; cases: number;
  median_hours: number | null; p90_hours: number | null; mean_hours?: number | null;
}
export interface ProcessVariant {
  rank: number; activities: string[]; steps: number; cases: number; share: number; median_hours: number | null; happy_path: boolean;
}
export interface ProcessDeviation { kind: "missing" | "extra" | "out_of_order"; activity: string; cases: number; share: number; sentence: string }
/** skills/process_mining.py `analyze_cases` + the router's provenance (version "process-mining/1"). */
export interface ProcessAnalysis {
  version: string;
  title: string;
  asset: { id: string; fq: string; name: string; business_name: string | null };
  mapping: ProcessMapping;
  filters: { column: string; op: string; value?: unknown; values?: unknown[] }[];
  segment: string | number | boolean | null;
  summary: {
    cases: number; events: number; activities: number; variants: number; mean_events_per_case: number;
    start: string; end: string; median_hours: number | null; p90_hours: number | null;
    rework_share: number; cancelled_share: number; fitness: number; handover_share: number;
  };
  highlights: string[];
  activities: { activity: string; events: number; cases: number; starts: number; ends: number }[];
  edges: ProcessEdge[];
  variants: { total: number; top: ProcessVariant[]; other_cases: number; other_share: number; happy_path_rank: number | null };
  throughput: {
    cases: number; median_hours: number | null; p90_hours: number | null; mean_hours: number | null;
    histogram: { label: string; from_hours: number; to_hours: number | null; cases: number }[];
    by_end_activity: { activity: string; cases: number; median_hours: number | null; p90_hours: number | null }[];
  };
  bottlenecks: (ProcessEdge & { weight_hours: number; sentence: string })[];
  rework: { cases: number; share: number; activities: { activity: string; cases: number; share: number; extra_events: number }[] };
  cancellations: {
    activities: string[]; cases: number; share: number; median_hours_to_cancel: number | null;
    after: { activity: string; cases: number; share: number }[]; by_resource: { resource: string; cases: number }[];
  };
  conformance: {
    reference: string[]; source: string; completed_cases: number; conforming_cases: number; fitness: number;
    excluded: { open: number; cancelled: number }; deviations: ProcessDeviation[];
    deviating_variants: { activities: string[]; cases: number; missing: string[]; extra: string[]; out_of_order: string[] }[];
  };
  handovers: {
    cases_with_handover: number; share: number;
    pairs: { source: string; target: string; count: number; cases: number }[];
    ping_pong: { a: string; b: string; cases: number }[];
    resources: { resource: string; events: number; cases: number; handovers_out: number; handovers_in: number }[];
  };
  provenance: {
    queries: string[]; sql: string; dialect: string; computed_at: string; method: string;
    coverage: { events_read: number; pages: number; page_rows: number; max_events: number; truncated: boolean; split_case: boolean; note: string };
  };
  artifact?: { id: string; name: string; version: number };
}
export interface SavedProcessAnalysis {
  id: string; name: string; version: number; created_at: string | null; updated_at: string | null;
  asset: ProcessAnalysis["asset"] | null; segment: ProcessAnalysis["segment"]; mapping: ProcessMapping | null;
  summary: ProcessAnalysis["summary"] | null;
}

// ----------------------------------------------------------------------------------- request desk (S13, S11-T1/T7)
export type RequestClass = "R1" | "R2" | "R3" | "R4" | "R5" | "R6" | "R7" | "R8" | "R9";
export type RequestState =
  | "new" | "triaged" | "auto_answering" | "auto_answered" | "auto_with_review" | "in_investigation" | "escalated"
  | "answered" | "accepted" | "reopened" | "declined";
export type RequestPriority = "low" | "normal" | "high" | "urgent";
export type RequestActionType = Schemas["RequestActionIn"]["type"];

/** Who/what classified a request (desk/triage.py): rules first, a typed decision or small model only when no rule was sure. */
export interface RequestTriage {
  class: RequestClass | null; confidence: number; reason: string;
  answered_by: "rules" | "decision" | "llm_small" | "fallback"; flags: string[]; missing: string[]; model?: string;
}
/** How a request was answered: an Ask turn, an investigation run, a reviewed definition or an analyst's text. */
export interface RequestAnswer {
  via: "auto" | "auto_with_review" | "human"; kind?: "ask_turn" | "run" | "context_entry";
  turn_id?: string; thread_id?: string; run_id?: string; query_id?: string | null; answered_by?: string | null;
  governance?: "governed" | "ad_hoc"; explanation?: string | null; entry_id?: string; name?: string; definition?: string;
  text?: string; answered_by_user?: string; edited?: boolean; evidence?: Dict[]; certified?: Dict[];
}
/** A draft schedule or monitor for an R4/R5 request: never created by the desk; the analyst submits it (approval-gated). */
export interface RequestProposal {
  kind: "schedule" | "monitor"; endpoint: string; body: Dict; summary: string; needs: string[]; approval: string;
}
/** What an escalated request carries for the analyst (analysts only). Context is data, never instructions. */
export interface DataPack {
  reason: string; triage: Partial<RequestTriage>; intent: Dict; missing: unknown[];
  refusal: { kind?: string; title?: string; message?: string; remedy?: string } | null;
  context_receipts: { kind: "context_entry" | "asset"; id: string; name?: string; asset?: string; entry_kind?: string;
    business_name?: string | null; trusted?: boolean; excerpt: string }[];
  candidate_sql: { sql: string; from: string; status?: string | null; error?: string | null }[];
  evidence: { kind: string; id: string; thread_id?: string; status?: string; inspector?: string }[];
  proposal?: RequestProposal;
}
export interface RequestTimelineEvent {
  seq: number; type: string; from_state: string | null; to_state: string | null; actor: string; payload: Dict; created_at: string;
}
export interface DeskRequest {
  id: string; workspace_id: string; requester_id: string; channel: string; text: string; normalized_intent: Dict;
  class: RequestClass | null; state: RequestState; priority: RequestPriority; sla_due_at: string | null;
  sla_breached_at: string | null; sla_breached: boolean; assignee_id: string | null; conversation_id: string | null;
  run_ids: string[]; triage: Partial<RequestTriage>; answer: RequestAnswer | null; resolution: string | null;
  human_minutes: number | null; cost_usd: number; duplicate_of: string | null; followers: string[]; revision: number;
  created_at: string; updated_at: string; first_answer_at: string | null; closed_at: string | null;
  data_pack?: DataPack | null;
  original?: { id: string; state: RequestState; answer: RequestAnswer | null } | null;
  timeline?: RequestTimelineEvent[];
}
export interface CreatedRequest { request: DeskRequest; conversation_id: string | null }

/** FR-053 value metrics over a period (desk/value.py); `daily` is the nightly rollup (desk_daily, class `all`). */
export interface ValueSummary {
  n: number; duplicates: number; closed: number; accepted: number; auto_resolved: number;
  auto_resolution_rate: number | null; accepted_rate: number | null; tta_p50_seconds: number | null;
  tta_p95_seconds: number | null; human_minutes: number; est_minutes_saved: number; unbaselined: number; cost_usd: number;
}
export interface ValueDay extends ValueSummary { day: string; class: string; computed_at: string | null }
export interface ValueReport extends ValueSummary {
  workspace_id: string; from: string; to: string; truncated: boolean;
  by_class: Record<string, ValueSummary & { baseline_minutes: number | null }>;
  baselines: Record<string, number>; daily: ValueDay[]; rollup_computed_at: string | null;
}
export interface ClassBaseline {
  id: string; workspace_id: string; class: RequestClass; baseline_minutes: number; source: string; measured_at: string; created_at: string;
}

/** Whether billable model calls are refused because no store can prove the spend caps (Redis, or Postgres in lite). */
export function spendCountersDown(h: { counters_available: boolean; spend_counters?: { available: boolean } }): boolean {
  return h.spend_counters ? !h.spend_counters.available : !h.counters_available;
}

// ----------------------------------------------------------------------------------- Goal API v2 (S06)
/** Budget counters a goal run may not exceed (contracts/goals.py RunLimits). */
export interface RunLimits {
  max_steps?: number; max_duration_s?: number; max_scan_bytes?: number; max_cost_usd?: number;
  max_replans?: number; max_rows?: number; max_retries?: number; max_parallel?: number;
}
/** A `run.budget` payload and the `budget` of GET /v2/runs/{id}: limits, use, what is left, what ran out. */
export interface BudgetSnapshot {
  limits: RunLimits & { source_slots?: Record<string, number> };
  used: Record<string, number>;
  remaining: Record<string, number>;
  exhausted: string[];
}
export interface RunLimitations {
  outcome: string; reason: string; limits: RunLimits; used: Record<string, number>;
  completed_steps: string[]; not_run: string[]; verified_findings: string[]; unverified_findings: number;
}
export interface ComplexityDecisionView {
  tier: "low" | "medium" | "complex"; floor: string; rules_tier: string; requested_tier: string | null;
  playbook: string; playbook_reason: string; limits: RunLimits; reasons: string[];
  features: { table_count: number; join_depth: number; estimated_scan_rows: number; ambiguity_flags: string[]; policy_tier: string };
  clarify: { value?: string; backend?: string; missing_inputs?: string[] };
}
export interface GoalRunView {
  run_id: string; goal_id: string | null; workspace_id: string; state: string; control: string; objective: string;
  autonomy_level: number; plan_version: number; plan_hash: string | null; playbook: string | null; error: string | null;
  complexity: ComplexityDecisionView | null; budget: BudgetSnapshot | null; limitations: RunLimitations | null;
  steps: { key: string; agent: string; title: string; status: string; attempts: number; error: string | null }[];
  events_url: string; created_at: string | null; finished_at: string | null;
}
export interface PlanVersionTask {
  key: string; agent?: string; status: string; attempts?: number; error?: string | null; depends_on?: string[];
  plan_version?: number; output_keys?: string[];
}
export interface PlanVersionView {
  run_id: string; version: number; current: boolean; plan: Dict | null; plan_hash: string | null;
  superseded_by?: { reason_code: string; detail: string | null; actor: string | null; at: string | null };
  tasks: PlanVersionTask[];
}
export interface GoalSummary {
  goal_id: string; run_id: string; question: string; mode: string; state: string; tier: string | null;
  playbook: string | null; plan_version: number; limits: RunLimits; used: Record<string, number>;
  limitation: string | null; error: string | null; created_at: string | null; finished_at: string | null;
}
export interface AutonomySummary {
  workspace_id: string; window_days: number;
  runs: { by_state: Record<string, number>; total: number };
  queues: Record<string, number>;
  exceptions: { count: number; waiting_for_people: number;
    runs: { run_id: string; objective: string; state: string; origin: string | null; reason: string | null; finished_at: string | null }[] };
  budgets: { month_spend_usd: number; month_budget_usd: number; goal_spend_usd: number; run_cost_budget_usd: number; max_parallel_steps: number };
  schedules: { total: number; enabled: number; next_run_at: string | null };
  monitors: { total: number; enabled: number };
  alerts: { raised: number; by_status: Record<string, number>; resolved_without_investigation: number; noise_rate: number | null };
}
export interface GoalCreated {
  goal_id: string; run_id: string; state: string; events_url: string; tier: string; playbook: string; limits: RunLimits;
}
export type GoalInput = Schemas["GoalIn"];
export type GoalSignalType = "pause" | "resume" | "cancel" | "redirect" | "approve_step";

/** A fresh Idempotency-Key; a retry of the same submission reuses it (the server replays the first result). */
export function newIdempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  return c?.randomUUID ? c.randomUUID() : `goal-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export const goalsApi = {
  create: (body: GoalInput, idempotencyKey: string) =>
    request<GoalCreated>("POST", apiPath("post", "/api/v2/goals", { body }), body, { headers: { "Idempotency-Key": idempotencyKey } }),
  list: (ws: string, state?: string, limit = 50) =>
    get("/api/v2/goals", { query: { workspace_id: ws, state, limit } }) as Promise<GoalSummary[]>,
  summary: (ws: string, days = 30) => get("/api/v2/autonomy", { query: { workspace_id: ws, days } }) as Promise<AutonomySummary>,
  run: (run: string) => get("/api/v2/runs/{run_id}", { path: { run_id: run } }) as Promise<GoalRunView>,
  plan: (run: string, version?: number) =>
    get("/api/v2/runs/{run_id}/plan", { path: { run_id: run }, query: { version } }) as Promise<PlanVersionView>,
  signal: (run: string, type: GoalSignalType, payload: Dict = {}) =>
    post("/api/v2/runs/{run_id}/signals", { path: { run_id: run }, body: { type, payload } }) as Promise<{ run_id: string; signal: string; state: string; result: Dict }>,
};
