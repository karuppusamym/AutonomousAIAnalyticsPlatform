/**
 * Ask screen helpers (P4-U02): thread grouping, provenance and staleness pills, one refusal state per
 * kind, and the promote actions. Pure functions, so the rules are unit-tested without rendering.
 */
import type {
  AnswerCost, AskPromotion, AskRefusal, AskStaleness, AskThread, AskTurn, ContextReceipt, ConversationMemory, ConversationMessage, DecisionRow,
} from "../api";
import { fmtUsd } from "./format";
import type { StateKind } from "../components/ui";
import type { Tone } from "./status";

export interface ThreadGroup {
  label: string;
  items: AskThread[];
}

/** Today / Previous 7 days / Earlier, newest activity first; empty groups are dropped. */
export function groupThreads(threads: AskThread[], now: Date = new Date()): ThreadGroup[] {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const week = day - 7 * 86_400_000;
  const groups: ThreadGroup[] = [{ label: "Today", items: [] }, { label: "Previous 7 days", items: [] }, { label: "Earlier", items: [] }];
  const sorted = [...threads].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at));
  for (const t of sorted) {
    const at = Date.parse(t.updated_at);
    groups[at >= day ? 0 : at >= week ? 1 : 2].items.push(t);
  }
  return groups.filter((g) => g.items.length > 0);
}

export interface Pill {
  tone: Tone;
  label: string;
  title?: string;
}

/** Where the answer came from, in the order a reader checks it: governed or ad hoc, who answered, the gateway, the tables. */
export function provenancePills(turn: AskTurn): Pill[] {
  const p = turn.provenance ?? {};
  const out: Pill[] = [];
  if (turn.status === "answered") {
    out.push(p.governance === "governed"
      ? { tone: turn.evidence_status?.state === "changed" ? "warning" : "success", label: "Approved metric calculation",
        title: "Compiled from a pinned approved definition; see the evidence for its current state" }
      : { tone: "neutral", label: "Ad hoc analysis", title: "This answer does not use a compiled approved metric definition" });
  }
  if (turn.answered_by === "registry") {
    const name = p.verified_query?.name ?? (turn.verified_query?.name as string | undefined);
    out.push({ tone: "success", label: `Verified query${name ? `: ${name}` : ""}`, title: "Answered from the verified-query registry, no model call" });
  } else if (turn.answered_by === "rules") {
    out.push({ tone: "success", label: "Built from the catalog · no model", title: "A schema-bound rule built the SQL from the catalog; no model call" });
  } else if (turn.answered_by === "model") {
    out.push({ tone: "info", label: `Generated SQL${turn.model ? ` · ${turn.model}` : ""}`,
      title: "A model wrote the SQL; the platform validated and ran it" });
  }
  if (turn.status === "answered") out.push({ tone: "neutral", label: "Validated by the query gateway", title: "Read-only, within your access" });
  if ((p.repairs ?? 0) > 0) out.push({ tone: "warning", label: `${p.repairs} repair${p.repairs === 1 ? "" : "s"}`, title: "The gateway refused earlier SQL" });
  if (p.cache_hit) out.push({ tone: "neutral", label: "Cached result" });
  for (const a of p.assets ?? []) {
    const src = [a.source_name, a.execution_mode].filter(Boolean).join(", ");
    out.push({ tone: "neutral", label: a.business_name ? `${a.business_name} (${a.asset})` : a.asset, title: src || undefined });
  }
  return out;
}

const STALE_TONE: Record<AskStaleness["state"], Tone> = { fresh: "success", aging: "warning", stale: "danger", changed: "warning", unknown: "neutral" };

export function stalenessPill(s: AskStaleness | null | undefined): Pill {
  const state = s?.state ?? "unknown";
  return { tone: STALE_TONE[state] ?? "neutral", label: s?.label ?? "Data freshness unknown", title: s?.data_as_of ? `Oldest table data: ${s.data_as_of}` : undefined };
}

export interface RefusalView {
  state: StateKind;
  action: "parameters" | "rephrase" | "explain" | "sources" | "access" | "retry";
  actionLabel: string;
}

