import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { to } from "../routes";
import { api, saveBlob, type Artifact, type ReportContent } from "../api";
import { ErrorBox, Field, Tag } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { REPORT_FORMATS, REPORT_KINDS } from "../lib/schedules";

const FORMAT_LABEL: Record<string, string> = { pdf: "PDF", xlsx: "Excel", docx: "Word", pptx: "PowerPoint", html: "HTML", md: "Markdown" };

export function reportFormats(a: Artifact): string[] {
  const files = (a.content as ReportContent).files ?? {};
  const known: string[] = REPORT_FORMATS.filter((f) => f in files);
  return known.concat(Object.keys(files).filter((f) => !known.includes(f)));
}

/** Formats that were asked for but could not be rendered here, each with its reason (e.g. the `reports` extra). */
export function unavailableFormats(a: Artifact): [string, string][] {
  return Object.entries((a.content as ReportContent).unavailable_formats ?? {});
}

export { NewReportDoc, ReportBuilder } from "../components/ReportBuilder";

const KIND_LABEL: Record<string, string> = { ask_answer: "Ask answer", document: "Report", digest: "Dashboard digest" };

export function ReportRow({ wsId, report: a, highlighted = false }: { wsId: string; report: Artifact; highlighted?: boolean }) {
  const c = a.content as ReportContent;
  const formats = reportFormats(a);
  const missing = unavailableFormats(a);
  const act = useAction();
  const [preview, setPreview] = useState<string | null>(null);
  const download = async (fmt: string) => {
    const file = await act.run(() => api.downloadReport(a.id, fmt));
    if (file) saveBlob(file);
  };
  const togglePreview = async () => {
    if (preview !== null) {
      setPreview(null);
      return;
    }
    const file = await act.run(() => api.downloadReport(a.id, "html"));
    if (file) setPreview(await file.blob.text());
  };
  useEffect(() => {
    if (highlighted) document.getElementById(`report-${a.id}`)?.scrollIntoView?.({ block: "nearest" });
  }, [highlighted, a.id]);
  return (
    <section id={`report-${a.id}`} className={`card ${highlighted ? "row-active" : ""}`} aria-label={`Report ${c.title ?? a.name}`}>
      <header className="card-header">
        <h2 className="card-title">{c.title ?? a.name} <Tag tone="info">{KIND_LABEL[c.kind ?? ""] ?? c.kind ?? a.name}</Tag></h2>
        <div className="card-actions small muted">{fmtDate(a.created_at)}</div>
      </header>
      <div className="card-body">
        <div className="chip-row small">
          {a.run_id && <Link to={to.run(wsId, a.run_id)}>Source investigation</Link>}
          {c.origin?.type === "ask" && c.origin.thread_id && <Link to={`${to.ask(wsId)}?thread=${encodeURIComponent(c.origin.thread_id)}`}>Source question</Link>}
          {typeof c.insights === "number" && <span>{c.insights} findings</span>}
          {typeof c.metrics === "number" && <span>{c.metrics} metrics</span>}
          {typeof c.alerts === "number" && <span>{c.alerts} alerts</span>}
          <span className="muted">v{a.version}</span>
        </div>
        <div className="form-actions report-actions">
          {formats.length === 0 && <span className="muted small">No files recorded.</span>}
          {formats.map((f) => (
            <button key={f} type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void download(f)}>
              Download {FORMAT_LABEL[f] ?? f}</button>
          ))}
          {formats.includes("html") && (
            <button type="button" className="btn btn-sm btn-ghost" disabled={act.busy} aria-expanded={preview !== null} onClick={() => void togglePreview()}>
              {preview !== null ? "Close preview" : "Preview"}</button>
          )}
        </div>
        {missing.length > 0 && (
          <ul className="list compact small" aria-label="Formats not generated">
            {missing.map(([f, why]) => <li key={f} className="muted">{FORMAT_LABEL[f] ?? f} not generated: {why}</li>)}
          </ul>
        )}
        <ErrorBox error={act.error} />
        {preview !== null && (
          // The report HTML is rendered in a fully sandboxed frame (no scripts, no same-origin, no forms), never in the page DOM.
          <iframe className="report-preview" title={`Preview: ${c.title ?? a.name}`} sandbox="" srcDoc={preview} />
        )}
      </div>
    </section>
  );
}

/** Reports are an Outputs filter: this form sits above the list when the filter is Reports. */
export function GenerateReportForm({ wsId, onCreated }: { wsId: string; onCreated: (a: Artifact) => void }) {
  const id = useId();
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const completed = useMemo(() => (runs.data ?? []).filter((r) => r.status === "COMPLETED"), [runs.data]);
  const [runId, setRunId] = useState("");
  const [kind, setKind] = useState("executive");
  const [formats, setFormats] = useState<string[]>(["html", "pdf", "xlsx"]);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!formats.length) {
      act.setError("Choose at least one format.");
      return;
    }
    const r = await act.run(() => api.createReport(wsId, { run_id: runId || null, kind, formats }));
    if (r) onCreated(r);
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Generate report">
      <div className="form-row">
        <Field label="Investigation" htmlFor={`${id}-run`} hint={runs.data && !completed.length ? "No completed investigations yet." : undefined}>
          <select id={`${id}-run`} value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">Latest completed investigation</option>
            {completed.map((r) => <option key={r.id} value={r.id}>{fmtDate(r.finished_at ?? r.created_at)} — {r.objective.slice(0, 70)}</option>)}
          </select>
        </Field>
        <Field label="Kind" htmlFor={`${id}-kind`}>
          <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value)}>
            {REPORT_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
        <fieldset className="autonomy">
          <legend>Formats</legend>
          <div className="toggle-group">
            {REPORT_FORMATS.map((f) => (
              <label key={f} className="toggle small">
                <input type="checkbox" checked={formats.includes(f)}
                  onChange={(e) => setFormats((prev) => (e.target.checked ? [...prev, f] : prev.filter((x) => x !== f)))} /> {FORMAT_LABEL[f] ?? f}
              </label>
            ))}
          </div>
        </fieldset>
      </div>
      <ErrorBox error={runs.error} />
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Generating…" : "Generate report"}</button>
      </div>
    </form>
  );
}
