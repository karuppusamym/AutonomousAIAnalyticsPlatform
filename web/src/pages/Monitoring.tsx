import { useId, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { to } from "../routes";
import { api, type Alert, type Monitor } from "../api";
import { useAuth } from "../auth";
import { EChart, canvasSupported } from "../components/Chart";
import { OperateHealth } from "../components/OperateHealth";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag, TechnicalDetails } from "../components/ui";
import { buildMonitorOption, DARK, LIGHT } from "../lib/charts";
import { fmtDate, fmtNumber, fmtPct } from "../lib/format";
import { useAction, useAsync, usePrefersDark } from "../lib/hooks";
import {
  GRAINS, ML_MONITOR_KINDS, MONITOR_KINDS, OPS, buildMonitorConfig, describeMonitorConfig, emptyMonitorForm, explainTriage, monitorMessage, monitorOverlay,
  validateMonitorForm, type MonitorFormState,
} from "../lib/monitors";
import { severityTone } from "../lib/status";

type Tab = "monitors" | "alerts" | "health";
const ALERT_FILTERS = ["open", "acknowledged", "resolved", ""] as const;

/** `embedded`: shown as a section of the Autonomy Center rather than as its own page. */
export function MonitoringPage({ embedded = false }: { embedded?: boolean } = {}) {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "alerts" ? "alerts" : params.get("tab") === "health" ? "health" : "monitors";
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const { user } = useAuth();
  const role = user?.is_admin ? "owner" : ws.data?.role;
  const monitors = useAsync(() => api.listMonitors(wsId), [wsId]);
  const openAlerts = useAsync(() => api.listAlerts(wsId, "open"), [wsId]);
  const setTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    next.set("tab", t);
    if (t !== "alerts") next.delete("alert");
    setParams(next);
  };
  return (
    <div className={embedded ? "embedded-page" : "page"}>
      <PageHeader level={embedded ? 2 : 1} title="Monitors & alerts"
        subtitle="Metric thresholds, drift, change points, forecast deviations and data quality, checked on a schedule and grouped into alerts." />
      <Tabs value={tab} onChange={setTab} tabs={[
        { id: "monitors", label: `Monitors${monitors.data ? ` (${monitors.data.length})` : ""}` },
        { id: "alerts", label: `Alerts${openAlerts.data?.length ? ` (${openAlerts.data.length} open)` : ""}` },
        { id: "health", label: "Models & pipelines" },
      ]} />
      <div className="tab-panel" role="tabpanel">
        {tab === "health" ? <OperateHealth wsId={wsId} role={role} /> : tab === "monitors" ? (
          <MonitorsSection wsId={wsId} monitors={monitors.data} error={monitors.error} loading={monitors.loading} startCreating={params.get("new") === "1"}
            reload={() => { void monitors.reload(); void openAlerts.reload(); }}
            onPatched={(m) => monitors.setData((prev) => prev?.map((x) => (x.id === m.id ? m : x)))} />
        ) : (
          <AlertsSection wsId={wsId} focus={params.get("alert")} monitors={monitors.data ?? []} onChanged={() => void openAlerts.reload()} />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------------- monitors
function MonitorsSection({ wsId, monitors, error, loading, reload, onPatched, startCreating = false }: {
  wsId: string; monitors: Monitor[] | undefined; error: string | null; loading: boolean; reload: () => void; onPatched: (m: Monitor) => void;
  startCreating?: boolean;
}) {
  const [creating, setCreating] = useState(startCreating);
  return (
    <div className="stack">
      <div className="toolbar">
        <span className="muted small">Evaluate now runs the monitor immediately; scheduled evaluation uses a “Monitor evaluation” schedule.</span>
        {!creating && <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>New monitor</button>}
      </div>
      {creating && (
        <Card title="New monitor">
          <MonitorForm wsId={wsId} onCancel={() => setCreating(false)} onSaved={() => { setCreating(false); reload(); }} />
        </Card>
      )}
      <ErrorBox error={error} onRetry={reload} />
      {loading && !monitors && <Loading />}
      {monitors?.length === 0 && !creating && <EmptyState title="No monitors yet">Monitor a validated metric from a completed investigation.</EmptyState>}
      <div className="grid-2">
        {monitors?.map((m) => <MonitorCard key={m.id} monitor={m} onPatched={onPatched} onEvaluated={reload} />)}
      </div>
    </div>
  );
}

function MonitorCard({ monitor: m, onPatched, onEvaluated }: { monitor: Monitor; onPatched: (m: Monitor) => void; onEvaluated: () => void }) {
  const act = useAction();
  const [seriesKey, setSeriesKey] = useState(0);
  const [lastAlert, setLastAlert] = useState<string | null>(null);
  const patch = async (body: { enabled?: boolean; auto_investigate?: boolean }) => {
    const r = await act.run(() => api.updateMonitor(m.id, body));
    if (r) onPatched(r);
  };
  const evaluate = async () => {
    const r = await act.run(() => api.evaluateMonitor(m.id));
    if (r) {
      setLastAlert(r.alert_id ? r.alert_id : null);
      setSeriesKey((k) => k + 1);
      onEvaluated();
    }
  };
  const msg = monitorMessage(m);
  return (
    <section className="card monitor-card" aria-label={`Monitor ${m.name}`}>
      <header className="card-header">
        <h2 className="card-title">{m.name} <Tag tone="info">{[...MONITOR_KINDS, ...ML_MONITOR_KINDS].find((k) => k.id === m.kind)?.label ?? m.kind}</Tag></h2>
        <div className="card-actions"><StatusBadge status={m.state} /></div>
      </header>
      <div className="card-body">
        <p className="small muted">{describeMonitorConfig(m)}</p>
        {msg && <p className={`small ${m.state === "alerting" || m.state === "error" ? "warn-text" : ""}`}>{msg}</p>}
        {m.kind !== "data_quality" && !m.kind.startsWith("ml_") && <MonitorChart monitor={m} refreshKey={seriesKey} />}
        <p className="small muted">Last evaluated {fmtDate(m.last_evaluated_at)}</p>
        <div className="chip-row">
          <label className="switch">
            <input type="checkbox" role="switch" checked={m.enabled} disabled={act.busy} onChange={(e) => void patch({ enabled: e.target.checked })}
              aria-label={`Enable monitor ${m.name}`} />
            <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
            <span className="small">{m.enabled ? "enabled" : "disabled"}</span>
          </label>
          <label className="switch">
            <input type="checkbox" role="switch" checked={m.auto_investigate} disabled={act.busy}
              onChange={(e) => void patch({ auto_investigate: e.target.checked })} aria-label={`Auto-investigate for ${m.name}`} />
            <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
            <span className="small">auto-investigate</span>
          </label>
          <button type="button" className="btn btn-sm" onClick={() => void evaluate()} disabled={act.busy}>{act.busy ? "Evaluating…" : "Evaluate now"}</button>
        </div>
        {m.auto_investigate && <p className="field-hint">Automatic investigations start only at workspace autonomy ≥ 3 and when policy allows.</p>}
        <ErrorBox error={act.error} />
        {lastAlert && <Notice tone="warning">Alert raised — see the Alerts tab.</Notice>}
      </div>
    </section>
  );
}

function MonitorChart({ monitor: m, refreshKey }: { monitor: Monitor; refreshKey: number }) {
  const series = useAsync(() => api.monitorSeries(m.id), [m.id, refreshKey]);
  const dark = usePrefersDark();
  const points = useMemo(() => series.data?.points ?? [], [series.data]);
  const overlay = useMemo(() => monitorOverlay(m, points), [m, points]);
  const option = useMemo(() => buildMonitorOption(points, overlay, dark ? DARK : LIGHT), [points, overlay, dark]);
  if (series.error) return <p className="small muted">Series unavailable: {series.error}</p>;
  if (!series.data) return series.loading ? <Loading label="Loading series…" /> : null;
  if (!points.length) return <p className="small muted">No periods yet.</p>;
  const [lastP, lastV] = points[points.length - 1];
  const caption = `${series.data.label} per ${series.data.grain}: latest ${lastP} = ${fmtNumber(lastV)}`
    + (overlay.baselineMedian != null ? ` · baseline median ${fmtNumber(overlay.baselineMedian)}` : "")
    + (overlay.threshold ? ` · threshold ${overlay.threshold.op} ${overlay.threshold.value}` : "");
  return (
    <figure className="monitor-chart">
      {option && canvasSupported() ? <EChart option={option} height={160} label={caption} /> : null}
      <figcaption className="small muted">{caption}</figcaption>
    </figure>
  );
}

export function MonitorForm({ wsId, onSaved, onCancel }: { wsId: string; onSaved: (m: Monitor) => void; onCancel?: () => void }) {
  const id = useId();
  const [f, setF] = useState<MonitorFormState>(emptyMonitorForm);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [submitted, setSubmitted] = useState(false);
  const act = useAction();
  const metrics = useAsync(() => api.listArtifacts(wsId, { type: "metric" }), [wsId]);
  const assets = useAsync(() => (f.kind === "data_quality" ? api.listAssets(wsId) : Promise.resolve([])), [wsId, f.kind]);
  const metricOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const a of metrics.data ?? []) {
      if (!seen.has(a.name)) seen.set(a.name, String((a.content?.display_name as string | undefined) ?? a.name));
    }
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [metrics.data]);
  const set = (patch: Partial<MonitorFormState>) => {
    const next = { ...f, ...patch };
    setF(next);
    if (submitted) setErrors(validateMonitorForm(next));
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const errs = validateMonitorForm(f);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const saved = await act.run(() => api.createMonitor(wsId, {
      name: f.name.trim(), kind: f.kind, config: buildMonitorConfig(f), auto_investigate: f.autoInvestigate,
    }));
    if (saved) onSaved(saved);
  };
  const err = (k: string) => errors[k] && <div className="field-error" role="alert">{errors[k]}</div>;
  const isMetric = f.kind !== "data_quality";
  return (
    <form className="form" onSubmit={submit} noValidate aria-label="New monitor">
      <div className="form-row">
        <Field label="Name" htmlFor={`${id}-name`}>
          <input id={`${id}-name`} value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Weekly MTTR drift" />
          {err("name")}
        </Field>
        <Field label="Kind" htmlFor={`${id}-kind`} hint={MONITOR_KINDS.find((k) => k.id === f.kind)?.description}>
          <select id={`${id}-kind`} value={f.kind} onChange={(e) => set({ kind: e.target.value as MonitorFormState["kind"] })}>
            {MONITOR_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
      </div>
      {isMetric && (
        <div className="form-row">
          <Field label="Metric" htmlFor={`${id}-metric`} hint={metrics.data && !metricOptions.length ? "No validated metrics yet: complete an investigation first." : undefined}>
            <select id={`${id}-metric`} value={f.metric} onChange={(e) => set({ metric: e.target.value })}>
              <option value="">Choose a metric…</option>
              {metricOptions.map(([name, display]) => <option key={name} value={name}>{display}{display !== name ? ` (${name})` : ""}</option>)}
            </select>
            {err("metric")}
          </Field>
          <Field label="Grain" htmlFor={`${id}-grain`}>
            <select id={`${id}-grain`} value={f.grain} onChange={(e) => set({ grain: e.target.value as MonitorFormState["grain"] })}>
              {GRAINS.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          </Field>
        </div>
      )}
      <ErrorBox error={metrics.error} />
      {f.kind === "metric_threshold" && (
        <div className="form-row">
          <Field label="Operator" htmlFor={`${id}-op`}>
            <select id={`${id}-op`} value={f.op} onChange={(e) => set({ op: e.target.value as MonitorFormState["op"] })}>
              {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          </Field>
          <Field label="Value" htmlFor={`${id}-value`}>
            <input id={`${id}-value`} type="number" step="any" value={f.value} onChange={(e) => set({ value: e.target.value })} />
            {err("value")}
          </Field>
          <Field label="Severity" htmlFor={`${id}-sev`} hint="The decision model may raise it, never lower it.">
            <select id={`${id}-sev`} value={f.severity} onChange={(e) => set({ severity: e.target.value as MonitorFormState["severity"] })}>
              <option value="info">info</option><option value="warning">warning</option><option value="critical">critical</option>
            </select>
          </Field>
        </div>
      )}
      {f.kind === "metric_drift" && (
        <div className="form-row">
          <Field label="Lookback periods" htmlFor={`${id}-lb`}>
            <input id={`${id}-lb`} type="number" min={4} value={f.lookback} onChange={(e) => set({ lookback: e.target.value })} />
            {err("lookback")}
          </Field>
          <Field label="Robust z threshold" htmlFor={`${id}-z`} hint="Critical at twice the threshold.">
            <input id={`${id}-z`} type="number" step="0.1" min={0} value={f.zThreshold} onChange={(e) => set({ zThreshold: e.target.value })} />
            {err("zThreshold")}
          </Field>
        </div>
      )}
      {f.kind === "change_point" && (
        <Field label="Recent periods" htmlFor={`${id}-rp`} hint="Only a change inside this window raises an alert.">
          <input id={`${id}-rp`} type="number" min={1} value={f.recentPeriods} onChange={(e) => set({ recentPeriods: e.target.value })} />
          {err("recentPeriods")}
        </Field>
      )}
      {f.kind === "forecast_deviation" && (
        <div className="form-row">
          <Field label="Interval z" htmlFor={`${id}-fz`} hint="Width of the forecast interval in standard deviations (2.5 ≈ 98.8%). Critical at twice this.">
            <input id={`${id}-fz`} type="number" step="0.1" min={0} value={f.forecastZ} onChange={(e) => set({ forecastZ: e.target.value })} />
            {err("forecastZ")}
          </Field>
          <Field label="History periods" htmlFor={`${id}-hist`} hint="How many recent periods the forecast is fitted on.">
            <input id={`${id}-hist`} type="number" min={4} value={f.history} onChange={(e) => set({ history: e.target.value })} />
            {err("history")}
          </Field>
          <Field label="Season length (optional)" htmlFor={`${id}-season`} hint="e.g. 7 for daily data with a weekly cycle; empty = inferred from the grain.">
            <input id={`${id}-season`} type="number" min={2} value={f.seasonalPeriods} onChange={(e) => set({ seasonalPeriods: e.target.value })} />
            {err("seasonalPeriods")}
          </Field>
        </div>
      )}
      {f.kind === "data_quality" && (
        <fieldset className="autonomy">
          <legend>Assets (none selected = every asset in scope)</legend>
          <ErrorBox error={assets.error} />
          <div className="toggle-group">
            {(assets.data ?? []).filter((a) => a.selected).map((a) => (
              <label key={a.id} className="toggle small">
                <input type="checkbox" checked={f.assets.includes(a.fq)}
                  onChange={(e) => set({ assets: e.target.checked ? [...f.assets, a.fq] : f.assets.filter((x) => x !== a.fq) })} /> {a.fq}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <label className="toggle">
        <input type="checkbox" checked={f.autoInvestigate} onChange={(e) => set({ autoInvestigate: e.target.checked })} />
        Auto-investigate material alerts (needs autonomy ≥ 3)
      </label>
      <ErrorBox error={act.error} />
      <div className="form-actions">
        {onCancel && <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>}
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Creating…" : "Create monitor"}</button>
      </div>
    </form>
  );
}

// ---------------------------------------------------------------------------------- alerts
function AlertsSection({ wsId, focus, monitors, onChanged }: { wsId: string; focus: string | null; monitors: Monitor[]; onChanged: () => void }) {
  const [status, setStatus] = useState<(typeof ALERT_FILTERS)[number]>(focus ? "" : "open");
  const alerts = useAsync(() => api.listAlerts(wsId, status || undefined), [wsId, status]);
  const names = useMemo(() => new Map(monitors.map((m) => [m.id, m.name])), [monitors]);
  return (
    <div className="stack">
      <div className="chip-row" role="toolbar" aria-label="Filter alerts by status">
        {ALERT_FILTERS.map((s) => (
          <button key={s || "all"} type="button" className={`chip ${status === s ? "active" : ""}`} aria-pressed={status === s} onClick={() => setStatus(s)}>
            {s || "all"}</button>
        ))}
      </div>
      <ErrorBox error={alerts.error} onRetry={alerts.reload} />
      {alerts.loading && !alerts.data && <Loading />}
      {alerts.data?.length === 0 && <EmptyState title={status ? `No ${status} alerts` : "No alerts"} />}
      <ul className="alert-list">
        {alerts.data?.map((a) => (
          <AlertItem key={a.id} wsId={wsId} alert={a} monitorName={a.monitor_id ? names.get(a.monitor_id) : undefined} highlighted={focus === a.id}
            onChanged={(u) => { alerts.setData((prev) => prev?.map((x) => (x.id === u.id ? u : x))); onChanged(); }} />
        ))}
      </ul>
    </div>
  );
}

export function AlertItem({ wsId, alert: a, monitorName, highlighted = false, onChanged }: {
  wsId: string; alert: Alert; monitorName?: string; highlighted?: boolean; onChanged: (a: Alert) => void;
}) {
  const act = useAction();
  const navigate = useNavigate();
  const [note, setNote] = useState<string | null>(null);
  const tone = severityTone(a.severity);
  const triage = a.data?.triage;
  const explanation = explainTriage(a);
  const doAction = async (action: "acknowledge" | "resolve") => {
    const r = await act.run(() => api.alertAction(a.id, action));
    if (r) onChanged(r);
  };
  const investigate = async () => {
    const r = await act.run(() => api.investigateAlert(a.id));
    if (r === undefined) return;
    if (r.run_id) {
      onChanged({ ...a, investigation_run_id: r.run_id });
      navigate(to.run(wsId, r.run_id));
    } else {
      setNote("Investigation was not started (policy or autonomy does not allow it).");
    }
  };
  return (
    <li className={`card alert-item alert-sev-${tone} ${highlighted ? "row-active" : ""}`} aria-label={`Alert ${a.title}`}>
      <div className="card-body">
        <div className="toolbar">
          <div className="chip-row">
            <Tag tone={tone}>{a.severity}</Tag>
            <strong>{a.title}</strong>
            <StatusBadge status={a.status} />
          </div>
          <span className="muted small">{fmtDate(a.created_at)}</span>
        </div>
        <p className="small">{a.message}</p>
        <div className="chip-row small">
          {monitorName && <span className="muted">monitor: {monitorName}</span>}
          {a.investigation_run_id && <Link to={to.run(wsId, a.investigation_run_id)}>Investigation</Link>}
          {a.resolved_at && <span className="muted">resolved {fmtDate(a.resolved_at)}</span>}
        </div>
        <div className="triage" aria-label="Triage explanation">
          <p className="small triage-title"><strong>Why this severity</strong>{explanation.escalated && <Tag tone="warning">raised</Tag>}</p>
          <ul className="small">{explanation.lines.map((l) => <li key={l}>{l}</li>)}</ul>
          {typeof triage?.p_material === "number" && (
            <TechnicalDetails><p className="small">JEV triage{triage.model ? ` by ${triage.model}` : ""}: <Tag tone="jev">p(material) {fmtPct(triage.p_material, 0)}</Tag></p></TechnicalDetails>
          )}
        </div>
        <div className="form-actions">
          {a.status === "open" && (
            <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void doAction("acknowledge")}>Acknowledge</button>
          )}
          {a.status !== "resolved" && (
            <button type="button" className="btn btn-sm btn-success" disabled={act.busy} onClick={() => void doAction("resolve")}>Resolve</button>
          )}
          {!a.investigation_run_id && (
            <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void investigate()}>Investigate</button>
          )}
        </div>
        <ErrorBox error={act.error} />
        {note && <Notice tone="warning">{note}</Notice>}
      </div>
    </li>
  );
}
