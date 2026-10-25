import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { fmtValue } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { valueApi } from "../lib/investigationValue";
import { roleAtLeast, to } from "../routes";
import { Card, ErrorBox, Field, Notice } from "./ui";

export function ReviewedContext({ wsId, role, compact = false }: { wsId: string; role?: string; compact?: boolean }) {
  const data = useAsync(() => valueApi.context(wsId), [wsId]);
  const act = useAction();
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [kind, setKind] = useState("term");
  const [subject, setSubject] = useState("");
  const [value, setValue] = useState("");
  const [notice, setNotice] = useState("");
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!data.data || !subject.trim() || !value.trim()) return;
    const group = kind === "accepted_corrections" ? "knowledge" : "domain";
    const result = await act.run(() => api.patchBrief(wsId, data.data!.version, { reason: "Reviewed reusable business context",
      ops: [{ op: "set", assertion: { group, field: kind, subject: subject.trim(), value: value.trim(), evidence: [], note: "Reviewed by the person saving this fact" } }] }));
    if (result) { setNotice("Saved as reviewed context. Future guided investigations reuse the new version."); setEditing(false); setSubject(""); setValue(""); await data.reload(); }
  };
  const facts = data.data?.facts ?? [];
  return <Card title="Business context we can reuse" label={compact ? "Reviewed context for this investigation" : "Reusable business context"} className="reviewed-context">
    <ErrorBox error={data.error} onRetry={data.reload} />
    {data.data && <p className="small muted">Brief v{data.data.version} · {facts.length} reviewed facts · {data.data.open_questions} suggestions awaiting review. Suggestions are excluded from guided investigation snapshots.</p>}
    <details open={editing || undefined}><summary>{compact ? "Show reviewed context" : "Review definitions and corrections"}</summary>
      {facts.length ? <ul className="list small">{facts.map((f) => <li key={f.key}><strong>{f.subject || f.field.replace(/_/g, " ")}</strong>: {fmtValue(f.value)} <span className="muted">· {f.review_state} · v{f.version}</span></li>)}</ul>
        : <p className="small muted">No reviewed facts yet. Add your business definitions before interpreting a finding.</p>}
      <p className="small"><Link to={to.data(wsId, "grounding")}>Review context coverage and map business terms</Link> · <Link to={to.data(wsId, "brief")}>Review all facts, suggestions, metrics and relationships in the catalog</Link></p>
      {!compact && roleAtLeast(role, "editor") && <button type="button" className="btn btn-sm" onClick={() => setEditing((v) => !v)}>{editing ? "Close context editor" : "Add a definition or correction"}</button>}
      {editing && <form className="form" aria-label="Save reviewed business context" onSubmit={save}>
        <Field label="Context type" htmlFor={`${id}-kind`}><select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="term">Business definition</option><option value="accepted_corrections">Reviewed correction</option><option value="prohibited_interpretation">Interpretation to avoid</option></select></Field>
        <Field label="Term or subject" htmlFor={`${id}-subject`} hint="For a table or column, use its catalog name so scope checks apply."><input id={`${id}-subject`} value={subject} onChange={(e) => setSubject(e.target.value)} required maxLength={400} /></Field>
        <Field label="Reviewed meaning" htmlFor={`${id}-value`}><textarea id={`${id}-value`} value={value} onChange={(e) => setValue(e.target.value)} required maxLength={2000} /></Field>
        <ErrorBox error={act.error} onRetry={data.reload} />
        <button className="btn btn-primary btn-sm" disabled={act.busy || !value.trim() || !subject.trim()}>Save reviewed context</button>
      </form>}
    </details>
    {notice && <Notice tone="success">{notice}</Notice>}
  </Card>;
}
