/**
 * Mock routes for conversational dashboards (CONV-01..03): a draft from a prompt, builder chat that adds charts
 * from questions (or answers with a clarifying reply), versions and the publish request. Shapes follow
 * `api/routers/dashboards_v2.py`; state is reset per test (`resetDrafts`).
 */
import type { ArtifactDetail, BuilderChart, BuilderDashboard, BuilderDocument, BuilderOp, BuilderVersion } from "../api";

const T = "2026-10-29T09:00:00Z";
type Reply = { status: number; body: unknown } | null;
const ok = (body: unknown, status = 200): Reply => ({ status, body });

interface Draft { dash: BuilderDashboard; versions: (BuilderVersion & { document: BuilderDocument })[] }
const drafts = new Map<string, Draft>();
let seq = 0;

export function resetDrafts(): void {
  drafts.clear();
  seq = 0;
}

const prov = (q: string) => ({ sources: ["servicenow.incident"], query_ids: [`qx_${q}`], created_by: "user:usr_admin", created_via: "conversation" });

function chart(id: string, title: string, type: string, x: string, y: string, rows: unknown[][], ref: BuilderChart["dataset_ref"]): BuilderChart {
  return { id, title, type, dataset_ref: ref, encoding: { x, y: [y] }, labels: [], provenance: prov(id),
    fields: [{ name: x, type: type === "line" ? "temporal" : "nominal" }, { name: y, type: "quantitative" }],
    preview: { columns: [x, y], rows } };
}

function draftDocument(title: string): BuilderDocument {
  return {
    schema_version: "dashboard.v2", id: "incident_operations", title, audience: "operational", sensitivity: "internal",
    kpis: [
      { id: "kpi_incident_count", title: "Incident count", metric: { name: "incident_count", version: 1 }, value: 4210 },
      { id: "kpi_resolution_time", title: "Resolution time", metric: { name: "resolution_time", version: 2 }, value: 14.2, unit: "h" },
    ],
    charts: [
      chart("c_incident_count_trend", "Incident count per month", "line", "incident.opened_at", "incident_count",
        [["2026-08-01", 1320], ["2026-09-01", 1460], ["2026-10-01", 1430]], { kind: "result_handle", handle_id: "qx_trend" }),
      chart("c_incident_count_by_priority", "Incident count by priority", "bar", "incident.priority", "incident_count",
        [["1 - Critical", 210], ["2 - High", 980], ["3 - Moderate", 3020]], { kind: "metric", name: "incident_count", version: 1 }),
      chart("c_resolution_time_by_category", "Resolution time by category", "bar", "incident.category", "resolution_time",
        [["Network", 18.5], ["Database", 12.0], ["Software", 9.1]], { kind: "metric", name: "resolution_time", version: 2 }),
    ],
    text_blocks: [],
    filters: [{ id: "f_incident_priority", field: "incident.priority", op: "in", value: null, scope: ["c_incident_count_by_priority"],
      resolved: { input: null, via: "control" } }],
    layout: { columns: 12, items: [
      { ref: "kpi_incident_count", x: 0, y: 0, w: 3, h: 2 }, { ref: "kpi_resolution_time", x: 3, y: 0, w: 3, h: 2 },
      { ref: "c_incident_count_trend", x: 0, y: 2, w: 6, h: 4 }, { ref: "c_incident_count_by_priority", x: 6, y: 2, w: 6, h: 4 },
      { ref: "c_resolution_time_by_category", x: 0, y: 6, w: 6, h: 4 },
    ] },
  };
}

function save(d: Draft, document: BuilderDocument, diff: BuilderOp[]): void {
  const version = d.dash.version + 1;
  d.dash = { ...d.dash, version, status: "draft", document, content_hash: `h${version}`, updated_at: T };
  d.versions.unshift({ version, diff, content_hash: `h${version}`, created_by: "user:usr_admin", created_at: T, document });
}

export function draftArtifacts(ws: string): ArtifactDetail[] {
  return [...drafts.values()].map(({ dash }) => ({
    id: dash.id, workspace_id: ws, run_id: null, type: "dashboard", name: dash.name, version: dash.version, status: dash.status,
    platform: null, external_id: null, external_url: null, creator_agent: null, creator_user: "usr_admin", content_hash: dash.content_hash,
    created_at: "2026-10-29T10:00:00Z", updated_at: T, content: dash.document as unknown as Record<string, unknown>,
    versions: [], lineage: { nodes: [], edges: [] },
  }) as unknown as ArtifactDetail);
}

