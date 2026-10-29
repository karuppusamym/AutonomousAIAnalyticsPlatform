import { useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Branch, type BranchCompare, type ContainerType, type MergeResult, type PinResult, type Step, type StepResult,
  type StepRevision, type StepWithResult } from "../api";
import { fmtDate, fmtValue, optionLabel, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { ChartView } from "./Chart";
import { Drawer } from "./Drawer";
import { FilteredPreview } from "./FilteredPreview";
import { isStaleEdit, StaleEditNotice } from "./StaleEdit";
import { ReverifyButton, VerificationBadge, WhyState } from "./WhyNumber";
import { CodeBlock, DataTable, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag, TechnicalDetails } from "./ui";

/** Why a step has no verdict, in words (UI review P3-6: "no verdict recorded (recorded)" said nothing). */
export function noVerdictText(kind: string, status: string): string {
  if (status === "recorded") {
    if (kind === "plan") return "The investigation's plan, as it ran; plans are not verified.";
    if (kind === "method") return "A test the investigation ran; its outcome is under Statistical result, and a finding built on it carries the verdict.";
    return "Recorded from the investigation; not verified on its own.";
  }
  if (status === "running" || status === "pending") return "Not verified yet: the step is still running.";
  return `No verdict yet (${status.replace(/_/g, " ")}).`;
}

const KIND_WORDS: Record<string, string> = {
  plan: "plan", query: "query", method: "method", recipe: "recipe", train: "training", chart: "chart", claim: "claim",
};
const REASON_WORDS: Record<string, string> = {
  created: "created", edited: "edited", rerun: "re-run on today's data", upstream_changed: "re-ran because a step it reads changed",
  forked: "copied into this branch", ingested: "recorded from the investigation",
};
const CHECK_WORDS: Record<string, string> = {
  empty_result: "not empty", magnitude: "plausible size", truncation: "complete", grouping: "grouping", fanout: "no join fan-out",
  dq_gate: "data-quality gates", numbers: "numbers bound to results", chart_fields: "chart fields exist",
};

export function parseContainer(v: string | null): { type: ContainerType; id: string } | null {
  if (!v) return null;
  const [type, ...rest] = v.split(":");
  const id = rest.join(":");
  if (!id || !["run", "ask_thread", "notebook"].includes(type)) return null;
  return { type: type as ContainerType, id };
}

/** A step's result: its table (with governed filters for query steps), chart, text or statistic. */
export function StepResultView({ wsId, step, result }: { wsId: string; step: Step; result: StepResult | undefined }) {
  if (!result) return null;
  if (typeof result.text === "string") return <p className="small step-text">{result.text}</p>;
  const cols = result.columns ?? [];
  const rows = result.rows ?? [];
  const chart = step.chart_spec?.type ?? (step.kind === "chart" ? (step.spec.chart as { type?: string } | undefined)?.type : undefined);
  const sql = typeof step.spec.sql === "string" ? step.spec.sql : null;
  return (
    <div className="stack">
      {chart && cols.length > 0 && <ChartView type={chart} preview={{ columns: cols, rows }} title={step.title} height={220} showTable={false} />}
      {cols.length > 0 && (sql
        ? <FilteredPreview wsId={wsId} sql={sql} columns={cols} rows={rows} caption={`Result of ${step.title}`} />
        : <DataTable columns={cols} rows={rows} caption={`Result of ${step.title}`} maxRows={20} />)}
      {result.truncated && <p className="small warn-text">The result was truncated; the self-check records how many rows are missing.</p>}
      {result.stat && <TechnicalDetails value={result.stat} label="Statistical result" />}
    </div>
  );
}

/**
 * "Why this number?" for a step (spec v4 §7, P7-08): the number traced through its fact binding,
 * the step version, the query receipt, the data version, and the definition and verdict, each with
 * its current state. Built from the step's own records; a void verdict says why.
 */
export function StepWhyDrawer({ step, result, onClose }: { step: Step; result: StepResult | undefined; onClose: () => void }) {
  const first = result?.rows?.find((r) => r.some((c) => typeof c === "number"));
  const numIdx = first ? first.findIndex((c) => typeof c === "number") : -1;
  const number = first && numIdx >= 0 ? String(first[numIdx]) : null;
  const receipts = step.receipts.filter((r) => r.kind === "query" || r.sql || r.query_id);
  const vr = step.verification_record;
  const isVoid = vr?.badge === "void";
  const dataChanged = isVoid && vr?.void?.kind === "data";
  const links: { link: string; title: string; state: string; body: React.ReactNode }[] = [
    { link: "fact", title: "The fact behind it", state: number ? "ok" : "unknown",
      body: number ? <>{number} is <code>{result?.columns?.[numIdx]}</code> in the row for <strong>{fmtValue(first?.[0])}</strong>.</> : "No number in this result." },
    { link: "step", title: "The step that produced it", state: step.status === "ok" ? "ok" : step.status === "flagged" || step.status === "failed" ? "failed" : "unknown",
      body: <>{step.title}, version {step.version} of {step.current_version} ({REASON_WORDS[step.reason] ?? step.reason}).</> },
    { link: "query_receipt", title: "The query that read the data", state: receipts.length ? "ok" : step.kind === "claim" || step.kind === "chart" ? "not_applicable" : "unknown",
      body: receipts.length ? receipts.map((r, k) => (
        <div key={String(r.query_id ?? k)} className="small">{r.row_count !== undefined ? `${String(r.row_count)} rows` : "query"}
          {r.result_hash ? <> · result <code>{shortHash(String(r.result_hash), 10)}</code></> : null}
          {r.sql ? <details className="evidence-item"><summary>Show the SQL</summary><CodeBlock code={String(r.sql)} /></details> : null}</div>
      )) : "Reads upstream steps, not the data directly." },
    { link: "data_version", title: "The data it was read from", state: dataChanged ? "changed" : receipts.some((r) => r.snapshot) ? "ok" : "unknown",
      body: receipts.map((r) => r.snapshot).filter(Boolean).join(", ") || (Object.keys(step.inputs).length ? `upstream: ${Object.keys(step.inputs).join(", ")}` : "not recorded") },
    { link: "verdict", title: "The definition and the verification", state: !vr ? "unknown" : isVoid ? "void" : vr.badge === "verified" ? "ok" : "failed",
      body: vr ? <VerificationBadge state={vr} /> : "No verdict recorded for this step." },
  ];
  return (
    <Drawer title={number ? `Why ${number}?` : "Why this number?"} onClose={onClose}>
      <p className="small">{step.title}</p>
      <ol className="trust-list" aria-label="How this number is traced">
        {links.map((l) => (
          <li key={l.link} className="trust-item">
            <WhyState state={l.state} />
            <div><strong>{l.title}</strong><div className="small">{l.body}</div></div>
          </li>
        ))}
      </ol>
      <TechnicalDetails value={{ step_id: step.id, version: step.version, spec_hash: step.spec_hash, snapshot: step.result_snapshot, inputs: step.inputs }} />
    </Drawer>
  );
}

export function StepVersions({ wsId, stepId }: { wsId: string; stepId: string }) {
  const versions = useAsync(() => api.stepVersions(wsId, stepId), [wsId, stepId]);
  const [open, setOpen] = useState<number | null>(null);
  const old = useAsync(() => (open ? api.step(wsId, stepId, open) : Promise.resolve(undefined)), [wsId, stepId, open]);
  if (versions.error) return <ErrorBox error={versions.error} onRetry={versions.reload} />;
  if (!versions.data) return <Loading />;
  return (
    <ol className="version-list" aria-label="Versions of this step">
      {versions.data.versions.map((v) => (
        <li key={v.version} aria-label={`Version ${v.version}`}>
          <div className="chip-row small">
            <strong>v{v.version}</strong>{v.version === versions.data!.current_version && <Tag tone="info">current</Tag>}
            <StatusBadge status={v.status} /> <span className="muted">{REASON_WORDS[v.reason] ?? v.reason} · {fmtDate(v.created_at)}</span>
          </div>
          <VerificationBadge state={v.verification_record} />
          {typeof v.spec.sql === "string" && <CodeBlock code={v.spec.sql} />}
          {v.version !== versions.data!.current_version && (
            <button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === v.version}
              onClick={() => setOpen((o) => (o === v.version ? null : v.version))}>{open === v.version ? "Hide its result" : `Show the result of v${v.version}`}</button>
          )}
          {open === v.version && old.data && old.data.version === v.version && (
            <DataTable columns={old.data.result.columns ?? []} rows={old.data.result.rows ?? []} caption={`Result of version ${v.version}`} />
          )}
        </li>
      ))}
    </ol>
  );
}

