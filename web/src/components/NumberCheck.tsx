import { useId, useState } from "react";
import { api, type NumberCheck, type NumberCheckHandoff } from "../api";
import { useAction } from "../lib/hooks";
import { fmtNumber } from "../lib/format";
import { TRUST, trustOfAuthority } from "../lib/trust";
import { TrustBadge } from "./TrustBadge";
import { CodeBlock, ErrorBox, Field, KeyValue, Notice } from "./ui";

const VERDICT: Record<NumberCheck["verdict"], { tone: "success" | "danger" | "warning"; title: string }> = {
  confirmed: { tone: "success", title: "Confirmed" },
  contradicted: { tone: "danger", title: "Contradicted" },
  unverifiable: { tone: "warning", title: "Could not check" },
};

/**
 * S07-T8: check a number someone quotes (another BI tool, a spreadsheet, a slide) against this workspace's governed
 * answer. The question runs through Ask under the viewer's scope; the claimed value is compared in code and never
 * shown to a model.
 */
export function NumberCheckPanel({ wsId, onOpenThread }: { wsId: string; onOpenThread?: (threadId: string) => void }) {
  const id = useId();
  const [question, setQuestion] = useState("");
  const [claimed, setClaimed] = useState("");
  const [label, setLabel] = useState("");
  const [source, setSource] = useState("");
  const [out, setOut] = useState<NumberCheck | null>(null);
  const act = useAction();

  const submit = async () => {
    setOut(null);
    const res = await act.run(() => api.checkNumber({
      workspace_id: wsId, question: question.trim(), claimed: claimed.trim(),
      label: label.trim() || null, source: source.trim() || null,
    }));
    if (res) setOut(res);
  };

  return (
    <div className="stack">
      <p className="small muted">Got a number from another tool, a spreadsheet or a slide? Say what it measures and the
        number. It is recomputed through the same checked path as Ask, and the number you enter is never shown to a model.</p>
      <form className="form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Field label="What the number measures" htmlFor={`${id}-q`}>
          <input id={`${id}-q`} value={question} onChange={(e) => setQuestion(e.target.value)} required minLength={3}
            placeholder="How many incidents are there in total?" />
        </Field>
        <div className="grid-2">
          <Field label="Claimed number" htmlFor={`${id}-c`} hint="As written: 1204, 1,204.5 or 12.5%">
            <input id={`${id}-c`} value={claimed} onChange={(e) => setClaimed(e.target.value)} required inputMode="decimal" />
          </Field>
          <Field label="Row (optional)" htmlFor={`${id}-l`} hint="For a breakdown: the row the number is about">
            <input id={`${id}-l`} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. 1 - Critical" />
          </Field>
        </div>
        <Field label="Where it came from (optional)" htmlFor={`${id}-s`}>
          <input id={`${id}-s`} value={source} onChange={(e) => setSource(e.target.value)} maxLength={120} placeholder="e.g. Quarterly slide" />
        </Field>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy || question.trim().length < 3 || !claimed.trim()}>
            {act.busy ? "Checking…" : "Check number"}
          </button>
        </div>
      </form>
      <ErrorBox error={act.error} />
      {out && <NumberCheckResult check={out} onOpenThread={onOpenThread} />}
    </div>
  );
}

export function NumberCheckResult({ check, onOpenThread }: { check: NumberCheck; onOpenThread?: (threadId: string) => void }) {
  const v = VERDICT[check.verdict];
  const ev = check.evidence;
  const trust = trustOfAuthority(check.authority);
  const act = useAction();
  const [sent, setSent] = useState<NumberCheckHandoff | null>(null);
  // a confirmed number on a certified definition is settled; anything else may need a person
  const canHandOff = !!ev.turn_id && !(check.verdict === "confirmed" && trust === "certified");
  const handOff = async () => {
    const res = await act.run(() => api.handOffNumberCheck({
      workspace_id: check.workspace_id, turn_id: ev.turn_id!, claimed: check.claimed.text, label: check.label,
      column: check.column, tolerance: check.tolerance, source: check.source,
    }));
    if (res) setSent(res);
  };
  return (
    <section className="stack" aria-label="Number check result">
      <Notice tone={v.tone}>
        <strong>{v.title}.</strong>{" "}
        {check.verdict === "confirmed" && check.computed && <>The data gives {fmtNumber(check.comparison?.compared_to ?? check.computed.value)}, which matches {check.claimed.text}.</>}
        {check.verdict === "contradicted" && check.computed && <>You entered {check.claimed.text}; the data gives {fmtNumber(check.comparison?.compared_to ?? check.computed.value)}.</>}
        {check.verdict === "unverifiable" && <>{check.reason}</>}
      </Notice>
      {check.note && <p className="small muted">{check.note}</p>}
      {check.verdict === "unverifiable" && check.remedy && <p className="small">{check.remedy}</p>}
      <KeyValue items={[
        ["Claimed", check.claimed.text + (check.source ? ` (${check.source})` : "")],
        ["Computed", check.computed ? `${fmtNumber(check.computed.value)}${check.computed.row_label ? ` · row ${check.computed.row_label}` : ""}${check.computed.column ? ` · ${check.computed.column}` : ""}` : "—"],
        ["Difference", check.comparison ? `${fmtNumber(check.comparison.difference)}${check.comparison.relative != null ? ` (${fmtNumber(check.comparison.relative * 100, 1)}%)` : ""}` : "—"],
        ["Trust", check.computed ? <><TrustBadge level={trust} /> <span className="small muted">{TRUST[trust].hint}</span></> : "—"],
        ["Tables", ev.assets.length ? ev.assets.join(", ") : "—"],
        ["Data", ev.staleness?.label ?? "—"],
      ]} />
      {ev.sql && <CodeBlock code={ev.sql} label={`SQL · receipt ${ev.query_id ?? "—"}`} />}
      {trust === "checked" && check.computed && (
        <p className="small">To make this number <strong>Certified</strong>, open the answer and save it as a verified query or
          metric; an owner approves it, and the same question is answered from it from then on.</p>
      )}
      <div className="btn-row">
        {ev.thread_id && onOpenThread && (
          <button type="button" className="btn btn-sm" onClick={() => onOpenThread(ev.thread_id!)}>
            {trust === "checked" ? "Open the answer to certify it" : "Open the answer"}</button>
        )}
        {canHandOff && !sent && (
          <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void handOff()}
            title="Creates a Request Desk request with this check and its evidence, assigned to an analyst">
            {act.busy ? "Sending…" : "Send to an analyst"}</button>
        )}
      </div>
      <ErrorBox error={act.error} />
      {sent && <Notice tone="success">Sent to the Request Desk as {sent.request_id}{sent.assignee_id ? ", assigned to an analyst" : ""}.
        The check and its evidence go with it.</Notice>}
    </section>
  );
}
