import { useId, useState, type FormEvent } from "react";
import {
  api, saveBlob, type ContextExportFormat, type ContextPreview, type GlossaryImportReport, type GlossarySheetFormat, type Source,
} from "../api";
import { fmtNumber } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Card, CodeBlock, DataTable, ErrorBox, Field, KeyValue, Loading, Notice, Tag } from "./ui";

const FORMATS: { id: ContextExportFormat; label: string; hint: string }[] = [
  { id: "okf", label: "OKF bundle (.zip)", hint: "Open Knowledge Format: re-imports here or into another workspace as a knowledge pack." },
  { id: "json", label: "JSON (.json)", hint: "One versioned file with a content digest, for scripts and other tools." },
  { id: "markdown", label: "Markdown (.md)", hint: "One readable file to review or share." },
];

/** Plain names for the parts of a prompt (context compiler sections and the caller's inputs). */
const SECTION_LABELS: Record<string, string> = {
  system: "Instructions (fixed per purpose)", header: "Workspace header", objective: "Objective", question: "Your question",
  analysis_context: "Pinned analysis context", catalog: "Tables and columns", glossary: "Glossary", business_rules: "Business rules",
  metrics: "Metrics", external: "Other knowledge sources", prior_findings: "Earlier verified findings",
  negative_knowledge: "Hypotheses already rejected", episodes: "Past runs", omitted: "Note of what was left out",
  user_instructions: "Your instructions", redacted_values: "Redaction note",
};

const plural = (n: number, one: string, many = `${one}s`) => `${fmtNumber(n)} ${n === 1 ? one : many}`;

const FALLBACK_PURPOSES =[{ purpose: "sql_generation", label: "Writing SQL for Ask", description: "" }];

/**
 * Data → Catalog & definitions → Import & export (Stream D): download everything the platform knows about the
 * workspace's data — or one source's — as OKF, JSON or Markdown, and see exactly what a model would be sent for a
 * purpose (token estimate, cache status, every section), without calling one. Editors see the shared context
 * cache; owners can clear it.
 */
export function ContextExport({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const list = sources.data ?? [];
  return (
    <div className="stack">
      <DownloadCard wsId={wsId} sources={list} />
      <GlossarySheetCard wsId={wsId} canEdit={canEdit} />
      <AgentViewCard wsId={wsId} sources={list} />
      {canEdit && <CacheCard wsId={wsId} canClear={ws.data?.role === "owner"} />}
    </div>
  );
}

function SourceSelect({ id, sources, value, onChange, label }: {
  id: string; sources: Source[]; value: string; onChange: (v: string) => void; label: string;
}) {
  return (
    <Field label={label} htmlFor={id}>
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Whole workspace</option>
        {sources.map((s) => <option key={s.id} value={s.id}>{s.name} ({s.kind})</option>)}
      </select>
    </Field>
  );
}

function DownloadCard({ wsId, sources }: { wsId: string; sources: Source[] }) {
  const id = useId();
  const [scope, setScope] = useState("");
  const [format, setFormat] = useState<ContextExportFormat>("okf");
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setDone(null);
    const f = await act.run(() => api.exportContext(wsId, format, scope || null));
    if (f) {
      saveBlob(f);
      setDone(f.filename);
    }
  };
  return (
    <Card title="Download context">
      <p className="small muted">Everything the platform knows about this data, in one file: the brief (facts and open questions), sources and
        their last crawl, every table and column with its meaning, tags and profile, joins, the data model and its suggestion, approved
        metrics, glossary and rules, analysis contexts and the list of knowledge documents.</p>
      <Notice tone="info">Never included: passwords and secret references, connection settings, or rows of data. Sensitive and personal
        columns keep only completeness and cardinality.</Notice>
      <form className="form" onSubmit={submit} aria-label="Download the workspace context" noValidate>
        <div className="form-row">
          <SourceSelect id={`${id}-scope`} sources={sources} value={scope} onChange={setScope} label="Scope" />
          <Field label="Format" htmlFor={`${id}-format`} hint={FORMATS.find((f) => f.id === format)?.hint}>
            <select id={`${id}-format`} value={format} onChange={(e) => setFormat(e.target.value as ContextExportFormat)}>
              {FORMATS.map((f) => <option key={f.id} value={f.id}>{f.label}</option>)}
            </select>
          </Field>
        </div>
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Preparing…" : "Download"}</button>
        </div>
      </form>
      {done && <p className="small" role="status">Downloaded {done}.</p>}
    </Card>
  );
}

