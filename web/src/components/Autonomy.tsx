/**
 * Goal runs (S06) in the UI: the run view's budget meters and plan versions (S11-T3), and the goal
 * pieces of the Autonomy Center (S11-T5). Everything shown comes from the Goal API v2; nothing is
 * estimated in the browser. A run without goal limits shows no meters rather than invented ones.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  goalsApi, newIdempotencyKey, type AutonomySummary, type BudgetSnapshot, type GoalCreated, type GoalRunView,
  type GoalSummary, type PlanVersionTask, type PlanVersionView, type RunEvent,
} from "../api";
import { to } from "../routes";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Card, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, StatusBadge, Tag, TechnicalDetails, Value } from "./ui";

// ---------------------------------------------------------------------------------- budget meters
/** Each limit, its counter and how to print it. Duration and scan bytes read in their own units. */
export const BUDGET_LINES: { limit: string; counter: string; label: string; unit: "int" | "usd" | "seconds" | "bytes" }[] = [
  { limit: "max_steps", counter: "steps", label: "Steps", unit: "int" },
  { limit: "max_duration_s", counter: "duration_s", label: "Wall clock", unit: "seconds" },
  { limit: "max_cost_usd", counter: "cost_usd", label: "Model spend", unit: "usd" },
  { limit: "max_scan_bytes", counter: "scan_bytes", label: "Data scanned", unit: "bytes" },
  { limit: "max_rows", counter: "rows", label: "Rows read", unit: "int" },
  { limit: "max_replans", counter: "replans", label: "Replans", unit: "int" },
  { limit: "max_retries", counter: "retries", label: "Retries", unit: "int" },
];

