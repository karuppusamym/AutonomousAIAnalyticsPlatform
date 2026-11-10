/**
 * The knowledge studio part of the shared mock backend (P4-U04): packs with revisions, documents
 * with trust fields, the review queue, import/export, the push approval and the semantic graph.
 * Stateful like the rest of the mock: `resetKnowledgeState` runs from `resetMockState`, so every
 * Playwright test and vitest case starts from the same packs and three pending drafts. The server's
 * rules the UI depends on are mirrored (base sha, `verified` can only gain your own entry, a human
 * edit turns a queue draft into owner content, read-only platform/imported packs).
 */
import type {
  Approval, ContextReceipt, Dict, KnowledgeDocSummary, KnowledgeDocument, KnowledgeGraph, KnowledgeGraphEdge, KnowledgeGraphNode,
  KnowledgeImportReport, KnowledgePackInfo, KnowledgeRevisionInfo, KnowledgeSuggestion, ReviewResult, SemanticMetric, VerifiedEntry,
} from "../api";

const T = "2026-09-25T09:00:00Z";
const ME = "usr_admin";

export const WS_PACK = "kpk_ws";
export const PLATFORM_PACK = "kpk_platform";
export const IMPORTED_PACK = "kpk_atlas";
export const SUGGESTION_TERM = "ksug_reopen";

interface MockDoc { frontmatter: Dict; body: string }
interface MockRevision { number: number; author: string; reason: string; origin: string; created_at: string; files: Record<string, MockDoc> }
interface MockPack { info: Omit<KnowledgePackInfo, "files" | "head_revision" | "content_digest">; revisions: MockRevision[] }

/** A stable 64-hex digest (FNV-1a, eight seeds): a stand-in for sha256 in the mock. */
export function mockHash(s: string): string {
  let out = "";
  for (let seed = 0; seed < 8; seed++) {
    let h = 0x811c9dc5 ^ seed;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 0x01000193) >>> 0;
    }
    out += h.toString(16).padStart(8, "0");
  }
  return out;
}

const text = (d: MockDoc) => `---\n${JSON.stringify(d.frontmatter, null, 2)}\n---\n\n${d.body.trim()}\n`;
const sha = (d: MockDoc) => mockHash(text(d));
const docId = (pack: string, path: string) => `kdoc_${mockHash(`${pack}\0${path}`).slice(0, 24)}`;
const clone = <X,>(x: X): X => JSON.parse(JSON.stringify(x)) as X;

function verifiedOf(fm: Dict): VerifiedEntry[] {
  const v = fm.verified;
  if (Array.isArray(v)) return v.filter((e): e is VerifiedEntry => !!e && typeof e === "object");
  return v && typeof v === "object" ? [v as VerifiedEntry] : [];
}

function tierOf(fm: Dict): string {
  const v = verifiedOf(fm);
  if (!v.length) return "unverified";
  return v.some((e) => String(e.by).startsWith("human:")) ? "human-reviewed" : "machine-confirmed";
}

const ext = (fm: Dict): Dict => (fm.analystos && typeof fm.analystos === "object" ? fm.analystos as Dict : {});

const term = (title: string, body: string, fm: Dict = {}): MockDoc => ({
  frontmatter: { type: "Glossary Term", title, status: "stable", analystos: { kind: "term" }, ...fm }, body: `# Definition\n\n${body}`,
});

function initialPacks(): Record<string, MockPack> {
  const ws: Record<string, MockDoc> = {
    "glossary/p1.md": term("P1", "A P1 incident is priority 1 (critical): the service is down for many users. Resolution time is tracked as [MTTR](/metrics/mttr.md).", {
      verified: [{ by: `human:${ME}`, at: "2026-09-20T09:00:00+00:00" }], tags: ["itsm"],
      analystos: { kind: "term", synonyms: ["priority 1"], mapped_columns: ["servicenow.incident.priority"] } }),
    "metrics/mttr.md": { frontmatter: { type: "Metric", title: "MTTR", status: "draft", analystos: { kind: "metric" } },
      body: "# Definition\n\nMean time to resolve an incident, in hours." },
  };
  return {
    [WS_PACK]: {
      info: { id: WS_PACK, kind: "workspace", slug: "workspace", title: "Workspace knowledge", read_only: false, writable: true, okf_root: "",
        okf_version: "0.2", git_remote: "https://git.example.com/itsm/knowledge.git", git_branch: "main", origin: {}, updated_at: T },
      revisions: [{ number: 1, author: `user:${ME}`, reason: "seed the glossary", origin: "user", created_at: "2026-09-20T09:00:00Z", files: ws }],
    },
    [PLATFORM_PACK]: {
      info: { id: PLATFORM_PACK, kind: "platform", slug: "platform", title: "Platform knowledge", read_only: true, writable: false, okf_root: "",
        okf_version: "0.2", git_remote: null, git_branch: "main", origin: {}, updated_at: T },
      revisions: [{ number: 1, author: "system:seed", reason: "installed domain packs", origin: "seed", created_at: T, files: {
        "domain/itsm/sla-breach.md": term("SLA breach", "An incident resolved after its SLA target.", {
          verified: [{ by: "process:domain-pack", at: "2026-09-01T00:00:00+00:00" }], analystos: { kind: "term", domain_pack: "itsm" } }),
      } }],
    },
  };
}