/** Stewards' round trip (review P2-11): the glossary as CSV/XLSX, and a file back in as review drafts. */
function GlossarySheetCard({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const id = useId();
  const [format, setFormat] = useState<GlossarySheetFormat>("xlsx");
  const [note, setNote] = useState<string | null>(null);
  const [report, setReport] = useState<GlossaryImportReport | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const dl = useAction();
  const imp = useAction();
  const download = async () => {
    setNote(null);
    const f = await dl.run(() => api.exportGlossarySheet(wsId, format));
    if (f) {
      saveBlob(f);
      setNote(`Downloaded ${f.filename}: ${plural(f.terms, "term")}${f.omittedTerms || f.omittedLinks
        ? `; left out for your access: ${plural(f.omittedTerms, "term")}, ${plural(f.omittedLinks, "column link")}` : ""}.`);
    }
  };
  const upload = async (dryRun: boolean) => {
    if (!file) return;
    const r = await imp.run(() => api.importGlossarySheet(wsId, file, dryRun));
    if (r) setReport(r);
  };
  const problems = report?.results.filter((r) => r.status === "error" || r.status === "skipped" || r.warnings?.length) ?? [];
  return (
    <Card title="Glossary spreadsheet">
      <p className="small muted">Edit the glossary in a spreadsheet: term, definition, synonyms, linked columns, owner, trust and status.
        Importing never changes the glossary. Each row becomes a draft in the review queue (source: spreadsheet import) and counts as
        reviewed only after a person approves it.</p>
      <div className="form-row">
        <Field label="Format" htmlFor={`${id}-fmt`}>
          <select id={`${id}-fmt`} value={format} onChange={(e) => setFormat(e.target.value as GlossarySheetFormat)}>
            <option value="xlsx">Excel (.xlsx)</option>
            <option value="csv">CSV (.csv)</option>
          </select>
        </Field>
      </div>
      <div className="form-actions">
        <button type="button" className="btn btn-primary" disabled={dl.busy} onClick={() => void download()}>
          {dl.busy ? "Preparing…" : "Download glossary"}
        </button>
      </div>
      <ErrorBox error={dl.error} />
      {note && <p className="small" role="status">{note}</p>}
      {canEdit ? (
        <form className="form" onSubmit={(e) => { e.preventDefault(); void upload(false); }} aria-label="Import a glossary spreadsheet" noValidate>
          <Field label="Import a spreadsheet" htmlFor={`${id}-file`} hint="A .csv or .xlsx in the same format, up to 2 MB and 2,000 rows.">
            <input id={`${id}-file`} type="file" accept=".csv,.xlsx" onChange={(e) => { setFile(e.target.files?.[0] ?? null); setReport(null); }} />
          </Field>
          <ErrorBox error={imp.error} />
          <div className="form-actions">
            <button type="button" className="btn btn-sm" disabled={!file || imp.busy} onClick={() => void upload(true)}>Check only</button>
            <button type="submit" className="btn btn-primary" disabled={!file || imp.busy}>{imp.busy ? "Reading…" : "Queue for review"}</button>
          </div>
        </form>
      ) : <p className="small" role="note">Editors can import a glossary spreadsheet.</p>}
      {report && (
        <div className="stack" aria-label="Import result">
          <Notice tone={report.error ? "warning" : "info"}>
            {report.dry_run ? "Checked, nothing queued: " : "Queued for review: "}{plural(report.queued, "draft")}; already waiting {report.already_pending};
            already decided {report.already_decided}; unchanged {report.unchanged}; skipped {report.skipped}; with errors {report.error}.
            {report.ignored_headers.length > 0 && ` Ignored columns: ${report.ignored_headers.join(", ")}.`}
          </Notice>
          {problems.length > 0 && (
            <DataTable caption="Rows that need attention" columns={["Row", "Term", "Result", "Detail"]}
              rows={problems.map((r) => [r.row, r.term, r.status, [r.error ?? r.reason, ...(r.warnings ?? [])].filter(Boolean).join("; ")])} />
          )}
        </div>
      )}
    </Card>
  );
}

function AgentViewCard({ wsId, sources }: { wsId: string; sources: Source[] }) {
  const id = useId();
  const purposes = useAsync(() => api.contextPurposes(wsId), [wsId]);
  const [purpose, setPurpose] = useState("sql_generation");
  const [question, setQuestion] = useState("");
  const [scope, setScope] = useState("");
  const [preview, setPreview] = useState<ContextPreview | null>(null);
  const act = useAction();
  const dl = useAction();
  const options = purposes.data?.length ? purposes.data : FALLBACK_PURPOSES;
  const chosen = options.find((p) => p.purpose === purpose);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const p = await act.run(() => api.contextPreview(wsId, purpose, question.trim(), scope || null));
    if (p) setPreview(p);
  };
  const download = async () => {
    const f = await dl.run(() => api.downloadContextPreview(wsId, purpose, question.trim(), scope || null));
    if (f) saveBlob(f);
  };
  return (
    <Card title="What the agents see">
      <p className="small muted">The exact context a model would be sent for a task, built the same way the real call builds it, with
        your access. No model is called and nothing is spent.</p>
      <form className="form" onSubmit={submit} aria-label="Preview the model context" noValidate>
        <div className="form-row">
          <Field label="Task" htmlFor={`${id}-purpose`} hint={chosen?.description || undefined}>
            <select id={`${id}-purpose`} value={purpose} onChange={(e) => setPurpose(e.target.value)}>
              {options.map((p) => <option key={p.purpose} value={p.purpose}>{p.label}</option>)}
            </select>
          </Field>
          <SourceSelect id={`${id}-scope`} sources={sources} value={scope} onChange={setScope} label="Tables from" />
        </div>
        <Field label="Question (optional)" htmlFor={`${id}-q`} hint="Relevance decides which tables and knowledge are sent, so ask what you would ask.">
          <input id={`${id}-q`} value={question} maxLength={4000} placeholder="e.g. Why did revenue drop last month?"
            onChange={(e) => setQuestion(e.target.value)} />
        </Field>
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Building…" : "Preview"}</button>
        </div>
      </form>
      {preview && <PreviewView preview={preview} onDownload={() => void download()} busy={dl.busy} />}
      <ErrorBox error={dl.error} />
    </Card>
  );
}

