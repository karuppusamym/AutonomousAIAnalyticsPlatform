import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Materialization, type Pipeline, type PipelineCheck, type PipelineRun } from "../api";
import { diffJson, preview } from "../lib/diff";
import { fmtDate, fmtNumber, fmtValue, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { parsePolicy } from "../lib/policy";
import { roleAtLeast, to } from "../routes";
import { ApprovalStepper } from "./ApprovalStepper";
import { Card, CodeBlock, DataTable, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, Tag, TechnicalDetails } from "./ui";

type Spec = {
  name?: string; recipes?: { name: string; version?: number }[]; inputs?: { asset: string; max_age_hours?: number }[];
  output?: { recipe?: string; output?: string; keys?: string[]; grain?: string[] }; joins?: { node: string; cardinality?: string }[];
  destination?: { engine?: string; schema?: string; schema_name?: string; table?: string } | null; freshness?: { max_age_hours?: number } | null;
};

const CHECK_WORDS: Record<string, string> = {
  input_version: "Input version pinned", input_freshness: "Input fresh enough", join_cardinality: "Join cardinality as declared",
  unmatched_rows: "Unmatched join rows within limit", fanout: "No join fan-out", gate: "Contract gate", complete_output: "Output complete (not truncated)",
  aggregate_reconciliation: "Reconciles with its inputs", budget: "Within budget",
};

const RUN_WORDS: Record<string, string> = {
  succeeded: "Every check passed", blocked: "Blocked: a check failed, nothing was written and the last good version stays",
  awaiting_approval: "Every check passed; writing it needs an approval", failed: "Failed", running: "Running",
};

const destOf = (spec: Spec) => (spec.destination ? `${spec.destination.schema ?? spec.destination.schema_name}.${spec.destination.table}` : null);

/** The source-to-output DAG of a pipeline spec, as nodes with a text equivalent. */
export function PipelineDag({ spec }: { spec: Spec }) {
  const nodes: { kind: string; label: string; detail?: string }[] = [
    ...(spec.inputs ?? []).map((i) => ({ kind: "source", label: i.asset, detail: i.max_age_hours ? `fresh within ${i.max_age_hours} h` : undefined })),
    ...(spec.recipes ?? []).map((r) => ({ kind: "recipe", label: r.name, detail: r.version ? `recipe v${r.version}` : "recipe" })),
    ...(spec.joins ?? []).map((j) => ({ kind: "join", label: j.node, detail: j.cardinality?.replace(/_/g, " ") })),
    { kind: "output", label: spec.output?.output ?? "output", detail: spec.output?.keys?.length ? `keys ${spec.output.keys.join(", ")}` : undefined },
    ...(destOf(spec) ? [{ kind: "destination", label: destOf(spec)!, detail: "managed table (approval to write)" }] : []),
  ];
  return (
    <ol className="dag" aria-label="Source to output">
      {nodes.map((n, i) => (
        <li key={`${n.kind}-${n.label}-${i}`} className="dag-node" data-kind={n.kind === "destination" ? "output" : n.kind}>
          <span className="small muted">{n.kind}</span><code>{n.label}</code>{n.detail && <span className="small">{n.detail}</span>}
          {i < nodes.length - 1 && <span className="sr-only">, then</span>}
        </li>
      ))}
    </ol>
  );
}

function checkDetail(c: PipelineCheck): string {
  const v = (k: string) => (c[k] === undefined || c[k] === null ? "—" : fmtValue(c[k]));
  switch (c.check) {
    case "join_cardinality": return `${v("join")}: declared ${v("declared")}, observed ${v("observed")}`;
    case "unmatched_rows": return `${v("join")}: ${v("unmatched_left_pct")}% unmatched (limit ${v("max_unmatched_pct")}%)`;
    case "fanout": return `${v("join")}: rows × ${v("row_multiplication")} (limit ${v("max_row_multiplication")})`;
    case "gate": return `${v("gate")} (${v("severity")}): ${v("status")}, ${v("failed_rows")} rows`;
    case "input_freshness": return `${v("asset")}: ${v("age_hours")} h old (limit ${v("max_age_hours")} h)`;
    case "input_version": return `${v("asset")}: pinned ${v("pinned")}, now ${v("current")}`;
    case "aggregate_reconciliation": return `${v("name")}: ${v("difference_pct")}% apart (tolerance ${v("tolerance_pct")}%)`;
    case "complete_output": return c.truncated ? "truncated" : "complete";
    default: return "";
  }
}

/**
 * A dry run's result (P6-03): the rows ledger (input, output, quarantined, rejected and late rows each
 * shown on its own, never folded into one green count), join diagnostics, contract gates and
 * reconciliation, the compiled SQL, and the candidate preview.
 */
export function DryRunResult({ run }: { run: PipelineRun }) {
  const r = run.reconciliation;
  const gates = run.candidate?.gates;
  const quarantined = r?.dropped_rows ?? gates?.dropped_rows ?? null;
  const late = r?.late_rows;
  const failing = (run.checks ?? []).filter((c) => !c.ok);
  return (
    <div className="stack" role="region" aria-label={`Dry run ${run.id}`}>
      <p className="readiness-verdict"><StatusBadge status={run.status} label={run.status.replace(/_/g, " ")} />{" "}
        <span className="small">{RUN_WORDS[run.status] ?? run.status}.</span></p>
      {run.error && <Notice tone="danger">{run.error}</Notice>}
      {r && (
        <div className="rows-ledger" aria-label="Rows" role="group">
          <div className="stat"><span className="stat-label">Input rows</span><span className="stat-value">{fmtNumber(r.input_rows, 0)}</span>
            <span className="small muted">{Object.entries(r.inputs).map(([k, n]) => `${k} ${fmtNumber(n, 0)}`).join(" · ")}</span></div>
          <div className="stat"><span className="stat-label">Output rows</span><span className="stat-value">{fmtNumber(r.output_rows, 0)}</span></div>
          <div className="stat" data-attention={quarantined ? "true" : undefined}><span className="stat-label">Quarantined rows</span>
            <span className="stat-value">{quarantined === null ? "unknown" : fmtNumber(quarantined, 0)}</span><span className="small muted">dropped by a gate, kept aside</span></div>
          <div className="stat" data-attention={r.rejected_rows ? "true" : undefined}><span className="stat-label">Rejected rows</span>
            <span className="stat-value">{fmtNumber(r.rejected_rows, 0)}</span></div>
          <div className="stat" data-attention={late ? "true" : undefined}><span className="stat-label">Late rows</span>
            <span className="stat-value">{late === undefined ? "not reported" : late === null ? "not measured" : fmtNumber(late, 0)}</span>
            <span className="small muted">{late === null && r.late_rows_reason ? r.late_rows_reason : "arrived after the watermark window"}</span></div>
        </div>
      )}
      {failing.length > 0 && <Notice tone="danger">{failing.length} check{failing.length > 1 ? "s" : ""} failed: {failing.map((c) => CHECK_WORDS[c.check] ?? c.check).join(", ")}.</Notice>}
      {!!run.checks?.length && (
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact">
            <caption className="cap">Checks</caption>
            <thead><tr><th scope="col">Check</th><th scope="col">Result</th><th scope="col">Detail</th></tr></thead>
            <tbody>
              {run.checks.map((c, k) => (
                <tr key={`${c.check}-${k}`}><td>{CHECK_WORDS[c.check] ?? c.check}</td>
                  <td><StatusBadge status={c.ok ? (c.status === "warned" || c.status === "dropped" ? "warn" : "pass") : "fail"}
                    label={c.ok ? (c.status === "warned" ? "warning" : c.status === "dropped" ? "rows quarantined" : "pass") : "fail"} /></td>
                  <td className="small">{checkDetail(c)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {r && Object.keys(r.unmatched).length > 0 && (
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact">
            <caption className="cap">Join diagnostics</caption>
            <thead><tr><th scope="col">Join</th><th scope="col" className="num">Unmatched left rows</th><th scope="col" className="num">Unmatched left keys</th>
              <th scope="col" className="num">Unmatched right keys</th><th scope="col" className="num">Row multiplication</th></tr></thead>
            <tbody>
              {Object.entries(r.unmatched).map(([join, u]) => (
                <tr key={join}><td><code>{join}</code></td><td className="num">{fmtNumber(u.unmatched_left_rows, 0)} ({fmtValue(u.unmatched_left_pct)}%)</td>
                  <td className="num">{fmtNumber(u.unmatched_left_keys, 0)}</td><td className="num">{fmtNumber(u.unmatched_right_keys, 0)}</td>
                  <td className="num">{u.row_multiplication === null ? "—" : `× ${fmtValue(u.row_multiplication)}`}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!r?.aggregates.length && (
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact">
            <caption className="cap">Reconciliation</caption>
            <thead><tr><th scope="col">Check</th><th scope="col" className="num">Input</th><th scope="col" className="num">Output</th>
              <th scope="col" className="num">Difference</th><th scope="col">Within tolerance</th></tr></thead>
            <tbody>
              {r.aggregates.map((a) => (
                <tr key={a.name}><td>{a.name} <span className="muted small">({a.func}{a.input.column ? ` of ${a.input.column}` : ""})</span></td>
                  <td className="num">{fmtValue(a.input.value)}</td><td className="num">{fmtValue(a.output.value)}</td>
                  <td className="num">{fmtValue(a.difference)} ({fmtValue(a.difference_pct)}%)</td>
                  <td><StatusBadge status={a.ok ? "pass" : "fail"} label={a.ok ? `yes (≤ ${a.tolerance_pct}%)` : `no (> ${a.tolerance_pct}%)`} /></td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {gates?.warnings?.length ? <ul className="small" aria-label="Gate warnings">{gates.warnings.map((w) => <li key={w}>{w}</li>)}</ul> : null}
      {run.candidate && (
        <details className="card">
          <summary>Candidate output ({fmtNumber(run.candidate.row_count, 0)} rows; nothing written yet)</summary>
          <div className="card-body">
            <DataTable columns={run.candidate.columns.map((c) => c.name)} rows={run.candidate.preview} caption={`Candidate of ${run.pipeline_name}`} maxRows={20} />
          </div>
        </details>
      )}
      {run.sql?.output && (
        <details className="card"><summary>Compiled SQL ({run.sql.dialect})</summary><div className="card-body"><CodeBlock code={run.sql.output} /></div></details>
      )}
      <TechnicalDetails value={{ manifest: run.manifest, plan_hash: run.plan_hash, candidate: run.candidate?.snapshot, query_ids: run.query_ids }} />
    </div>
  );
}

function TransformationDiff({ current, previous }: { current: Pipeline; previous: Pipeline | undefined }) {
  if (!previous) return <p className="small muted">This is the first version: nothing to compare with.</p>;
  const changes = diffJson(previous.spec, current.spec);
  return (
    <div className="stack">
      <p className="small">Version {current.version} against version {previous.version}: {changes.length} change{changes.length === 1 ? "" : "s"}.</p>
      {changes.length > 0 && (
        <div className="table-wrap" tabIndex={0}>
          <table className="table table-compact diff-table">
            <caption className="sr-only">Changes from version {previous.version}</caption>
            <thead><tr><th scope="col">Path</th><th scope="col">Change</th><th scope="col">v{previous.version}</th><th scope="col">v{current.version}</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path} className={`diff-${c.kind}`}><td><code>{c.path}</code></td>
                  <td><Tag tone={c.kind === "added" ? "success" : c.kind === "removed" ? "danger" : "warning"}>{c.kind}</Tag></td>
                  <td className="small diff-before">{preview(c.before)}</td><td className="small diff-after">{preview(c.after)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PipelineDetail({ wsId, pipeline, all, role, onChanged }: {
  wsId: string; pipeline: Pipeline; all: Pipeline[]; role: string | undefined; onChanged: () => void;
}) {
  const runs = useAsync(() => api.pipelineRuns(wsId, pipeline.id), [wsId, pipeline.id]);
  const [run, setRun] = useState<PipelineRun | null>(null);
  const [done, setDone] = useState<Materialization | null>(null);
  const act = useAction();
  const spec = pipeline.spec as Spec;
  const previous = all.filter((p) => p.name === pipeline.name && p.version < pipeline.version).sort((a, b) => b.version - a.version)[0];
  const dry = async () => {
    setDone(null);
    const r = await act.run(() => api.dryRunPipeline(wsId, pipeline.id));
    if (r) {
      setRun(r);
      void runs.reload();
    }
  };
  const publish = async () => {
    if (await act.run(() => api.publishPipeline(wsId, pipeline.id))) onChanged();
  };
  const shown = run ?? runs.data?.[0] ?? null;
  return (
    <div className="stack" role="region" aria-label={`Pipeline ${pipeline.name} v${pipeline.version}`}>
      <div className="toolbar">
        <h3 className="h-sm">{pipeline.name} v{pipeline.version}</h3><StatusBadge status={pipeline.status} />
        {destOf(spec) && <span className="small muted">writes <code>{destOf(spec)}</code></span>}
        {pipeline.status === "draft" && roleAtLeast(role, "editor") && (
          <button type="button" className="btn btn-sm" onClick={() => void publish()} disabled={act.busy}>Publish</button>)}
      </div>
      <PipelineDag spec={spec} />
      <details className="card"><summary>Transformation diff</summary><div className="card-body"><TransformationDiff current={pipeline} previous={previous} /></div></details>
      {roleAtLeast(role, "analyst") && (
        <div className="chip-row">
          <button type="button" className="btn btn-sm btn-primary" onClick={() => void dry()} disabled={act.busy || pipeline.status !== "published"}>
            {act.busy ? "Dry-running…" : "Dry run"}</button>
          <span className="small muted">{pipeline.status === "published" ? "Reads through the gateway into snapshots; writes nothing." : "Publish the pipeline first: runs use published versions."}</span>
        </div>
      )}
      <ErrorBox error={act.error ?? runs.error} />
      {shown && <DryRunResult run={shown} />}
      {shown?.status === "awaiting_approval" && shown.approval_id && !done && roleAtLeast(role, "editor") && (
        <ApprovalStepper wsId={wsId} label="Materialize this candidate" what={`writing ${destOf(spec) ?? "the output"} from dry run ${shown.id}`}
          request={async () => ({ status: "approval_required", approval_id: shown.approval_id!, payload_hash: shown.plan_hash ?? undefined })}
          confirm={async (approvalId) => { const m = await api.materialize(wsId, shown.id, approvalId); setDone(m); return { status: "done" }; }}
          onDone={() => { void runs.reload(); }} />
      )}
      {done && (
        <Notice tone="success">Promoted <code>{done.schema_name}.{done.table_name}</code> version {done.version} ({fmtNumber(done.row_count, 0)} rows). The previous
          version stays for rollback in <Link to={to.outputs(wsId, { type: "table", table: done.table_name })}>Results → Managed tables</Link>.</Notice>
      )}
      {!!runs.data?.length && (
        <details className="card">
          <summary>Runs ({runs.data.length})</summary>
          <div className="card-body">
            <ul className="list small" aria-label="Pipeline runs">
              {runs.data.map((r) => <li key={r.id}>{r.id} · {r.mode} · <StatusBadge status={r.status} /> · {fmtDate(r.created_at)}{r.error ? ` — ${r.error}` : ""}</li>)}
            </ul>
          </div>
        </details>
      )}
    </div>
  );
}

/** Work → Prepare data → Pipelines (P6-03): pipeline versions, their DAG and diff, dry runs, and approve → materialize. */
export function PipelinesCard({ wsId, role, selected, onSelect }: { wsId: string; role: string | undefined; selected: string | null; onSelect: (id: string | null) => void }) {
  const list = useAsync(() => api.pipelines(wsId), [wsId]);
  const open = list.data?.find((p) => p.id === selected);
  return (
    <Card title="Pipelines" label="Pipelines">
      <p className="muted small">Published recipes with their input versions, join expectations, contract gates and reconciliation. A dry run writes
        nothing; materializing a candidate needs an approval and keeps the previous version for rollback.</p>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && <EmptyState title="No pipelines yet">The engineer agent proposes one from a published recipe (playbook.prepare).</EmptyState>}
      {!!list.data?.length && (
        <ul className="list selectable" aria-label="Pipelines">
          {list.data.map((p) => (
            <li key={p.id}>
              <button type="button" className={`list-button ${p.id === selected ? "active" : ""}`} aria-current={p.id === selected ? "true" : undefined} onClick={() => onSelect(p.id)}>
                <span>{p.name} v{p.version}</span><span className="small muted">{p.status}{p.published_at ? ` · ${fmtDate(p.published_at)}` : ""}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {open && list.data && <PipelineDetail key={open.id} wsId={wsId} pipeline={open} all={list.data} role={role} onChanged={list.reload} />}
      {roleAtLeast(role, "editor") && <NewPipeline wsId={wsId} onSaved={(p) => { void list.reload(); onSelect(p.id); }} />}
      {roleAtLeast(role, "owner") && <Destinations wsId={wsId} />}
    </Card>
  );
}

/** A PipelineSpec over published recipes, saved as a draft version (the server checks it against the recipes). */
function NewPipeline({ wsId, onSaved }: { wsId: string; onSaved: (p: Pipeline) => void }) {
  const id = useId();
  const [text, setText] = useState("");
  const act = useAction();
  const parsed = text.trim() ? parsePolicy(text) : { value: undefined, error: undefined };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.value) return;
    const p = await act.run(() => api.savePipeline(wsId, parsed.value!));
    if (p) {
      setText("");
      onSaved(p);
    }
  };
  return (
    <details className="advanced">
      <summary>Advanced: new pipeline from a specification</summary>
      <form className="form" onSubmit={save} aria-label="New pipeline">
        <Field label="Pipeline specification (JSON)" htmlFor={`${id}-spec`}
          hint="Published recipes, the output's keys and schema, and optionally a destination the workspace allows.">
          <textarea id={`${id}-spec`} className="mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={!!parsed.error} />
        </Field>
        {parsed.error && <p className="warn-text small" role="alert">Invalid JSON: {parsed.error}</p>}
        <ErrorBox error={act.error} />
        <div className="form-actions"><button type="submit" className="btn btn-sm" disabled={act.busy || !parsed.value}>Save draft</button></div>
      </form>
    </details>
  );
}

/** Owners allowlist the schemas the managed writer may create tables in (never a source schema). */
function Destinations({ wsId }: { wsId: string }) {
  const id = useId();
  const list = useAsync(() => api.writerDestinations(wsId), [wsId]);
  const [schema, setSchema] = useState("");
  const [tables, setTables] = useState("");
  const act = useAction();
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const names = tables.split(",").map((t) => t.trim()).filter(Boolean);
    if (await act.run(() => api.designateDestination(wsId, { schema: schema.trim(), tables: names }))) {
      setSchema("");
      setTables("");
      void list.reload();
    }
  };
  return (
    <details className="advanced">
      <summary>Destinations the writer may use ({list.data?.length ?? 0})</summary>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {!!list.data?.length && (
        <ul className="list compact small" aria-label="Writer destinations">
          {list.data.map((d) => <li key={d.id} className="list-item"><code>{d.schema_name}</code>
            <span className="muted">{d.tables?.length ? d.tables.join(", ") : "any table"}</span><StatusBadge status={d.status} /></li>)}
        </ul>
      )}
      <form className="form" onSubmit={add} aria-label="Allow a destination">
        <div className="form-row">
          <Field label="Schema" htmlFor={`${id}-schema`}><input id={`${id}-schema`} value={schema} onChange={(e) => setSchema(e.target.value)} placeholder="aos_out" /></Field>
          <Field label="Tables" htmlFor={`${id}-tables`} hint="Comma-separated; empty allows any table."><input id={`${id}-tables`} value={tables} onChange={(e) => setTables(e.target.value)} /></Field>
        </div>
        <ErrorBox error={act.error} />
        <div className="form-actions"><button type="submit" className="btn btn-sm" disabled={act.busy || !schema.trim()}>Allow destination</button></div>
      </form>
    </details>
  );
}

/** Outputs → Managed tables: each materialized table's versions, and rollback to the version the current one replaced. */
export function MaterializationsOutput({ wsId, role, table }: { wsId: string; role: string | undefined; table: string | null }) {
  const mats = useAsync(() => api.materializations(wsId), [wsId]);
  const act = useAction();
  const [note, setNote] = useState<string | null>(null);
  if (mats.error) return <ErrorBox error={mats.error} onRetry={mats.reload} />;
  if (!mats.data) return <Loading />;
  const tables = [...new Set(mats.data.map((m) => `${m.schema_name}.${m.table_name}`))].filter((t) => !table || t.endsWith(`.${table}`));
  const rollback = async (m: Materialization) => {
    if (!window.confirm(`Roll ${m.schema_name}.${m.table_name} back from version ${m.version}? Readers of the view see the previous version again.`)) return;
    const r = await act.run(() => api.rollbackMaterialization(wsId, m.id));
    if (r) {
      setNote(`Rolled back: ${r.schema_name}.${r.table_name} serves version ${r.version} again; version ${m.version} is kept as rolled back.`);
      void mats.reload();
    }
  };
  return (
    <div className="stack">
      {note && <Notice tone="success">{note}</Notice>}
      <ErrorBox error={act.error} />
      {tables.length === 0 && <EmptyState title="No managed tables yet">Materialize a pipeline's approved candidate from Investigations → Prepare data.</EmptyState>}
      {tables.map((t) => {
        const versions = mats.data!.filter((m) => `${m.schema_name}.${m.table_name}` === t).sort((a, b) => b.version - a.version);
        return (
          <section key={t} className="card" aria-label={`Table ${t}`}>
            <div className="card-body stack">
              <h2 className="h-sm"><code>{t}</code></h2>
              <div className="table-wrap" tabIndex={0}>
                <table className="table table-compact">
                  <caption className="sr-only">Versions of {t}</caption>
                  <thead><tr><th scope="col">Version</th><th scope="col">Status</th><th scope="col" className="num">Rows</th><th scope="col">Content</th>
                    <th scope="col">Promoted</th><th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>
                    {versions.map((m) => (
                      <tr key={m.id}><td>v{m.version} <span className="muted small">{m.version_table}</span></td><td><StatusBadge status={m.status} /></td>
                        <td className="num">{fmtNumber(m.row_count, 0)}</td><td><code>{shortHash(m.content_fingerprint, 10)}</code></td>
                        <td className="small">{fmtDate(m.promoted_at)}</td>
                        <td>{m.status === "promoted" && m.previous_id && roleAtLeast(role, "owner") && (
                          <button type="button" className="btn btn-xs" onClick={() => void rollback(m)} disabled={act.busy}>Roll back v{m.version}</button>)}
                          {m.status === "promoted" && m.previous_id && !roleAtLeast(role, "owner") && <span className="small muted">an owner can roll back</span>}</td></tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        );
      })}
    </div>
  );
}

/** Operate → Models & pipelines: failed and blocked runs with a reason and a safe action, and stale destinations. */
export function PipelineHealth({ wsId }: { wsId: string }) {
  const runs = useAsync(() => api.pipelineRuns(wsId), [wsId]);
  const mats = useAsync(() => api.materializations(wsId), [wsId]);
  const pipes = useAsync(() => api.pipelines(wsId), [wsId]);
  const failures = (runs.data ?? []).filter((r) => r.status === "failed" || r.status === "blocked");
  const now = Date.now();
  const stale = (mats.data ?? []).filter((m) => m.status === "promoted").map((m) => {
    const p = (pipes.data ?? []).find((x) => x.id === m.pipeline_id);
    const limit = (p?.spec as Spec | undefined)?.freshness?.max_age_hours;
    const age = m.promoted_at ? (now - Date.parse(m.promoted_at)) / 3_600_000 : null;
    return { m, limit, age };
  }).filter((x) => x.limit && x.age !== null && x.age > x.limit);
  return (
    <section className="card" aria-label="Pipeline health">
      <div className="card-body stack">
        <h2 className="h-sm">Pipelines</h2>
        <ErrorBox error={runs.error ?? mats.error ?? pipes.error} />
        {(runs.loading && !runs.data) && <Loading />}
        {runs.data && failures.length === 0 && stale.length === 0 && <Notice tone="success">No failed runs and no stale destinations.</Notice>}
        {failures.length > 0 && (
          <ul className="list" aria-label="Failed and blocked pipeline runs">
            {failures.map((r) => (
              <li key={r.id} className="assertion">
                <div className="chip-row"><strong>{r.pipeline_name} v{r.pipeline_version}</strong> <StatusBadge status={r.status} /> <span className="small muted">{fmtDate(r.created_at)}</span></div>
                <span className="small">{r.error ?? "no reason recorded"}</span>
                <span className="small muted">{r.status === "blocked" ? "Nothing was written; the last good version keeps serving." : "Nothing was promoted."} Safe next step: fix the
                  cause, then dry-run again from <Link to={to.work(wsId, "prepare", { pipeline: r.pipeline_id })}>Investigations → Prepare data</Link>; no automatic retry.</span>
              </li>
            ))}
          </ul>
        )}
        {stale.length > 0 && (
          <ul className="list" aria-label="Stale destinations">
            {stale.map(({ m, limit, age }) => (
              <li key={m.id} className="assertion">
                <div className="chip-row"><code>{m.schema_name}.{m.table_name}</code> <Tag tone="warning">stale</Tag></div>
                <span className="small">Version {m.version} was promoted {fmtNumber(age, 0)} hours ago; the pipeline promises {limit} hours.</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