const field = (value: unknown, confidence: number, provenance: Dict, extra: Dict = {}) => ({ value, confidence, provenance, ...extra });

function initialSuggestions(): KnowledgeSuggestion[] {
  const base = { batch: null, status: "pending" as const, decided_by: null, decided_at: null, reason: null, revision: null, created_at: T };
  const model = { source: "model", model: "openrouter/auto", purpose: "metadata_enrichment", prompt_version: "crawl-enrich-v2", crawl_run: "crl_7" };
  return [
    { ...base, id: SUGGESTION_TERM, kind: "term", subject: "term:reopen rate", title: "Reopen rate", path: "glossary/reopen-rate.md", confidence: 0.55,
      origin: "crawler.enrichment", proposed_by: "model:openrouter/auto",
      fields: { body: field("The reopen rate is the share of resolved incidents that were reopened within seven days.", 0.55, model),
        synonyms: field(["reopen ratio"], 0.8, { source: "rule", rule: "synonym-table" }) } },
    { ...base, id: "ksug_incident", kind: "table_description", subject: "asset:ast_inc", title: "incident", path: "catalog/ast_inc.md", confidence: 0.62,
      origin: "crawler.enrichment", proposed_by: "model:openrouter/auto",
      fields: { description: field("One row per IT incident, from report to closure.", 0.62, model, { before: { value: "One row per incident.", origin: "rule" } }),
        business_name: field("Incidents", 0.9, { source: "rule", rule: "table-name" }) } },
    { ...base, id: "ksug_note", kind: "note", subject: "feedback:fb_1", title: "Exclude auto-closed tickets", path: "notes/exclude-auto-closed-tickets.md",
      confidence: 0.7, origin: "learning.feedback", proposed_by: `user:${ME}`,
      fields: { body: field("Analysts exclude auto-closed tickets (close_code = auto) from resolution-time analysis.", 0.7,
        { source: "feedback", feedback_id: "fb_1", by: `user:${ME}` }) } },
  ];
}

/** What `POST …/knowledge/glossary/scan` queues in the mock: a code-set skeleton, a defined abbreviation, a question. */
export const SCAN_TERM = "ksug_gl_priority";
export const SCAN_ABBR = "ksug_gl_cmdb";
export const SCAN_QUESTION = "ksug_desc_flag";

