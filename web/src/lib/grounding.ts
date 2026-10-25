import { request } from "../api";

export interface MetricMeaning { outcome_column: string; numerator: string; denominator: string; grain: string; unit: string; time_column: string | null; exclusions: string; true_value: string | number | boolean | null;calculation?:"auto"|"numeric_mean"|"flag_rate"|"duration_hours";end_column?:string|null }
export interface Predicate { column: string; op: "=" | "!=" | ">" | ">=" | "<" | "<=" | "in" | "not_in"; value: string | number | boolean | (string | number)[] }
export interface BindingInput { term: string; definition: string; kind: "definition" | "metric" | "population_rule" | "quality_rule" | "policy"; columns: string[]; domain: string; source_ids: string[]; owner_id: string | null; effective_from: string | null; expires_on: string | null; state: "draft" | "approved" | "retired"; evidence: string; examples: string; metric: MetricMeaning | null; predicate: Predicate | null }
export interface Binding { id: string; term: string; definition: BindingInput; revision: number; mapping_state: string; state: string }
export interface KnownMeaning { id: string; term: string; definition: string; columns: string[]; state?: string; by?: string; revision?: number; evidence_ids?: string[] }
export interface GroundingReport { question: string; resolutions: KnownMeaning[]; missing: string[]; needs_clarification: boolean; model_status: string; used_count: number; available_count: number; clarification: string[];analysis_filters?:{column:string;op:string;value:unknown}[]; metric_proposals: { term: string; definition: string; columns: string[]; missing_contract_fields: string[] }[] }
export interface Studio { mappings: Binding[]; catalog: {ref: string; name: string; type: string; description: string;source_id?:string;source_name?:string}[]; domains: string[]; available_knowledge: KnownMeaning[]; coverage: { columns: number; linked_columns: number; unmapped_columns: string[]; note: string } }
export interface GoldenInput { question: string; expected_columns: string[]; expect_clarification: boolean; notes: string }
export interface Evaluation { id: string; result: { total: number; passed: number; use_model: boolean; cases: {id: string; question: string; passed: boolean; actual_columns: string[]}[] } }
export interface GoldenResults { questions: {id: string; definition: GoldenInput}[]; evaluations: Evaluation[]; note: string }
const base = (ws: string) => `/workspaces/${encodeURIComponent(ws)}/grounding`;
export const groundingApi = {
  studio: (ws: string) => request<Studio>("GET", base(ws)),
  check: (ws: string, id: string) => request<{note: string; checks:{column:string;sampled_rows:number;nonnull_rows:number;distinct_values:number;query_id:string;matching_rows?:number}[]}>("POST", base(ws) + `/bindings/${encodeURIComponent(id)}/check`),
  save: (ws: string, body: BindingInput, edit?: Binding) => edit
    ? request<Binding>("PUT", base(ws) + `/bindings/${encodeURIComponent(edit.id)}`, body, { headers: { "If-Match": `"${edit.revision}"` } })
    : request<Binding>("POST", base(ws) + "/bindings", body),
  resolve: (ws: string, question: string, use_model = true) => request<GroundingReport>("POST", base(ws) + "/resolve", { question, use_model }),
  golden: (ws: string) => request<GoldenResults>("GET", base(ws) + "/golden"),
  addGolden: (ws: string, body: GoldenInput) => request("POST", base(ws) + "/golden", body),
  evaluate: (ws: string, use_model = false) => request<Evaluation>("POST", base(ws) + "/golden/evaluate", {use_model}),
  run: (ws: string, run: string) => request<{grounding: GroundingReport; brief_receipts: {purpose: string; receipt: { id: string; version: number }}[]; coverage: {covered: boolean; missing: string[]} | null}>("GET", base(ws) + `/runs/${encodeURIComponent(run)}`),
  recheck: (ws: string, run: string) => request("POST", base(ws) + `/runs/${encodeURIComponent(run)}/recheck`),
};
