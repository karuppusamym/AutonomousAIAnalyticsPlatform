import { useEffect, useMemo, useRef, useState } from "react";
import {
  api, errorMessage, type BuilderChart, type BuilderDashboard, type DashboardView, type DrillLevel, type DrillPlan, type DrillResult,
  type DrillStep, type ViewFilter, type ViewTile,
} from "../api";
import { label, type Preview } from "../lib/charts";
import {
  chipText, DEBOUNCE_MS, drillKey, fieldLabel, LruCache, memberOf, membersOf, toggleFilter, valueText, viewKey,
} from "../lib/explore";
import { useExpertView } from "../lib/expert";
import { useAsync } from "../lib/hooks";
import type { ChartSelection } from "./Chart";
import { DataTable, Loading, Tag } from "./ui";

/**
 * Cross-filtering and drill-down in the builder preview (CONV-06). Everything here is view state for this browser
 * session: a click filters the other charts and KPIs that share the field, a drill goes one level down, and "Show
 * rows" lists the rows behind a selection. The server validates every field and value and re-queries each affected
 * tile through its governed path as the viewer; nothing is saved until "Apply as dashboard filter" (an edit).
 */

interface DrillState { path: DrillStep[]; result?: DrillResult; loading: boolean; error?: string }
interface RowsState { chartId: string; path: DrillStep[]; result?: DrillResult; loading: boolean; error?: string }

export interface Explore {
  plans: Map<string, { filterFields: string[]; drill: DrillPlan }>;
  filters: ViewFilter[];
  view: DashboardView | null;
  tiles: Map<string, ViewTile>;
  pending: boolean;
  error: string | null;
  drills: Record<string, DrillState>;
  rows: RowsState | null;
  select: (chart: BuilderChart, s: ChartSelection) => void;
  setFilter: (chart: BuilderChart, field: string, value: unknown | undefined) => void;
  removeFilter: (field: string) => void;
  clear: () => void;
  drillTo: (chartId: string, path: DrillStep[]) => void;
  showRows: (chartId: string, path?: DrillStep[]) => void;
  closeRows: () => void;
  preview: (chart: BuilderChart) => Preview | undefined;
}

