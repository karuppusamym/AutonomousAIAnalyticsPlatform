import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, saveBlob, type ClassBaseline, type RequestClass, type ValueReport, type ValueSummary } from "../api";
import { useAuth } from "../auth";
import { canvasSupported, EChart } from "../components/Chart";
import { Card, DataTable, EmptyState, ErrorBox, Loading, Notice, PageHeader, Stat, Tabs, Value } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { CLASS_LABELS } from "./Desk";

type Period = "7" | "30" | "90";
const CLASSES: RequestClass[] = ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8", "R9"];

/** Seconds as the shortest readable unit; null stays unknown (never 0). */
export function fmtDuration(s: number | null | undefined): string {
  if (s == null || Number.isNaN(s)) return "unknown";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  if (s < 172_800) return `${(s / 3600).toFixed(1)} h`;
  return `${(s / 86_400).toFixed(1)} d`;
}


function range(period: Period): { from: string; to: string } {
  const end = new Date();
  const start = new Date(end.getTime() - Number(period) * 86_400_000);
  return { from: start.toISOString(), to: end.toISOString() };
}

/**
 * Overview → Value (spec S11-T7, S13 §3.6, FR-053): how much analyst work the Request Desk took on. Headline numbers
 * are computed live for the period; the trend reads the nightly rollup. Minutes saved rest on class baselines and say
 * so: a class without a baseline adds nothing rather than a guess.
 */
