/**
 * Mock report documents (CONV-04) for the shared in-memory backend: create, a few rule phrasings, versions, the
 * sandboxed preview and an export that needs an approval. Only the shapes the UI reads; the server decides the rest.
 */
import type { Artifact, ReportDocBlock, ReportDocSection, ReportDocView, ReportOp } from "../api";

const T = "2026-10-29T09:00:00Z";
type ChartBlock = Extract<ReportDocBlock, { kind: "chart" }>;
const CHART: ChartBlock = { kind: "chart", id: "chart_resolution_time_trend", title: "Resolution time trend", chart_type: "line",
  columns: ["week", "hours"], rows: [["2026-10-05", 12.5], ["2026-10-12", 14]],
  source: { label: "Dashboard “Operations” v3, chart “Resolution time trend”", assets: ["itsm.incidents"] },
  ref: { kind: "dashboard_chart", artifact_id: "art_dash_v2", element_id: "trend" } };
const BACKLOG: ChartBlock = { kind: "chart", id: "chart_emea_backlog", title: "EMEA backlog by priority", chart_type: "bar",
  columns: ["priority", "open"], rows: [["P1", 7], ["P2", 31]], source: { label: "Dashboard “Operations” v3, chart “EMEA backlog by priority”" },
  ref: { kind: "dashboard_chart", artifact_id: "art_dash_v2", element_id: "backlog" } };

interface Doc { view: ReportDocView; versions: { version: number; diff: ReportOp[]; content_hash: string; created_by: string; created_at: string }[] }
const docs = new Map<string, Doc>();
const approvals: { id: string; format: string; doc: string }[] = [];

export function resetReports(): void {
  docs.clear();
  approvals.length = 0;
}

export function reportArtifacts(ws: string): Artifact[] {
  return [...docs.values()].map(({ view }) => ({
    id: view.id, workspace_id: ws, run_id: null, type: "report_document", name: view.name, version: view.version, status: view.status,
    platform: null, external_id: null, external_url: null, creator_agent: null, creator_user: "usr_admin", content_hash: view.content_hash,
    created_at: T, updated_at: T, content: view.document as unknown as Record<string, unknown>,
  } as Artifact));
}

const slug = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "s";

function bump(d: Doc, sections: ReportDocSection[], diff: ReportOp[]): void {
  const version = d.view.version + 1;
  d.view = { ...d.view, version, status: "draft", content_hash: `h${version}`, document: { ...d.view.document, sections } };
  d.versions.unshift({ version, diff, content_hash: `h${version}`, created_by: "user:usr_admin", created_at: T });
}

function converse(d: Doc, text: string) {
  const sections = d.view.document.sections.map((s) => ({ ...s, blocks: [...s.blocks] }));
  const turn = (ops: ReportOp[], extra: Record<string, unknown> = {}) => ({
    status: "applied", proposal: { ops, answered_by: "rules", notes: [], model: null }, refused: [], deferred: [], ...extra });
  let m = /^add a section on (.+)$/i.exec(text);
  if (m) {
    const title = m[1][0].toUpperCase() + m[1].slice(1);
    const blocks = /backlog/i.test(title) ? [BACKLOG] : [];
    sections.push({ id: slug(title), title, blocks });
    const ops: ReportOp[] = [{ op: "section.add", title, section_id: slug(title) }, ...(blocks.length ? [{ op: "block.add", section_id: slug(title), ref: BACKLOG.ref }] : [])];
    bump(d, sections, ops);
    return turn(ops, { report: d.view });
  }
  if (/^include the resolution trend chart$/i.test(text)) {
    sections[sections.length - 1].blocks.push(CHART);
    const ops = [{ op: "block.add", section_id: sections[sections.length - 1].id, ref: CHART.ref }];
    bump(d, sections, ops);
    return turn(ops, { report: d.view });
  }
  m = /^set the summary to:\s*(.+)$/i.exec(text);
  if (m) {
    if (/\b99\b/.test(m[1])) {
      return { status: "refused", report: d.view, deferred: [], proposal: { ops: [{ op: "text.set", section_id: "summary" }], answered_by: "rules", notes: [], model: null },
        refused: [{ index: 0, op: "text.set", reason: "the text quotes numbers this report cannot back: 99. Add the chart, KPI or finding that shows them first, or leave the number out" }] };
    }
    sections[0].blocks[0] = { kind: "text", id: "t_summary", markdown: m[1] };
    const ops = [{ op: "text.set", section_id: "summary", markdown: m[1] }];
    bump(d, sections, ops);
    return turn(ops, { report: d.view });
  }
  m = /^export it as (?:a )?(pdf|word|powerpoint)/i.exec(text);
  if (m) {
    const format = { pdf: "pdf", word: "docx", powerpoint: "pptx" }[m[1].toLowerCase()]!;
    const id = `apr_report_${approvals.length + 1}`;
    approvals.push({ id, format, doc: d.view.id });
    d.view = { ...d.view, status: "approval_required" };
    return { status: "unchanged", report: d.view, refused: [], deferred: [{ op: "export.request", format }],
      proposal: { ops: [{ op: "export.request", format }], answered_by: "rules", notes: [], model: null },
      export: { status: "approval_required", format, snapshot_hash: "5e1f", sensitivity: "internal", approval: { id, status: "pending", risk_tier: "medium" } } };
  }
  return { status: "no_ops", report: d.view, refused: [], deferred: [],
    proposal: { ops: [], answered_by: "none", notes: ["I could not map that to a report change; try “add a section on EMEA backlog”."], model: null } };
}

