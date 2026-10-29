import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import * as echarts from "echarts/core";
import { GraphChart } from "echarts/charts";
import {
  api, saveBlob, type ProcessAnalysis, type ProcessCandidate, type ProcessEdge, type ProcessMapping, type ProcessTablesResult, type SavedProcessAnalysis,
} from "../api";
import { DARK, LIGHT } from "../lib/charts";
import { fmtDate } from "../lib/format";
import { useAction, useAsync, usePrefersDark } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { canvasSupported, EChart } from "./Chart";
import { Card, CodeBlock, EmptyState, ErrorBox, Field, Loading, Notice, Stat, Tag, TechnicalDetails } from "./ui";

echarts.use([GraphChart]);

/** Plain duration, the same wording as the analysis sentences (skills/process_mining.py `fmt_hours`). */
export function fmtHours(h: number | null | undefined): string {
  if (h === null || h === undefined || Number.isNaN(h)) return "n/a";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(1)} h`;
  return `${(h / 24).toFixed(1)} days`;
}

const pct = (x: number | null | undefined) => (x === null || x === undefined ? "n/a" : `${Math.round(x * 100)}%`);
const n = (x: number) => x.toLocaleString("en-US");

/**
 * Layers for the process map: a depth-first walk from the start steps (most frequent transitions first) keeps
 * every edge except those back to a step still on the walk, so the main flow runs top to bottom and loops
 * (rework) are the curved edges going back up. An activity's depth is its longest path from a start in that
 * acyclic backbone.
 */
export function mapLayers(activities: string[], edges: ProcessEdge[], starts: string[] = []): Map<string, number> {
  const outs = new Map<string, string[]>(activities.map((a) => [a, []]));
  for (const e of [...edges].sort((a, b) => b.count - a.count || a.source.localeCompare(b.source) || a.target.localeCompare(b.target))) {
    if (e.source !== e.target && outs.has(e.source) && outs.has(e.target)) outs.get(e.source)!.push(e.target);
  }
  const incoming = new Set(edges.filter((e) => e.source !== e.target).map((e) => e.target));
  const roots = [...starts, ...activities.filter((a) => !incoming.has(a)), ...activities].filter((a) => outs.has(a));
  const out = new Map<string, Set<string>>(activities.map((a) => [a, new Set<string>()]));
  const state = new Map<string, "open" | "done">();
  const walk = (a: string) => {
    state.set(a, "open");
    for (const t of outs.get(a)!) {
      if (state.get(t) === "open") continue;  // a loop back: drawn, but not part of the backbone
      out.get(a)!.add(t);
      if (!state.has(t)) walk(t);
    }
    state.set(a, "done");
  };
  roots.forEach((r) => { if (!state.has(r)) walk(r); });
  const depth = new Map<string, number>();
  const visit = (a: string, stack: Set<string>): number => {
    if (depth.has(a)) return depth.get(a)!;
    stack.add(a);
    let d = 0;
    for (const [src, targets] of out) if (targets.has(a) && !stack.has(src)) d = Math.max(d, visit(src, stack) + 1);
    stack.delete(a);
    depth.set(a, d);
    return d;
  };
  activities.forEach((a) => visit(a, new Set()));
  return depth;
}

function processMapOption(a: ProcessAnalysis, minCases: number, dark: boolean): object {
  const pal = dark ? DARK : LIGHT;
  const edges = a.edges.filter((e) => e.cases >= minCases);
  const shown = new Set(edges.flatMap((e) => [e.source, e.target]));
  const acts = a.activities.filter((x) => shown.has(x.activity));
  const starts = [...acts].filter((x) => x.starts > 0).sort((x, y) => y.starts - x.starts).map((x) => x.activity);
  const depth = mapLayers(acts.map((x) => x.activity), edges, starts);
  const layers = new Map<number, string[]>();
  acts.forEach((x) => layers.set(depth.get(x.activity) ?? 0, [...(layers.get(depth.get(x.activity) ?? 0) ?? []), x.activity]));
  const maxEvents = Math.max(1, ...acts.map((x) => x.events));
  const maxCount = Math.max(1, ...edges.map((e) => e.count));
  const pos = new Map<string, [number, number]>();
  layers.forEach((names, d) => names.forEach((name, i) => pos.set(name, [(i - (names.length - 1) / 2) * 220, d * 110])));
  return {
    tooltip: { trigger: "item", backgroundColor: pal.tooltipBg, textStyle: { color: pal.text } },
    series: [{
      type: "graph", layout: "none", roam: true, draggable: true, edgeSymbol: ["none", "arrow"], edgeSymbolSize: 9,
      label: { show: true, color: pal.text, fontSize: 12, formatter: "{b}" },
      edgeLabel: { show: true, fontSize: 10, color: pal.textMuted, formatter: (p: { data: { label?: string } }) => p.data.label ?? "" },
      data: acts.map((x) => ({
        name: x.activity, x: pos.get(x.activity)![0], y: pos.get(x.activity)![1],
        symbolSize: 18 + 26 * Math.sqrt(x.events / maxEvents),
        itemStyle: { color: pal.series[0] },
        tooltip: { formatter: `${x.activity}<br/>${n(x.events)} events in ${n(x.cases)} cases` },
      })),
      links: edges.map((e) => {
        const back = (depth.get(e.target) ?? 0) <= (depth.get(e.source) ?? 0);
        return {
          source: e.source, target: e.target,
          label: e.count >= maxCount * 0.05 ? `${n(e.count)} · ${fmtHours(e.median_hours)}` : "",
          lineStyle: { width: 1 + 7 * (e.count / maxCount), color: back ? pal.series[1] : pal.textMuted, opacity: 0.75,
            curveness: e.source === e.target ? 0.8 : back ? 0.35 : 0.05 },
          tooltip: { formatter: `${e.source} → ${e.target}<br/>${n(e.count)} times in ${n(e.cases)} cases<br/>median ${fmtHours(e.median_hours)}, 1 in 10 over ${fmtHours(e.p90_hours)}` },
        };
      }),
    }],
  };
}

/** Work → Process: pick a detected event log (mapping pre-filled), analyse it, read the result in plain words. */
export function ProcessPanel({ wsId, role }: { wsId: string; role?: string | null }) {
  const canRun = roleAtLeast(role, "analyst");
  const cands = useAsync(() => (canRun ? api.processCandidates(wsId) : Promise.resolve({ version: "", candidates: [] })), [wsId, canRun]);
  const saved = useAsync(() => api.processAnalyses(wsId), [wsId]);
  const [analysis, setAnalysis] = useState<ProcessAnalysis | null>(null);
  const openSaved = useAction();
  return (
    <div className="stack">
      <p className="muted">
        Process and task mining reads an activity log (one row per step of a case: created, assigned, reassigned,
        resolved…) through the governed gateway and shows how cases really flow: the common paths, where they wait,
        rework, cancellations, handovers between teams, and steps skipped against the expected path.
      </p>
      {canRun && <>
        <ErrorBox error={cands.error} onRetry={cands.reload} />
        {cands.loading && !cands.data && <Loading label="Looking for event logs…" />}
        {cands.data && cands.data.candidates.length === 0 && (
          <EmptyState title="No event log found" action={<Link className="btn btn-sm" to={to.sources(wsId)}>Sources</Link>}>
            Select a table with a case id, an activity and a time column (an audit or activity log) in Sources, then come back.
          </EmptyState>
        )}
        {!!cands.data?.candidates.length && (
          <ProcessForm wsId={wsId} candidates={cands.data.candidates} onDone={(a) => { setAnalysis(a); if (a.artifact) void saved.reload(); }} />
        )}
        {!!cands.data?.candidates.length && <ProcessTablesCard wsId={wsId} candidate={cands.data.candidates[0]} role={role} />}
      </>}
      {!canRun && <Notice>Analysts run process analyses; you can open the saved ones below.</Notice>}
      {!!saved.data?.length && (
        <Card title="Saved process analyses" label="Saved process analyses">
          <ul className="list compact">
            {saved.data.map((s: SavedProcessAnalysis) => (
              <li key={s.id} className="list-item">
                <button type="button" className="btn btn-ghost btn-sm" disabled={openSaved.busy}
                  onClick={() => void openSaved.run(async () => {
                    const art = await api.getArtifact(s.id);
                    setAnalysis({ ...(art.content as unknown as ProcessAnalysis), artifact: { id: art.id, name: art.name, version: art.version } });
                  })}>{s.name}</button>
                <span className="muted small">v{s.version} · {s.summary ? `${n(s.summary.cases)} cases` : ""} · {fmtDate(s.updated_at ?? s.created_at)}</span>
              </li>
            ))}
          </ul>
          <ErrorBox error={openSaved.error} />
        </Card>
      )}
      {analysis && <ProcessAnalysisView wsId={wsId} analysis={analysis} />}
    </div>
  );
}

const SELECTS: [keyof ProcessMapping, string, boolean][] = [
  ["case_column", "Case", true], ["activity_column", "Activity", true], ["timestamp_column", "Time", true],
  ["resource_column", "Resource (optional)", false],
];

/** "Created → Assigned > Closed" or one step per line → the activity names in order; empty → null (the pack's or inferred path is used). */
export function parseExpectedPath(text: string): string[] | null {
  const steps = text.split(/\s*(?:→|->|>|\r?\n)\s*/).map((t) => t.trim()).filter(Boolean);
  return steps.length ? steps : null;
}

function ProcessForm({ wsId, candidates, onDone }: { wsId: string; candidates: ProcessCandidate[]; onDone: (a: ProcessAnalysis) => void }) {
  const firstValue = (c: ProcessCandidate) => (c.segments[0]?.values[0] ? String(c.segments[0].values[0].value) : "");
  const [assetId, setAssetId] = useState(candidates[0].asset_id);
  const cand = candidates.find((c) => c.asset_id === assetId) ?? candidates[0];
  const [mapping, setMapping] = useState<ProcessMapping>(cand.mapping);
  const segment = cand.segments[0];
  const [segValue, setSegValue] = useState<string>(firstValue(cand));
  const [save, setSave] = useState(true);
  const [expected, setExpected] = useState("");
  const act = useAction();
  const [last, setLast] = useState<ProcessAnalysis | null>(null);
  const pick = (id: string) => {  // a table's own suggested mapping replaces the previous one
    const next = candidates.find((c) => c.asset_id === id) ?? candidates[0];
    setAssetId(next.asset_id);
    setMapping(next.mapping);
    setSegValue(firstValue(next));
    setExpected("");
  };
  const submit = () => void act.run(async () => {
    const filters = segment && segValue !== "" ? [{ column: segment.column, op: "=" as const, value: segValue }] : [];
    const a = await api.analyzeProcess(wsId, { asset_id: cand.asset_id, ...mapping, resource_column: mapping.resource_column || null,
      filters, save, ...(parseExpectedPath(expected) ? { reference_path: parseExpectedPath(expected) } : {}) });
    setLast(a);
    onDone(a);
  });
  const id = (k: string) => `process-${k}`;
  const declared = segment?.values.find((v) => String(v.value) === segValue)?.reference_path;
  const pathHint = declared?.length ? { path: declared, by: cand.declared_by ?? "" } : null;
  return (
    <Card title="Event log" label="Event log">
      <form className="form" aria-label="Process analysis mapping" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div className="form-row">
          <Field label="Event log table" htmlFor={id("asset")}
            hint={cand.declared_by ? `Mapping declared by ${cand.declared_by}` : cand.reasons.slice(0, 2).join("; ")}>
            <select id={id("asset")} value={cand.asset_id} onChange={(e) => pick(e.target.value)}>
              {candidates.map((c) => <option key={c.asset_id} value={c.asset_id}>{c.business_name || c.name}{c.row_count ? ` (${n(c.row_count)} rows)` : ""}</option>)}
            </select>
          </Field>
          {segment && (
            <Field label="Segment" htmlFor={id("segment")} hint={`Analyse one value of ${segment.column}; each has its own process.`}>
              <select id={id("segment")} value={segValue} onChange={(e) => setSegValue(e.target.value)}>
                {segment.values.map((v) => <option key={String(v.value)} value={String(v.value)}>
                  {v.label ? `${v.label} (${String(v.value)})` : String(v.value)}{v.count ? ` · ${n(v.count)} events` : ""}</option>)}
                <option value="">All values together</option>
              </select>
            </Field>
          )}
        </div>
        <div className="form-row">
          {SELECTS.map(([key, label, required]) => (
            <Field key={key} label={label} htmlFor={id(key)}>
              <select id={id(key)} value={mapping[key] ?? ""} required={required}
                onChange={(e) => setMapping({ ...mapping, [key]: e.target.value || null })}>
                {!required && <option value="">None</option>}
                {cand.columns.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </Field>
          ))}
        </div>
        <Field label="Expected path (optional)" htmlFor={id("expected")}
          hint={pathHint ? `Leave empty to use the ${pathHint.by ? pathHint.by + " model" : "model"}: ${pathHint.path.join(" → ")}. Or type your own: activities in order, separated by →, > or new lines.`
            : "Leave empty to compare with the most common completed path. Or type the happy path: activities in order, separated by →, > or new lines."}>
          <textarea id={id("expected")} rows={2} value={expected} onChange={(e) => setExpected(e.target.value)}
            placeholder={pathHint ? pathHint.path.join(" → ") : "Created → Assigned → Worked → Closed"} />
        </Field>
        <div className="chip-row">
          <label className="field-check"><input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} /> Save to Results</label>
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Analyzing…" : "Analyze"}</button>
        </div>
        <ErrorBox error={act.error} />
        {last?.artifact && <Notice tone="success">Saved as <Link to={to.outputs(wsId, { artifact: last.artifact.id })}>{last.artifact.name}</Link> (v{last.artifact.version}).</Notice>}
      </form>
    </Card>
  );
}

const MIN_SHARES: [number, string][] = [[0, "every transition"], [0.01, "seen in 1% of cases or more"], [0.05, "seen in 5% of cases or more"],
  [0.1, "seen in 10% of cases or more"]];

/** One process analysis (fresh or saved): used in Work → Process and for a saved analysis in Outputs. */
export function ProcessAnalysisView({ analysis: a, wsId }: { analysis: ProcessAnalysis; wsId: string }) {
  const dark = usePrefersDark();
  const [minShare, setMinShare] = useState(0.01);
  const minCases = Math.max(1, Math.ceil(a.summary.cases * minShare));
  const option = useMemo(() => processMapOption(a, minCases, dark), [a, minCases, dark]);
  const shownEdges = a.edges.filter((e) => e.cases >= minCases);
  const cf = a.conformance;
  const maxBin = Math.max(1, ...a.throughput.histogram.map((b) => b.cases));
  const download = () => saveBlob({ blob: new Blob([JSON.stringify(a, null, 2)], { type: "application/json" }),
    filename: `${(a.title || "process-analysis").replace(/[^A-Za-z0-9._-]+/g, "_")}.json` });
  return (
    <section className="stack" aria-label="Process analysis">
      <Card title={a.title} actions={<>
        {a.artifact && <Link className="btn btn-sm btn-ghost" to={to.outputs(wsId, { artifact: a.artifact.id })}>Open in Results</Link>}
        <button type="button" className="btn btn-sm" onClick={download}>Download JSON</button></>}>
        <div className="stats-row">
          <Stat label="cases" value={n(a.summary.cases)} />
          <Stat label="events" value={n(a.summary.events)} />
          <Stat label="different paths" value={n(a.summary.variants)} />
          <Stat label="median first → last step" value={fmtHours(a.summary.median_hours)} />
          <Stat label="1 in 10 takes longer than" value={fmtHours(a.summary.p90_hours)} />
          <Stat label="follow the expected path" value={pct(a.summary.fitness)} hint={`of ${n(cf.completed_cases)} completed cases`} />
        </div>
        <ul aria-label="Highlights">
          {a.highlights.map((h) => <li key={h}>{h}</li>)}
        </ul>
        {a.provenance?.coverage?.truncated && <Notice tone="warning">Only part of the log was read ({a.provenance.coverage.note}).</Notice>}
      </Card>

      <Card title="Process map" label="Process map" actions={
        <label className="small">Show <select aria-label="Transitions shown" value={minShare} onChange={(e) => setMinShare(Number(e.target.value))}>
          {MIN_SHARES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>}>
        <p className="muted small">Circles are steps (bigger = more events); arrows are “then directly”, thicker = more often, labelled with how
          often and the median wait. Orange arrows go back to an earlier step (rework).</p>
        {canvasSupported()
          ? <EChart option={option} height={Math.min(640, Math.max(360, 70 * (new Set(shownEdges.map((e) => e.target)).size + 1)))}
            label={`Process map of ${a.summary.activities} steps and ${shownEdges.length} transitions`} />
          : null}
        <details open={!canvasSupported()}>
          <summary>Transitions as a table ({shownEdges.length})</summary>
          <div className="table-wrap">
            <table className="table">
              <caption className="sr-only">Transitions</caption>
              <thead><tr><th>From</th><th>Then</th><th className="num">Times</th><th className="num">Cases</th><th className="num">Median wait</th><th className="num">1 in 10 over</th></tr></thead>
              <tbody>{shownEdges.map((e) => <tr key={`${e.source}>${e.target}`}><td>{e.source}</td><td>{e.target}</td><td className="num">{n(e.count)}</td>
                <td className="num">{n(e.cases)}</td><td className="num">{fmtHours(e.median_hours)}</td><td className="num">{fmtHours(e.p90_hours)}</td></tr>)}</tbody>
            </table>
          </div>
        </details>
      </Card>

      <Card title="Most common paths" label="Most common paths">
        <div className="table-wrap">
          <table className="table">
            <caption className="sr-only">Most common paths</caption>
            <thead><tr><th>#</th><th>Path</th><th className="num">Cases</th><th className="num">Share</th><th className="num">Median duration</th></tr></thead>
            <tbody>{a.variants.top.map((v) => (
              <tr key={v.rank}>
                <td>{v.rank}</td>
                <td>{v.activities.join(" → ")} {v.happy_path && <Tag tone="success">expected path</Tag>}</td>
                <td className="num">{n(v.cases)}</td><td className="num">{pct(v.share)}</td><td className="num">{fmtHours(v.median_hours)}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
        <p className="muted small">{n(a.variants.total)} paths in all; the ones not listed cover {n(a.variants.other_cases)} cases ({pct(a.variants.other_share)}).</p>
      </Card>

      <div className="grid-2">
        <Card title="Where cases wait" label="Where cases wait">
          {a.bottlenecks.length ? <ol>{a.bottlenecks.map((b) => <li key={`${b.source}>${b.target}`}>{b.sentence}</li>)}</ol>
            : <EmptyState title="No waiting between steps" />}
        </Card>
        <Card title="Throughput" label="Throughput">
          <p>Half the cases take up to <strong>{fmtHours(a.throughput.median_hours)}</strong> from first to last step; 1 in 10 more than <strong>{fmtHours(a.throughput.p90_hours)}</strong>.</p>
          <ul aria-label="Cases by duration" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {a.throughput.histogram.map((b) => (
              <li key={b.label} style={{ display: "grid", gridTemplateColumns: "6.5rem minmax(0, 1fr) 4rem", alignItems: "center", gap: "0.5rem" }}>
                <span className="small">{b.label}</span>
                <span aria-hidden="true" style={{ display: "block", height: "0.7rem", borderRadius: 3, background: "var(--accent, #2a78d6)",
                  width: `${(100 * b.cases) / maxBin}%`, minWidth: b.cases ? 2 : 0 }} />
                <span className="small num">{n(b.cases)}</span></li>))}
          </ul>
          <p className="muted small">By last step: {a.throughput.by_end_activity.slice(0, 4).map((e) => `${e.activity} ${n(e.cases)} (median ${fmtHours(e.median_hours)})`).join("; ")}</p>
        </Card>
        <Card title="Rework" label="Rework">
          <p>{n(a.rework.cases)} cases ({pct(a.rework.share)}) repeat a step.</p>
          <ul>{a.rework.activities.slice(0, 6).map((r) => <li key={r.activity}>‘{r.activity}’ repeats in {n(r.cases)} cases ({pct(r.share)}), {n(r.extra_events)} extra times</li>)}</ul>
        </Card>
        <Card title="Cancellations" label="Cancellations">
          {a.cancellations.cases ? <>
            <p>{n(a.cancellations.cases)} cases ({pct(a.cancellations.share)}) were cancelled after a median {fmtHours(a.cancellations.median_hours_to_cancel)}.</p>
            <ul>{a.cancellations.after.map((c) => <li key={c.activity}>after ‘{c.activity}’: {n(c.cases)} ({pct(c.share)})</li>)}</ul>
          </> : <p className="muted">No case ended in {a.cancellations.activities.length ? a.cancellations.activities.join(" or ") : "a cancellation step"}.</p>}
        </Card>
      </div>

      <Card title="Conformance" label="Conformance">
        <p>Expected path ({cf.source}): <strong>{cf.reference.join(" → ")}</strong></p>
        <p>{n(cf.conforming_cases)} of {n(cf.completed_cases)} completed cases ({pct(cf.fitness)}) follow it exactly; {n(cf.excluded.open)} still open
          and {n(cf.excluded.cancelled)} cancelled cases are not judged.</p>
        {cf.deviations.length ? <ul>{cf.deviations.slice(0, 8).map((d) => <li key={`${d.kind}:${d.activity}`}>{d.sentence}</li>)}</ul>
          : <p className="muted">No deviation.</p>}
      </Card>

      <Card title="Handovers between teams" label="Handovers">
        {a.handovers.pairs.length ? <>
          <p>{n(a.handovers.cases_with_handover)} cases ({pct(a.handovers.share)}) change hands at least once.</p>
          <div className="table-wrap">
            <table className="table">
              <caption className="sr-only">Handovers</caption>
              <thead><tr><th>From</th><th>To</th><th className="num">Times</th><th className="num">Cases</th></tr></thead>
              <tbody>{a.handovers.pairs.slice(0, 10).map((p) => <tr key={`${p.source}>${p.target}`}><td>{p.source}</td><td>{p.target}</td>
                <td className="num">{n(p.count)}</td><td className="num">{n(p.cases)}</td></tr>)}</tbody>
            </table>
          </div>
          {!!a.handovers.ping_pong.length && <p>Ping-pong (handed back and forth): {a.handovers.ping_pong.slice(0, 3)
            .map((p) => `${p.a} ⇄ ${p.b} in ${n(p.cases)} cases`).join("; ")}.</p>}
        </> : <p className="muted">{a.mapping.resource_column ? "No case changed hands." : "Choose a resource column to see handovers."}</p>}
      </Card>

      <TechnicalDetails label="How this was computed">
        <p className="small">{a.provenance.method}. {n(a.provenance.coverage.events_read)} events in {a.provenance.coverage.pages} gateway
          {a.provenance.coverage.pages === 1 ? " query" : " queries"} ({a.provenance.queries.join(", ")}); {a.version}; computed {fmtDate(a.provenance.computed_at)}.</p>
        <p className="small">Mapping: case <code>{a.mapping.case_column}</code>, activity <code>{a.mapping.activity_column}</code>, time <code>{a.mapping.timestamp_column}</code>
          {a.mapping.resource_column ? <>, resource <code>{a.mapping.resource_column}</code></> : null} on <code>{a.asset.fq}</code>.</p>
        <CodeBlock code={a.provenance.sql} label="First page SQL" />
      </TechnicalDetails>
    </section>
  );
}


/** Questions the rules answer from the process tables, no model (they read the tables' own column names). */
export const PROCESS_QUESTIONS = [
  "average duration hours by segment",
  "cases by segment and status",
  "average wait hours by from activity",
  "cases by follows expected path",
];

/**
 * The event log as two ordinary workspace tables (one row per case, one row per step-to-step move), so Ask, step-by-step
 * answers, investigations and their agents, metrics, monitors and dashboards all work on process data through the
 * paths they already use. Editors build or refresh them; everyone sees whether they exist and where to use them.
 */
export function ProcessTablesCard({ wsId, candidate, role }: { wsId: string; candidate: ProcessCandidate; role?: string | null }) {
  const canBuild = roleAtLeast(role, "editor");
  const catalog = useAsync(() => api.catalog(wsId, { q: candidate.name }), [wsId, candidate.name]);
  const build = useAction();
  const [built, setBuilt] = useState<ProcessTablesResult | null>(null);
  const names = [`${candidate.name}_cases`, `${candidate.name}_transitions`];
  const existing = (catalog.data ?? []).filter((a) => names.includes(a.name) && a.lifecycle === "active");
  const ready = built ? built.tables.length === 2 : existing.length === 2;
  const run = async () => {
    const r = await build.run(() => api.buildProcessTables(wsId, {
      asset_id: candidate.asset_id, ...candidate.mapping, resource_column: candidate.mapping.resource_column || null,
    }));
    if (r) {
      setBuilt(r);
      void catalog.reload();
    }
  };
  return (
    <Card title="Use this process everywhere" label="Process tables">
      <p className="muted small">
        Turns the event log into two ordinary tables of this workspace: <code>{names[0]}</code> (one row per case: duration,
        path, status, whether it followed the expected path, rework, handovers) and <code>{names[1]}</code> (one row per
        step-to-step move with its wait). Ask, step-by-step answers, investigations and their agents, metrics, monitors and
        dashboards then work on process data like any other table — read through the same gateway, profiled and described.
      </p>
      {ready ? (
        <Notice tone="success">
          {built ? <>Built {n(built.cases)} cases and {n(built.transitions)} transitions
            {built.segments.length > 1 ? ` across ${built.segments.length} kinds of case` : ""}. </> : <>The process tables exist. </>}
          <Link to={to.ask(wsId)}>Ask about them</Link> · <Link to={to.workspace(wsId)}>Start work on them</Link> ·{" "}
          <Link to={to.catalog(wsId)}>See them in the catalog</Link>
        </Notice>
      ) : (
        <p className="small">Not built yet{canBuild ? "" : ": an editor builds them"}.</p>
      )}
      {ready && (
        <div className="stack">
          <p className="small muted">Try asking (answered from the tables by rules, no model):</p>
          <ul className="small">{PROCESS_QUESTIONS.map((q) => <li key={q}>“{q}”</li>)}</ul>
        </div>
      )}
      {built?.truncated && <Notice tone="warning">The event log was larger than one read allows, so the tables cover the first cases only.</Notice>}
      {canBuild && (
        <div className="btn-row">
          <button type="button" className="btn btn-sm btn-primary" disabled={build.busy} onClick={() => void run()}>
            {build.busy ? "Building…" : ready ? "Refresh the process tables" : "Build the process tables"}
          </button>
        </div>
      )}
      <ErrorBox error={build.error} />
    </Card>
  );
}