function EditStepForm({ wsId, step, onDone, onCancel }: { wsId: string; step: StepWithResult | Step; onDone: (r: StepRevision) => void; onCancel: () => void }) {
  const id = useId();
  const isSql = typeof step.spec.sql === "string";
  const [title, setTitle] = useState(step.title);
  const [text, setText] = useState(isSql ? String(step.spec.sql) : JSON.stringify(step.spec, null, 2));
  const [bad, setBad] = useState<string | null>(null);
  const act = useAction();
  const [mine, setMine] = useState<unknown>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    let spec: Record<string, unknown>;
    try {
      spec = isSql ? { ...step.spec, sql: text } : JSON.parse(text) as Record<string, unknown>;
    } catch {
      setBad("The spec is not valid JSON.");
      return;
    }
    setBad(null);
    setMine({ title, spec, version: step.current_version });
    const r = await act.run(() => api.editStep(wsId, step.id, step.current_version, { spec, title: title !== step.title ? title : null }));
    if (r) onDone(r);
  };
  return (
    <form className="form step-edit" onSubmit={submit} aria-label={`Edit ${step.title}`}>
      <Field label="Title" htmlFor={`${id}-t`}><input id={`${id}-t`} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      <Field label={isSql ? "SQL" : "Spec (JSON)"} htmlFor={`${id}-s`} hint="Saving makes a new version and re-runs it and every step that reads it.">
        <textarea id={`${id}-s`} rows={isSql ? 6 : 8} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      {bad && <p className="warn-text small" role="alert">{bad}</p>}
      {isStaleEdit(act.failure)
        ? <StaleEditNotice what="This step" mine={mine} loadCurrent={async () => { const cur = await api.step(wsId, step.id); return { title: cur.title, spec: cur.spec, version: cur.current_version }; }}
          onReload={() => { act.clear(); onCancel(); }} />
        : <ErrorBox error={act.error} />}
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Re-running…" : "Save and re-run"}</button>
      </div>
    </form>
  );
}

