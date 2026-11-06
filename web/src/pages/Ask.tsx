import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ApiError, api, errorMessage, streamAskTurn, type AskInspector, type AskMode, type AskPromotion, type AskStage, type AskThreadDetail, type AskTurn,
  type ChartHint, type ConversationDetail, type Dict, type QueryResult, type SqlExplanation,
} from "../api";
import { ConversationBar } from "../components/ConversationBar";
import { AddAnswerToDashboard } from "../components/DashboardDraft";
import { AnalystAnswer } from "../components/AnalystAnswer";
import { ChartView } from "../components/Chart";
import { CitationsPanel } from "../components/Citations";
import { NumberCheckPanel } from "../components/NumberCheck";
import { TrustBadge } from "../components/TrustBadge";
import {
  Card, CodeBlock, DataTable, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, PageHeader, StateView, Tabs, TechnicalDetails,
} from "../components/ui";
import { NumberTrail, VerificationBadge, WhyState } from "../components/WhyNumber";
import { WhatIfPanel } from "../components/WhatIf";
import {
  PROMOTE_LABELS, PROMOTE_MIN_ROLE, answeredByLabel, branchPointBefore, canComplete, canPromote, costLine, decisionLine, groupThreads,
  mergePromotion, messageToTurn, promotionText, provenancePills, receiptView, refusalText, refusalView, stalenessPill, topProbabilities, turnSuggestions,
  type Pill,
} from "../lib/ask";
import { guessChart } from "../lib/charts";
import { useExpertView } from "../lib/expert";
import { trustOf } from "../lib/trust";
import { fmtDate, fmtMs, fmtNumber, fmtUsd } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { exampleSql, starterQuestions } from "../lib/starters";
import { needsRole, roleAtLeast, to } from "../routes";
import { useAuth } from "../auth";

/** Reorder a result so the hinted x / y columns come first (chart builders read columns 0 and 1). */
export function projectForChart(res: QueryResult, x?: string | null, y?: string | null) {
  const xi = x ? res.columns.indexOf(x) : -1;
  const yi = y ? res.columns.indexOf(y) : -1;
  if (xi < 0 || yi < 0) return { columns: res.columns, rows: res.rows };
  return { columns: [res.columns[xi], res.columns[yi]], rows: res.rows.map((r) => [r[xi], r[yi]]) };
}

/** Answer card body: an ECharts visual when the shape allows one, and always the table. */
function ResultView({ res, hint, caption = "Query result" }: { res: QueryResult; hint?: ChartHint; caption?: string }) {
  const projected = projectForChart(res, hint?.x, hint?.y);
  const type = guessChart(projected.columns, projected.rows, hint?.type);
  return (
    <>
      <p className="muted small">{res.row_count} rows{res.truncated ? " (truncated by policy max_rows)" : ""} · query <code>{res.query_id}</code>
        {res.duration_ms !== undefined && res.duration_ms !== null ? ` · ${fmtMs(res.duration_ms)}` : ""}{res.cache_hit ? " · cache hit" : ""}</p>
      {type && type !== "table" && <ChartView type={type} preview={projected} height={280} showTable={false} />}
      <DataTable columns={res.columns} rows={res.rows} maxRows={200} caption={caption} />
    </>
  );
}

function GatewayError({ error }: { error: unknown }) {
  if (!error) return null;
  if (error instanceof ApiError && error.code === "sql_rejected") {
    return (
      <div className="alert alert-danger" role="alert">
        <strong>Rejected by the query gateway.</strong> {error.message}
        <p className="small muted">Only read-only SELECTs over the workspace's selected assets are allowed; restricted and PII columns are withheld per policy.</p>
      </div>
    );
  }
  return <ErrorBox error={errorMessage(error)} />;
}

/** The deterministic explanation of a statement, the gateway's verdict and its plan — nothing is executed. */
export function ExplainView({ ex }: { ex: SqlExplanation }) {
  const g = ex.gateway;
  const list = (xs: string[] | undefined) => (xs?.length ? xs.join(", ") : null);
  const items: [string, ReactNode][] = [];
  if (ex.tables?.length) items.push(["Tables", <span key="t">{ex.tables.map((t) => <code key={t} className="tag">{t}</code>)}</span>]);
  if (ex.joins?.length) {
    items.push(["Joins", <ul key="j" className="list compact">{ex.joins.map((j, i) => (
      <li key={i} className="small"><code>{j.table}</code> ({j.kind}){j.on ? <> on <code>{j.on}</code></> : null}</li>
    ))}</ul>]);
  }
  if (ex.filter) items.push(["Filter", <code key="f">{ex.filter}</code>]);
  if (ex.group_by?.length) items.push(["Group by", list(ex.group_by)]);
  if (ex.aggregations?.length) items.push(["Aggregations", list(ex.aggregations)]);
  if (ex.having) items.push(["Having", <code key="h">{ex.having}</code>]);
  if (ex.order_by?.length) items.push(["Order by", list(ex.order_by)]);
  if (ex.limit) items.push(["Limit", ex.limit]);
  if (ex.ctes?.length) items.push(["Named subqueries", list(ex.ctes)]);
  const plan = ex.plan;
  return (
    <section className="stack explain" aria-label="Query explanation">
      {g.accepted ? (
        <Notice tone="success"><strong>The gateway would accept this query.</strong> It is read-only and stays within your scope.</Notice>
      ) : (
        <div className="alert alert-danger" role="alert">
          <strong>The gateway would reject this query{g.code ? ` (${g.code})` : ""}.</strong> {g.reason}
        </div>
      )}
      <p>{ex.summary}</p>
      {items.length > 0 && <KeyValue items={items} />}
      {plan && (plan.available ? (
        <div aria-label="Source plan">
          <p className="small"><strong>Source plan</strong> (EXPLAIN through the gateway, not run): {plan.node} ·
            about {fmtNumber(plan.estimated_rows, 0)} rows · cost {fmtNumber(plan.total_cost, 1)}</p>
          {plan.nodes?.length ? <p className="small muted">Steps: {plan.nodes.join(" → ")}</p> : null}
        </div>
      ) : <p className="small muted">No source plan: {plan.reason}</p>)}
      <p className="muted small">Explained deterministically from the parsed SQL — no model call, and no statement executed.</p>
    </section>
  );
}