export function formatBudget(value: number | undefined, unit: "int" | "usd" | "seconds" | "bytes"): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return "unknown";
  if (unit === "usd") return `$${value.toFixed(value < 1 ? 4 : 2)}`;
  if (unit === "seconds") {
    const s = Math.round(value);
    return s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min` : s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
  }
  if (unit === "bytes") {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = value;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
  }
  return Math.round(value).toLocaleString("en-US");
}

/** The latest `run.budget` payload in the live events, if any is newer than what the page loaded. */
export function latestBudget(events: RunEvent[]): BudgetSnapshot | null {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === "run.budget") return events[i].payload as unknown as BudgetSnapshot;
  }
  return null;
}

export function BudgetMeters({ budget }: { budget: BudgetSnapshot }) {
  const lines = BUDGET_LINES.filter((l) => budget.limits[l.limit as keyof BudgetSnapshot["limits"]] !== undefined);
  if (!lines.length) return <p className="muted small">This run has no budget limits.</p>;
  return (
    <div aria-label="Remaining budget">
      {lines.map((l) => {
        const max = Number(budget.limits[l.limit as keyof BudgetSnapshot["limits"]]);
        const used = Number(budget.used?.[l.counter] ?? 0);
        const pct = max > 0 ? Math.min(100, (used / max) * 100) : used > 0 ? 100 : 0;
        const out = budget.exhausted.some((x) => x.startsWith(l.limit));
        const tone = out || pct >= 90 ? "danger" : pct >= 70 ? "warning" : "success";
        return (
          <div className="meter" key={l.limit} data-testid={`budget-${l.counter}`}>
            <div className="meter-head small">
              <span>{l.label}{out && <> <Tag tone="danger">used up</Tag></>}</span>
              <span>{formatBudget(used, l.unit)}<span className="muted"> of {formatBudget(max, l.unit)}</span></span>
            </div>
            <div className="confidence-track" role="meter" aria-label={`${l.label} used of the goal's budget`}
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}
              aria-valuetext={`${formatBudget(used, l.unit)} of ${formatBudget(max, l.unit)}`}>
              <div className={`confidence-fill fill-${tone}`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
      {budget.limits.max_parallel !== undefined && (
        <p className="small muted">Up to {budget.limits.max_parallel} steps at once
          {budget.limits.source_slots && Object.keys(budget.limits.source_slots).length > 0
            ? <>; each source allows {Object.values(budget.limits.source_slots).join(" / ")} query steps across runs</> : null}.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------- the goal panel
const REASON_WORDS: Record<string, string> = {
  missing_dataset: "a dataset was missing", empty_result: "a result came back empty", rejected_hypothesis: "a hypothesis was rejected",
  quality_failure: "a data-quality check failed", user_redirect: "you redirected it", budget_downgrade: "the budget was tightened",
};

export function reasonWords(code: string | undefined | null): string {
  return code ? REASON_WORDS[code] ?? code.replace(/_/g, " ") : "unknown";
}

/**
 * The goal behind a run: tier and why, the playbook, remaining budget (live from `run.budget`
 * events), and what a budget stop left standing. Renders nothing for a run started without a goal.
 */
export function GoalRunPanel({ runId, events, refreshKey }: { runId: string; events: RunEvent[]; refreshKey: string }) {
  const view = useAsync(() => goalsApi.run(runId), [runId, refreshKey]);
  const live = useMemo(() => latestBudget(events), [events]);
  const v = view.data;
  if (view.error && !v) return null; // an older server without the Goal API: the run view works as before
  if (!v) return <Card title="Goal budget"><Loading /></Card>;
  const budget = live ?? v.budget;
  if (!v.goal_id && !budget) return null;
  const c = v.complexity;
  return (
    <Card title="Goal budget">
      {c && (
        <p className="small">
          <Tag tone={c.tier === "complex" ? "warning" : c.tier === "medium" ? "info" : "neutral"}>{c.tier}</Tag>{" "}
          <span className="muted">playbook</span> <code>{v.playbook ?? c.playbook}</code>
          {c.clarify?.value === "clarify" && <> · <Tag tone="warning">needs clarification</Tag></>}
        </p>
      )}
      {budget ? <BudgetMeters budget={budget} /> : <p className="muted small">No budget reported yet.</p>}
      {v.limitations && (
        <Notice tone="warning">
          <strong>Stopped by a budget:</strong> {v.limitations.reason}. {v.limitations.verified_findings.length} verified finding
          {v.limitations.verified_findings.length === 1 ? "" : "s"} stand; {v.limitations.unverified_findings} unverified stay drafts;
          {" "}{v.limitations.not_run.length} step{v.limitations.not_run.length === 1 ? "" : "s"} did not run.
        </Notice>
      )}
      {c && (
        <TechnicalDetails label="Why this tier">
          <ul className="small">{c.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          <p className="small muted">Policy floor {c.floor}; rules said {c.rules_tier}{c.requested_tier ? `; requested ${c.requested_tier}` : ""}. {c.playbook_reason}.</p>
        </TechnicalDetails>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------- plan versions
export interface PlanDiff { added: string[]; removed: string[]; changed: { key: string; from: string; to: string }[] }

/** What changed in the tasks between a superseded version and another one (keys and statuses). */
export function diffPlans(older: PlanVersionTask[], newer: PlanVersionTask[]): PlanDiff {
  const a = new Map(older.map((t) => [t.key, t]));
  const b = new Map(newer.map((t) => [t.key, t]));
  return {
    added: [...b.keys()].filter((k) => !a.has(k)),
    removed: [...a.keys()].filter((k) => !b.has(k)),
    changed: [...b.keys()].filter((k) => a.has(k) && a.get(k)!.status !== b.get(k)!.status)
      .map((k) => ({ key: k, from: a.get(k)!.status, to: b.get(k)!.status })),
  };
}

/**
 * Every plan version of a run (S06 §3.5): the current one, and each superseded one exactly as it was
 * replaced (immutable), with the reason and what changed against the next version.
 */
export function PlanVersions({ runId, current }: { runId: string; current: number }) {
  const [version, setVersion] = useState(current);
  useEffect(() => setVersion(current), [current]);
  const shown = useAsync<PlanVersionView>(() => goalsApi.plan(runId, version === current ? undefined : version), [runId, version, current]);
  const next = useAsync<PlanVersionView | null>(
    () => (version < current ? goalsApi.plan(runId, version + 1 === current ? undefined : version + 1) : Promise.resolve(null)),
    [runId, version, current]);
  const diff = shown.data && next.data ? diffPlans(shown.data.tasks, next.data.tasks) : null;
  if (current < 1) return <EmptyState title="No plan yet">The plan appears once the run has been planned.</EmptyState>;
  return (
    <div>
      <div className="btn-row" role="group" aria-label="Plan versions">
        {Array.from({ length: current }, (_, i) => i + 1).map((n) => (
          <button key={n} type="button" className={`btn btn-sm ${n === version ? "btn-primary" : "btn-ghost"}`}
            aria-pressed={n === version} onClick={() => setVersion(n)}>v{n}{n === current ? " (current)" : ""}</button>
        ))}
      </div>
      <ErrorBox error={shown.error} onRetry={shown.reload} />
      {!shown.data && !shown.error && <Loading />}
      {shown.data && (
        <>
          {shown.data.superseded_by ? (
            <Notice tone="info">
              Version {shown.data.version} was replaced because {reasonWords(shown.data.superseded_by.reason_code)}
              {shown.data.superseded_by.detail ? <> (“{shown.data.superseded_by.detail}”)</> : null}
              {shown.data.superseded_by.at ? <> on {fmtDate(shown.data.superseded_by.at)}</> : null}. Its steps and outputs are kept as they were;
              approvals given for it no longer apply.
            </Notice>
          ) : <p className="small muted">The plan the run follows now.</p>}
          {diff && (diff.added.length + diff.removed.length + diff.changed.length > 0) && (
            <p className="small" data-testid="plan-diff">
              Against v{shown.data.version + 1}: {diff.changed.length} step{diff.changed.length === 1 ? "" : "s"} changed
              {diff.removed.length ? <>, {diff.removed.length} removed ({diff.removed.join(", ")})</> : null}
              {diff.added.length ? <>, {diff.added.length} added ({diff.added.join(", ")})</> : null}.
            </p>
          )}
          <table className="table">
            <caption className="sr-only">Steps of plan version {shown.data.version}</caption>
            <thead><tr><th scope="col">Step</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Note</th></tr></thead>
            <tbody>
              {shown.data.tasks.map((t) => {
                const changed = diff?.changed.find((d) => d.key === t.key);
                return (
                  <tr key={t.key}>
                    <td><code>{t.key}</code></td>
                    <td><StatusBadge status={t.status} />{changed && <span className="small muted"> → {changed.to.toLowerCase()} in v{shown.data!.version + 1}</span>}</td>
                    <td><Value value={t.attempts} format="int" /></td>
                    <td className="small muted">{t.error ?? (t.output_keys?.length ? `kept: ${t.output_keys.join(", ")}` : "")}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {shown.data.plan_hash && <p className="small muted">Plan hash <code>{shown.data.plan_hash.slice(0, 12)}</code> — an approval binds to this.</p>}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------------- start a goal
const OUTPUTS = [
  { id: "investigation", label: "Investigation" },
  { id: "dashboard_draft", label: "Dashboard draft (publishing still needs approval)" },
  { id: "report", label: "Report" },
] as const;

export function GoalForm({ wsId, onStarted }: { wsId: string; onStarted: (g: GoalCreated) => void }) {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<"goal_supervised" | "goal_autonomous">("goal_supervised");
  const [outputs, setOutputs] = useState<string[]>(["investigation"]);
  const [maxSteps, setMaxSteps] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [key, setKey] = useState(newIdempotencyKey);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const limits: Record<string, number> = {};
    if (maxSteps) limits.max_steps = Number(maxSteps);
    if (maxCost) limits.max_cost_usd = Number(maxCost);
    const body = { workspace_id: wsId, question: question.trim(), mode, requested_outputs: outputs as ("investigation" | "dashboard_draft" | "report")[],
      ...(Object.keys(limits).length ? { limits } : {}) };
    // The same key for a retry of this submission: a lost response replays the goal instead of starting a second one.
    const created = await act.run(() => goalsApi.create(body, key));
    if (created) {
      setKey(newIdempotencyKey());
      setQuestion("");
      onStarted(created);
    }
  };
  const tooShort = question.trim().length < 10;
  return (
    <form onSubmit={submit} aria-label="Start a goal">
      <Field label="Goal" htmlFor="goal-question" hint="What should be answered or built. The platform picks a playbook and budget; you can only tighten the budget.">
        <textarea id="goal-question" rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} />
      </Field>
      <fieldset className="field">
        <legend>Who approves the plan</legend>
        <label><input type="radio" name="goal-mode" checked={mode === "goal_supervised"} onChange={() => setMode("goal_supervised")} /> A person approves the plan before any step runs</label>
        <label><input type="radio" name="goal-mode" checked={mode === "goal_autonomous"} onChange={() => setMode("goal_autonomous")} /> Run on its own (certified playbooks only; publishing still needs approval)</label>
      </fieldset>
      <fieldset className="field">
        <legend>Outputs</legend>
        {OUTPUTS.map((o) => (
          <label key={o.id}><input type="checkbox" checked={outputs.includes(o.id)}
            onChange={(e) => setOutputs((prev) => e.target.checked ? [...prev, o.id] : prev.filter((x) => x !== o.id))} /> {o.label}</label>
        ))}
      </fieldset>
      <div className="form-row">
        <Field label="Max steps (optional)" htmlFor="goal-steps"><input id="goal-steps" type="number" min={1} max={500} value={maxSteps} onChange={(e) => setMaxSteps(e.target.value)} /></Field>
        <Field label="Max model spend, USD (optional)" htmlFor="goal-cost"><input id="goal-cost" type="number" min={0} step="0.01" value={maxCost} onChange={(e) => setMaxCost(e.target.value)} /></Field>
      </div>
      <ErrorBox error={act.error} />
      <button type="submit" className="btn btn-primary" disabled={act.busy || tooShort || !outputs.length}>Start goal</button>
    </form>
  );
}

// ---------------------------------------------------------------------------------- goal list
export function GoalList({ wsId, goals }: { wsId: string; goals: GoalSummary[] }) {
  if (!goals.length) return <EmptyState title="No goals yet">Start one above; its run appears here with its budget.</EmptyState>;
  return (
    <table className="table">
      <caption className="sr-only">Goals, newest first</caption>
      <thead><tr><th scope="col">Goal</th><th scope="col">State</th><th scope="col">Tier · playbook</th><th scope="col">Steps</th><th scope="col">Spend</th><th scope="col">Started</th></tr></thead>
      <tbody>
        {goals.map((g) => (
          <tr key={g.goal_id}>
            <td><Link to={to.run(wsId, g.run_id)}>{g.question}</Link>
              {g.limitation && <div className="small muted">Stopped: {g.limitation}</div>}
              {!g.limitation && g.error && <div className="small muted">{g.error}</div>}</td>
            <td><StatusBadge status={g.state} />{g.plan_version > 1 && <span className="small muted"> · plan v{g.plan_version}</span>}</td>
            <td className="small">{g.tier ?? "—"} · <code>{(g.playbook ?? "").replace(/^playbook\./, "")}</code></td>
            <td className="small">{formatBudget(g.used.steps, "int")}{g.limits.max_steps !== undefined && <span className="muted"> / {g.limits.max_steps}</span>}</td>
            <td className="small">{formatBudget(g.used.cost_usd, "usd")}{g.limits.max_cost_usd !== undefined && <span className="muted"> / {formatBudget(g.limits.max_cost_usd, "usd")}</span>}</td>
            <td className="small">{fmtDate(g.created_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ---------------------------------------------------------------------------------- overview
export function AutonomyOverview({ wsId, summary, onView }: { wsId: string; summary: AutonomySummary; onView: (v: "goals" | "schedules" | "monitors") => void }) {
  const s = summary;
  const queued = s.queues.NEW + s.queues.PLANNING + s.queues.READY;
  const spendPct = s.budgets.month_budget_usd > 0 ? Math.min(100, (s.budgets.month_spend_usd / s.budgets.month_budget_usd) * 100) : 0;
  return (
    <div className="grid-2">
      <Card title="Queues" label="Run queues">
        <KeyValue items={[
          ["Running", formatBudget(s.queues.RUNNING, "int")],
          ["Queued or planning", formatBudget(queued, "int")],
          ["Waiting for a person", formatBudget(s.queues.WAITING_USER, "int")],
          ["Paused", formatBudget(s.queues.PAUSED, "int")],
        ]} />
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => onView("goals")}>See goals</button>
      </Card>
      <Card title={`Exceptions (${s.exceptions.count})`} label="Exceptions">
        {s.exceptions.waiting_for_people > 0 && (
          <p className="small"><Link to={to.approvals(wsId)}>{s.exceptions.waiting_for_people} run{s.exceptions.waiting_for_people === 1 ? "" : "s"} waiting for a person</Link></p>
        )}
        {s.exceptions.runs.length === 0 && s.exceptions.waiting_for_people === 0 && <p className="muted small">Nothing failed, stopped or expired in the last {s.window_days} days.</p>}
        <ul className="small">
          {s.exceptions.runs.slice(0, 8).map((r) => (
            <li key={r.run_id}><StatusBadge status={r.state} /> <Link to={to.run(wsId, r.run_id)}>{r.objective}</Link>
              {r.reason && <span className="muted"> — {r.reason}</span>}</li>
          ))}
        </ul>
      </Card>
      <Card title="Budgets" label="Budgets">
        <div className="meter">
          <div className="meter-head small"><span>Model spend this month</span>
            <span>{formatBudget(s.budgets.month_spend_usd, "usd")}<span className="muted"> of {formatBudget(s.budgets.month_budget_usd, "usd")}</span></span></div>
          <div className="confidence-track" role="meter" aria-label="Model spend this month of the workspace budget" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(spendPct)}>
            <div className={`confidence-fill fill-${spendPct >= 90 ? "danger" : spendPct >= 70 ? "warning" : "success"}`} style={{ width: `${spendPct}%` }} />
          </div>
        </div>
        <KeyValue items={[
          [`Goal runs, last ${s.window_days} days`, formatBudget(s.budgets.goal_spend_usd, "usd")],
          ["Per-run spend cap", formatBudget(s.budgets.run_cost_budget_usd, "usd")],
          ["Steps at once per goal", formatBudget(s.budgets.max_parallel_steps, "int")],
        ]} />
      </Card>
      <Card title="Schedules and monitors" label="Schedules and monitors">
        <KeyValue items={[
          ["Schedules enabled", `${s.schedules.enabled} of ${s.schedules.total}`],
          ["Next schedule", s.schedules.next_run_at ? fmtDate(s.schedules.next_run_at) : "—"],
          ["Monitors enabled", `${s.monitors.enabled} of ${s.monitors.total}`],
          [`Alerts, last ${s.window_days} days`, formatBudget(s.alerts.raised, "int")],
          ["Resolved without an investigation", s.alerts.noise_rate === null ? "—"
            : `${s.alerts.resolved_without_investigation} (${Math.round(s.alerts.noise_rate * 100)}%)`],
        ]} />
        <div className="btn-row">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onView("schedules")}>Schedules</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onView("monitors")}>Monitors &amp; alerts</button>
        </div>
      </Card>
    </div>
  );
}

export type { GoalRunView };