function PreviewView({ preview, onDownload, busy }: { preview: ContextPreview; onDownload: () => void; busy: boolean }) {
  const t = preview.estimated_tokens;
  const c = preview.cache;
  const cacheText = !c.enabled ? "Context cache is turned off"
    : !c.key ? "Not shared for this task (it is reused within one run or Ask thread)"
      : c.hit ? "Knowledge lookup already cached: this call would reuse it" : "Not cached yet: the first call would compute and keep it";
  return (
    <div className="stack" aria-label="Context preview">
      {preview.refused && <Notice tone="warning">The model would not be called: {preview.refused}</Notice>}
      {preview.trimmed && <Notice tone="warning">The context is over the prompt limit; whole entries would be left out to fit.</Notice>}
      <KeyValue items={[
        ["Estimated size", <span key="t"><strong>~{fmtNumber(t.total)} tokens</strong> <span className="muted">
          ({fmtNumber(t.stable)} stable, cached between calls · {fmtNumber(t.volatile)} per call)</span></span>],
        ["Context cache", <span key="c">{cacheText} <Tag tone={c.shared ? "success" : "neutral"}>{c.shared ? "shared" : "this server"}</Tag></span>],
        ["Tables in scope", `${preview.scope.assets.length}${preview.scope.denied_columns
          ? ` (${plural(preview.scope.denied_columns, "column")} hidden by policy)` : ""}`],
        ["Left out", preview.omitted.length ? `${plural(preview.omitted.length, "entry", "entries")} (not relevant, or over a cap)` : "nothing"],
        ["Knowledge version", preview.knowledge_version ? <code key="k">{preview.knowledge_version.slice(0, 12)}</code> : "—"],
      ]} />
      <DataTable caption="Prompt sections" columns={["Section", "Sent", "Items", "Characters"]}
        rows={preview.sections.map((s) => [SECTION_LABELS[s.name] ?? s.name, s.part === "stable" ? "cached prefix" : "every call",
          s.no_match ? "nothing relevant" : s.items, fmtNumber(s.chars)])} />
      <details>
        <summary>Workspace context (cached prefix)</summary>
        <CodeBlock code={preview.preamble_text || "(empty)"} label="Preamble" />
      </details>
      <details>
        <summary>Per-call inputs</summary>
        <CodeBlock code={preview.volatile_text} label="JSON" />
      </details>
      <details>
        <summary>Instructions ({preview.system_prompt})</summary>
        <CodeBlock code={preview.system_text} label="System" />
      </details>
      <div className="form-actions">
        <button type="button" className="btn btn-sm" disabled={busy} onClick={onDownload}>Download as text</button>
      </div>
    </div>
  );
}