/** One refusal state per kind, each with the action that is its remedy. Unknown kinds read as failures. */
export const REFUSAL_VIEWS: Record<string, RefusalView> = {
  needs_input: { state: "refused", action: "parameters", actionLabel: "Answer with these values" },
  clarify: { state: "refused", action: "rephrase", actionLabel: "Rephrase the question" },
  sql_rejected: { state: "refused", action: "explain", actionLabel: "Explain the SQL" },
  no_model: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  no_scope: { state: "empty", action: "sources", actionLabel: "Go to Sources" },
  policy_denied: { state: "not-entitled", action: "access", actionLabel: "See the workspace policy" },
  budget_exceeded: { state: "refused", action: "retry", actionLabel: "Try again" },
  spend_cap: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  timeout: { state: "failed", action: "rephrase", actionLabel: "Narrow the question" },
  unavailable: { state: "failed", action: "retry", actionLabel: "Try again" },
  failed: { state: "failed", action: "retry", actionLabel: "Try again" },
  // Why no model could write the SQL: each cause has its own remedy (the server's text says what to fix).
  mode_off: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  no_api_key: { state: "failed", action: "retry", actionLabel: "Try again after the restart" },
  provider_cooldown: { state: "failed", action: "retry", actionLabel: "Try again" },
  policy_blocked: { state: "not-entitled", action: "access", actionLabel: "See the workspace policy" },
  residency_blocked: { state: "not-entitled", action: "access", actionLabel: "See the workspace policy" },
  approval_required: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  model_budget: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  cap_reached: { state: "refused", action: "explain", actionLabel: "Write the SQL instead" },
  context_over_budget: { state: "refused", action: "rephrase", actionLabel: "Narrow the question" },
  invalid_output: { state: "failed", action: "retry", actionLabel: "Ask again" },
};

/** Follow-up questions offered with a turn: other groupings of a rules answer, or the rephrasings a
 * rules clarify proposes. Each is a complete question the rules answer without a model. */
export function turnSuggestions(turn: AskTurn): string[] {
  const fromAnswer = turn.provenance?.suggestions ?? [];
  const fromRefusal = (turn.refusal?.details?.suggestions as string[] | undefined) ?? [];
  return (turn.status === "answered" ? fromAnswer : fromRefusal).filter((q) => typeof q === "string" && q.trim());
}

export function refusalView(r: AskRefusal | null | undefined): RefusalView {
  return REFUSAL_VIEWS[r?.kind ?? "failed"] ?? REFUSAL_VIEWS.failed;
}

export const PROMOTE_LABELS: Record<string, string> = {
  verified_query: "Save as verified query",
  metric: "Propose as metric",
  monitor: "Monitor this",
  dashboard: "Add to dashboard",
  investigate: "Investigate why",
  report: "Save as report",
};

/** The workspace role each promotion needs (the server checks it again; services/ask.py, saved_analysis.py). */
export const PROMOTE_MIN_ROLE: Record<string, "analyst" | "editor"> = {
  verified_query: "analyst", metric: "editor", monitor: "editor", dashboard: "editor", investigate: "analyst", report: "analyst",
  schedule: "editor",
};

const APPROVAL_WORDS: Record<string, string> = {
  pending: "waiting for an approver", approved: "approved", rejected: "rejected", expired: "expired", invalidated: "no longer valid",
  executed: "already used", missing: "no longer on record",
};

/** What a promotion did, in words (an approval is pending, not done). */
export function promotionText(p: AskPromotion): string {
  const name = p.name ? ` "${p.name}"` : "";
  const dashboard = String(p.dashboard ?? "").trim();
  switch (p.target) {
    case "verified_query": return `Saved as verified query${name}: the same question now answers without a model.`;
    case "metric": return p.status === "approved" ? `Metric${name} is approved.` : `Metric${name} proposed: an approver approves it in the semantic layer.`;
    case "monitor": return `Monitor${name} created.`;
    case "dashboard":
      if (p.status === "approval_required") {
        if (!p.approval_status || p.approval_status === "pending") {
          return `Waiting for approval before the chart is added to ${dashboard || "the dashboard"}.`;
        }
        return p.approval_status === "approved"
          ? `Approved: complete it to publish the chart to ${dashboard || "the dashboard"}.`
          : `Adding the chart to ${dashboard || "the dashboard"}: approval ${APPROVAL_WORDS[p.approval_status] ?? p.approval_status}. Request it again.`;
      }
      if (p.status === "published") {
        return p.destination === "preview"
          ? `Published to ${dashboard || "the dashboard"} in the in-platform preview (no BI tool is connected).`
          : `Published to ${dashboard || "the dashboard"} in ${String(p.destination ?? "the BI tool")}.`;
      }
      return `Added to the dashboard ${dashboard}.`.trim();
    case "investigate": return "Investigation started.";
    case "report": return "Saved as a report: find it in Outputs, filtered to reports.";
    case "schedule": return p.status === "created" ? "Calculation scheduled."
      : `Schedule approval ${APPROVAL_WORDS[p.approval_status ?? "pending"] ?? p.approval_status}.`;
    default: return `${p.target}: ${p.status}`;
  }
}

/** A promotion waiting on its approval can be completed once approved; a rejected or expired one cannot. */
export function canComplete(p: AskPromotion): boolean {
  return p.status === "approval_required" && !!p.approval_id && p.approval_status === "approved";
}

/** Record a promotion on the turn as the server does: completing one replaces its pending entry. */
export function mergePromotion(list: AskPromotion[], p: AskPromotion): AskPromotion[] {
  const i = p.approval_id ? list.findIndex((x) => x.approval_id === p.approval_id && x.target === p.target && x.status === "approval_required") : -1;
  if (i < 0) return list.some((x) => x.id === p.id && x.status === p.status && x.target === p.target) ? list : [...list, p];
  return list.map((x, k) => (k === i ? p : x));
}