function scanSuggestions(): KnowledgeSuggestion[] {
  const base = { batch: "gscan_1", status: "pending" as const, decided_by: null, decided_at: null, reason: null, revision: null, created_at: T,
    proposed_by: `user:${ME}` };
  const rule = { source: "rule", rule: "code_set", scan: "gscan_1" };
  return [
    { ...base, id: SCAN_TERM, kind: "glossary_term", subject: "glossary:priority", title: "Priority", path: "glossary/priority.md",
      confidence: 0.2, origin: "glossary.scan",
      fields: { name: field("Priority", 0.6, rule), body: field("Priority codes: 1 = ?; 2 = ?; 3 = ?; 4 = ?; 5 = ?.", 0.2, rule),
        synonyms: field([], 0.5, rule), mapped_columns: field(["servicenow.incident.priority"], 0.9, rule),
        evidence: field([{ kind: "code_set", where: "incident.priority", values: ["1", "2", "3", "4", "5"], detail: "5 values: 1–5" },
          { kind: "ask", question: "how many P1 incidents breached", turn_id: "askt_1", detail: "asked in Ask" }], 1, rule),
        question: field("What do the Priority codes 1–5 mean?", 1, rule), placeholder: field(true, 1, rule) } },
    { ...base, id: SCAN_ABBR, kind: "glossary_term", subject: "glossary:cmdb", title: "CMDB", path: "glossary/cmdb.md", confidence: 0.5,
      origin: "glossary.scan",
      fields: { name: field("CMDB", 0.6, rule), body: field("CMDB: Configuration Management Database.", 0.5, { ...rule, rule: "abbreviation" }),
        synonyms: field(["Configuration Management Database"], 0.5, rule), mapped_columns: field([], 0.5, rule),
        evidence: field([{ kind: "abbreviation", where: "cmdb_ci", detail: "“cmdb” in the name cmdb_ci" }], 1, rule),
        question: field("What does CMDB mean in your business?", 1, rule), placeholder: field(false, 1, rule) } },
    { ...base, id: SCAN_QUESTION, kind: "description_question", subject: "describe:column:ast_inc:u_flag2", title: "Describe incident.u_flag2",
      path: "notes/describe-incident-u-flag2.md", confidence: 0.2, origin: "glossary.describe",
      fields: { question: field("What does “u_flag2” in Incident mean?", 1, rule),
        description: field("U flag2.", 0.2, { source: "rule", rule: "catalog" }),
        evidence: field([{ kind: "description", where: "incident.u_flag2", detail: "the name rules did not understand this column" }], 1, rule),
        target: field({ kind: "column", asset_id: "ast_inc", column: "u_flag2" }, 1, rule) } },
  ];
}

const k = { packs: initialPacks(), suggestions: initialSuggestions(), question: "", goldenRegression: false };

export function resetKnowledgeState(): void {
  k.packs = initialPacks();
  k.suggestions = initialSuggestions();
  k.question = "";
  k.goldenRegression = false;
}

/** Make approving SUGGESTION_TERM break one passing golden question (the review gate's 409). */
export function setGoldenRegression(on: boolean): void {
  k.goldenRegression = on;
}

/** The effect knowledge/effect.py reports: one golden case, broken by SUGGESTION_TERM when the flag is set. */
function goldenEffect(decisions: { id: string; action: string }[]): Dict {
  const breaks = k.goldenRegression && decisions.some((d) => d.id === SUGGESTION_TERM && d.action !== "reject");
  const after = !breaks;
  return { evaluated: true, baseline: { passed: 1, total: 2, rate: 0.5 }, projected: { passed: breaks ? 0 : 2, total: 2, rate: breaks ? 0 : 1 },
    delta_points: breaks ? -50 : 50, counts: { newly_failing: breaks ? 1 : 0, fixed: breaks ? 0 : 1, still_failing: 0, still_passing: breaks ? 0 : 1 },
    cases: [{ id: "gold_reopen", question: "What is the reopen rate for P1 incidents?", before: true, after, change: breaks ? "newly_failing" : "still_passing",
      expected_columns: ["itsm.incident.reopen_count"], columns_before: ["itsm.incident.reopen_count"],
      columns_after: breaks ? [] : ["itsm.incident.reopen_count"], served_before: null, served_after: null },
    { id: "gold_mttr", question: "What is MTTR by priority?", before: false, after: !breaks, change: breaks ? "still_failing" : "fixed",
      expected_columns: [], columns_before: [], columns_after: [], served_before: null, served_after: breaks ? null : "vq_mttr" }]
      .sort((a, b) => Number(a.after) - Number(b.after)),
    errors: [], fingerprint: "fp" };
}

/** Queue what a glossary scan would (tests of the Overview count). */
export function seedScan(): void {
  k.suggestions.push(...scanSuggestions());
}

/** The mock's stand-in for the context compiler: remember what was asked last. */
export function recordQuestion(q: string): void {
  k.question = q.toLowerCase();
}

/** Receipts for reviewed workspace documents whose title words all appear in the last question. */
export function knowledgeReceipts(): ContextReceipt[] {
  const head = headOf(k.packs[WS_PACK]);
  return Object.entries(head?.files ?? {}).filter(([, d]) => ext(d.frontmatter).review && String(d.frontmatter.title ?? "")
    .toLowerCase().split(/\s+/).every((w) => w && k.question.includes(w)))
    .map(([path, d]) => ({ id: `${docId(WS_PACK, path)}#definition`, section: "glossary", kind: "glossary", name: String(d.frontmatter.title),
      source: String(ext(d.frontmatter).origin ?? "review"), trusted: true, sha256: sha(d), document_id: docId(WS_PACK, path), path,
      anchor: "definition", rank: 1 }));
}