function PinForm({ wsId, step, onClose }: { wsId: string; step: Step; onClose: () => void }) {
  const id = useId();
  const [target, setTarget] = useState<"tile" | "schedule">("tile");
  const [dashboard, setDashboard] = useState("Pinned steps");
  const [name, setName] = useState(step.title);
  const [cron, setCron] = useState("0 7 * * 1");
  const [requested, setRequested] = useState<PinResult | null>(null);
  const [pinned, setPinned] = useState<PinResult | null>(null);
  const act = useAction();
  const body = { target, version: step.version, dashboard: target === "tile" ? dashboard : null, name: target === "schedule" ? name : null,
    cron: target === "schedule" ? cron : null, timezone: "UTC" };
  const request = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.pinStep(wsId, step.id, body));
    if (r?.status === "approval_required") setRequested(r);
    else if (r) setPinned(r);
  };
  const confirm = async () => {
    if (!requested?.approval_id) return;
    const r = await act.run(() => api.pinStep(wsId, step.id, { ...body, approval_id: requested.approval_id }));
    if (r) setPinned(r);
  };
  if (pinned) {
    return <Notice tone="success">Pinned version {step.version} to {target === "tile" ? `the tile board "${dashboard}"` : `the schedule "${name}"`}.
      {target === "schedule" ? " It re-verifies the frozen query at every run." : " A refresh replays the frozen query."}</Notice>;
  }
  return (
    <div className="stack">
      {!requested && (
        <form className="form" onSubmit={request} aria-label={`Pin ${step.title}`}>
          <Field label="Pin to" htmlFor={`${id}-target`}>
            <select id={`${id}-target`} value={target} onChange={(e) => setTarget(e.target.value as "tile" | "schedule")}>
              <option value="tile">a dashboard tile</option>
              <option value="schedule">a schedule</option>
            </select>
          </Field>
          {target === "tile" ? (
            <Field label="Dashboard" htmlFor={`${id}-dash`}><input id={`${id}-dash`} value={dashboard} onChange={(e) => setDashboard(e.target.value)} /></Field>
          ) : (
            <div className="form-row">
              <Field label="Schedule name" htmlFor={`${id}-name`}><input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <Field label="Cron (UTC)" htmlFor={`${id}-cron`}><input id={`${id}-cron`} value={cron} onChange={(e) => setCron(e.target.value)} /></Field>
            </div>
          )}
          <p className="small muted">Pinning writes outside this thread, so it needs an approval bound to the frozen query's hash.</p>
          <ErrorBox error={act.error} />
          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={act.busy}>Request approval to pin</button>
          </div>
        </form>
      )}
      {requested && (
        <div className="stack" role="status">
          <Notice tone="warning">Approval requested: an approver decides it in the <Link to={to.approvals(wsId)}>approvals inbox</Link>.
            It is bound to payload <code>{shortHash(requested.payload_hash, 10)}</code>; a changed step needs a new approval.</Notice>
          <ErrorBox error={act.error} />
          <div className="chip-row">
            <button type="button" className="btn btn-sm btn-primary" onClick={() => void confirm()} disabled={act.busy}>Pin with the approved request</button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>Later</button>
          </div>
          <TechnicalDetails value={requested} />
        </div>
      )}
    </div>
  );
}

