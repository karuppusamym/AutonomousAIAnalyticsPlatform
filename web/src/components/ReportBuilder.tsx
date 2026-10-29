import { useState, type FormEvent } from "react";
import {
  api, errorMessage, saveBlob, type ReportDocBlock, type ReportDocFormat, type ReportDocTurn, type ReportDocVersion,
  type ReportDocView, type ReportOp,
} from "../api";
import { useExpertView } from "../lib/expert";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Markdown } from "./Markdown";
import { Card, EmptyState, ErrorBox, Loading, Notice, StatusBadge, Tag } from "./ui";

const FORMAT_LABEL: Record<ReportDocFormat, string> = { pdf: "PDF", docx: "Word", pptx: "PowerPoint", html: "Web page" };
const KIND_WORD: Record<ReportDocBlock["kind"], string> = { text: "Text", chart: "Chart", kpi: "Number", finding: "Finding" };

/** One line per report op, in plain words (each version's diff). */
export function describeReportOp(op: ReportOp): string {
  const o = op as Record<string, unknown>;
  const ref = (o.ref ?? {}) as Record<string, unknown>;
  switch (op.op) {
    case "report.set_title": return `title “${o.title}”`;
    case "section.add": return `add section “${o.title}”`;
    case "section.remove": return `remove section ${o.section_id}`;
    case "section.rename": return `rename ${o.section_id} to “${o.title}”`;
    case "section.move": return `move section ${o.section_id} ${o.direction ?? `to position ${Number(o.position) + 1}`}`;
    case "text.set": return `write text in ${o.section_id}`;
    case "text.shorten": return `shorten the text of ${o.section_id}`;
    case "block.add": return `add ${String(ref.kind ?? "content").replace(/_/g, " ")} to ${o.section_id}`;
    case "block.remove": return `remove ${o.block_id}`;
    case "block.move": return `move ${o.block_id}${o.section_id ? ` to ${o.section_id}` : ` ${o.direction ?? ""}`}`;
    case "report.pin_schedule": return o.schedule_id ? `refresh and deliver with schedule ${o.schedule_id}` : "stop the scheduled delivery";
    case "export.request": return `ask to export as ${FORMAT_LABEL[o.format as ReportDocFormat] ?? o.format}`;
    case "refresh": return "refreshed from its sources";
    case "revert": return `revert to v${o.to_version}`;
    case "fork": return "draft copied from the exported report";
    case "create": return "created";
    default: return op.op;
  }
}

function fmtValue(v: number | null | undefined, format?: string): string {
  if (v == null) return "—";
  if (format === "percent") return `${(v * 100).toFixed(1)}%`;
  if (format === "hours") return `${v.toLocaleString()} h`;
  if (format === "currency") return `$${v.toLocaleString()}`;
  return v.toLocaleString();
}

function BlockView({ block, expert }: { block: ReportDocBlock; expert: boolean }) {
  switch (block.kind) {
    case "text":
      if (block.held) return <Notice tone="warning">{block.held}</Notice>;
      return block.markdown.trim() ? <Markdown text={block.markdown} /> : <p className="muted small">No text yet — say e.g. “set the summary to: …”.</p>;
    case "kpi":
      return (
        <div className="kpi">
          <div className="kpi-value">{fmtValue(block.value, block.format)}{block.unit ? ` ${block.unit}` : ""}</div>
          <div className="kpi-label">{block.title}{block.previous_value != null && <span className="muted small"> · was {fmtValue(block.previous_value, block.format)}</span>}</div>
        </div>
      );
    case "chart":
      return (
        <div className="stack small">
          <strong>{block.title}</strong>
          <span className="muted">{block.rows.length} rows · {block.columns.join(", ")}</span>
          {block.caption && <p>{block.caption}</p>}
        </div>
      );
    case "finding":
      return (
        <div className="stack small">
          <strong>{block.title}</strong>
          <p>{block.text}</p>
          {expert && <span className="muted">confidence {(block.confidence * 100).toFixed(0)}%</span>}
        </div>
      );
  }
}

