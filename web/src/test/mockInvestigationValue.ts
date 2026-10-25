import type { Run } from "../api";
import type { InvestigationAction, PilotReview, ReviewedContext } from "../lib/investigationValue";

export const TEMPLATE_FIXTURES = [
  { id: "sla_change", version: 1, title: "Explain an SLA increase", description: "Compare breach rates and workload.", question: "Investigate whether SLA breaches increased. Compare groups and priorities." },
  { id: "team_comparison", version: 1, title: "Compare team performance", description: "Check workload and priority mix.", question: "Compare assignment-group performance while checking priority mix." },
  { id: "recurring_incidents", version: 1, title: "Find recurring incidents", description: "Find repeated problems.", question: "Investigate recurring incidents and separate duplicates." },
  { id: "data_quality", version: 1, title: "Check data quality", description: "Check missing data and duplicates.", question: "Investigate missing data, duplicates and timestamps." },
];
let actions: InvestigationAction[] = [];
let reviews: PilotReview[] = [];
export function resetInvestigationValue() { actions = []; reviews = []; }
export function investigationValueRoute(method: string, path: string, body: string | null | undefined, ws: string, run: Run, headers: Record<string, string>) {
  const root = `/workspaces/${ws}`;
  if (!path.startsWith(root)) return null;
  if (method === "GET" && path === root + "/investigation-templates") return { status: 200, body: TEMPLATE_FIXTURES };
  if (method === "GET" && path === root + "/reviewed-context") return { status: 200, body: { version: 0, content_hash: null, facts: [], open_questions: 0 } satisfies ReviewedContext };
  if (method === "POST" && path === root + "/guided-investigations") return { status: 200, body: run };
  if (method === "GET" && path === root + "/investigation-actions") return { status: 200, body: actions };
  const action = new RegExp(`^${root}/findings/([^/]+)/actions(?:/([^/]+))?$`).exec(path);
  if (action && ["POST", "PUT"].includes(method)) {
    const existing = actions.find((a) => a.id === action[2]);
    if (existing && headers["if-match"] !== `"${existing.revision}"`) return { status: 412, body: { error: { message: "Action changed; reload" } } };
    const saved = { ...JSON.parse(body || "{}"), id: existing?.id ?? `act_${actions.length + 1}`, insight_id: action[1], workspace_id: ws,
      run_id: run.id, created_by: "usr_admin", revision: (existing?.revision ?? 0) + 1,
      verification_state: { badge: "verified", state: "ACTIVE", record_id: "vr", void: null } } as InvestigationAction;
    actions = [...actions.filter((a) => a.id !== saved.id), saved];
    return { status: 200, body: saved };
  }
  if (method === "GET" && path === root + "/pilot-reviews") {
    const summary = (kind: string) => {
      const group = reviews.filter((r) => r.evidence_kind === kind); const reviewed = group.filter((r) => r.accuracy !== "not_reviewed");
      const baseline = group.reduce((n, r) => n + r.baseline_minutes, 0); const saved = group.reduce((n, r) => n + r.baseline_minutes - r.assisted_minutes, 0);
      return { assessments: group.length, reviewers: new Set(group.map((r) => r.reviewer_id)).size, investigations: new Set(group.map((r) => r.run_id)).size,
        minutes_saved: saved, time_saved_fraction: baseline ? saved / baseline : null, accuracy_reviewed: reviewed.length,
        correct_fraction: reviewed.length ? reviewed.filter((r) => r.accuracy === "correct").length / reviewed.length : null,
        average_usefulness: group.length ? group.reduce((n, r) => n + r.usefulness, 0) / group.length : null,
        would_use_again_fraction: group.length ? group.filter((r) => r.would_use_again).length / group.length : null };
    };
    return { status: 200, body: { reviews, summary: { real_pilot: summary("real_pilot"), rehearsal: summary("rehearsal") }, self_reported: true, at_limit: false } };
  }
  if (["POST", "PUT"].includes(method) && path.startsWith(root + "/pilot-reviews")) {
    const existing = reviews.find((r) => path.endsWith("/" + r.id));
    const saved = { ...JSON.parse(body || "{}"), id: existing?.id ?? `prv_${reviews.length + 1}`, reviewer_id: "usr_admin", revision: (existing?.revision ?? 0) + 1 } as PilotReview;
    reviews = [...reviews.filter((r) => r.id !== saved.id), saved];
    return { status: 200, body: saved };
  }
  return null;
}