function ForkForm({ step, busy, onFork, onCancel }: { step: Step; busy: boolean; onFork: (name: string, downstream: boolean) => void; onCancel: () => void }) {
  const id = useId();
  const [name, setName] = useState("what if");
  const [downstream, setDownstream] = useState(true);
  return (
    <form className="form" onSubmit={(e) => { e.preventDefault(); onFork(name.trim() || "what if", downstream); }} aria-label={`Fork from ${step.title}`}>
      <Field label="Branch name" htmlFor={`${id}-n`}><input id={`${id}-n`} value={name} onChange={(e) => setName(e.target.value)} /></Field>
      <label className="toggle small"><input type="checkbox" checked={downstream} onChange={(e) => setDownstream(e.target.checked)} /> Also copy the steps after it</label>
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={busy}>Fork</button>
      </div>
    </form>
  );
}

type Panel = "edit" | "versions" | "pin" | "fork" | null;

function StepCard({ wsId, step, role, highlight, onRevised, onFork, forking }: {
  wsId: string; step: Step; role: string | undefined; highlight: string | undefined;
  onRevised: (r: StepRevision) => void; onFork: (step: Step, name: string, downstream: boolean) => void; forking: boolean;
}) {
  const detail = useAsync(() => api.step(wsId, step.id), [wsId, step.id, step.version]);
  const [panel, setPanel] = useState<Panel>(null);
  const [why, setWhy] = useState(false);
  const whyRef = useRef<HTMLButtonElement>(null);
  const act = useAction();
  const canRun = roleAtLeast(role, "analyst") && !step.inherited;
  const canPin = roleAtLeast(role, "editor");
  // services/steps.py pin(): only a query or an AnalysisSpec method step has a frozen query to replay
  const pinKind = step.kind === "query" || (step.kind === "method" && !!step.spec.analysis_spec);
  const pinnable = pinKind && step.status === "ok" && step.verification_record?.state === "ACTIVE" && step.verification_record.badge === "verified";
  const vr = step.verification_record;
  const failed = step.checks.filter((c) => !c.passed);
  const rerun = async () => {
    const r = await act.run(() => api.rerunStep(wsId, step.id));
    if (r) onRevised(r);
  };
  const toggle = (p: Panel) => setPanel((cur) => (cur === p ? null : p));
  return (
    <li className={`step-card ${step.inherited ? "inherited" : ""}`} data-status={step.status} data-void={vr?.badge === "void" ? "true" : undefined}
      aria-label={`Step ${step.seq}: ${step.title}`}>
      <div className="step-head">
        <h3>{step.title}</h3>
        <Tag>{KIND_WORDS[step.kind] ?? step.kind}</Tag>
        <span className="small muted">v{step.version}{step.current_version !== step.version ? ` of ${step.current_version}` : ""}</span>
        <StatusBadge status={step.status} />
        {step.inherited && <Tag tone="info">from the parent branch</Tag>}
        {highlight && <Tag tone="warning">{highlight}</Tag>}
      </div>
      <div className="small"><VerificationBadge state={vr} /> {!vr && <span className="muted">{noVerdictText(step.kind, step.status)}</span>}
        {canRun && <ReverifyButton state={vr} onDone={() => void detail.reload()} />}</div>
      {step.checks.length > 0 && (
        <ul className="step-checks small" aria-label={`Checks of ${step.title}`}>
          {step.checks.map((c, k) => (
            <li key={`${c.check}-${k}`} title={c.detail}>
              <span aria-hidden="true">{c.passed ? "✓" : c.corrected ? "↻" : "✗"}</span> {CHECK_WORDS[c.check] ?? c.check}
              <span className="sr-only">: {c.passed ? "passed" : c.corrected ? "failed, corrected and re-run" : "failed"}</span>
            </li>
          ))}
        </ul>
      )}
      {failed.length > 0 && (
        <ul className="small">
          {failed.map((c, k) => <li key={k}>{c.corrected ? "Corrected: " : "Flagged: "}{c.detail}</li>)}
        </ul>
      )}
      {step.error && <p className="small warn-text">{step.error}</p>}
      <ErrorBox error={detail.error} />
      {detail.data && <StepResultView wsId={wsId} step={step} result={detail.data.result} />}
      <div className="step-actions">
        {detail.data?.result?.rows?.length ? (
          <button type="button" ref={whyRef} className="btn btn-xs btn-ghost" onClick={() => setWhy(true)} aria-haspopup="dialog">
            Why this number?<span className="sr-only"> ({step.title})</span></button>
        ) : null}
        {canRun && <button type="button" className="btn btn-xs" aria-expanded={panel === "edit"} onClick={() => toggle("edit")}>Edit<span className="sr-only"> {step.title}</span></button>}
        {canRun && <button type="button" className="btn btn-xs" onClick={() => void rerun()} disabled={act.busy}>Re-run<span className="sr-only"> {step.title}</span></button>}
        <button type="button" className="btn btn-xs btn-ghost" aria-expanded={panel === "versions"} onClick={() => toggle("versions")}>
          Versions ({step.current_version})<span className="sr-only"> of {step.title}</span></button>
        {canPin && (
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={panel === "pin"} onClick={() => toggle("pin")} disabled={!pinnable}
            title={pinnable ? undefined : "Only a verified, ok version can be pinned"}>Pin<span className="sr-only"> {step.title}</span></button>
        )}
        {roleAtLeast(role, "analyst") && (
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={panel === "fork"} onClick={() => toggle("fork")}>
            Fork from here<span className="sr-only"> ({step.title})</span></button>
        )}
      </div>
      {canPin && !pinnable && <p className="small muted">{pinKind ? "Pinning needs an ok version with an active verified verdict."
        : "Only a query or method step can be pinned; pin the step this one reads."}</p>}
      <ErrorBox error={act.error} />
      {panel === "edit" && <EditStepForm wsId={wsId} step={detail.data ?? step} onCancel={() => setPanel(null)}
        onDone={(r) => { setPanel(null); onRevised(r); }} />}
      {panel === "versions" && <StepVersions wsId={wsId} stepId={step.id} />}
      {panel === "pin" && <PinForm wsId={wsId} step={step} onClose={() => setPanel(null)} />}
      {panel === "fork" && <ForkForm step={step} busy={forking} onCancel={() => setPanel(null)} onFork={(n, d) => onFork(step, n, d)} />}
      {why && <StepWhyDrawer step={step} result={detail.data?.result} onClose={() => { setWhy(false); whyRef.current?.focus(); }} />}
    </li>
  );
}