function CacheCard({ wsId, canClear }: { wsId: string; canClear: boolean }) {
  const stats = useAsync(() => api.contextCache(wsId), [wsId]);
  const act = useAction();
  const [cleared, setCleared] = useState<number | null>(null);
  const clear = async () => {
    const r = await act.run(() => api.clearContextCache(wsId));
    if (r) {
      setCleared(r.cleared);
      void stats.reload();
    }
  };
  const s = stats.data;
  const rows = s ? Object.entries(s.by_kind).flatMap(([kind, purposes]) => Object.entries(purposes).map(([purpose, n]) =>
    [kind === "retrieval" ? "Knowledge lookup" : kind === "compiled" ? "Compiled context" : kind, purpose, n.hits, n.misses, fmtNumber(n.chars_reused)])) : [];
  return (
    <Card title="Context cache" actions={<button type="button" className="btn btn-xs btn-ghost" onClick={() => void stats.reload()}>Refresh</button>}>
      <p className="small muted">Compiled context and knowledge lookups are kept for a while and reused by every step, worker and Ask
        question that needs the same thing. Clearing it only makes the next calls rebuild them.</p>
      <ErrorBox error={stats.error} onRetry={stats.reload} />
      {!s && !stats.error && <Loading />}
      {s && (
        <>
          <KeyValue items={[
            ["Entries", `${s.total_entries}${Object.keys(s.entries).length ? ` (${Object.entries(s.entries).map(([k, n]) => `${n} ${k}`).join(", ")})` : ""}`],
            ["Reused", `${s.totals.hits} times, ${fmtNumber(s.totals.chars_reused)} characters not rebuilt`],
            ["Built", `${s.totals.misses} times`],
            ["Kept for", s.enabled ? `${Math.round(s.ttl_seconds / 60)} minutes${s.shared ? ", shared by every worker" : ", on this server only"}` : "turned off"],
          ]} />
          {rows.length > 0 && <DataTable caption="Reuse by task" columns={["Kind", "Task", "Reused", "Built", "Characters reused"]} rows={rows} />}
        </>
      )}
      {canClear ? (
        <div className="form-actions">
          <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void clear()}>Clear cache</button>
        </div>
      ) : <p className="small" role="note">Owners can clear the cache.</p>}
      {cleared !== null && <p className="small" role="status">Cleared {plural(cleared, "entry", "entries")}.</p>}
      <ErrorBox error={act.error} />
    </Card>
  );
}
