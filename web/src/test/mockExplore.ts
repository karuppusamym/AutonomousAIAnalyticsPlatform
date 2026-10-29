/**
 * Mock routes for dashboard exploration (CONV-06) on a draft dashboard: what each chart offers, a cross-filtered
 * view, drill levels and the rows behind a selection, and saving a view filter as a typed op. Shapes follow
 * `dashboards/interact.py`; numbers are deterministic shares of the stored previews, so tests can assert them.
 */
import type { BuilderDashboard, BuilderDocument, BuilderOp } from "../api";

type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });
const fail = (status: number, message: string): Reply => ({ status, body: { error: { code: "invalid_input", message, details: {} } } });

/** The share of all rows each member has, per field (canonical name). */
const SHARE: Record<string, Record<string, number>> = {
  priority: { "1 - Critical": 0.05, "2 - High": 0.23, "3 - Moderate": 0.72 },
  category: { Network: 0.4, Database: 0.35, Software: 0.25 },
};
const canon = (f: string) => (f.split(".").pop() ?? f).toLowerCase();
const label = (f: string) => { const c = canon(f); return c.charAt(0).toUpperCase() + c.slice(1); };

interface Filter { field: string; op: string; value: unknown; source?: string | null }

function plans(doc: BuilderDocument) {
  return doc.charts.map((c) => {
    const x = c.encoding.x ?? "";
    const temporal = c.fields.find((f) => f.name === x)?.type === "temporal";
    if (temporal) {
      return { id: c.id, filter_fields: [], drill: { kind: "time", field: x, levels: [
        { field: x, grain: "month", label: "Month" }, { field: x, grain: "day", label: "Day" }] } };
    }
    const other = canon(x) === "priority" ? "incident.category" : "incident.priority";
    return { id: c.id, filter_fields: c.fields.filter((f) => f.type === "nominal").map((f) => f.name),
      drill: { kind: "dimension", field: x, levels: [{ field: x, label: label(x) }, { field: other, label: label(other) }] } };
  });
}

function share(filters: Filter[], except: string): number {
  return filters.filter((f) => f.source !== except).reduce((s, f) => s * (SHARE[canon(f.field)]?.[String(f.value)] ?? 1), 1);
}

function validate(doc: BuilderDocument, filters: Filter[]): string | null {
  for (const f of filters) {
    const field = doc.charts.flatMap((c) => c.fields).find((x) => canon(x.name) === canon(f.field));
    if (!field) return `'${f.field}' is not a field of this dashboard`;
    if (field.type !== "nominal") return `${label(f.field)} is a ${field.type} field: drill into it instead of filtering`;
    if (!(String(f.value) in (SHARE[canon(f.field)] ?? {}))) return `'${String(f.value)}' is not a known value of ${label(f.field)}`;
  }
  return null;
}