// ------------------------------------------------------------------------------------ pills, stages, refusals
function Pills({ pills, label }: { pills: Pill[]; label: string }) {
  return (
    <ul className="pill-row" aria-label={label}>
      {pills.map((p, i) => (
        <li key={i} className={`badge badge-${p.tone}`} title={p.title}><span className="badge-dot" aria-hidden="true" />{p.label}</li>
      ))}
    </ul>
  );
}

function Stages({ stages, live }: { stages: AskStage[]; live: boolean }) {
  if (!stages.length) return null;
  const visible = stages.filter((s) => s.key !== "done" || !live);
  if (live) return (
    <div className="ask-progress" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <span>{visible[visible.length - 1]?.text ?? "Working…"}</span>
      {visible.length > 1 && <details><summary>{visible.length - 1} completed step{visible.length === 2 ? "" : "s"}</summary>
        <ol className="ask-stages small">{visible.slice(0, -1).map((s, i) => <li key={i}>{s.text}</li>)}</ol>
      </details>}
    </div>
  );
  return (
    <ol className="ask-stages small" aria-label="Progress" aria-live={live ? "polite" : undefined}>
      {visible.map((s, i) => (
        <li key={i} className="muted">{s.text}</li>
      ))}
    </ol>
  );
}

function ParameterForm({ turn, onSubmit, busy }: { turn: AskTurn; onSubmit: (p: Dict) => void; busy: boolean }) {
  const missing = turn.refusal?.details?.missing ?? [];
  const [values, setValues] = useState<Record<string, string>>({});
  const id = useId();
  return (
    <form className="form-row" onSubmit={(e) => { e.preventDefault(); onSubmit({ ...(turn.refusal?.details?.parameters ?? {}), ...values }); }}>
      {missing.map((m) => (
        <Field key={m.name} label={m.name.replace(/_/g, " ")} htmlFor={`${id}-${m.name}`}>
          {m.values?.length ? (
            <select id={`${id}-${m.name}`} required value={values[m.name] ?? ""} onChange={(e) => setValues({ ...values, [m.name]: e.target.value })}>
              <option value="" disabled>Choose…</option>
              {m.values.map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          ) : (
            <input id={`${id}-${m.name}`} required value={values[m.name] ?? ""} onChange={(e) => setValues({ ...values, [m.name]: e.target.value })} />
          )}
        </Field>
      ))}
      <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={busy}>Answer with these values</button></div>
    </form>
  );
}

/** Follow-up questions (other groupings, or the rephrasings a clarify proposes), asked with one click. */
function Suggestions({ turn, busy, onAsk }: { turn: AskTurn; busy: boolean; onAsk: (q: string) => void }) {
  const items = turnSuggestions(turn);
  if (!items.length) return null;
  return (
    <section className="stack" aria-label={turn.status === "answered" ? "Follow-up questions" : "Did you mean"}>
      <p className="small muted">{turn.status === "answered" ? "Also try:" : "Did you mean:"}</p>
      <div className="btn-row">
        {items.map((q) => <button key={q} type="button" className="btn btn-xs" disabled={busy} onClick={() => onAsk(q)}>{q}</button>)}
      </div>
    </section>
  );
}

function RefusalState({ turn, ws, busy, onParameters, onRephrase, onExplain, onRetry }: {
  turn: AskTurn; ws: string; busy: boolean; onParameters: (p: Dict) => void; onRephrase: () => void; onExplain: (sql: string) => void; onRetry: () => void;
}) {
  const r = turn.refusal;
  const expert = useExpertView();
  const { user } = useAuth();
  const text = refusalText(r, expert || !!user?.is_admin);
  const view = text.business ? { ...refusalView(r), action: "retry" as const, actionLabel: "Try again" } : refusalView(r);
  const lastSql = turn.attempts.length ? turn.attempts[turn.attempts.length - 1].sql : turn.sql;
  let action: ReactNode = null;
  if (view.action === "rephrase") action = <button type="button" className="btn btn-sm" onClick={onRephrase}>{view.actionLabel}</button>;
  else if (view.action === "explain") action = <button type="button" className="btn btn-sm" onClick={() => onExplain(lastSql ?? "")}>{view.actionLabel}</button>;
  else if (view.action === "sources") action = <Link className="btn btn-sm" to={to.sources(ws)}>{view.actionLabel}</Link>;
  else if (view.action === "access") action = <Link className="btn btn-sm" to={to.governance(ws)}>{view.actionLabel}</Link>;
  else if (view.action === "retry") action = <button type="button" className="btn btn-sm" onClick={onRetry} disabled={busy}>{view.actionLabel}</button>;
  return (
    <div data-refusal={r?.kind ?? "failed"}>
      <StateView kind={view.state} title={text.title} action={action}>
        {text.message && <p>{text.message}</p>}
        <p><strong>What to do:</strong> {text.remedy}</p>
        {view.action === "parameters" && <ParameterForm turn={turn} onSubmit={onParameters} busy={busy} />}
      </StateView>
    </div>
  );
}

// ------------------------------------------------------------------------------------ promote
type MonitorKind = "metric_drift" | "metric_threshold";

/** `role` undefined = not known yet: every action is offered and the server decides. */
function allowed(role: string | null | undefined, target: string): boolean {
  return role === undefined || roleAtLeast(role, PROMOTE_MIN_ROLE[target] ?? "analyst");
}

function PromoteBar({ turn, role, onRecorded }: { turn: AskTurn; role: string | null | undefined; onRecorded: (p: AskPromotion) => void }) {
  const { wsId = "" } = useParams();
  const navigate = useNavigate();
  const act = useAction();
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [investigationOpen, setInvestigationOpen] = useState(false);
  const [dashboardOpen, setDashboardOpen] = useState(false);
  const [objective, setObjective] = useState(turn.question);
  const [kind, setKind] = useState<MonitorKind>("metric_drift");
  const [op, setOp] = useState(">");
  const [value, setValue] = useState("");
  const [grain, setGrain] = useState<"day" | "week" | "month">("week");
  const id = useId();

  const promote = async (body: Parameters<typeof api.promoteTurn>[1]) => {
    const p = await act.run(() => api.promoteTurn(turn.id, body));
    if (!p) return;
    onRecorded(p);
    if (p.target === "investigate") navigate(to.run(wsId, p.id));
  };
  const submitMonitor = async (e: FormEvent) => {
    e.preventDefault();
    await promote(kind === "metric_threshold"
      ? { target: "monitor", kind, op, value: Number(value), grain }
      : { target: "monitor", kind, grain });
    setMonitorOpen(false);
  };
  const can = (target: string) => allowed(role, target);
  const hint = role === undefined ? null
    : needsRole(role, "analyst", "Saving, reporting and investigating an answer") ?? needsRole(role, "editor", "Proposing a metric, a monitor or a dashboard chart");
  return (
    <section className="stack" aria-label="Promote this answer">
      <div className="btn-row">
        {can("verified_query") && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "verified_query" })}>{PROMOTE_LABELS.verified_query}</button>}
        {can("metric") && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "metric" })}>{PROMOTE_LABELS.metric}</button>}
        {can("monitor") && <button type="button" className="btn btn-sm" disabled={act.busy} aria-expanded={monitorOpen} onClick={() => setMonitorOpen((o) => !o)}>{PROMOTE_LABELS.monitor}</button>}
        {can("dashboard") && <button type="button" className="btn btn-sm" disabled={act.busy} aria-expanded={dashboardOpen} onClick={() => setDashboardOpen((o) => !o)}>{PROMOTE_LABELS.dashboard}</button>}
        {can("report") && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void promote({ target: "report" })}>{PROMOTE_LABELS.report}</button>}
        {can("investigate") && <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} aria-expanded={investigationOpen} onClick={() => setInvestigationOpen((open) => !open)}>{PROMOTE_LABELS.investigate}</button>}
      </div>
      {hint && <p className="small muted" role="note">{hint}</p>}
      {dashboardOpen && (
        <AddAnswerToDashboard turn={turn} wsId={wsId} onRecorded={onRecorded} onClose={() => setDashboardOpen(false)}
          onPublish={() => void promote({ target: "dashboard" })} />
      )}
      {investigationOpen && (
        <form className="form" aria-label="Plan an investigation" onSubmit={(e) => {
          e.preventDefault();
          if (objective.trim().length >= 10 && objective.trim().length <= 2000) void promote({ target: "investigate", objective: objective.trim() });
        }}>
          <Field label="What should the investigation explain?" htmlFor={`${id}-objective`} hint="Describe the outcome, period and comparison you care about. For example: Why did late deliveries rise last month, and which products or regions contributed?">
            <textarea id={`${id}-objective`} rows={3} required minLength={10} maxLength={2000} value={objective} onChange={(e) => setObjective(e.target.value)} />
          </Field>
          <p className="small muted">This starts a new analysis using data you can access in this workspace. Sample rows are examples; the investigation is not limited to those rows and does not automatically reuse this answer’s SQL filters. Include any required period or population in your question; review the proposed plan before execution.</p>
          <p className="small muted">The investigation tests possible explanations. A hypothesis may be supported, rejected or inconclusive; publishing its outputs still requires the applicable approval.</p>
          <div className="form-actions">
            <button type="button" className="btn btn-sm" onClick={() => setInvestigationOpen(false)}>Cancel</button>
            <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || objective.trim().length < 10 || objective.trim().length > 2000}>{act.busy ? "Starting…" : "Start investigation"}</button>
          </div>
        </form>
      )}
      {monitorOpen && (
        <form className="form-row" aria-label="New monitor" onSubmit={submitMonitor}>
          <Field label="Watch for" htmlFor={`${id}-kind`}>
            <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value as MonitorKind)}>
              <option value="metric_drift">Unusual change (drift)</option>
              <option value="metric_threshold">A threshold</option>
            </select>
          </Field>
          {kind === "metric_threshold" && (
            <>
              <Field label="Operator" htmlFor={`${id}-op`}>
                <select id={`${id}-op`} value={op} onChange={(e) => setOp(e.target.value)}>
                  {[">", ">=", "<", "<="].map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              </Field>
              <Field label="Threshold" htmlFor={`${id}-value`}>
                <input id={`${id}-value`} type="number" required value={value} onChange={(e) => setValue(e.target.value)} />
              </Field>
            </>
          )}
          <Field label="Every" htmlFor={`${id}-grain`}>
            <select id={`${id}-grain`} value={grain} onChange={(e) => setGrain(e.target.value as "day" | "week" | "month")}>
              <option value="day">day</option><option value="week">week</option><option value="month">month</option>
            </select>
          </Field>
          <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>Create monitor</button></div>
        </form>
      )}
      <ErrorBox error={act.error} />
      {turn.promotions.length > 0 && (
        <ul className="list compact" aria-label="Promotions">
          {turn.promotions.filter((p) => p.target !== "schedule").map((p, i) => (
            <li key={i} className="list-item small" role="status">
              {promotionText(p)}
              {p.note && <span className="muted"> {p.note}</span>}
              {p.target === "monitor" && <Link to={to.monitoring(wsId)}>Open monitors</Link>}
              {p.target === "report" && <Link to={to.reports(wsId, p.id)}>Open the report</Link>}
              {p.target === "draft" && <Link to={to.outputs(wsId, { type: "dashboard", dashboard: p.id })}>Open the draft</Link>}
              {p.target === "dashboard" && p.status === "published" && (
                p.url && /^https?:\/\//.test(p.url) ? <a href={p.url} target="_blank" rel="noopener noreferrer">Open the dashboard</a>
                  : <Link to={to.studio(wsId, p.id)}>Open the chart</Link>)}
              {p.status === "approval_required" && !canComplete(p) && p.approval_status !== "rejected" && p.approval_status !== "expired"
                && <Link to={to.approvals(wsId)}>Open approvals</Link>}
              {canComplete(p) && p.target === "dashboard" && allowed(role, "dashboard") && (
                <button type="button" className="btn btn-xs btn-primary" disabled={act.busy}
                  onClick={() => void promote({ target: "dashboard", approval_id: p.approval_id })}>Complete: publish the chart</button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// ------------------------------------------------------------------------------------ turn
/** The schedule request recorded on the turn (services/saved_analysis.py), so a reload can still activate it. */
export function scheduleRequest(turn: AskTurn): AskPromotion | null {
  const all = turn.promotions.filter((p) => p.target === "schedule");
  const last = all[all.length - 1];
  if (!last || (last.status === "approval_required" && (last.approval_status === "rejected" || last.approval_status === "expired"
    || last.approval_status === "invalidated" || last.approval_status === "missing"))) return null;
  return last;
}

function ScheduleAnswer({ turn, role }: { turn: AskTurn; role: string | null | undefined }) {
  const recorded = scheduleRequest(turn);
  const [open, setOpen] = useState(!!recorded && recorded.status === "approval_required");
  const [cron, setCron] = useState(recorded?.cron ?? "0 9 * * *");
  const [approval, setApproval] = useState<string | undefined>(recorded?.status === "approval_required" ? recorded.approval_id ?? undefined : undefined);
  const [expires, setExpires] = useState<string | undefined>(recorded?.expires_at);
  const [created, setCreated] = useState(recorded?.status === "created");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const timezone = String(recorded?.timezone ?? "") || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const approved = recorded?.approval_id === approval && recorded?.approval_status === "approved";
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const result = await api.scheduleAsk(turn.id, { name: turn.question.slice(0, 200), cron, timezone, approval_id: approval });
      setApproval(result.approval_id ?? approval); setExpires(result.expires_at ?? expires);
      setCreated(result.status === "created");
    } catch (err) { setError(err); }
    finally { setBusy(false); }
  };
  if (created) return <Notice tone="success">Calculation scheduled. <Link to={to.schedules(turn.workspace_id)}>View schedules</Link></Notice>;
  const hint = role === undefined ? null : needsRole(role, "editor", "Scheduling a calculation");
  if (hint) return <p className="small muted" role="note">{hint}</p>;
  return <div className="stack">
    <button className="btn btn-sm" onClick={() => setOpen(!open)}>Schedule this calculation</button>
    {open && <div className="stack">
      <label>Refresh frequency <select value={cron} disabled={!!approval} onChange={(e) => setCron(e.target.value)}>
        <option value="0 9 * * *">Daily at 9 AM</option><option value="0 9 * * 1">Mondays at 9 AM</option>
      </select></label>
      <p className="small">Time zone: {timezone}. Each refresh uses this saved SQL and your current access.
        Changed metric definitions require a new analysis. Runs stop when the approval expires.</p>
      {approval && <Notice tone={approved ? "success" : "warning"}>{approved ? "Approved: activate it now." : "Approval requested."}{" "}
        <Link to={to.approvals(turn.workspace_id)}>Review approvals</Link>
        {expires && <> · expires {fmtDate(expires)}</>}</Notice>}
      <ErrorBox error={error ? errorMessage(error) : null} />
      <button className="btn" disabled={busy} onClick={() => void submit()}>{approval ? "Activate approved schedule" : "Request schedule approval"}</button>
    </div>}
  </div>;
}

/** "Why these numbers?": each number of the answer traced through GET /ask/turns/{id}/why, loaded when opened. */
function AskWhy({ turn, onSelect }: { turn: AskTurn; onSelect: () => void }) {
  const [open, setOpen] = useState(false);
  const why = useAsync(() => (open ? api.whyAskTurn(turn.id) : Promise.resolve(undefined)), [turn.id, open]);
  const d = why.data;
  return (
    <details className="stack" onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>Why these numbers?</summary>
      <p className="small">Every value in this result comes from query <code>{turn.result?.query_id}</code>.
        {turn.provenance.semantic ? " The calculation uses the approved definitions listed below." : " The SQL is an ad hoc calculation."}</p>
      {turn.provenance.semantic && <KeyValue items={[
        ["Semantic model", `Version ${turn.provenance.semantic.model_version}`],
        ["Metric definitions", turn.provenance.semantic.metrics.map((m) => `${m.name} v${m.version}`).join(", ")],
      ]} />}
      {turn.evidence_status?.reasons.map((reason) => <p className="small" key={reason}>{reason}</p>)}
      {open && !d && !why.error && <Loading label="Tracing the numbers…" />}
      <ErrorBox error={why.error} onRetry={why.reload} />
      {d && (
        <section className="stack" aria-label="How each number is traced">
          <p className="small">Verification: <VerificationBadge state={d.verification_state} />{" "}
            {d.state === "ok" ? "Every link behind these numbers still holds." : <>At least one link is <WhyState state={d.state} />.</>}</p>
          {d.numbers.length === 0 && <p className="small muted">No numbers in this answer.</p>}
          {d.numbers.slice(0, 6).map((n, k) => <NumberTrail key={`${n.text}-${k}`} n={n} />)}
          {d.numbers.length > 6 && <p className="small muted">Showing 6 of {d.numbers.length} numbers.</p>}
        </section>
      )}
      <button className="btn btn-xs" type="button" onClick={onSelect}>Inspect SQL and source evidence</button>
    </details>
  );
}

/** Who or what answered, and what it cost (S11 UX invariant: shown on every answer). */
export function AnsweredBy({ turn }: { turn: AskTurn }) {
  return (
    <p className="answered-by small" aria-label="Answered by">
      <span><TrustBadge level={trustOf(turn)} /> <strong>Answered by</strong> {answeredByLabel(turn)}</span>
      <span className="muted" title={turn.cost?.tokens_saved ? `${turn.cost.tokens_saved} tokens saved by rules and cache` : undefined}>
        {costLine(turn.cost)}
      </span>
    </p>
  );
}

/** What a reader must know beside the numbers (an incomplete period, an approved metric not used), from the server. */
function AnswerNotes({ turn }: { turn: AskTurn }) {
  const notes = (turn.provenance?.notes ?? []).filter((n) => typeof n === "string" && n.trim());
  if (!notes.length) return null;
  return (
    <ul className="stack small answer-notes" aria-label="Notes on this answer">
      {notes.map((n) => <li key={n}><Notice tone="warning">{n}</Notice></li>)}
    </ul>
  );
}

/** A message a branch shows from the conversation it continues: read here, acted on in the original. */
function InheritedTurn({ turn }: { turn: AskTurn }) {
  return (
    <article className="ask-turn ask-turn-inherited" aria-label={`Question ${turn.seq}, from the original conversation`}>
      <header className="ask-question"><p><strong>{turn.question}</strong> <span className="tag">from the original</span></p></header>
      {turn.explanation && <p>{turn.explanation}</p>}
      {turn.status === "answered" && turn.result ? <ResultView res={turn.result} hint={turn.chart} caption={turn.question} />
        : <p className="small muted">Not answered ({turn.refusal?.kind ?? turn.status}).</p>}
      <AnsweredBy turn={turn} />
    </article>
  );
}

function TurnView({ turn, role, selected, onSelect, busy, onParameters, onRephrase, onExplain, onRetry, onRecorded, onAsk, onRerun, onRerunStep,
  onResynthesize, onBranch, onEdit }: {
  turn: AskTurn; role: string | null | undefined; selected: boolean; onSelect: () => void; busy: boolean; onParameters: (p: Dict) => void; onRephrase: () => void;
  onExplain: (sql: string) => void; onRetry: () => void; onRecorded: (p: AskPromotion) => void; onAsk: (q: string) => void;
  onRerun: (sql?: string) => void; onRerunStep: (n: number, sql?: string) => void; onResynthesize: () => void;
  onBranch: () => void; onEdit: (question: string) => void;
}) {
  const { wsId = "" } = useParams();
  const [editing, setEditing] = useState(false);
  const [editedSql, setEditedSql] = useState(turn.sql ?? "");
  const [rewording, setRewording] = useState(false);
  const [reworded, setReworded] = useState(turn.question);
  const editId = useId();
  return (
    <article className={`ask-turn ${selected ? "ask-turn-selected" : ""}`} aria-label={`Question ${turn.seq}`}>
      <header className="ask-question">
        <p><strong>{turn.question}</strong>{turn.analysis && <span className="tag tag-info analyst-tag">step by step</span>}</p>
        <span className="btn-row">
          <button type="button" className="btn btn-xs btn-ghost" disabled={busy} onClick={onBranch}
            title="Start a new conversation that continues after this answer; this one stays as it is">Branch from here</button>
          <button type="button" className="btn btn-xs btn-ghost" disabled={busy} aria-expanded={rewording} onClick={() => setRewording(!rewording)}
            title="Ask this question differently in a new branch; the original answer stays">Edit question</button>
          <button type="button" className="btn btn-xs btn-ghost" aria-pressed={selected} onClick={onSelect}>Inspect</button>
        </span>
      </header>
      {rewording && (
        <form className="stack" onSubmit={(e) => { e.preventDefault(); if (reworded.trim()) { setRewording(false); onEdit(reworded.trim()); } }}>
          <label htmlFor={editId}>Edited question</label>
          <textarea id={editId} rows={2} value={reworded} onChange={(e) => setReworded(e.target.value)} />
          <p className="small muted">It is asked in a new branch that keeps everything before this question; this conversation is not changed.</p>
          <div className="btn-row">
            <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !reworded.trim()}>Ask in a new branch</button>
            <button type="button" className="btn btn-sm" onClick={() => setRewording(false)}>Cancel</button>
          </div>
        </form>
      )}
      {turn.status === "answered" && turn.analysis ? (
        <>
          <AnalystAnswer turn={turn} busy={busy} onRerunStep={onRerunStep} onResynthesize={onResynthesize} onAsk={onAsk} />
          <AnsweredBy turn={turn} />
        </>
      ) : turn.status === "answered" && turn.result ? (
        <div className="stack">
          <Pills label="Provenance" pills={[...provenancePills(turn), stalenessPill(turn.staleness)]} />
          <AnsweredBy turn={turn} />
          {turn.explanation && <p>{turn.explanation}</p>}
          <AnswerNotes turn={turn} />
          <AskWhy turn={turn} onSelect={onSelect} />
          {turn.evidence_status?.state === "changed" && <Notice tone="warning">The evidence has changed since this answer.
            Review the recorded definitions and ask again before using these numbers.</Notice>}
          <ResultView res={turn.result} hint={turn.chart} caption={turn.question} />
          <div className="btn-row">
            <button className="btn btn-sm" disabled={busy} onClick={() => onRerun()}>Refresh saved calculation</button>
            <button className="btn btn-sm" disabled={busy} onClick={() => setEditing(!editing)}>Edit SQL and rerun</button>
          </div>
          {editing && <form className="stack" onSubmit={(e) => { e.preventDefault(); onRerun(editedSql); }}>
            <label>Edited SQL<textarea className="mono" rows={6} value={editedSql} onChange={(e) => setEditedSql(e.target.value)} /></label>
            <p className="small muted">The result will be saved as a new ad hoc analysis. The original answer stays in this conversation.</p>
            <button className="btn btn-primary" disabled={busy || !editedSql.trim()}>Run edited SQL</button>
          </form>}
          {turn.sql && <CodeBlock code={turn.sql} label={`SQL${turn.model ? ` · ${turn.model}` : turn.answered_by === "registry" ? " · verified query" : turn.answered_by === "rules" ? " · built from the catalog" : ""}`} />}
          <WhatIfPanel turn={turn} />
          <Suggestions turn={turn} busy={busy} onAsk={onAsk} />
          {canPromote(turn) && <PromoteBar turn={turn} role={role} onRecorded={onRecorded} />}
          {canPromote(turn) && <ScheduleAnswer turn={turn} role={role} />}
        </div>
      ) : (
        <>
          <RefusalState turn={turn} ws={wsId} busy={busy} onParameters={onParameters} onRephrase={onRephrase} onExplain={onExplain} onRetry={onRetry} />
          <AnsweredBy turn={turn} />
          {typeof turn.refusal?.details?.assumption === "string" && (
            <button type="button" className="btn btn-sm" disabled={busy}
              onClick={() => onAsk(`${turn.question}\n\nProceed with this assumption: ${turn.refusal!.details!.assumption as string}`)}>
              Go ahead assuming {turn.refusal.details.assumption as string}
            </button>
          )}
          <Suggestions turn={turn} busy={busy} onAsk={onAsk} />
        </>
      )}
    </article>
  );
}

// ------------------------------------------------------------------------------------ inspector
type InspectorTab = "result" | "sql" | "evidence" | "decision";

function Inspector({ turn }: { turn: AskTurn }) {
  const [tab, setTab] = useState<InspectorTab>("result");
  const data = useAsync<AskInspector>(() => api.askInspector(turn.id), [turn.id, turn.promotions.length]);
  const d = data.data;
  const p = turn.provenance ?? {};
  return (
    <aside className="card ask-inspector" aria-label="Answer inspector">
      <div className="card-body stack">
        <Tabs<InspectorTab> value={tab} onChange={setTab}
          tabs={[{ id: "result", label: "Result" }, { id: "sql", label: "SQL" }, { id: "evidence", label: "Evidence" }, { id: "decision", label: "Decision" }]} />
        {data.loading && !d && <Loading label="Loading the answer's record…" />}
        <ErrorBox error={data.error} onRetry={data.reload} />
        {tab === "result" && (turn.result
          ? <KeyValue items={[
            ["Rows", `${turn.result.row_count}${turn.result.truncated ? " (truncated)" : ""}`],
            ["Columns", turn.result.columns.join(", ")],
            ["Query", <code key="q">{turn.result.query_id}</code>],
            ["Answered in", fmtMs(turn.latency_ms)],
            ["Answered by", turn.answered_by === "registry" ? "verified query (no model)" : turn.answered_by === "rules" ? "rule built from the catalog (no model)"
              : turn.answered_by === "semantic" ? "approved metric calculation (no model)"
              : turn.answered_by === "model" ? "generated SQL" : "—"],
          ]} />
          : <EmptyState title="No result">This question was not answered; the refusal says why.</EmptyState>)}
        {tab === "sql" && (
          <div className="stack">
            {turn.sql ? <CodeBlock code={turn.sql} label="SQL as written" /> : <EmptyState title="No SQL ran" />}
            {d?.query?.executed_sql && d.query.executed_sql !== turn.sql && <CodeBlock code={d.query.executed_sql} label="SQL as the gateway ran it (row limit applied)" />}
            {d?.query && <p className="small muted">Gateway audit: {d.query.status}, {d.query.row_count} rows, {fmtMs(d.query.duration_ms)}, fingerprint <code>{String(d.query.fingerprint ?? "").slice(0, 12)}</code></p>}
            {turn.attempts.length > 0 && (
              <Notice tone="warning">
                <strong>{turn.attempts.length} repair attempt{turn.attempts.length > 1 ? "s" : ""}</strong> — the gateway rejected earlier SQL:
                <ol className="small">{turn.attempts.map((a, i) => <li key={i}><code className="clamp-2">{a.sql}</code><div className="warn-text">{a.error}</div></li>)}</ol>
              </Notice>
            )}
          </div>
        )}
        {tab === "evidence" && (
          <div className="stack">
            {(p.assets ?? []).length > 0 ? (
              <DataTable caption="Tables the answer read" columns={["Table", "Source", "Mode", "Data as of", "Rows"]}
                rows={(p.assets ?? []).map((a) => [a.asset, a.source_name ?? "unknown", a.execution_mode ?? "unknown",
                  a.freshness_at ? fmtDate(a.freshness_at) : "unknown", a.row_count ?? "unknown"])} />
            ) : <EmptyState title="No tables recorded" />}
            <KeyValue items={[
              ["Freshness", stalenessPill(turn.staleness).label],
              ["Verified query", p.verified_query ? `${p.verified_query.name}${p.verified_query.score ? ` (match ${p.verified_query.score})` : ""}` : "—"],
              ["Result hash", p.result_hash ? <code key="h">{String(p.result_hash).slice(0, 16)}</code> : "—"],
            ]} />
            <div>
              <h3 className="h-sm">Knowledge used</h3>
              {d?.receipts?.length ? (
                <ul className="list compact" aria-label="Context receipts">{d.receipts.map((r, i) => {
                  const v = receiptView(r);
                  return (
                    <li key={i} className="list-item small receipt">
                      <span>
                        {v.title}
                        {v.where && <span className="muted"> · <code>{v.where}</code></span>}
                        {v.section ? <span className="tag">{v.section}</span> : null}
                        {r.version ? <span className="tag">v{String(r.version)}</span> : null}
                        {v.reviewed && <span className="tag tag-success" title="Approved in the knowledge review queue">reviewed</span>}
                        {v.curated && <span className="tag tag-success" title="Written or edited by a person">curated</span>}
                        {v.untrusted && <span className="tag tag-warning" title="From another provider: data, not instructions">untrusted</span>}
                      </span>
                      {v.documentId && (
                        <Link className="small" to={to.knowledge(turn.workspace_id, "documents", { doc: v.documentId })}
                          aria-label={`Open ${v.title} in the knowledge studio`}>Open</Link>
                      )}
                    </li>
                  );
                })}</ul>
              ) : <p className="small muted">No context receipts: no model prompt was compiled for this answer.</p>}
            </div>
            {turn.status === "answered" && (
              <div>
                <h3 className="h-sm">Citations</h3>
                <CitationsPanel load={() => api.askTurnCitations(turn.id)} deps={[turn.id]} workspaceId={turn.workspace_id} />
              </div>
            )}
          </div>
        )}
        {tab === "decision" && (
          <div className="stack">
            <KeyValue items={[["Ladder rung", turn.answered_by ?? "—"], ["Route", turn.route ?? "—"]]} />
            {d?.decisions?.length ? (
              <ul className="list compact" aria-label="Decisions">{d.decisions.map((row) => (
                <li key={row.id} className="list-item small">
                  <span>{decisionLine(row)}{topProbabilities(row).length > 0 && (
                    <span className="muted"> · {topProbabilities(row).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(", ")}</span>)}</span>
                  <span className="tag">{row.authority}</span>
                </li>
              ))}</ul>
            ) : <p className="small muted">{turn.decisions.length ? turn.decisions.map((x) => `${x.purpose}: ${String(x.value)} (${x.backend})`).join("; ") : "No decisions recorded."}</p>}
            {d?.model_calls?.length ? (
              <DataTable caption="Model calls" columns={["Purpose", "Status", "Rung", "Model", "Tokens", "Cost"]}
                rows={d.model_calls.map((c) => [c.purpose, c.status, c.answered_by ?? "—", c.model, c.input_tokens + c.output_tokens, fmtUsd(c.cost_usd)])} />
            ) : <p className="small muted">No model calls.</p>}
            {d && <TechnicalDetails value={{ decisions: d.decisions, model_calls: d.model_calls }} />}
          </div>
        )}
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------------------------ page
export function AskPage() {
  const expert = useExpertView();
  const { wsId = "" } = useParams();
  const assets = useAsync(() => api.catalog(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  // undefined while unknown: actions stay offered and the server decides; null = not a member
  const role = ws.data ? ws.data.role ?? null : undefined;
  const [params, setParams] = useSearchParams();
  const threadId = params.get("thread");
  const [search, setSearch] = useState("");
  const threads = useAsync(() => api.askThreads(wsId, search.trim() || undefined), [wsId, search]);
  const [thread, setThread] = useState<AskThreadDetail | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [live, setLive] = useState<AskStage[]>([]);
  const [pending, setPending] = useState<string | null>(null);
  const [askErr, setAskErr] = useState<unknown>(null);
  const [asking, setAsking] = useState(false);
  const [convRefresh, setConvRefresh] = useState(0);
  const conversation = useAsync<ConversationDetail | undefined>(
    () => (thread?.id ? api.conversation(thread.id) : Promise.resolve(undefined)), [thread?.id, convRefresh]);
  const inherited = useMemo(() => (conversation.data?.id === thread?.id ? conversation.data?.messages ?? [] : [])
    .filter((m) => m.inherited).map(messageToTurn), [conversation.data, thread?.id]);
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const consoleRef = useRef<HTMLTextAreaElement>(null);

  const [sql, setSql] = useState("");
  const [maxRows, setMaxRows] = useState(500);
  const [qres, setQres] = useState<QueryResult | null>(null);
  const [qErr, setQErr] = useState<unknown>(null);
  const [running, setRunning] = useState(false);
  const [explain, setExplain] = useState<SqlExplanation | null>(null);
  const [explaining, setExplaining] = useState(false);
  const sqlExamples = exampleSql(assets.data ?? []);
  const starters = starterQuestions(assets.data ?? []);

  useEffect(() => {
    let stale = false;
    if (!threadId) {
      setThread(null);
      return;
    }
    if (thread?.id === threadId) return;
    api.askThread(threadId).then((t) => {
      if (stale) return;
      setThread(t);
      setSelected(t.turns.length ? t.turns[t.turns.length - 1].id : null);
    }).catch((err) => { if (!stale) setAskErr(err); });
    return () => { stale = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  const selectedTurn = useMemo(() => thread?.turns.find((t) => t.id === selected) ?? null, [thread, selected]);
  const groups = useMemo(() => groupThreads(threads.data ?? []), [threads.data]);

  const [mode, setMode] = useState<AskMode>(() => {
    try {
      return window.localStorage.getItem("analystos.ask.mode") === "analyst" ? "analyst" : "quick";
    } catch {
      return "quick";
    }
  });
  const chooseMode = (m: AskMode) => {
    setMode(m);
    try { window.localStorage.setItem("analystos.ask.mode", m); } catch { /* remembered for this tab only */ }
  };

  const replaceTurn = (turn: AskTurn) =>
    setThread((t) => (t ? { ...t, turns: t.turns.map((x) => (x.id === turn.id ? turn : x)) } : t));
  const stepAction = async (fn: () => Promise<AskTurn>) => {
    setAsking(true);
    setAskErr(null);
    try { replaceTurn(await fn()); } catch (err) { setAskErr(err); } finally { setAsking(false); }
  };

  const ask = async (text: string, parameters?: Dict, target?: AskThreadDetail) => {
    const q = text.trim();
    if (!q) return;
    setAsking(true);
    setAskErr(null);
    setLive([]);
    setPending(q);
    try {
      let current = target ?? thread;
      if (!current) {
        current = await api.createAskThread(wsId);
        setThread(current);
        setParams((prev) => { const n = new URLSearchParams(prev); n.set("thread", current!.id); return n; }, { replace: true });
      }
      const turn = await streamAskTurn(current.id, q, parameters, { onStage: (s) => setLive((xs) => [...xs, s]) }, undefined, mode);
      setThread((t) => (t ? { ...t, title: t.turns.length ? t.title : q, turns: [...t.turns, turn] } : t));
      setSelected(turn.id);
      setQuestion("");
      setConvRefresh((n) => n + 1);  // the message moved the conversation state on
      void threads.reload();
    } catch (err) {
      setAskErr(err);
    } finally {
      setAsking(false);
      setPending(null);
    }
  };

  /** A branch continues after `fromId` (null: before the first question) and opens; `thenAsk` asks there at once. */
  const branchFrom = async (fromId: string | null, thenAsk?: string) => {
    if (!thread) return;
    setAsking(true);
    setAskErr(null);
    let detail: AskThreadDetail;
    try {
      const b = await api.branchConversation(thread.id, fromId);
      detail = await api.askThread(b.id);
      setThread(detail);
      setSelected(null);
      setParams((prev) => { const n = new URLSearchParams(prev); n.set("thread", b.id); return n; });
      void threads.reload();
    } catch (err) {
      setAskErr(err);
      setAsking(false);
      return;
    }
    setAsking(false);
    if (thenAsk) await ask(thenAsk, undefined, detail);
  };
  const visibleTurns = [...inherited, ...(thread?.turns ?? [])];

  const recordPromotion = (turnId: string, p: AskPromotion) =>
    setThread((t) => (t ? { ...t, turns: t.turns.map((x) => (x.id === turnId ? { ...x, promotions: mergePromotion(x.promotions, p) } : x)) } : t));

  const rerun = async (turnId: string, statement?: string) => {
    setAsking(true);
    setAskErr(null);
    try {
      const result = await api.rerunAsk(turnId, statement);
      setThread((t) => t ? { ...t, turns: [...t.turns, result] } : t);
      setSelected(result.id);
    } catch (err) { setAskErr(err); }
    finally { setAsking(false); }
  };

  const openThread = (id: string | null) => {
    setParams((prev) => { const n = new URLSearchParams(prev); if (id) n.set("thread", id); else n.delete("thread"); return n; });
    if (!id) { setThread(null); setSelected(null); }
  };

  const explainSql = (text: string) => {
    setSql(text);
    consoleRef.current?.scrollIntoView?.({ block: "center" });
    consoleRef.current?.focus();
  };

  const runExplain = async () => {
    setExplaining(true);
    setQErr(null);
    setExplain(null);
    try {
      setExplain(await api.explainQuery(wsId, sql, maxRows));
    } catch (err) {
      setQErr(err);
    } finally {
      setExplaining(false);
    }
  };

  const submitSql = async (e: FormEvent) => {
    e.preventDefault();
    setExplain(null);
    setRunning(true);
    setQErr(null);
    setQres(null);
    try {
      setQres(await api.query(wsId, sql, maxRows));
    } catch (err) {
      setQErr(err);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="page">
      <PageHeader title="Ask a question" subtitle="Ask in plain words. Answers come from your connected data, and every number is checked before you see it." />
      <div className="ask-layout">
        <nav className="card ask-threads" aria-label="Threads" data-tour="ask-threads">
          <div className="card-body stack">
            <button type="button" className="btn btn-sm" onClick={() => openThread(null)}>New thread</button>
            <Field label="Search threads" htmlFor="ask-search">
              <input id="ask-search" type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Words from a question" />
            </Field>
            <ErrorBox error={threads.error} onRetry={threads.reload} />
            {groups.length === 0 && !threads.loading && <p className="small muted">{search ? "No thread matches." : "No threads yet."}</p>}
            {groups.map((g) => (
              <div key={g.label}>
                <h2 className="h-sm muted">{g.label}</h2>
                <ul className="list selectable">
                  {g.items.map((t) => (
                    <li key={t.id}>
                      <button type="button" className={`list-button ${t.id === threadId ? "active" : ""}`} aria-current={t.id === threadId ? "true" : undefined}
                        onClick={() => openThread(t.id)}>
                        <span className="clamp-2">{t.title}{t.parent_conversation_id && <span className="tag">branch</span>}</span>
                        <span className="small muted">{t.turn_count ?? 0} question{t.turn_count === 1 ? "" : "s"}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </nav>

        <div className="stack ask-main">
          <Card title={thread && thread.turns.length ? thread.title : "Ask a question"}
            actions={thread && thread.turns.length ? <Link className="btn btn-xs btn-ghost" to={to.thread(wsId, "ask_thread", thread.id)}>Open as a Data Thread</Link> : undefined}>
            <div className="stack">
              {thread && <ConversationBar conversation={conversation.data?.id === thread.id ? conversation.data : undefined}
                threads={threads.data ?? []} refreshKey={convRefresh} busy={asking} onOpen={(id) => openThread(id)}
                onChanged={() => setConvRefresh((n) => n + 1)} />}
              {inherited.map((t) => <InheritedTurn key={t.id} turn={t} />)}
              {thread?.turns.map((t) => (
                <TurnView key={t.id} turn={t} role={role} selected={t.id === selected} onSelect={() => setSelected(t.id)} busy={asking}
                  onParameters={(p) => void ask(t.question, p)} onRephrase={() => { setQuestion(t.question); questionRef.current?.focus(); }}
                  onExplain={explainSql} onRetry={() => void ask(t.question, t.parameters)} onRecorded={(p) => recordPromotion(t.id, p)} onAsk={(q) => void ask(q)}
                  onRerun={(statement) => void rerun(t.id, statement)}
                  onRerunStep={(n, statement) => void stepAction(() => api.rerunAskStep(t.id, n, statement))}
                  onResynthesize={() => void stepAction(() => api.resynthesizeAsk(t.id))}
                  onBranch={() => void branchFrom(t.id)}
                  onEdit={(q) => void branchFrom(branchPointBefore(visibleTurns, t.id), q)} />
              ))}
              {pending && (
                <article className="ask-turn" aria-label="Question in progress" aria-busy="true">
                  <p><strong>{pending}</strong></p>
                  {live.length ? <Stages stages={live} live /> : <Loading label="Starting…" />}
                </article>
              )}
              {!thread?.turns.length && !pending && starters.length > 0 && (
                <div className="starter-questions">
                  <p className="small muted">Try asking — built from your tables, no model involved:</p>
                  <div className="chip-row" aria-label="Suggested questions">
                    {starters.map((q) => (
                      <button key={q} type="button" className="btn btn-sm" disabled={asking} onClick={() => void ask(q)}>{q}</button>
                    ))}
                  </div>
                </div>
              )}
              <form className="form" data-tour="ask-box" onSubmit={(e) => { e.preventDefault(); void ask(question); }}>
                <Field label="Question" htmlFor="ask-q">
                  <textarea id="ask-q" ref={questionRef} rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} required
                    placeholder={starters[1] ?? starters[0] ?? "What would you like to know about your data?"}
                    onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit(); }} />
                </Field>
                <div className="form-actions ask-actions">
                  <div className="seg" role="radiogroup" aria-label="How to answer">
                    {(["quick", "analyst"] as const).map((m) => (
                      <button key={m} type="button" role="radio" aria-checked={mode === m} className={`seg-btn ${mode === m ? "active" : ""}`}
                        title={m === "quick" ? "One query, the fastest answer" : "Up to four steps: totals, breakdowns, trends and drivers, then a cited answer"}
                        onClick={() => chooseMode(m)}>{m === "quick" ? "Quick answer" : "Step by step"}</button>
                    ))}
                  </div>
                  <button type="submit" className="btn btn-primary" disabled={asking || !question.trim()}>{asking ? "Thinking…" : "Ask"}</button>
                </div>
              </form>
              <GatewayError error={askErr} />
            </div>
          </Card>

          <Card title="Check a number">
            <NumberCheckPanel wsId={wsId} onOpenThread={(id) => { void threads.reload(); openThread(id); }} />
          </Card>

          {expert && <Card title="SQL console">
            {assets.error && <ErrorBox error={assets.error} onRetry={assets.reload} />}
            {sqlExamples.length > 0 && <div className="chip-row" aria-label="Example queries">
              {sqlExamples.map((example) => <button key={example.label} type="button" className="btn btn-sm"
                onClick={() => { setSql(example.sql); setExplain(null); setQres(null); setQErr(null); }}>{example.label}</button>)}
            </div>}
            <form className="form" onSubmit={submitSql}>
              <Field label="SQL (read-only)" htmlFor="sql" hint="Paste SQL and Explain it first: the validator and the source's plan, nothing executed. Run uses the same gateway, scope and audit as the agents.">
                <textarea id="sql" ref={consoleRef} className="mono" rows={6} value={sql} onChange={(e) => setSql(e.target.value)} spellCheck={false} required
                  placeholder={sqlExamples[1]?.sql ?? sqlExamples[0]?.sql ?? "SELECT … FROM schema.table"}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) (e.currentTarget.form as HTMLFormElement | null)?.requestSubmit(); }} />
              </Field>
              <div className="form-actions">
                <label className="inline-field small">Max rows <input type="number" min={1} max={50000} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value))} /></label>
                <button type="button" className="btn" onClick={() => void runExplain()} disabled={explaining || running || !sql.trim()}
                  title="Explain the query and check it against the gateway without running it">{explaining ? "Explaining…" : "Explain"}</button>
                <button type="submit" className="btn btn-primary" disabled={running || !sql.trim()}>{running ? "Running…" : "Run (Ctrl+Enter)"}</button>
              </div>
            </form>
            <GatewayError error={qErr} />
            {explain && <ExplainView ex={explain} />}
            {qres && <ResultView res={qres} />}
          </Card>}
        </div>

        {selectedTurn ? <Inspector turn={selectedTurn} /> : (
          <aside className="card ask-inspector" aria-label="Answer inspector">
            <div className="card-body"><EmptyState title="No answer selected">Ask a question, or pick one, to see its result, SQL, evidence and the decisions behind it.</EmptyState></div>
          </aside>
        )}
      </div>
    </div>
  );
}