export function draftsRoute(m: string, p: string, body: string | null | undefined, ws: string): Reply {
  if (!p.startsWith("/v2/") && !p.startsWith("/artifacts/art_draft_")) return null;  // other routes may send files, not JSON
  let json: Record<string, unknown> = {};
  try {
    json = body ? JSON.parse(body) as Record<string, unknown> : {};
  } catch {
    return null;
  }
  if (m === "POST" && p === `/v2/workspaces/${ws}/dashboards/draft`) {
    const prompt = String(json.prompt ?? "");
    const plan = { title: "Incident operations dashboard", audience: "operational", grounded_on: "semantic", ranked_by: "rules", topic: ["incident"] };
    if (/weather/i.test(prompt)) {
      return ok({ status: "clarify", plan: { ...plan, grounded_on: "none" }, built: [], failed: [],
        clarify: "I could not match that to data this workspace has reviewed. Name what the dashboard is about, for example: incident count, resolution time.",
        examples: ["Build a incident count dashboard"] });
    }
    const id = `art_draft_${++seq}`;
    const document = draftDocument(plan.title);
    const d: Draft = {
      dash: { id, workspace_id: ws, name: `dashboard:${document.id}`, title: document.title, version: 1, status: "draft", platform: null,
        external_url: null, content_hash: "h1", updated_at: T, document },
      versions: [{ version: 1, diff: [{ op: "create", origin: { kind: "prompt", prompt } }], content_hash: "h1", created_by: "user:usr_admin",
        created_at: T, document }],
    };
    drafts.set(id, d);
    const built = [...document.kpis.map((k) => ({ tile: k.id, kind: "kpi", title: k.title, answered_by: "semantic", governance: "governed", chart_type: "kpi" })),
      ...document.charts.map((c) => ({ tile: c.id, kind: "chart", title: c.title, answered_by: "semantic", governance: "governed", chart_type: c.type,
        query_id: `qx_${c.id}` }))];
    return ok({ status: "created", dashboard: d.dash, plan, built,
      failed: [{ tile: "c_sla_breach_rate_by_priority", kind: "chart", title: "SLA breach rate by priority", question: "sla breach rate by priority",
        reason: "metric sla_breach_rate reads data you may not see" }] }, 201);
  }
  if (m === "GET" && p === `/v2/workspaces/${ws}/dashboards`) {
    return ok([...drafts.values()].map(({ dash }) => ({ ...dash, document: undefined, title: dash.document.title })));
  }
  const art = /^\/artifacts\/(art_draft_\d+)$/.exec(p);
  if (m === "GET" && art && drafts.has(art[1])) return ok(draftArtifacts(ws).find((a) => a.id === art[1]));
  const r = /^\/v2\/dashboards\/(art_draft_\d+)(\/[a-z-]+)?(?:\/(\d+))?$/.exec(p);
  const d = r ? drafts.get(r[1]) : undefined;
  if (!r || !d) return null;
  const action = r[2] ?? "";
  if (m === "GET" && action === "") return ok(d.dash);
  if (m === "GET" && action === "/story") {  // CONV-05 summary panel on the builder
    return ok({ dashboard_id: d.dash.id, version: d.dash.version, previous_version: null, title: d.dash.document.title,
      shows: `${d.dash.document.title}: ${d.dash.document.charts.length} charts.`, facts: [], changes: [], wording: "rules",
      notes: [], story_hash: "s1", markdown: "" });
  }
  if (m === "GET" && action === "/versions" && r[3]) return ok(d.versions.find((v) => v.version === Number(r[3])));
  if (m === "GET" && action === "/versions") return ok(d.versions.map(({ document: _doc, ...v }) => v));
  if (m === "POST" && action === "/messages") {
    const text = String(json.text ?? "");
    const add = /^\s*(?:add|plot|show(?: me)?)\s+(?:a\s+)?(?:chart\s+(?:of|showing)\s+)?(.+?)\s*$/i.exec(text);
    if (add && /weather/i.test(add[1])) {
      const op = { op: "chart.from_question", question: add[1] };
      return ok({ status: "refused", dashboard: d.dash, deferred: [], proposal: { ops: [op], answered_by: "rules", notes: [], model: null },
        refused: [{ index: 0, op: "chart.from_question", reason: "no table in your scope has weather data",
          suggestion: { clarify: "Ask about data the workspace has, for example incidents.", kind: "not_in_data", examples: ["resolution time by priority"] } }],
        clarify: { clarify: "Ask about data the workspace has, for example incidents.", kind: "not_in_data", examples: ["resolution time by priority"] } });
    }
    if (add) {
      const question = add[1];
      const id = `c_${question.toLowerCase().replace(/[^a-z0-9]+/g, "_").slice(0, 40)}`;
      const c = chart(id, question.charAt(0).toUpperCase() + question.slice(1), "bar", "incident.priority", "resolution_time",
        [["1 - Critical", 4.5], ["2 - High", 9.0], ["3 - Moderate", 20.3]], { kind: "metric", name: "resolution_time", version: 2 });
      const items = d.dash.document.layout.items;
      const y = Math.max(0, ...items.map((i) => i.y + i.h));
      const document = { ...d.dash.document, charts: [...d.dash.document.charts, c], layout: { ...d.dash.document.layout, items: [...items, { ref: id, x: 0, y, w: 6, h: 4 }] } };
      const op = { op: "chart.from_question", question };
      const applied = { ...op, result: { element: id, answered_by: "semantic", governance: "governed", chart_type: "bar", query_id: "qx_new" } };
      save(d, document, [applied]);
      return ok({ status: "applied", dashboard: d.dash, refused: [], deferred: [], applied: [applied],
        proposal: { ops: [op], answered_by: "rules", notes: [], model: null } });
    }
    const type = /\b(bar|line|pie|table)\b/i.exec(text)?.[1]?.toLowerCase();
    const target = String(json.focus ?? "") || d.dash.document.charts[0]?.id;
    if (type && target) {
      const op = { op: "chart.set_type", chart_id: target, type };
      const document = { ...d.dash.document, charts: d.dash.document.charts.map((c) => c.id === target ? { ...c, type } : c) };
      save(d, document, [op]);
      return ok({ status: "applied", dashboard: d.dash, refused: [], deferred: [], applied: [op],
        proposal: { ops: [op], answered_by: "rules", notes: [], model: null } });
    }
    return ok({ status: "no_ops", dashboard: d.dash, proposal: { ops: [], answered_by: "none", notes: ["I could not map that to a dashboard change."], model: null } });
  }
  if (m === "POST" && action === "/publish-request") {
    d.dash = { ...d.dash, status: "approval_required" };
    return ok({ approval_id: "apr_draft_pub", status: "pending", risk_tier: "medium", payload_hash: "ph1" }, 202);
  }
  return null;
}