export function exploreRoute(m: string, action: string, json: Record<string, unknown>, dash: BuilderDashboard,
  save: (document: BuilderDocument, diff: BuilderOp[]) => void): Reply {
  const doc = dash.document;
  if (m === "GET" && action === "/explore") {
    return ok({ version: dash.version, charts: plans(doc), kpis: doc.kpis.map((k) => ({ id: k.id, dimensions: ["incident.priority", "incident.category"] })),
      limits: { max_filters: 5, max_concurrent: 4, detail_rows: 100 } });
  }
  const filters = (json.filters as Filter[] | undefined) ?? [];
  if ((m === "POST" && (action === "/view" || action === "/drill"))) {
    if (json.base_version != null && json.base_version !== dash.version) {
      return { status: 409, body: { error: { code: "conflict", message: `dashboard is at version ${dash.version}`, details: {} } } };
    }
    const bad = validate(doc, filters);
    if (bad) return fail(422, bad);
  }
  if (m === "POST" && action === "/view") {
    const saved = doc.filters.filter((f) => f.value != null) as Filter[];
    const all = [...saved, ...filters];
    let n = 0;
    const tiles = [...doc.charts.map((c) => {
      // a metric chart is filtered by any dimension of its metric (the semantic compiler adds the predicate)
      const applies = filters.filter((f) => f.source !== c.id && (c.dataset_ref.kind === "metric" || c.fields.some((x) => canon(x.name) === canon(f.field))));
      const own = filters.some((f) => f.source === c.id);
      const savedHere = saved.filter((f) => c.fields.some((x) => x.name === f.field));
      if (!applies.length && !(json.refresh_saved && savedHere.length)) {
        const names = filters.filter((f) => f.source !== c.id).map((f) => label(f.field)).join(", ");
        return { id: c.id, kind: "chart", status: own ? "source" : "not_filtered", ...(own || !names ? {} : { reason: `This chart has no ${names} field.` }) };
      }
      const k = share(all, c.id);
      const rows = (c.preview?.rows ?? []).map((r) => {
        const keep = savedHere.every((f) => String(r[0]) === String(f.value));
        return keep ? [r[0], Math.round(Number(r[1]) * k * 10) / 10] : null;
      }).filter(Boolean);
      return { id: c.id, kind: "chart", status: applies.length ? "filtered" : "saved_filter", filtered_by: applies.map((f) => label(f.field)),
        preview: { columns: c.preview?.columns ?? [], rows }, query_id: `qx_view_${++n}`, cache_hit: false };
    }), ...doc.kpis.map((kp) => filters.length
      ? { id: kp.id, kind: "kpi", status: "filtered", filtered_by: filters.map((f) => label(f.field)),
        value: Math.round(Number(kp.value ?? 0) * share(filters, "") * 10) / 10, query_id: `qx_view_${++n}` }
      : { id: kp.id, kind: "kpi", status: "not_filtered" })];
    const apply = filters.map((f) => ({ op: "dashboard.add_filter", field: f.field, operator: "eq", value: f.value,
      scope: doc.charts.filter((c) => c.fields.some((x) => x.name === f.field)).map((c) => c.id) }));
    return ok({ version: dash.version, filters: filters.map((f) => ({ ...f, label: label(f.field), via: "vocabulary" })), tiles, apply,
      limits: { max_concurrent: 4, max_queries: 24 } });
  }
  if (m === "POST" && action === "/drill") {
    const chart = doc.charts.find((c) => c.id === json.chart_id);
    if (!chart) return { status: 404, body: { error: { code: "not_found", message: "no chart", details: {} } } };
    const plan = plans(doc).find((p) => p.id === chart.id)!;
    const path = (json.path as { field: string; value: unknown }[] | undefined) ?? [];
    const levels = plan.drill.levels;
    if (path.length > levels.length) return fail(422, "cannot drill deeper than the last level");
    const level = path.length < levels.length ? levels[path.length] : null;
    const steps = path.map((s, i) => ({ ...s, label: plan.drill.kind === "time" ? (i === 0 ? "Sep 2026" : String(s.value).slice(0, 10)) : String(s.value),
      ...(plan.drill.kind === "time" ? { grain: (levels[i] as { grain?: string }).grain } : {}) }));
    const out: Record<string, unknown> = { chart_id: chart.id, kind: plan.drill.kind, depth: path.length, levels, path: steps, level,
      filtered_by: filters.filter((f) => f.source !== chart.id).map((f) => label(f.field)) };
    if (json.preview !== false && path.length && level) {
      out.preview = plan.drill.kind === "time"
        ? { columns: [chart.encoding.x, chart.encoding.y[0]], rows: [["2026-09-01T00:00:00", 41], ["2026-09-02T00:00:00", 52], ["2026-09-03T00:00:00", 47]] }
        : { columns: [level.field, chart.encoding.y[0]], rows: [["Network", 84], ["Database", 74], ["Software", 52]] };
      out.query_id = "qx_drill_1";
    }
    if (json.detail) {
      out.detail = { columns: ["number", "priority", "category", "opened_at"], hidden_columns: 1, truncated: false, query_id: "qx_rows_1",
        rows: [["INC00012", "1 - Critical", "Network", "2026-09-02T08:00:00"], ["INC00031", "1 - Critical", "Database", "2026-09-02T11:30:00"]] };
    }
    return ok(out);
  }
  if (m === "POST" && action === "/ops") {
    const ops = (json.ops as BuilderOp[] | undefined) ?? [];
    if (json.base_version != null && json.base_version !== dash.version) return { status: 409, body: { error: { code: "conflict", message: "stale", details: {} } } };
    const added = ops.filter((o) => o.op === "dashboard.add_filter").map((o, i) => ({ id: `f_${canon(String(o.field))}${i ? `_${i}` : ""}`,
      field: String(o.field), op: String(o.operator ?? "eq"), value: o.value, scope: (o.scope as string[]) ?? [], resolved: { input: o.value, via: "vocabulary" } }));
    save({ ...doc, filters: [...doc.filters, ...added] }, ops);
    return ok({ status: "applied", dashboard: undefined, refused: [], deferred: [], applied: ops });
  }
  return null;
}