function AddStepForm({ wsId, branchId, steps, onAdded }: { wsId: string; branchId: string; steps: Step[]; onAdded: () => void }) {
  const id = useId();
  const [kind, setKind] = useState<"query" | "claim">("query");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [dep, setDep] = useState("");
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!text.trim()) return;
    const r = await act.run(() => api.addStep(wsId, branchId, { kind, title: title.trim() || null,
      spec: kind === "query" ? { sql: text } : { text }, depends_on: dep ? [dep] : [] }));
    if (r) {
      setText("");
      setTitle("");
      onAdded();
    }
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Add a step">
      <div className="form-row">
        <Field label="Kind" htmlFor={`${id}-k`}>
          <select id={`${id}-k`} value={kind} onChange={(e) => setKind(e.target.value as "query" | "claim")}>
            <option value="query">query (SQL through the gateway)</option>
            <option value="claim">claim (its numbers must bind)</option>
          </select>
        </Field>
        <Field label="Title" htmlFor={`${id}-t`}><input id={`${id}-t`} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      </div>
      <Field label={kind === "query" ? "SQL" : "Claim"} htmlFor={`${id}-x`}>
        <textarea id={`${id}-x`} rows={3} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
      {kind === "claim" && (
        <Field label="Reads" htmlFor={`${id}-d`}>
          <select id={`${id}-d`} value={dep} onChange={(e) => setDep(e.target.value)}>
            <option value="">no upstream step</option>
            {steps.filter((x) => x.kind === "query" || x.kind === "method").map((x) => <option key={x.id} value={x.id}>{optionLabel(x.title)}</option>)}
          </select>
        </Field>
      )}
      <ErrorBox error={act.error} />
      <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !text.trim()}>Add and run</button></div>
    </form>
  );
}