/** Only an answer that returned rows can be promoted; the server checks this again. */
export function canPromote(turn: AskTurn): boolean {
  return turn.status === "answered" && !!turn.result && turn.evidence_status?.state !== "changed";
}

/** One line per decision for the Decision tab: who decided what, and whether a fallback happened. */
export function decisionLine(d: DecisionRow): string {
  const answer = typeof d.answer === "string" ? d.answer : JSON.stringify(d.answer);
  const by = d.backend === "rules" ? "rule" : d.model ? `${d.backend} (${d.model})` : d.backend;
  return `${d.purpose}: ${answer} — decided by ${by}${d.fallback_reason ? `; fallback: ${d.fallback_reason}` : ""}`;
}

/** The model probabilities of a decision, highest first (JEV / classifier backends). */
export function topProbabilities(d: { probabilities?: Record<string, number> | null }, n = 3): [string, number][] {
  return Object.entries(d.probabilities ?? {}).sort(([, a], [, b]) => b - a).slice(0, n);
}

/** A context receipt (P4-K05/U04): title, where in a pack it is, and whether a person reviewed it. */
export function receiptView(r: ContextReceipt) {
  return {
    title: String(r.title ?? r.name ?? r.id ?? r.kind ?? "context item"),
    where: r.path ? `${r.path}${r.anchor ? `#${r.anchor}` : ""}` : null,
    reviewed: typeof r.source === "string" && r.source.startsWith("review:"),
    curated: r.source === "user",
    untrusted: r.trusted === false,
    section: r.section ?? r.kind ?? null,
    documentId: typeof r.document_id === "string" ? r.document_id : null,
  };
}

// ------------------------------------------------------------------------------------ conversations (S11-T2)
const RUNG_LABELS: Record<string, string> = {
  llm_small: "small model", llm_large: "large model", decision: "decision model", cache: "cache", rules: "rule",
};

/** Who or what answered (UX invariant: every answer names it): a rule, a verified query, an approved metric or a model. */
export function answeredByLabel(turn: Pick<AskTurn, "answered_by" | "model" | "status" | "route" | "cost">): string {
  if (turn.route === "investigate") return "an investigation run";
  switch (turn.answered_by) {
    case "registry": return "verified query (no model)";
    case "rules": return "rule built from the catalog (no model)";
    case "semantic": return "approved metric calculation (no model)";
    case "sample": return "example rows rule (no model)";
    case "model": {
      const rungs = (turn.cost?.rungs ?? []).map((r) => RUNG_LABELS[r] ?? r);
      return `model ${turn.model ?? turn.cost?.models[0] ?? ""}`.trim() + (rungs.length ? ` (${rungs.join(", ")})` : "");
    }
    default: return turn.status === "answered" ? "the governed Ask path" : "not answered";
  }
}

/** The answer's cost in one line: spend and model calls, or why there was none. */
export function costLine(cost: AnswerCost | undefined | null): string {
  if (!cost) return "cost not recorded";
  const parts: string[] = [];
  if (cost.model_calls > 0) {
    parts.push(`${fmtUsd(cost.usd)} · ${cost.model_calls} model call${cost.model_calls === 1 ? "" : "s"}`);
  } else {
    parts.push("$0 · no model call");
  }
  if (cost.cache_hits > 0) parts.push(`${cost.cache_hits} from cache`);
  if (cost.avoided > 0) parts.push(`${cost.avoided} model call${cost.avoided === 1 ? "" : "s"} avoided`);
  return parts.join(" · ");
}

/** A v2 message as the Ask turn the answer views render (the same row, with the fields the message names). */
export function messageToTurn(m: ConversationMessage): AskTurn {
  return { ...m.answer, id: m.id, thread_id: m.conversation_id, question: m.text, seq: m.seq, created_at: m.created_at,
    workspace_id: "" } as AskTurn;
}

export type MemoryScope = ConversationMemory["items"][number]["scope"];

/** Personal and team memory look different (S11 UX invariant): colour class and a written label. */
export const MEMORY_LABELS: Record<MemoryScope, { label: string; className: string; title: string }> = {
  session: { label: "This conversation", className: "memory-session", title: "Applies only in this conversation" },
  personal: { label: "Personal", className: "memory-personal", title: "Your own memory: applies in your conversations, nobody else sees it" },
  workspace: { label: "Team", className: "memory-team", title: "The approved team definition: applies to everyone in the workspace" },
};

/** The message a branch should continue after so that `turnId` can be asked again differently (null: before the first). */
export function branchPointBefore(turns: Pick<AskTurn, "id">[], turnId: string): string | null {
  const i = turns.findIndex((t) => t.id === turnId);
  return i > 0 ? turns[i - 1].id : null;
}
