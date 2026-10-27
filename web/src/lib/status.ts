/**
 * One status vocabulary -> one colour tone, used by every badge in the UI so a status looks the
 * same on the task board, the investigation tree, approvals, queries and the console.
 */
export type Tone = "success" | "running" | "warning" | "danger" | "neutral" | "info";

const TONES: Record<string, Tone> = {
  // success
  completed: "success", ok: "success", verified: "success", supported: "success", approved: "success",
  executed: "success", succeeded: "success", published: "success", ready: "success", allow: "success",
  final: "success", discovered: "info", validated: "success", resolved: "success",
  // in progress
  running: "running", planning: "running", testing: "running", started: "running", in_progress: "running",
  // needs attention
  waiting_user: "warning", paused: "warning", pending: "warning", inconclusive: "warning", partial: "warning",
  approval_required: "warning", awaiting_approval: "warning", open: "warning", acknowledged: "info", proposed: "info", registered: "info", new: "neutral", approved_plan: "warning",
  // bad
  failed: "danger", rejected: "danger", error: "danger", denied: "danger", deny: "danger", failed_verification: "danger",
  timeout: "danger", invalidated: "danger", alerting: "danger", refused: "danger",
  // inert
  cancelled: "neutral", superseded: "neutral", skipped: "neutral", draft: "neutral", expired: "neutral",
  rolled_back: "neutral", unknown: "neutral", disabled: "neutral",
  // steps, readiness, models and pipelines (wave 2)
  pass: "success", promoted: "success", champion: "success", fail: "danger", blocked: "danger", flagged: "warning", warn: "warning",
  needs_input: "warning", challenger: "info", candidate: "neutral", retired: "neutral", recorded: "neutral", unsupported: "neutral",
  not_applicable: "neutral", merged: "info", staged: "running",
  // goal runs (S06): stopped by a budget with validated partial results
  completed_with_limitations: "warning", throttled: "info",
};

/** Server enums whose plain reading is not just the lower-cased word. */
const STATUS_WORDS: Record<string, string> = {
  waiting_user: "waiting for you", failed_verification: "not verified", approval_required: "needs approval",
  awaiting_approval: "awaiting approval", approved_plan: "plan approved", needs_input: "needs input",
  completed_with_limitations: "stopped by a budget",
};

/**
 * The words a status badge shows: one vocabulary in one case everywhere, so "COMPLETED" from the
 * run API and "verified" from a finding read the same way ("completed", "verified").
 */
export function statusLabel(status: string | null | undefined): string {
  if (!status) return "unknown";
  const s = status.toLowerCase();
  return STATUS_WORDS[s] ?? s.replace(/_/g, " ");
}

export function toneFor(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  return TONES[status.toLowerCase()] ?? "neutral";
}

/** Alert severity -> tone (critical red, warning amber, info violet-blue). */
export function severityTone(severity: string | null | undefined): Tone {
  const s = (severity ?? "").toLowerCase();
  return s === "critical" ? "danger" : s === "warning" ? "warning" : s === "info" ? "info" : "neutral";
}

const HYPOTHESIS_ICONS: Record<string, string> = {
  supported: "✓",
  rejected: "✗",
  inconclusive: "?",
  testing: "◔",
  superseded: "↷",
  proposed: "○",
  approved: "●",
};

export function hypothesisIcon(status: string): string {
  return HYPOTHESIS_ICONS[status.toLowerCase()] ?? "○";
}

/** Run end states (contracts/events.py RUN_TERMINAL): a budget stop and a wall-clock expiry end a run too. */
export const TERMINAL_RUN = new Set(["COMPLETED", "FAILED", "REJECTED", "CANCELLED", "COMPLETED_WITH_LIMITATIONS", "EXPIRED"]);

/**
 * Autonomy in plain words (spec v4 §15): what agents do alone and what always waits for a person.
 * The L0–L4 codes appear only under Advanced.
 */
export function autonomyInWords(level: number | null | undefined): string {
  const l = AUTONOMY_LEVELS.find((x) => x.level === level);
  return l ? `${l.name}: ${l.description}` : "Not set";
}

export const AUTONOMY_LEVELS: { level: number; name: string; description: string }[] = [
  { level: 0, name: "Manual", description: "Agents do nothing on their own; every step is started by a person." },
  { level: 1, name: "Recommends", description: "Agents propose plans, hypotheses and queries; a person executes them." },
  { level: 2, name: "Executes after plan approval", description: "The analysis plan must be approved before agents run it." },
  { level: 3, name: "Executes; publish needs approval", description: "Agents run the analysis end to end; publishing needs an approval." },
  { level: 4, name: "Autonomous within policy", description: "Agents act on their own inside workspace policy. Publication still requires approval in this release." },
];