const MATCH_WORDS: Record<string, string> = {
  shared: "shared", equivalent: "same result", diverged: "diverged", only_a: "only in the first", only_b: "only in the second",
};

/** Two branches side by side: steps paired by lineage, spec differences, number deltas and both verdicts. */
export function BranchComparison({ cmp }: { cmp: BranchCompare }) {
  return (
    <div className="stack" aria-label={`Compare ${cmp.a.name} with ${cmp.b.name}`} role="group">
      <div className="compare-grid small"><strong>{cmp.a.name}</strong><strong>{cmp.b.name}</strong></div>
      {cmp.steps.map((r, k) => (
        <div key={`${r.root}-${k}`} className="compare-row" aria-label={`${r.a?.title ?? r.b?.title}: ${MATCH_WORDS[r.match] ?? r.match}`} role="group">
          <div className="chip-row small"><Tag tone={r.match === "diverged" ? "warning" : r.match.startsWith("only") ? "info" : "neutral"}>
            {MATCH_WORDS[r.match] ?? r.match}</Tag> <strong>{r.a?.title ?? r.b?.title}</strong></div>
          <div className="compare-grid small">
            <div>{r.a ? <>v{r.a.version} <StatusBadge status={r.a.status} /> <VerificationBadge state={r.verdicts?.a ?? r.a.verification_record} showCause={false} /></> : <span className="muted">—</span>}</div>
            <div>{r.b ? <>v{r.b.version} <StatusBadge status={r.b.status} /> <VerificationBadge state={r.verdicts?.b ?? r.b.verification_record} showCause={false} /></> : <span className="muted">—</span>}</div>
          </div>
          {r.numbers?.headline && (
            <p className="small">Headline: {fmtValue(r.numbers.headline.a)} → {fmtValue(r.numbers.headline.b)}
              {r.numbers.headline.delta !== null && <> (change {r.numbers.headline.delta > 0 ? "+" : ""}{fmtValue(r.numbers.headline.delta)})</>}</p>
          )}
          {r.numbers && r.numbers.same_result === false && !!r.numbers.cells?.length && (
            <div className="table-wrap" tabIndex={0}>
              <table className="table table-compact">
                <caption className="sr-only">Numbers that differ in {r.a?.title ?? r.b?.title}</caption>
                <thead><tr><th scope="col">Row</th><th scope="col">Column</th><th scope="col" className="num">{cmp.a.name}</th>
                  <th scope="col" className="num">{cmp.b.name}</th><th scope="col" className="num">Change</th></tr></thead>
                <tbody>
                  {r.numbers.cells.map((c) => (
                    <tr key={`${c.key}-${c.column}`}><td>{c.key}</td><td><code>{c.column}</code></td><td className="num">{fmtValue(c.a)}</td>
                      <td className="num">{fmtValue(c.b)}</td><td className="num">{c.delta === null ? "—" : fmtValue(c.delta)}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {Array.isArray(r.spec_diff) && r.spec_diff.length > 0 && (
            <details className="small"><summary>What changed in the spec</summary>
              {r.spec_diff.map((d) => (
                <div key={d.path} className="compare-grid"><CodeBlock code={String(d.a ?? "")} label={`${cmp.a.name}: ${d.path}`} />
                  <CodeBlock code={String(d.b ?? "")} label={`${cmp.b.name}: ${d.path}`} /></div>
              ))}
            </details>
          )}
        </div>
      ))}
    </div>
  );
}

/**
 * The Data Thread (spec v4 §7, P7-04/05): an investigation's or Ask thread's steps as cards, each with
 * its version, self-checks, verdict (a void one says why), result and "Why this number?". An analyst
 * edits or re-runs a step (its dependents re-run, earlier verdicts turn void, old versions stay
 * readable), forks a branch from any step, compares two branches side by side and merges one into a
 * report; an editor pins a verified step to a tile or a schedule through an approval.
 */
export function DataThread({ wsId, type, id, branch, role, onBranch }: {
  wsId: string; type: ContainerType; id: string; branch: string | null; role: string | undefined; onBranch: (b: string | null) => void;
}) {
  const main = useAsync(() => api.thread(wsId, type, id), [wsId, type, id]);
  const onBranchId = branch && branch !== main.data?.branch.id ? branch : null;
  const other = useAsync(() => (onBranchId ? api.branch(wsId, onBranchId) : Promise.resolve(undefined)), [wsId, onBranchId]);
  const [notice, setNotice] = useState<string | null>(null);
  const [marks, setMarks] = useState<Record<string, string>>({});
  const [compareWith, setCompareWith] = useState("");
  const [cmp, setCmp] = useState<BranchCompare | null>(null);
  const [merged, setMerged] = useState<MergeResult | null>(null);
  const [mergeTitle, setMergeTitle] = useState("");
  const act = useAction();
  const view = onBranchId ? other.data : main.data;
  const branches: Branch[] = main.data?.branches?.length ? main.data.branches : main.data ? [main.data.branch] : [];
  const current = view?.branch;
  const canRun = roleAtLeast(role, "analyst");

  const reload = () => { void main.reload(); if (onBranchId) void other.reload(); };
  const onRevised = (r: StepRevision) => {
    const m: Record<string, string> = { [r.step.id]: `now v${r.step.version}` };
    for (const d of r.rerun) m[d.id] = d.status === "flagged" ? "re-ran: flagged" : "re-ran";
    setMarks(m);
    setNotice(`${r.step.title} is now version ${r.step.version}. ${r.rerun.length ? `${r.rerun.length} step${r.rerun.length > 1 ? "s" : ""} that read it re-ran` : "No other step reads it"}`
      + `${r.voided_records.length ? `; ${r.voided_records.length} earlier verdict${r.voided_records.length > 1 ? "s are" : " is"} now void` : ""}. Earlier versions stay readable under Versions.`);
    setCmp(null);
    reload();
  };
  const fork = async (step: Step, name: string, downstream: boolean) => {
    if (!current) return;
    const r = await act.run(() => api.fork(wsId, current.id, { from_step_id: step.id, name, include_downstream: downstream }));
    if (r) {
      setNotice(`Forked "${r.branch.name}" from ${step.title}. Steps before it are shared with ${current.name}; edit the copies freely.`);
      setMarks({});
      void main.reload();
      onBranch(r.branch.id);
    }
  };
  const compare = async (e: FormEvent) => {
    e.preventDefault();
    if (!current || !compareWith) return;
    const r = await act.run(() => api.compareBranches(wsId, compareWith, current.id));
    if (r) setCmp(r);
  };
  const merge = async (e: FormEvent) => {
    e.preventDefault();
    if (!current) return;
    const r = await act.run(() => api.mergeBranch(wsId, current.id, { title: mergeTitle.trim() || null }));
    if (r) {
      setMerged(r);
      void main.reload();
    }
  };
  const ingest = async () => {
    if (type === "notebook") return;
    if (await act.run(() => api.ingestThread(wsId, type, id))) void main.reload();
  };

  if (main.error) return <ErrorBox error={main.error} onRetry={main.reload} />;
  if (!main.data) return <Loading label="Loading the Data Thread…" />;
  return (
    <div className="stack">
      <div className="toolbar">
        <label className="inline-field small">Branch
          <select value={current?.id ?? ""} onChange={(e) => { setCmp(null); setMerged(null); setMarks({}); onBranch(e.target.value === main.data!.branch.id ? null : e.target.value); }}
            aria-label="Branch">
            {branches.map((b) => <option key={b.id} value={b.id}>{optionLabel(b.name, 50)}{b.parent_branch_id ? " (fork)" : ""}{b.status === "merged" ? " — merged" : ""}</option>)}
          </select>
        </label>
        {current?.forked_from && <span className="small muted">forked from step {current.forked_from.step_id} v{current.forked_from.version}</span>}
        {type === "run" && <Link className="btn btn-xs btn-ghost" to={to.run(wsId, id)}>Open the investigation</Link>}
      </div>
      {notice && <Notice tone="info">{notice}</Notice>}
      <ErrorBox error={act.error ?? other.error} />
      {view && view.steps.length === 0 && (
        <EmptyState title="No steps yet">
          {type !== "notebook" && canRun ? <button type="button" className="btn btn-sm btn-primary" onClick={() => void ingest()} disabled={act.busy}>
            Record this {type === "run" ? "investigation" : "Ask thread"} as steps</button> : "Nothing has been recorded as steps yet."}
        </EmptyState>
      )}
      {!view && <Loading />}
      {view && view.steps.length > 0 && <h2 className="h-sm">Steps on {current?.name ?? "main"} ({view.steps.length})</h2>}
      {view && view.steps.length > 0 && (
        <ol className="thread" aria-label={`Data Thread: ${current?.name ?? "main"}`}>
          {view.steps.map((st) => (
            <StepCard key={`${st.id}-${st.version}`} wsId={wsId} step={st} role={role} highlight={marks[st.id]} onRevised={onRevised} onFork={fork} forking={act.busy} />
          ))}
        </ol>
      )}
      {canRun && current && view && (
        <details className="card">
          <summary>Add a step to {current.name}</summary>
          <div className="card-body"><AddStepForm wsId={wsId} branchId={current.id} steps={view.steps} onAdded={reload} /></div>
        </details>
      )}
      {branches.length > 1 && current && (
        <section className="card" aria-label="Compare branches">
          <div className="card-body stack">
            <form className="form-inline" onSubmit={compare} aria-label="Compare branches">
              <label className="inline-field small">Compare {current.name} with
                <select value={compareWith} onChange={(e) => setCompareWith(e.target.value)} aria-label="Compare with">
                  <option value="">choose a branch…</option>
                  {branches.filter((b) => b.id !== current.id).map((b) => <option key={b.id} value={b.id}>{optionLabel(b.name, 50)}</option>)}
                </select>
              </label>
              <button type="submit" className="btn btn-sm" disabled={!compareWith || act.busy}>Compare side by side</button>
            </form>
            {cmp && <BranchComparison cmp={cmp} />}
          </div>
        </section>
      )}
      {canRun && current && view && view.steps.length > 0 && (
        <section className="card" aria-label="Merge into a report">
          <div className="card-body stack">
            {merged ? (
              <Notice tone="success">Merged into the report "{merged.report.content?.title || merged.report.name}" (version {merged.report.version}) with {merged.included} step
                {merged.included === 1 ? "" : "s"}{merged.excluded.length ? `; ${merged.excluded.length} left out (${merged.excluded.map((x) => String(x.reason)).join("; ")})` : ""}.
                {" "}<Link to={to.reports(wsId, merged.report.id)}>Open it in Results</Link>. It keeps this branch&apos;s lineage.</Notice>
            ) : (
              <form className="form-inline" onSubmit={merge} aria-label="Merge into a report">
                <label className="inline-field small">Report title
                  <input value={mergeTitle} onChange={(e) => setMergeTitle(e.target.value)} placeholder={`Data Thread: ${current.name}`} aria-label="Report title" />
                </label>
                <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy}>Merge {current.name} into a report</button>
                <span className="small muted">A branch with a void verdict cannot merge until it is re-run.</span>
              </form>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

/** Work → Data Thread: pick an investigation or an Ask thread, then its thread. */
export function DataThreadPanel({ wsId, role, container, branch, onChange }: {
  wsId: string; role: string | undefined; container: string | null; branch: string | null;
  onChange: (patch: { container?: string | null; branch?: string | null }) => void;
}) {
  const c = parseContainer(container);
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const threads = useAsync(() => api.askThreads(wsId), [wsId]);
  const id = useId();
  return (
    <div className="stack">
      <p className="muted small">Every investigation and Ask thread as a sequence of steps you can inspect, edit, re-run, branch and merge.</p>
      <Field label="Thread" htmlFor={`${id}-c`}>
        <select id={`${id}-c`} value={container ?? ""} onChange={(e) => onChange({ container: e.target.value || null, branch: null })}>
          <option value="">Choose an investigation or an Ask thread…</option>
          {(runs.data ?? []).length > 0 && <optgroup label="Investigations">
            {runs.data!.map((r) => <option key={r.id} value={`run:${r.id}`} title={r.objective}>{optionLabel(r.objective)}</option>)}</optgroup>}
          {(threads.data ?? []).length > 0 && <optgroup label="Ask threads">
            {threads.data!.map((t) => <option key={t.id} value={`ask_thread:${t.id}`} title={t.title}>{optionLabel(t.title)}</option>)}</optgroup>}
        </select>
      </Field>
      <ErrorBox error={runs.error ?? threads.error} />
      {c && <DataThread key={container!} wsId={wsId} type={c.type} id={c.id} branch={branch} role={role} onBranch={(b) => onChange({ branch: b })} />}
    </div>
  );
}