/** The report's sections and blocks, in order: what the document will contain. */
export function ReportOutline({ report, focus, onFocus }: { report: ReportDocView; focus: string | null; onFocus: (id: string) => void }) {
  const expert = useExpertView();
  const doc = report.document;
  if (!doc.sections.length) return <EmptyState title="Empty report">Ask for a section, e.g. “add a section on EMEA backlog”.</EmptyState>;
  return (
    <ol className="list report-outline" aria-label="Report sections">
      {doc.sections.map((s) => (
        <li key={s.id} className={`list-item stack${focus === s.id ? " row-active" : ""}`}>
          <button type="button" className="btn btn-sm btn-ghost" aria-pressed={focus === s.id} onClick={() => onFocus(s.id)}
            title="Select this section for the next request"><strong>{s.title}</strong></button>
          {s.blocks.length === 0 && <span className="muted small">Nothing in this section yet.</span>}
          <ul className="list compact">
            {s.blocks.map((b) => (
              <li key={b.id} className="stack" data-block-kind={b.kind}>
                <span className="small muted">{KIND_WORD[b.kind]}{"source" in b && b.source ? ` · from ${b.source.label}` : ""}
                  {"source" in b && b.source?.labels?.includes("experimental") && <> <Tag tone="warning">experimental</Tag></>}
                  {expert && <> · <code>{b.id}</code></>}</span>
                <BlockView block={b} expert={expert} />
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ol>
  );
}

interface Turn { text: string; result?: ReportDocTurn; error?: string }

/** The report chat panel: what a person types becomes typed report operations (the `report_design` ladder). */
export function ReportChatPanel({ report, focus, onUpdated }: {
  report: ReportDocView; focus: string | null; onUpdated: (r: ReportDocView, turn: ReportDocTurn) => void;
}) {
  const expert = useExpertView();
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    const said = text.trim();
    if (!said) return;
    setText("");
    setBusy(true);
    try {
      const result = await api.reportDocMessage(report.id, { text: said, focus, base_version: report.version });
      setTurns((t) => [...t, { text: said, result }]);
      onUpdated(result.report, result);
    } catch (err) {
      setTurns((t) => [...t, { text: said, error: errorMessage(err) }]);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Shape it by asking" label="Report builder chat">
      {/* focusable: the log scrolls, so keyboard users must be able to reach it */}
      <ol className="chat-log builder-chat" aria-live="polite" aria-label="Report conversation" tabIndex={0}>
        {turns.map((t, i) => (
          <li key={i} className="chat-msg">
            <p><strong>You:</strong> {t.text}</p>
            {t.error && <p className="warn-text small">{t.error}</p>}
            {t.result && (
              <div className="stack small">
                {expert && (t.result.proposal?.ops.length ?? 0) > 0 && (
                  <p>Changes ({t.result.proposal?.answered_by === "rules" ? "rules" : "model, checked"}): {t.result.proposal?.ops.map((o, j) => <Tag key={j}>{describeReportOp(o)}</Tag>)}</p>
                )}
                {t.result.status === "applied" && <p className="ok-text">Done: the report is now version {t.result.report.version}.</p>}
                {t.result.status === "unchanged" && !t.result.export && <p className="muted">Nothing changed.</p>}
                {t.result.forked_from && <Notice tone="info">This report was already exported, so the change went to a draft copy.</Notice>}
                {t.result.export && <Notice tone="info">Export requested: approval {t.result.export.approval.id} waits for an approver. Download it below once approved.</Notice>}
                {(t.result.refused ?? []).map((r, j) => <p key={j} className="warn-text">Not done — {r.reason}</p>)}
                {(t.result.proposal?.notes ?? []).map((n, j) => <p key={j} className="muted">{n}</p>)}
              </div>
            )}
          </li>
        ))}
      </ol>
      <form className="row" onSubmit={(e) => void send(e)}>
        <label htmlFor="report-say" className="sr-only">Describe a change to the report</label>
        <input id="report-say" className="grow" value={text} onChange={(e) => setText(e.target.value)} maxLength={4000}
          placeholder="e.g. “add a section on EMEA backlog”, “include the resolution trend chart”, “make the summary shorter”" />
        <button type="submit" className="btn btn-primary" disabled={busy || !text.trim()}>{busy ? "Working…" : "Send"}</button>
      </form>
      {focus && <p className="muted small">Requests about “this section” refer to <strong>{report.document.sections.find((s) => s.id === focus)?.title ?? focus}</strong>.</p>}
    </Card>
  );
}

function ReportPreview({ report }: { report: ReportDocView }) {
  const expert = useExpertView();
  const [open, setOpen] = useState(false);
  const preview = useAsync(() => (open ? api.reportDocPreview(report.id) : Promise.resolve(null)), [report.id, report.version, open]);
  return (
    <Card title="Preview" actions={<button type="button" className="btn btn-sm" aria-expanded={open} onClick={() => setOpen(!open)}>
      {open ? "Close preview" : "Show preview"}</button>}>
      {open && (preview.loading && !preview.data ? <Loading label="Rendering…" /> : preview.data ? (
        <>
          {/* The report HTML is rendered in a fully sandboxed frame (no scripts, no same-origin), never in the page DOM. */}
          <iframe className="report-preview" title={`Preview: ${report.document.title}`} sandbox="" srcDoc={preview.data.html} />
          {expert && <p className="muted small">Snapshot <code>{preview.data.snapshot_hash}</code></p>}
        </>
      ) : <ErrorBox error={preview.error} />)}
      {!open && <p className="muted small">See the report as it will be exported: every number comes from the charts, numbers and findings it includes.</p>}
    </Card>
  );
}

function ReportVersions({ report, onUpdated }: { report: ReportDocView; onUpdated: (r: ReportDocView) => void }) {
  const expert = useExpertView();
  const versions = useAsync(() => api.reportDocVersions(report.id), [report.id, report.version]);
  const act = useAction();
  const revert = async (v: ReportDocVersion) => {
    const r = await act.run(() => api.reportDocRevert(report.id, v.version, report.version));
    if (r) onUpdated(r.report);
  };
  return (
    <Card title="Versions">
      <ErrorBox error={versions.error ?? act.error} />
      {versions.loading && !versions.data ? <Loading /> : (
        <ol className="list compact">
          {(versions.data ?? []).map((v) => (
            <li key={v.version} className="list-item">
              <span><strong>v{v.version}</strong> {v.diff.map((o, i) => <Tag key={i}>{describeReportOp(o)}</Tag>)}</span>
              <span className="muted small">{expert ? `${v.created_by ?? "—"} · ` : ""}{fmtDate(v.created_at)}</span>
              {v.version !== report.version && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void revert(v)}>Go back to v{v.version}</button>}
            </li>
          ))}
        </ol>
      )}
    </Card>
  );
}

function ReportExport({ report, role, onUpdated, pending }: {
  report: ReportDocView; role: string | null; onUpdated: (r: ReportDocView) => void; pending: { id: string; format: ReportDocFormat } | null;
}) {
  const act = useAction();
  const [format, setFormat] = useState<ReportDocFormat>("pdf");
  const [request, setRequest] = useState<{ id: string; format: ReportDocFormat } | null>(null);
  const current = request ?? pending;
  const canEdit = role === "owner" || role === "editor";
  const ask = async () => {
    const r = await act.run(() => api.reportDocExportRequest(report.id, format));
    if (r) {
      setRequest({ id: r.approval.id, format });
      onUpdated(await api.reportDoc(report.id));
    }
  };
  const download = async () => {
    if (!current) return;
    const file = await act.run(() => api.reportDocExport(report.id, current.format, current.id));
    if (file) {
      saveBlob(file);
      onUpdated(await api.reportDoc(report.id));
    }
  };
  return (
    <Card title="Export" actions={<StatusBadge status={report.status} />}>
      <p className="small muted">An export leaves the platform, so an approver signs off this exact version first. Changing the report after asking cancels the request.</p>
      {canEdit ? (
        <div className="row wrap">
          <label htmlFor="report-format" className="sr-only">Export format</label>
          <select id="report-format" value={format} onChange={(e) => setFormat(e.target.value as ReportDocFormat)}>
            {(Object.keys(FORMAT_LABEL) as ReportDocFormat[]).map((f) => <option key={f} value={f}>{FORMAT_LABEL[f]}</option>)}
          </select>
          <button type="button" className="btn btn-primary btn-sm" disabled={act.busy} onClick={() => void ask()}>Ask to export</button>
        </div>
      ) : <p className="small muted">Editors can ask to export this report.</p>}
      {current && (
        <div className="row wrap">
          <span className="small">Approval <code>{current.id}</code> for {FORMAT_LABEL[current.format]}: once an approver accepts it,</span>
          <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void download()}>Download {FORMAT_LABEL[current.format]}</button>
        </div>
      )}
      <ErrorBox error={act.error} />
    </Card>
  );
}

/** A report document (CONV-04): outline, chat panel, preview, versions and approval-bound export. */
export function ReportBuilder({ docId, role, onSwitch }: { docId: string; role: string | null; onSwitch?: (id: string) => void }) {
  const doc = useAsync(() => api.reportDoc(docId), [docId]);
  const [focus, setFocus] = useState<string | null>(null);
  const [pending, setPending] = useState<{ id: string; format: ReportDocFormat } | null>(null);
  if (doc.loading && !doc.data) return <Loading label="Loading report…" />;
  if (!doc.data) return <ErrorBox error={doc.error} onRetry={() => void doc.reload()} />;
  const r = doc.data;
  const updated = (next: ReportDocView, turn?: ReportDocTurn) => {
    if (turn?.export) setPending({ id: turn.export.approval.id, format: turn.export.format as ReportDocFormat });
    if (next.id !== r.id && onSwitch) onSwitch(next.id);  // an edit of an exported report forked a draft
    doc.setData(next);
  };
  const sensitive = r.document.sensitivity && r.document.sensitivity !== "internal" && r.document.sensitivity !== "public";
  return (
    <div className="stack report-builder">
      <Card title={<>{r.document.title} <span className="muted small">· v{r.version}</span></>} actions={<>
        {sensitive && <Tag tone="warning">{r.document.sensitivity}</Tag>}
        {r.document.schedule_id && <Tag tone="info">delivered on a schedule</Tag>}
      </>} label="Report outline">
        {r.document.objective && <p className="muted small">{r.document.objective}</p>}
        <ReportOutline report={r} focus={focus} onFocus={(id) => setFocus(focus === id ? null : id)} />
      </Card>
      <ReportChatPanel report={r} focus={focus} onUpdated={updated} />
      <ReportPreview report={r} />
      <ReportExport report={r} role={role} onUpdated={(n) => updated(n)} pending={pending} />
      <ReportVersions report={r} onUpdated={(n) => updated(n)} />
    </div>
  );
}

/** Start a report document (Outputs › Reports), then shape it by asking. */
export function NewReportDoc({ wsId, onCreated }: { wsId: string; onCreated: (id: string) => void }) {
  const [title, setTitle] = useState("");
  const act = useAction();
  const create = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.createReportDoc(wsId, { title: title.trim() }));
    if (r) onCreated(r.id);
  };
  return (
    <form className="row" onSubmit={(e) => void create(e)} aria-label="Write a report">
      <label htmlFor="new-report-title" className="sr-only">Report title</label>
      <input id="new-report-title" className="grow" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Report title, e.g. Weekly operations" maxLength={300} />
      <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !title.trim()}>Start report</button>
      <ErrorBox error={act.error} />
    </form>
  );
}