export function useDashboardExplore(dashboard: BuilderDashboard): Explore {
  const { id, version, document: doc } = dashboard;
  const offers = useAsync(() => api.dashboardExplore(id), [id, version]);
  const [filters, setFilters] = useState<ViewFilter[]>([]);
  const [view, setView] = useState<DashboardView | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [paths, setPaths] = useState<Record<string, DrillStep[]>>({});
  const [drillResults, setDrillResults] = useState<Record<string, Omit<DrillState, "path">>>({});
  const [rows, setRows] = useState<RowsState | null>(null);
  const viewCache = useRef(new LruCache<DashboardView>());
  const drillCache = useRef(new LruCache<DrillResult>());
  const hasSaved = doc.filters.some((f) => f.value != null);
  const key = viewKey(version, filters, hasSaved);

  // The view: debounced, latest request wins (an older one is aborted), answers cached per selection.
  useEffect(() => {
    if (!filters.length && !hasSaved) {
      setView(null);
      setPending(false);
      setError(null);
      return;
    }
    const hit = viewCache.current.get(key);
    if (hit) {
      setView(hit);
      setPending(false);
      setError(null);
      return;
    }
    setPending(true);
    const ctl = new AbortController();
    const timer = setTimeout(() => {
      api.dashboardView(id, { filters, base_version: version, refresh_saved: hasSaved }, ctl.signal)
        .then((v) => {
          viewCache.current.set(key, v);
          setView(v);
          setError(null);
        })
        .catch((err) => { if (!ctl.signal.aborted) setError(errorMessage(err)); })
        .finally(() => { if (!ctl.signal.aborted) setPending(false); });
    }, DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      ctl.abort();
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps -- `key` encodes id, version and filters

  // Drill levels: one request per drilled chart, re-run when the filters change.
  const drillSig = JSON.stringify([version, paths, filters.map((f) => [f.field, f.value, f.source])]);
  useEffect(() => {
    const ctls: AbortController[] = [];
    for (const [chartId, path] of Object.entries(paths)) {
      if (!path.length) continue;
      const k = drillKey(version, chartId, path, filters, false);
      const hit = drillCache.current.get(k);
      if (hit) {
        setDrillResults((d) => ({ ...d, [chartId]: { result: hit, loading: false } }));
        continue;
      }
      const ctl = new AbortController();
      ctls.push(ctl);
      setDrillResults((d) => ({ ...d, [chartId]: { ...d[chartId], loading: true, error: undefined } }));
      api.dashboardDrill(id, { chart_id: chartId, path, filters, base_version: version }, ctl.signal)
        .then((r) => {
          drillCache.current.set(k, r);
          setDrillResults((d) => ({ ...d, [chartId]: { result: r, loading: false } }));
        })
        .catch((err) => {
          if (!ctl.signal.aborted) setDrillResults((d) => ({ ...d, [chartId]: { loading: false, error: errorMessage(err) } }));
        });
    }
    return () => ctls.forEach((c) => c.abort());
  }, [drillSig]); // eslint-disable-line react-hooks/exhaustive-deps -- `drillSig` encodes the paths and filters

  // Rows behind a selection (only when asked for).
  const rowsSig = rows ? JSON.stringify([version, rows.chartId, rows.path, filters.map((f) => [f.field, f.value, f.source])]) : "";
  useEffect(() => {
    if (!rows) return;
    const ctl = new AbortController();
    api.dashboardDrill(id, { chart_id: rows.chartId, path: rows.path, filters, base_version: version, detail: true, preview: false }, ctl.signal)
      .then((r) => setRows((cur) => cur && cur.chartId === rows.chartId ? { ...cur, result: r, loading: false } : cur))
      .catch((err) => { if (!ctl.signal.aborted) setRows((cur) => cur ? { ...cur, loading: false, error: errorMessage(err) } : cur); });
    return () => ctl.abort();
  }, [rowsSig]); // eslint-disable-line react-hooks/exhaustive-deps -- `rowsSig` encodes the request

  // A new version (an edit, or "Apply as dashboard filter") keeps the filters but drops drill paths and rows.
  useEffect(() => {
    setPaths({});
    setDrillResults({});
    setRows(null);
  }, [id, version]);

  const plans = useMemo(() => new Map((offers.data?.charts ?? []).map((c) => [c.id, { filterFields: c.filter_fields, drill: c.drill }])),
    [offers.data]);
  const tiles = useMemo(() => new Map((view?.tiles ?? []).map((t) => [t.id, t])), [view]);
  const charts = useMemo(() => new Map(doc.charts.map((c) => [c.id, c])), [doc.charts]);

  const setFilter = (chart: BuilderChart, field: string, value: unknown | undefined) => {
    setFilters((fs) => {
      const rest = fs.filter((f) => f.field !== field);
      return value === undefined ? rest : [...rest, { field, op: "eq", value, source: chart.id, label: fieldLabel(chart, field) }];
    });
  };
  const drillTo = (chartId: string, path: DrillStep[]) => {
    setPaths((p) => ({ ...p, [chartId]: path }));
    if (!path.length) setDrillResults((d) => ({ ...d, [chartId]: { loading: false } }));
  };
  const showRows = (chartId: string, path?: DrillStep[]) => {
    const chart = charts.get(chartId);
    const plan = plans.get(chartId)?.drill;
    let p = path ?? paths[chartId] ?? [];
    // A chart without a hierarchy lists the rows of its own selection (its own click does not filter it).
    if (!p.length && plan?.kind === "none" && plan.levels?.length) {
      const own = filters.find((f) => f.source === chartId && f.field === plan.levels![0].field);
      if (own && chart) p = [{ field: own.field, value: own.value, label: valueText(own.value) }];
    }
    setRows({ chartId, path: p, loading: true });
  };

  const select = (chart: BuilderChart, s: ChartSelection) => {
    const plan = plans.get(chart.id);
    const path = paths[chart.id] ?? [];
    const shown = path.length ? drillResults[chart.id]?.result?.preview : chart.preview;
    if (!plan || !shown?.columns?.length) return;
    // A stacked bar's legend names the series (column 1); a pie's legend and every bar or point name the axis (column 0).
    const column = s.from === "legend" && chart.type === "stacked_bar" && !path.length ? 1 : 0;
    const field = shown.columns[column];
    const value = memberOf(shown as Preview, column, s.name);
    if (value === undefined || !field) return;
    const levels = plan.drill.levels ?? [];
    const drillable = plan.drill.kind === "time" || (plan.drill.kind === "dimension" && path.length > 0);
    if (drillable && column === 0) {
      const step: DrillStep = { field: levels[path.length]?.field ?? field, value, label: label(value) };
      if (path.length + 1 < levels.length) drillTo(chart.id, [...path, step]);
      else showRows(chart.id, [...path, step]);
      return;
    }
    if (path.length) return; // a drilled chart's other columns are not dashboard fields
    if (plan.filterFields.includes(field)) setFilters((fs) => toggleFilter(fs, { field, op: "eq", value, source: chart.id, label: fieldLabel(chart, field) }));
  };

  const preview = (chart: BuilderChart): Preview | undefined => {
    if (paths[chart.id]?.length) return drillResults[chart.id]?.result?.preview as Preview | undefined;
    const t = tiles.get(chart.id);
    if (t?.preview && (t.status === "filtered" || t.status === "saved_filter")) return t.preview as Preview;
    return chart.preview as Preview | undefined;
  };

  const drills: Record<string, DrillState> = {};
  for (const [chartId, path] of Object.entries(paths)) drills[chartId] = { path, ...(drillResults[chartId] ?? { loading: false }) };

  return {
    plans, filters, view, tiles, pending, error: error ?? offers.error ?? null, drills, rows, select, setFilter,
    removeFilter: (field) => setFilters((fs) => fs.filter((f) => f.field !== field)),
    clear: () => setFilters([]),
    drillTo, showRows, closeRows: () => setRows(null), preview,
  };
}

/** The "Filtered by …" chips, apply and clear. */
export function ExploreBar({ explore, canEdit, onApply, applying }: {
  explore: Explore; canEdit: boolean; onApply: () => void; applying: boolean;
}) {
  const expert = useExpertView();
  const { filters, view, pending } = explore;
  const counts = view ? {
    filtered: view.tiles.filter((t) => t.status === "filtered").length,
    left: view.tiles.filter((t) => t.status === "not_filtered").length,
  } : null;
  return (
    <div className="explore-bar chip-row" role="region" aria-label="Dashboard filters">
      {filters.length === 0 && <span className="muted small">Click a bar, slice or legend item to filter the other charts, or use the lists under each chart.</span>}
      {filters.map((f) => (
        <button key={f.field} type="button" className="chip active" onClick={() => explore.removeFilter(f.field)}
          aria-label={`Remove filter ${chipText(f)}`}>Filtered by {chipText(f)} <span aria-hidden="true">×</span></button>
      ))}
      {filters.length > 1 && <button type="button" className="btn btn-sm" onClick={explore.clear}>Clear all</button>}
      {canEdit && filters.length > 0 && (
        <button type="button" className="btn btn-sm" disabled={applying || pending || !view?.apply.length} onClick={onApply}
          title="Save this filter on the dashboard as a new version">Apply as dashboard filter</button>
      )}
      <span className="muted small" aria-live="polite">
        {pending ? "Refreshing charts…" : counts && filters.length ? `${counts.filtered} filtered, ${counts.left} not filtered` : ""}
      </span>
      {expert && view && <span className="muted small">{view.tiles.filter((t) => t.query_id).length} governed re-queries
        (at most {view.limits.max_concurrent} at a time) · {view.tiles.filter((t) => t.cache_hit).length} from cache</span>}
    </div>
  );
}

/** What happened to one tile in the current view, in plain words. */
export function TileStatus({ explore, id, drilled }: { explore: Explore; id: string; drilled?: DrillState }) {
  const expert = useExpertView();
  if (drilled?.path.length) {
    return (
      <>
        <Tag tone="info">Drilled</Tag>
        {drilled.loading && <span className="muted small">Loading…</span>}
        {expert && drilled.result?.query_id && <span className="muted small"><code>{drilled.result.query_id}</code></span>}
      </>
    );
  }
  if (!explore.filters.length && !explore.view) return null;
  const t = explore.tiles.get(id);
  if (explore.pending && (!t || t.status !== "not_filtered")) return <span className="muted small">Refreshing…</span>;
  if (!t) return null;
  const text = { filtered: "Filtered", saved_filter: "Dashboard filter", source: "Your selection", not_filtered: "Not filtered",
    refused: "Not refreshed", error: "Not refreshed", busy: "Busy", skipped: "Not refreshed" }[t.status];
  const tone = t.status === "filtered" || t.status === "saved_filter" ? "info" : t.status === "not_filtered" || t.status === "source" ? "neutral" : "warning";
  return (
    <>
      <span title={t.reason}><Tag tone={tone}>{text}</Tag></span>
      {t.reason && (tone === "warning" ? <span className="warn-text small">{t.reason}</span> : <span className="sr-only">{t.reason}</span>)}
      {expert && t.query_id && <span className="muted small"><code>{t.query_id}</code>{t.cache_hit ? " (cache)" : ""}</span>}
    </>
  );
}

function crumbLabel(level: DrillLevel | undefined, step: DrillStep): string {
  return step.label ?? (level?.grain ? label(step.value) : valueText(step.value));
}

/** Keyboard path to everything a click does: filter lists, drill breadcrumbs and "Show rows". */
export function TileControls({ explore, chart }: { explore: Explore; chart: BuilderChart }) {
  const plan = explore.plans.get(chart.id);
  if (!plan) return null;
  const drilled = explore.drills[chart.id];
  const path = drilled?.path ?? [];
  const levels = plan.drill.levels ?? [];
  const shown = explore.preview(chart);
  const canDrill = (plan.drill.kind === "time" || plan.drill.kind === "dimension") && path.length + 1 < levels.length;
  const next = levels[path.length + 1];
  const filterFields = path.length ? [] : plan.filterFields.slice(0, 2);
  return (
    <div className="tile-controls row wrap">
      {filterFields.map((field) => {
        const column = shown?.columns?.indexOf(field) ?? -1;
        const current = explore.filters.find((f) => f.field === field && f.source === chart.id);
        const name = fieldLabel(chart, field);
        const members = membersOf(shown, column);
        const idx = current ? members.findIndex((m) => JSON.stringify(m.value) === JSON.stringify(current.value)) : -1;
        return (
          <select key={field} className="input-sm" aria-label={`Filter the dashboard by ${name}`} value={idx < 0 ? "" : String(idx)}
            onChange={(e) => explore.setFilter(chart, field, e.target.value === "" ? undefined : members[Number(e.target.value)].value)}>
            <option value="">{name}: all</option>
            {members.map((m, i) => <option key={m.text} value={String(i)}>{name}: {m.text}</option>)}
          </select>
        );
      })}
      {path.length > 0 && (
        <nav aria-label={`Drill path for ${chart.title}`} className="crumbs small">
          <ol>
            <li><button type="button" className="link-btn" onClick={() => explore.drillTo(chart.id, [])}>All</button></li>
            {path.map((step, i) => {
              const text = crumbLabel(levels[i], drilled?.result?.path[i] ?? step);  // the server names periods ("Sep 2026")
              return (
                <li key={i}>{i < path.length - 1
                  ? <button type="button" className="link-btn" onClick={() => explore.drillTo(chart.id, path.slice(0, i + 1))}>{text}</button>
                  : <span aria-current="location">{text}</span>}</li>
              );
            })}
          </ol>
        </nav>
      )}
      {canDrill && next && shown?.columns?.length ? (
        <select className="input-sm" aria-label={`Drill into ${next.label.toLowerCase()} for ${chart.title}`} value=""
          onChange={(e) => {
            const m = membersOf(shown, 0)[Number(e.target.value)];
            if (m) explore.drillTo(chart.id, [...path, { field: levels[path.length].field, value: m.value, label: m.text }]);
          }}>
          <option value="">Drill into {next.label.toLowerCase()}…</option>
          {membersOf(shown, 0).map((m, i) => <option key={m.text} value={String(i)}>{m.text}</option>)}
        </select>
      ) : null}
      <button type="button" className="btn btn-sm" onClick={() => explore.showRows(chart.id)}>Show rows</button>
      {drilled?.error && <span className="warn-text small">{drilled.error}</span>}
      {drilled?.result?.reason && drilled.result.status && <span className="warn-text small">{drilled.result.reason}</span>}
    </div>
  );
}

/** The rows behind a selection: only columns the viewer may see, row filters applied, at most the server's limit. */
export function RowsPanel({ explore, charts }: { explore: Explore; charts: BuilderChart[] }) {
  const expert = useExpertView();
  const rows = explore.rows;
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (rows) closeRef.current?.focus(); }, [rows?.chartId]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!rows) return null;
  const chart = charts.find((c) => c.id === rows.chartId);
  const where = rows.path.map((s) => s.label ?? valueText(s.value)).join(" › ");
  const title = `Rows behind ${chart?.title ?? rows.chartId}${where ? `: ${where}` : ""}`;
  const d = rows.result?.detail;
  return (
    <section className="explore-rows stack small" aria-label={title}>
      <div className="row">
        <strong className="grow">{title}</strong>
        <button ref={closeRef} type="button" className="btn btn-sm" onClick={explore.closeRows}>Close rows</button>
      </div>
      {explore.filters.length > 0 && <p className="muted">With {explore.filters.filter((f) => f.source !== rows.chartId).map(chipText).join("; ") || "no other filters"}.</p>}
      {rows.loading ? <Loading label="Listing rows…" /> : rows.error ? <p className="warn-text">{rows.error}</p>
        : d?.unavailable ? <p className="muted">{d.unavailable}</p>
          : d?.columns ? (
            <>
              {d.rows?.length ? <DataTable columns={d.columns} rows={d.rows} maxRows={100} caption={title} /> : <p className="muted">No rows.</p>}
              {(d.hidden_columns ?? 0) > 0 && <p className="muted">{d.hidden_columns} {d.hidden_columns === 1 ? "column is" : "columns are"} hidden by your access policy.</p>}
              {d.truncated && <p className="muted">Showing the first {d.rows?.length} rows.</p>}
              {expert && d.query_id && <p className="muted">Query <code>{d.query_id}</code></p>}
            </>
          ) : null}
    </section>
  );
}

/** "Apply as dashboard filter": the server's typed `dashboard.add_filter` ops, applied as one edit (a new version). */
export async function applyViewFilters(dashboard: BuilderDashboard, view: DashboardView) {
  return api.builderOps(dashboard.id, view.apply, dashboard.version);
}
