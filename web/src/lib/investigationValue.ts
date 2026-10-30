import { request, type Assertion, type VerificationState } from "../api";

export interface InvestigationTemplate { id: string; version: number; title: string; description: string; question: string }
export interface ReviewedContext { version: number; content_hash: string | null; facts: Assertion[]; open_questions: number }
/** Inclusive calendar dates (YYYY-MM-DD); `label` is the reader's own name for the range, shown, never parsed. */
export interface DateRange { start: string; end: string; label?: string | null }
export type ComparisonMode = "previous_period" | "same_period_last_year" | "custom" | "none";
export const COMPARISON_MODES: { id: ComparisonMode; label: string }[] = [
  { id: "previous_period", label: "The period just before" },
  { id: "same_period_last_year", label: "The same period last year" },
  { id: "custom", label: "Dates I choose" },
  { id: "none", label: "No comparison" },
];
/** PILOT-FIX-01: the period and baseline are typed date ranges; the server applies them as filters and compares them. */
export interface GuidedInput {
  template_id: string; period: DateRange; comparison_mode: ComparisonMode; baseline?: DateRange | null;
  focus: string; decision: string; source_ids?: string[];
}
export interface ActionInput {
  title: string; next_step: string; owner_id: string | null; due_date: string | null; impact_assessment: string;
  success_measure: string; open_questions: string; status: "proposed" | "in_progress" | "done" | "dismissed"; resolution_note: string;
}
export interface InvestigationAction extends ActionInput { id: string; workspace_id: string; run_id: string; insight_id: string; revision: number; created_by: string; verification_state?: VerificationState }
/** What a pilot assessment measured (VALUE-05): an investigation, or a conversational deliverable. */
export type PilotSubjectType = "investigation" | "ask" | "dashboard" | "report" | "digest";
export const PILOT_SUBJECT_LABEL: Record<PilotSubjectType, string> = {
  investigation: "Investigation", ask: "Ask conversation", dashboard: "Dashboard by conversation",
  report: "Report by conversation", digest: "Scheduled digest",
};
export interface PilotSubject { id: string; label: string; status: string }
export interface PilotInput {
  subject_type: PilotSubjectType; run_id: string | null; subject_id: string | null;
  evidence_kind: "rehearsal" | "real_pilot"; task: string; baseline_minutes: number; assisted_minutes: number;
  accuracy: "correct" | "partly_correct" | "incorrect" | "not_reviewed"; usefulness: number; would_use_again: boolean; notes: string;
  review_minutes: number | null; interventions: number;
}
export interface PilotReview extends PilotInput { id: string; reviewer_id: string; revision: number }
export interface PilotMeasures { assessments: number; reviewers: number; minutes_saved: number; time_saved_fraction: number | null; accuracy_reviewed: number; correct_fraction: number | null; average_usefulness: number | null; would_use_again_fraction: number | null; review_minutes?: number | null; interventions?: number }
export interface PilotSummary extends PilotMeasures { investigations: number; subjects?: number; by_feature?: Partial<Record<PilotSubjectType, PilotMeasures>> }
export interface PilotResults { reviews: PilotReview[]; summary: Record<"rehearsal" | "real_pilot", PilotSummary>; self_reported: boolean; at_limit: boolean }
const base = (ws: string) => `/workspaces/${encodeURIComponent(ws)}`;
const match = (revision: number) => ({ headers: { "If-Match": `"${revision}"` } });
export const valueApi = {
  templates: (ws: string) => request<InvestigationTemplate[]>("GET", base(ws) + "/investigation-templates"),
  context: (ws: string) => request<ReviewedContext>("GET", base(ws) + "/reviewed-context"),
  start: (ws: string, body: GuidedInput) => request<{ id: string }>("POST", base(ws) + "/guided-investigations", body),
  actions: (ws: string, run?: string) => request<InvestigationAction[]>("GET", base(ws) + "/investigation-actions" + (run ? `?run_id=${encodeURIComponent(run)}` : "")),
  saveAction: (ws: string, insight: string, body: ActionInput, existing?: InvestigationAction) => request<InvestigationAction>(existing ? "PUT" : "POST",
    base(ws) + `/findings/${encodeURIComponent(insight)}/actions` + (existing ? `/${encodeURIComponent(existing.id)}` : ""), body, existing ? match(existing.revision) : {}),
  reviews: (ws: string) => request<PilotResults>("GET", base(ws) + "/pilot-reviews"),
  subjects: (ws: string, type: PilotSubjectType) => request<PilotSubject[]>("GET", base(ws) + `/pilot-reviews/subjects?subject_type=${type}`),
  saveReview: (ws: string, body: PilotInput, existing?: PilotReview) => request<PilotReview>(existing ? "PUT" : "POST",
    base(ws) + "/pilot-reviews" + (existing ? `/${encodeURIComponent(existing.id)}` : ""), body, existing ? match(existing.revision) : {}),
};