function headOf(p: MockPack | undefined): MockRevision | undefined {
  return p?.revisions[p.revisions.length - 1];
}

function packInfo(p: MockPack): KnowledgePackInfo {
  const head = headOf(p);
  const files = head ? Object.keys(head.files).length : 0;
  return { ...p.info, head_revision: head?.number ?? null, files, content_digest: head ? mockHash(JSON.stringify(head.files)) : null };
}

function summary(pack: string, path: string, d: MockDoc): KnowledgeDocSummary {
  const fm = d.frontmatter;
  return { path, document_id: docId(pack, path), sha256: sha(d), size: text(d).length, markdown: true, reserved: false, type: String(fm.type ?? "") || null,
    title: String(fm.title ?? path), status: String(fm.status ?? "stable"), trust_tier: tierOf(fm), stale: false, kind: (ext(fm).kind as string) ?? null,
    authorship: ext(fm).review ? "review_queue" : "owner", tags: Array.isArray(fm.tags) ? fm.tags.map(String) : [] };
}

function full(pack: string, path: string, d: MockDoc, revision: number): KnowledgeDocument {
  const fm = d.frontmatter;
  const files = headOf(k.packs[pack])?.files ?? {};
  const links = [...d.body.matchAll(/\]\(([^)]+)\)/g)].map((m) => {
    const target = m[1].replace(/^\//, "");
    return { raw: m[1], kind: /^[a-z]+:/i.test(m[1]) ? "external" : "internal", target, exists: target in files };
  });
  return { ...summary(pack, path, d), pack_id: pack, revision, text: text(d), frontmatter: fm, body: d.body,
    trust: { tier: tierOf(fm), verified: verifiedOf(fm), status: String(fm.status ?? "stable"),
      stale_after: typeof fm.stale_after === "string" ? fm.stale_after : null, stale: false, trusted: typeof ext(fm).trusted === "boolean" ? ext(fm).trusted as boolean : null },
    sections: [...d.body.matchAll(/^# (.+)$/gm)].map((m) => ({ anchor: m[1].toLowerCase().replace(/\s+/g, "-"), heading: m[1] })), links };
}

function commit(pack: MockPack, files: Record<string, MockDoc>, reason: string, origin: string, deletes: string[] = []): MockRevision {
  const head = headOf(pack);
  const next: Record<string, MockDoc> = { ...clone(head?.files ?? {}), ...clone(files) };
  for (const p of deletes) delete next[p];
  const rev = { number: (head?.number ?? 0) + 1, author: `user:${ME}`, reason, origin, created_at: T, files: next };
  pack.revisions.push(rev);
  return rev;
}

function revisionInfo(pack: MockPack, path: string | null): KnowledgeRevisionInfo[] {
  const out: KnowledgeRevisionInfo[] = [];
  pack.revisions.forEach((r, i) => {
    const before = i > 0 ? pack.revisions[i - 1].files : {};
    const added = Object.keys(r.files).filter((p) => !(p in before)).sort();
    const removed = Object.keys(before).filter((p) => !(p in r.files)).sort();
    const changed = Object.keys(r.files).filter((p) => p in before && sha(before[p]) !== sha(r.files[p])).sort();
    if (path && ![...added, ...removed, ...changed].includes(path)) return;
    out.push({ number: r.number, parent: i > 0 ? pack.revisions[i - 1].number : null, author: r.author, reason: r.reason, origin: r.origin,
      files: Object.keys(r.files).length, content_digest: mockHash(JSON.stringify(r.files)), created_at: r.created_at, added, changed, removed,
      sha256: path && r.files[path] ? sha(r.files[path]) : null, conformance_problems: 0 });
  });
  return out.reverse();
}

const KIND_TYPE: Record<string, [string, string]> = {
  term: ["Glossary Term", "Definition"], definition: ["Definition", "Definition"], metric: ["Metric", "Definition"], rule: ["Business Rule", "Rule"],
  note: ["Note", "Note"], table_description: ["Table", "Description"], attested_computation: ["Attested Computation", "Statement"],
};

function primary(s: KnowledgeSuggestion): string {
  return s.kind === "attested_computation" ? "statement" : s.kind === "table_description" ? "description" : "body";
}

function review(decisions: { id: string; action: string; fields?: Dict; reason?: string | null }[]): ReviewResult {
  const files: Record<string, MockDoc> = {};
  const out: ReviewResult = { revision: null, approved: [], rejected: [], errors: [] };
  const decided: [KnowledgeSuggestion, "approved" | "rejected", string | null][] = [];
  const notes: Record<string, string> = {};
  for (const d of decisions) {
    const s = k.suggestions.find((x) => x.id === d.id);
    if (!s) { out.errors.push({ id: d.id, error: "not_found" }); continue; }
    if (s.status !== "pending") { out.errors.push({ id: d.id, error: "not_pending", status: s.status }); continue; }
    const origin = `review:${s.origin}`;
    if (s.kind === "glossary_term" || s.kind === "description_question") {
      // applied outside the pack: a glossary entry or catalog text (no document, no revision)
      if (d.action === "reject") {
        s.reason = d.reason ?? null;
        decided.push([s, "rejected", null]);
        continue;
      }
      const edited = d.action === "edit" ? (d.fields ?? {}) : {};
      const glossary = s.kind === "glossary_term";
      const missing = glossary ? s.fields.placeholder?.value === true && !("body" in edited)
        : !("description" in edited) && s.fields.description?.provenance?.source !== "model";
      if (missing) { out.errors.push({ id: d.id, error: glossary ? "definition_required" : "answer_required" }); continue; }
      for (const [name, value] of Object.entries(edited)) s.fields[name] = { value, confidence: 1, provenance: { source: "human", by: `user:${ME}` } };
      notes[s.id] = glossary ? "glossary term ctx_new created" : "catalog updated";
      decided.push([s, "approved", null]);
      continue;
    }
    if (d.action === "reject") {
      const path = `negative/${s.kind}-${s.title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}.md`;
      files[path] = { frontmatter: { type: "Negative Knowledge", title: `Rejected: ${s.title}`, status: "stable", verified: [{ by: `human:${ME}`, at: T }],
        analystos: { kind: "negative", origin, trusted: true, review: { suggestion_id: s.id, decision: "rejected" } } },
      body: `# Rejected\n\nA proposed ${s.kind.replace(/_/g, " ")} for ${s.subject} was reviewed and rejected.${d.reason ? `\n\n# Reason\n\n${d.reason}` : ""}` };
      s.reason = d.reason ?? null;
      decided.push([s, "rejected", path]);
      continue;
    }
    if (d.action === "edit") {
      for (const [name, value] of Object.entries(d.fields ?? {})) {
        s.fields[name] = { value, confidence: 1, provenance: { source: "human", by: `user:${ME}` } };
      }
    }
    const [type, heading] = KIND_TYPE[s.kind] ?? ["Note", "Note"];
    const syn = s.fields.synonyms?.value;
    files[s.path] = { frontmatter: { type, title: s.title, status: "stable", verified: [{ by: `human:${ME}`, at: T }], tags: [s.kind, "reviewed"],
      analystos: { kind: s.kind, origin, trusted: true, ...(Array.isArray(syn) ? { synonyms: syn } : {}),
        review: { suggestion_id: s.id, decision: "approved", origin: s.origin, confidence: s.confidence } } },
    body: `# ${heading}\n\n${String(s.fields[primary(s)]?.value ?? "")}` };
    decided.push([s, "approved", s.path]);
  }
  if (Object.keys(files).length) {
    const approved = decided.filter(([, st]) => st === "approved").length;
    out.revision = commit(k.packs[WS_PACK], files, `knowledge review: ${approved} approved, ${decided.length - approved} rejected`, "review").number;
  }
  for (const [s, st, path] of decided) {
    Object.assign(s, { status: st, decided_by: ME, decided_at: T, revision: out.revision });
    out[st].push({ id: s.id, path, ...(notes[s.id] ? { catalog: notes[s.id] } : {}) });
  }
  return out;
}

function save(pack: MockPack, body: Dict): [number, unknown] {
  if (!pack.info.writable) return [403, { error: { code: "forbidden", message: `knowledge pack ${pack.info.slug} is read-only`, details: {} } }];
  const path = String(body.path ?? "");
  const head = headOf(pack);
  const current = head?.files[path];
  const base = (body.base_sha256 as string | null) ?? null;
  if ((current && sha(current) !== base) || (!current && base)) {
    return [409, { error: { code: "conflict", message: `${path} changed since you opened it; reload and apply your edit again`, details: {} } }];
  }
  const fm = { ...(body.frontmatter as Dict) };
  if (!String(fm.type ?? "").trim()) return [422, { error: { code: "invalid_input", message: "frontmatter needs a non-empty `type` (OKF §4)", details: {} } }];
  const before = new Set(verifiedOf(current?.frontmatter ?? {}).map((e) => `${e.by}`));
  let kept = verifiedOf(fm);
  if (kept.some((e) => !before.has(String(e.by)))) {
    return [422, { error: { code: "invalid_input", message: "`verified` can only keep existing entries; use mark_reviewed to add your own review", details: {} } }];
  }
  if (body.mark_reviewed) kept = [...kept.filter((e) => e.by !== `human:${ME}`), { by: `human:${ME}`, at: T }];
  if (kept.length) fm.verified = kept;
  else delete fm.verified;
  const x = { ...ext(fm) };
  const prevReview = current ? ext(current.frontmatter).review : undefined;
  delete x.review;
  if (prevReview) x.reviewed_draft = prevReview;
  const prevOrigin = current ? ext(current.frontmatter).origin : undefined;
  if (prevOrigin && prevOrigin !== "user") x.origin_before_edit = prevOrigin;
  x.origin = "user"; // what the crawler invariants read as curated
  fm.analystos = x;
  const doc: MockDoc = { frontmatter: fm, body: String(body.body ?? "") };
  if (current && sha(current) === sha(doc)) return [200, { changed: false, revision: head!.number, document: full(pack.info.id, path, doc, head!.number) }];
  const rev = commit(pack, { [path]: doc }, String(body.reason || `${current ? "edit" : "create"} ${path}`), "studio");
  return [200, { changed: true, revision: rev.number, document: full(pack.info.id, path, doc, rev.number) }];
}

function graph(metrics: SemanticMetric[]): KnowledgeGraph {
  const nodes: KnowledgeGraphNode[] = [];
  const edges: KnowledgeGraphEdge[] = [];
  const node = (n: KnowledgeGraphNode) => { if (!nodes.some((x) => x.id === n.id)) nodes.push(n); return n.id; };
  const inc = node({ id: "table:ast_inc", kind: "table", label: "servicenow.incident" });
  const grp = node({ id: "table:ast_grp", kind: "table", label: "servicenow.sys_user_group" });
  const usr = node({ id: "table:ast_usr", kind: "table", label: "servicenow.sys_user" });
  edges.push({ source: inc, target: grp, kind: "join", governed: true, why: "validated or declared by a person", label: "assignment_group → sys_id" });
  edges.push({ source: inc, target: usr, kind: "join", governed: false, why: "discovered (discovered, confidence 0.62)", label: "caller_id → sys_id" });
  const ds = node({ id: "dataset:aos_incidents", kind: "dataset", label: "aos_incidents", status: "approved" });
  edges.push({ source: ds, target: inc, kind: "source", governed: true, why: "semantic model v1 approved", label: "" });
  const latest = new Map<string, SemanticMetric>();
  for (const m of metrics) if (m.status === "approved" || !latest.has(m.name) || latest.get(m.name)!.status !== "approved") latest.set(m.name, m);
  for (const m of latest.values()) {
    const id = node({ id: `metric:${m.name}`, kind: "metric", label: m.display_name ?? m.name, status: m.status });
    edges.push({ source: id, target: ds, kind: "measures", governed: m.status === "approved", why: `metric v${m.version} ${m.status}`, label: "" });
  }
  const head = headOf(k.packs[WS_PACK]);
  const byPath = new Map<string, string>();
  for (const [path, d] of Object.entries(head?.files ?? {})) {
    byPath.set(path, node({ id: `doc:${docId(WS_PACK, path)}`, kind: "document", label: String(d.frontmatter.title ?? path), path, pack_id: WS_PACK,
      trust_tier: tierOf(d.frontmatter) }));
  }
  for (const [path, d] of Object.entries(head?.files ?? {})) {
    const reviewed = tierOf(d.frontmatter) === "human-reviewed";
    const cols = ext(d.frontmatter).mapped_columns;
    if (Array.isArray(cols) && cols.some((c) => String(c).includes("incident"))) {
      edges.push({ source: byPath.get(path)!, target: inc, kind: "maps", governed: reviewed, why: `document ${tierOf(d.frontmatter)}`, label: "priority" });
    }
    for (const m of d.body.matchAll(/\]\(\/([^)]+)\)/g)) {
      const target = byPath.get(m[1]);
      if (target) edges.push({ source: byPath.get(path)!, target, kind: "links", governed: reviewed, why: `link in a ${tierOf(d.frontmatter)} document`, label: "" });
    }
  }
  for (const s of k.suggestions.filter((x) => x.status === "pending")) {
    const id = node({ id: `suggestion:${s.id}`, kind: "suggestion", label: s.title, suggestion_id: s.id, confidence: s.confidence });
    if (s.subject === "asset:ast_inc") edges.push({ source: id, target: inc, kind: "suggests", governed: false,
      why: `AI suggestion awaiting review (confidence ${s.confidence.toFixed(2)})`, label: "" });
  }
  return { nodes, edges, truncated: false, governed: edges.filter((e) => e.governed).length, inferred: edges.filter((e) => !e.governed).length };
}

const IMPORT_REPORT = (slug: string, changed: boolean): KnowledgeImportReport => ({
  pack_id: IMPORTED_PACK, slug, format: "atlas", okf_root: "bundle", revision: 1, changed, files: 9, documents: 7, ignored: [], conformance: [],
  dangling_links: 0, verified_claims: 3, attested_computations: 1, manifest: { okf_version: "0.2" }, warnings: [],
});

const PUSH_APPROVAL = (): Approval => ({
  id: "apr_kpush", workspace_id: "ws_demo", run_id: null, action: "knowledge.push", risk_tier: "medium",
  destination: "git:https://git.example.com/itsm/knowledge.git#main", affected_assets: [`knowledge_pack:${WS_PACK}`], payload_hash: "7a8b9c0d1e2f3a4b5c6d",
  plan_hash: null, policy_version: 2, requested_by: ME, status: "pending", decided_by: null, decided_at: null, reason: null, expires_at: "2099-01-01T00:00:00Z",
  evidence: {}, created_at: T, payload: { pack_id: WS_PACK, revision: headOf(k.packs[WS_PACK])?.number, branch: "main" },
});

type Reply = { status: number; body: unknown; contentType?: string };
const err = (status: number, code: string, message: string): Reply => ({ status, body: { error: { code, message, details: {} } } });

/** Route a knowledge request; null when the path is not a knowledge route. */
export function knowledgeRoute(m: string, p: string, url: URL, requestBody: string | null | undefined, ws: string,
  metrics: () => SemanticMetric[]): Reply | null {
  const W = `/workspaces/${ws}/knowledge`;
  if (!p.startsWith(W)) return null;
  const rest = p.slice(W.length);
  const body = requestBody && requestBody.trim().startsWith("{") ? JSON.parse(requestBody) as Dict : {};
  if (m === "GET" && rest === "/packs") {
    const order = { workspace: 0, imported: 1, platform: 2 } as Record<string, number>;
    return { status: 200, body: Object.values(k.packs).map(packInfo).sort((a, b) => order[a.kind] - order[b.kind]) };
  }
  if (m === "GET" && rest === "/suggestions") {
    const status = url.searchParams.get("status") || "pending";
    return { status: 200, body: k.suggestions.filter((s) => s.status === status) };
  }
  if (m === "POST" && rest === "/suggestions/preview") return { status: 200, body: goldenEffect((body.decisions ?? []) as never[]) };
  if (m === "POST" && rest === "/suggestions/review") {
    const decisions = (body.decisions ?? []) as { id: string; action: string }[];
    const effect = goldenEffect(decisions);
    if ((effect.counts as Dict).newly_failing && !body.override_reason) {
      return { status: 409, body: { error: { code: "conflict", message: "This batch makes 1 golden question(s) fail that pass today.",
        details: { code: "golden_regression", effect } } } };
    }
    return { status: 200, body: { ...review(decisions as never[]), effect } };
  }
  if (m === "GET" && rest === "/suggestions/summary") {
    const pending: Record<string, number> = {};
    for (const s of k.suggestions.filter((x) => x.status === "pending")) pending[s.kind] = (pending[s.kind] ?? 0) + 1;
    const questions = (pending.glossary_term ?? 0) + (pending.description_question ?? 0);
    return { status: 200, body: { pending, questions, total: Object.values(pending).reduce((a, b) => a + b, 0) } };
  }
  if (m === "POST" && rest === "/glossary/scan") {
    const fresh = scanSuggestions().filter((s) => !k.suggestions.some((x) => x.subject === s.subject));
    k.suggestions.push(...fresh);
    const terms = fresh.filter((s) => s.kind === "glossary_term").length;
    return { status: 200, body: { glossary_terms: terms, description_questions: fresh.length - terms, candidates: fresh.length,
      by_rule: { code_set: 1, abbreviation: 1 }, skipped_known: 3, skipped_decided: 0, assets: 2,
      model: { called: false, filled: 0, skipped: "no model available" } } };
  }
  if (m === "GET" && rest === "/graph") return { status: 200, body: graph(metrics()) };
  if (m === "GET" && rest === "/locate") {
    const want = url.searchParams.get("document_id");
    for (const pack of Object.values(k.packs)) {
      for (const path of Object.keys(headOf(pack)?.files ?? {})) {
        if (docId(pack.info.id, path) === want) {
          return { status: 200, body: { document_id: want, pack_id: pack.info.id, path, title: path, revision: headOf(pack)!.number } };
        }
      }
    }
    return err(404, "not_found", `document ${want} not found`);
  }
  if (m === "POST" && rest === "/import") {
    const slug = /name="slug"\r?\n\r?\n([^\r\n]+)/.exec(requestBody ?? "")?.[1] ?? "atlas-revenue";
    const exists = IMPORTED_PACK in k.packs;
    if (!exists) {
      k.packs[IMPORTED_PACK] = {
        info: { id: IMPORTED_PACK, kind: "imported", slug, title: slug, read_only: true, writable: false, okf_root: "bundle", okf_version: "0.2",
          git_remote: null, git_branch: "main", origin: { format: "atlas" }, updated_at: T },
        revisions: [{ number: 1, author: `user:${ME}`, reason: "import atlas-sample.zip", origin: "okf_import", created_at: T, files: {
          "bundle/concepts/purchase-agreement.md": { frontmatter: { type: "Atlas Business Concept", title: "Purchase agreement",
            verified: [{ by: "human:atlas-steward", at: "2026-08-01T00:00:00+00:00" }], atlas: { id: "c1" } },
          body: "# Definition\n\nA signed agreement with a customer to buy." },
        } }],
      };
    }
    return { status: 200, body: IMPORT_REPORT(slug, !exists) };
  }
  const packRoute = /^\/packs\/([^/]+)(\/documents|\/document|\/revisions|\/export|\/push\/request|\/push)$/.exec(rest);
  if (!packRoute) return null;
  const pack = k.packs[packRoute[1]];
  if (!pack) return err(404, "not_found", `knowledge pack ${packRoute[1]} not found`);
  const what = packRoute[2];
  const revParam = url.searchParams.get("revision");
  const rev = revParam ? pack.revisions.find((r) => r.number === Number(revParam)) : headOf(pack);
  if (m === "GET" && what === "/documents") {
    return { status: 200, body: { pack_id: pack.info.id, revision: rev?.number ?? null,
      documents: Object.entries(rev?.files ?? {}).sort(([a], [b]) => a.localeCompare(b)).map(([path, d]) => summary(pack.info.id, path, d)) } };
  }
  if (m === "GET" && what === "/document") {
    const path = url.searchParams.get("path") ?? "";
    const d = rev?.files[path];
    return d ? { status: 200, body: full(pack.info.id, path, d, rev!.number) } : err(404, "not_found", `${path} is not in pack ${pack.info.slug}`);
  }
  if (m === "PUT" && what === "/document") {
    const [status, out] = save(pack, body);
    return { status, body: out };
  }
  if (m === "GET" && what === "/revisions") return { status: 200, body: revisionInfo(pack, url.searchParams.get("path")) };
  if (m === "GET" && what === "/export") return { status: 200, body: "PK\u0003\u0004mock-okf-bundle", contentType: "application/zip" };
  if (m === "POST" && what === "/push/request") {
    return { status: 200, body: PUSH_APPROVAL() };
  }
  if (m === "POST" && what === "/push") return err(409, "conflict", `approval ${String(body.approval_id)} is pending, not approved`);
  return null;
}