function preview(d: Doc): string {
  const body = d.view.document.sections.map((s) => `<h2>${s.title}</h2>${s.blocks.map((b) => b.kind === "text" ? `<p>${b.markdown}</p>`
    : b.kind === "chart" ? `<h3>${b.title}</h3>` : "").join("")}`).join("");
  return `<!DOCTYPE html><html lang="en"><head><title>${d.view.document.title}</title></head><body><main><h1>${d.view.document.title}</h1>${body}</main></body></html>`;
}

export function reportRoute(m: string, p: string, body: string | null | undefined, ws: string):
  { status: number; body: unknown; contentType?: string } | null {
  const W = `/workspaces/${ws}`;
  if (m === "POST" && p === `/v2${W}/report-documents`) {
    const { title } = JSON.parse(body ?? "{}") as { title: string };
    const id = `art_report_${docs.size + 1}`;
    const view: ReportDocView = { id, workspace_id: ws, name: `report:${slug(title)}`, version: 1, status: "draft", content_hash: "h1", updated_at: T,
      document: { id: slug(title), title, sections: [{ id: "summary", title: "Summary", blocks: [{ kind: "text", id: "t_summary", markdown: "" }] }] } };
    docs.set(id, { view, versions: [{ version: 1, diff: [{ op: "create" }], content_hash: "h1", created_by: "user:usr_admin", created_at: T }] });
    return { status: 201, body: view };
  }
  if (m === "GET" && p === `/v2${W}/report-documents`) return { status: 200, body: [...docs.values()].map((d) => d.view) };
  if (m === "GET" && p === `/v2${W}/report-content`) return { status: 200, body: [CHART, BACKLOG].map((c) => ({ ref: c.ref, title: c.title, kind: "chart", where: "dashboard Operations" })) };
  const art = /^\/artifacts\/(art_report_\d+)$/.exec(p);
  if (m === "GET" && art && docs.has(art[1])) {
    const a = reportArtifacts(ws).find((x) => x.id === art[1])!;
    return { status: 200, body: { ...a, versions: [], lineage: { nodes: [], edges: [] } } };
  }
  const r = /^\/v2\/report-documents\/(art_report_\d+)(\/.*)?$/.exec(p);
  if (!r) return null;
  const d = docs.get(r[1]);
  if (!d) return { status: 404, body: { error: { code: "not_found", message: "report not found", details: {} } } };
  const rest = r[2] ?? "";
  if (m === "GET" && rest === "") return { status: 200, body: d.view };
  if (m === "POST" && rest === "/messages") return { status: 200, body: converse(d, (JSON.parse(body ?? "{}") as { text: string }).text.trim()) };
  if (m === "GET" && rest === "/versions") return { status: 200, body: d.versions };
  if (m === "GET" && rest === "/preview") return { status: 200, body: { html: preview(d), snapshot_hash: "5e1f", version: d.view.version } };
  if (m === "POST" && rest === "/exports") {
    const { format } = JSON.parse(body ?? "{}") as { format: string };
    const id = `apr_report_${approvals.length + 1}`;
    approvals.push({ id, format, doc: d.view.id });
    d.view = { ...d.view, status: "approval_required" };
    return { status: 202, body: { status: "approval_required", format, snapshot_hash: "5e1f", sensitivity: "internal", approval: { id, status: "pending", risk_tier: "medium" } } };
  }
  const ex = /^\/exports\/(pdf|docx|pptx|html)$/.exec(rest);
  if (m === "GET" && ex) {
    return { status: 409, body: { error: { code: "approval_required", message: "approval is pending", details: {} } } };
  }
  return null;
}