export function ValuePage() {
  const { wsId = "" } = useParams();
  const { user } = useAuth();
  const [period, setPeriod] = useState<Period>("30");
  const window_ = useMemo(() => range(period), [period]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const report = useAsync(() => api.value(wsId, window_), [wsId, window_]);
  const baselines = useAsync(() => api.valueBaselines(wsId), [wsId]);
  const action = useAction();
  const analyst = roleAtLeast(ws.data?.role, "analyst") || !!user?.is_admin;
  const owner = ws.data?.role === "owner" || !!user?.is_admin;
  const r = report.data;

  const refresh = async () => {
    if (await action.run(() => api.refreshValue(wsId, window_))) await report.reload();
  };
  const exportCsv = async () => {
    const file = await action.run(() => api.exportValue(wsId, window_));
    if (file) saveBlob(file);
  };

  return (
    <div className="page">
      <PageHeader title="Value"
        subtitle="How much of the analysts' request load the desk handled: what was answered automatically, how fast, and the analyst time it is estimated to have saved."
        actions={<>
          {analyst && <button type="button" className="btn btn-sm" disabled={action.busy} onClick={refresh}>Recompute the trend now</button>}
          <button type="button" className="btn btn-sm" disabled={action.busy} onClick={exportCsv}>Export CSV</button>
        </>} />
      <Tabs<Period> value={period} onChange={setPeriod}
        tabs={[{ id: "7", label: "Last 7 days" }, { id: "30", label: "Last 30 days" }, { id: "90", label: "Last 90 days" }]} />
      <ErrorBox error={report.error ?? action.error} onRetry={report.reload} />
      {report.loading && !r && <Loading />}
      {r && r.n === 0 && <EmptyState title="No requests in this period"
        action={<Link className="btn btn-sm" to={to.desk(wsId)}>Open the request desk</Link>}>
        The numbers appear once people send requests to the desk.
      </EmptyState>}
      {r && r.n > 0 && <>
        <Headline r={r} />
        {r.truncated && <Notice tone="warning">More requests than one report reads; the numbers cover the first 50,000 of the period.</Notice>}
        <Trend r={r} />
        <ByClass r={r} />
      </>}
      <Baselines wsId={wsId} rows={baselines.data ?? []} error={baselines.error} owner={owner} onSaved={() => { void baselines.reload(); void report.reload(); }} />
      <Card title="How these numbers are counted" label="Definitions">
        <ul className="list small">
          <li><strong>Auto-resolution rate</strong>: closed requests whose accepted answer came from the desk (directly, or prepared and approved by an analyst), out of all closed requests. Linked duplicates are not counted.</li>
          <li><strong>Accepted rate</strong>: accepted out of closed (accepted or declined).</li>
          <li><strong>Time to answer</strong>: from the request to its first answer, the median (p50) and the slowest 5% (p95).</li>
          <li><strong>Minutes saved (estimate)</strong>: for each accepted request, its class baseline minus the analyst minutes recorded on it. Baselines come from the practitioner protocol; a class without one is listed as not estimated.</li>
          <li><strong>Trend</strong>: the nightly rollup, by the day requests were created; recent days are recomputed each night as requests close.</li>
        </ul>
      </Card>
    </div>
  );
}

function Headline({ r }: { r: ValueReport }) {
  return (
    <Card label="Headline">
      <div className="stats-row">
        <Stat label="Requests" value={r.n} hint={r.duplicates ? `${r.duplicates} linked duplicate${r.duplicates === 1 ? "" : "s"} not counted` : undefined} />
        <Stat label="Auto-resolution rate" value={<Value value={r.auto_resolution_rate} format="pct" digits={0} unknownLabel="none closed yet" />}
          hint={`${r.auto_resolved} of ${r.closed} closed`} />
        <Stat label="Accepted rate" value={<Value value={r.accepted_rate} format="pct" digits={0} unknownLabel="none closed yet" />} />
        <Stat label="Time to answer (p50)" value={fmtDuration(r.tta_p50_seconds)} hint={`p95 ${fmtDuration(r.tta_p95_seconds)}`} />
        <Stat label="Analyst minutes" value={r.human_minutes} />
        <Stat label="Minutes saved (estimate)" value={Math.round(r.est_minutes_saved)}
          hint={r.unbaselined ? `${r.unbaselined} accepted request${r.unbaselined === 1 ? "" : "s"} in classes without a baseline not estimated` : "against class baselines"} />
        <Stat label="Model cost" value={<Value value={r.cost_usd} format="usd" />} />
      </div>
    </Card>
  );
}

function Trend({ r }: { r: ValueReport }) {
  const days = r.daily;
  const option = useMemo(() => ({
    tooltip: { trigger: "axis" },
    legend: { data: ["Requests", "Auto-resolution rate"] },
    grid: { left: 48, right: 48, top: 36, bottom: 32 },
    xAxis: { type: "category", data: days.map((d) => d.day) },
    yAxis: [{ type: "value", name: "Requests", minInterval: 1 }, { type: "value", name: "%", min: 0, max: 100 }],
    series: [
      { name: "Requests", type: "bar", data: days.map((d) => d.n) },
      { name: "Auto-resolution rate", type: "line", yAxisIndex: 1, connectNulls: false,
        data: days.map((d) => (d.auto_resolution_rate == null ? null : Math.round(d.auto_resolution_rate * 100))) },
    ],
  }), [days]);
  return (
    <Card title="Trend" label="Trend"
      actions={<span className="small muted">{r.rollup_computed_at ? `rollup ${fmtDate(r.rollup_computed_at)}` : "no rollup yet"}</span>}>
      {days.length === 0 ? <p className="muted">The nightly rollup has not covered this period yet. An analyst can recompute it now.</p> : <>
        {canvasSupported() && <EChart option={option} height={260} label="Requests per day and auto-resolution rate" />}
        <details open={!canvasSupported()}><summary>Table</summary>
          <DataTable caption="Daily rollup" columns={["Day", "Requests", "Closed", "Auto-resolution %", "p50 time to answer", "Analyst minutes", "Minutes saved"]}
            rows={days.map((d) => [d.day, d.n, d.closed, d.auto_resolution_rate == null ? "—" : Math.round(d.auto_resolution_rate * 100),
              fmtDuration(d.tta_p50_seconds), d.human_minutes, Math.round(d.est_minutes_saved)])} />
        </details>
      </>}
    </Card>
  );
}

function ByClass({ r }: { r: ValueReport }) {
  const rows = Object.entries(r.by_class) as [string, ValueSummary & { baseline_minutes: number | null }][];
  return (
    <Card title="By request class" label="By request class">
      <DataTable caption="Value by request class"
        columns={["Class", "Requests", "Closed", "Auto-resolution %", "Accepted %", "p50 time to answer", "Analyst minutes", "Baseline (min)", "Minutes saved"]}
        rows={rows.map(([cls, s]) => [
          `${cls}${CLASS_LABELS[cls] ? ` · ${CLASS_LABELS[cls]}` : ""}`, s.n, s.closed,
          s.auto_resolution_rate == null ? "—" : Math.round(s.auto_resolution_rate * 100),
          s.accepted_rate == null ? "—" : Math.round(s.accepted_rate * 100),
          fmtDuration(s.tta_p50_seconds), s.human_minutes, s.baseline_minutes ?? "not set",
          s.baseline_minutes == null ? "not estimated" : Math.round(s.est_minutes_saved),
        ])} />
    </Card>
  );
}

function Baselines({ wsId, rows, error, owner, onSaved }: {
  wsId: string; rows: ClassBaseline[]; error: string | null; owner: boolean; onSaved: () => void;
}) {
  const action = useAction();
  const [cls, setCls] = useState<RequestClass>("R1");
  const [minutes, setMinutes] = useState("");
  const save = async () => {
    const m = Number(minutes);
    if (await action.run(() => api.putValueBaseline({ workspace_id: wsId, class: cls, baseline_minutes: m, source: "practitioner_protocol" }))) {
      setMinutes("");
      onSaved();
    }
  };
  return (
    <Card title="Class baselines" label="Class baselines">
      <ErrorBox error={error} />
      <p className="small muted">Minutes an analyst takes for one request of the class without the desk, measured with the practitioner protocol.</p>
      {rows.length === 0 ? <p className="muted">No baselines yet, so no minutes-saved estimate.</p>
        : <DataTable caption="Baselines" columns={["Class", "Minutes", "Source", "Measured"]}
          rows={rows.map((b) => [`${b.class} · ${CLASS_LABELS[b.class]}`, b.baseline_minutes, b.source.replace(/_/g, " "), fmtDate(b.measured_at)])} />}
      {owner && <div className="toolbar">
        <label className="field"><span>Class</span>
          <select value={cls} onChange={(e) => setCls(e.target.value as RequestClass)}>
            {CLASSES.map((c) => <option key={c} value={c}>{c} · {CLASS_LABELS[c]}</option>)}
          </select>
        </label>
        <label className="field"><span>Baseline minutes</span>
          <input type="number" min={1} max={43200} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
        </label>
        <button type="button" className="btn btn-sm" disabled={action.busy || !(Number(minutes) > 0)} onClick={save}>Save baseline</button>
      </div>}
      <ErrorBox error={action.error} />
    </Card>
  );
}
