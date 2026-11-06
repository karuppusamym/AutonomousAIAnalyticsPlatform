import { useId, useMemo, useState } from "react";
import {
  api, ApiError, type GlossaryScanResult, type GoldenEffect, type KnowledgeSuggestion, type ReviewDecisionBody, type ReviewResult,
} from "../api";
import {
  batchDecisions, editableFields, editedFields, evidenceLines, fieldText, needsAnswer, primaryField, provenanceLine, QUESTION_KINDS,
  scanSummary,
} from "../lib/knowledge";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Card, ConfidenceBar, EmptyState, ErrorBox, Loading, Notice, Tag } from "./ui";

const STATUSES = ["pending", "approved", "rejected", "superseded"] as const;
const KIND_LABEL: Record<string, string> = {
  term: "glossary term", definition: "definition", metric: "metric", rule: "business rule", note: "note", negative: "negative knowledge",
  attested_computation: "attested computation", table_description: "table description", domain_candidate: "domain candidate",
  column_description: "column description", glossary_term: "suggested glossary term", description_question: "description needed",
};

/**
 * Knowledge → Review queue (P4-K07 data, P4-U04 screen): AI and learning-loop drafts with each
 * field's value, confidence and provenance. An editor approves or rejects a selection in one batch
 * (one pack revision), or edits a draft and approves it; a rejection needs a reason and becomes
 * negative knowledge that later prompts see.
 */
export function ReviewQueue({ wsId, canDecide, onOpenDocument }: {
  wsId: string; canDecide: boolean; onOpenDocument: (path: string) => void;
}) {
  const id = useId();
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("pending");
  const list = useAsync(() => api.knowledgeSuggestions(wsId, status), [wsId, status]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const [reasonMissing, setReasonMissing] = useState(false);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const act = useAction();
  const peek = useAction();
  const [effect, setEffect] = useState<GoldenEffect | null>(null);
  // A batch that breaks a passing golden question waits here until the reviewer writes why (knowledge/effect.py).
  const [blocked, setBlocked] = useState<{ decisions: ReviewDecisionBody[]; effect: GoldenEffect } | null>(null);
  const [override, setOverride] = useState("");
  const scan = useAction();
  const [scanned, setScanned] = useState<GlossaryScanResult | null>(null);
  const rows = useMemo(() => list.data ?? [], [list.data]);
  // Drafts a person must answer first (a glossary skeleton, a description question) are decided one at a time.
  const batchable = rows.filter((r) => r.kind !== "domain_candidate" && !needsAnswer(r));
  const pending = status === "pending";
  const allOn = batchable.length > 0 && batchable.every((r) => selected.has(r.id));

  const toggle = (sid: string, on: boolean) => setSelected((s) => {
    const n = new Set(s);
    if (on) n.add(sid);
    else n.delete(sid);
    return n;
  });
  const decide = async (decisions: ReviewDecisionBody[], overrideReason?: string) => {
    const r = await act.run(async () => {
      try {
        return await api.reviewSuggestions(wsId, decisions, overrideReason);
      } catch (err) {
        if (err instanceof ApiError && err.status === 409 && err.details.code === "golden_regression") {
          setBlocked({ decisions, effect: err.details.effect as GoldenEffect });
          setEffect(null);
          return null;
        }
        throw err;
      }
    });
    if (r) {
      setResult(r);
      setSelected(new Set());
      setReason("");
      setEffect(null);
      setBlocked(null);
      setOverride("");
      await list.reload();
    }
  };
  const checkEffect = async () => {
    const r = await peek.run(() => api.previewSuggestions(wsId, batchDecisions([...selected], "approve", "")));
    if (r) setEffect(r);
  };
  const runScan = async () => {
    const r = await scan.run(() => api.glossaryScan(wsId));
    if (r) {
      setScanned(r);
      setResult(null);
      await list.reload();
    }
  };
  const batch = async (action: "approve" | "reject") => {
    if (action === "reject" && !reason.trim()) {
      setReasonMissing(true);
      return;
    }
    setReasonMissing(false);
    await decide(batchDecisions([...selected], action, reason));
  };

  return (
    <div className="stack">
      {canDecide && (
        <div className="review-batch">
          <button type="button" className="btn btn-sm" disabled={scan.busy} onClick={() => void runScan()}>
            {scan.busy ? "Scanning…" : "Scan for glossary suggestions"}</button>
          <span className="small muted"> Looks at your tables, their codes and the questions Ask could not answer, and asks you
            what they mean. Nothing changes until you accept.</span>
        </div>
      )}
      <ErrorBox error={scan.error} />
      {scanned && <Notice tone="success">{scanSummary(scanned)}</Notice>}
      <div className="chip-row" role="toolbar" aria-label="Filter the review queue">
        {STATUSES.map((s) => (
          <button key={s} type="button" className={`chip ${status === s ? "active" : ""}`} aria-pressed={status === s}
            onClick={() => { setStatus(s); setSelected(new Set()); setResult(null); }}>{s}</button>
        ))}
      </div>
      {result && (
        <Notice tone={result.errors.length ? "warning" : "success"}>
          {result.revision !== null ? <>Workspace pack revision {result.revision}: </> : null}
          {result.approved.length} approved, {result.rejected.length} rejected
          {result.rejected.some((r) => r.path) && <> (recorded as negative knowledge)</>}.
          {result.effect?.evaluated && <> {goldenSummary(result.effect)}</>}
          {result.approved.some((a) => a.catalog) && (
            <ul className="small plain-list">{result.approved.filter((a) => a.catalog).map((a) => <li key={a.id}>{a.catalog}</li>)}</ul>
          )}
          {result.approved.filter((a) => a.path).map((a) => (
            <button key={a.id} type="button" className="btn btn-xs btn-ghost" onClick={() => onOpenDocument(a.path!)}>Open {a.path}</button>
          ))}
          {result.errors.length > 0 && (
            <ul className="small warn-list">{result.errors.map((e) => <li key={e.id}><code>{e.id}</code>: {e.error.replace(/_/g, " ")}</li>)}</ul>
          )}
        </Notice>
      )}
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && (
        <EmptyState title={pending ? "Nothing to review" : `No ${status} drafts`}>
          {pending ? "Crawls, accepted findings, approved KPIs and investigation feedback propose knowledge here." : null}
        </EmptyState>
      )}
      {pending && rows.length > 0 && !canDecide && <p className="small" role="note">Editors and owners decide drafts; you can read them.</p>}
      {pending && rows.length > 0 && canDecide && (
        <Card>
          <div className="review-batch">
            <label className="toggle small">
              <input type="checkbox" checked={allOn} onChange={(e) => setSelected(e.target.checked ? new Set(batchable.map((r) => r.id)) : new Set())} />
              Select all ({batchable.length})
            </label>
            <label className="sr-only" htmlFor={`${id}-reason`}>Reason for rejecting the selection</label>
            <input id={`${id}-reason`} value={reason} placeholder="Reason (required to reject)" maxLength={2000}
              onChange={(e) => { setReason(e.target.value); setReasonMissing(false); }}
              aria-invalid={reasonMissing || undefined} aria-describedby={reasonMissing ? `${id}-reason-err` : undefined} />
            <button type="button" className="btn btn-success btn-sm" disabled={!selected.size || act.busy} onClick={() => void batch("approve")}>
              Approve selected ({selected.size})</button>
            <button type="button" className="btn btn-danger btn-sm" disabled={!selected.size || act.busy} onClick={() => void batch("reject")}>
              Reject selected ({selected.size})</button>
            <button type="button" className="btn btn-sm" disabled={!selected.size || peek.busy || act.busy} onClick={() => void checkEffect()}>
              {peek.busy ? "Checking…" : "Check effect on golden questions"}</button>
          </div>
          <ErrorBox error={peek.error} />
          {effect && !blocked && <GoldenEffectPanel effect={effect} />}
          {blocked && (
            <Notice tone="warning">
              <p><strong>Not approved.</strong> This batch makes {blocked.effect.counts.newly_failing} golden question(s) fail that pass
                today. Remove the drafts that cause it, or approve with a written reason (it is kept in the audit log).</p>
              <GoldenEffectPanel effect={blocked.effect} />
              <label className="small" htmlFor={`${id}-override`}>Why approve anyway</label>
              <textarea id={`${id}-override`} value={override} maxLength={2000} rows={2} onChange={(e) => setOverride(e.target.value)} />
              <div className="review-batch">
                <button type="button" className="btn btn-success btn-sm" disabled={!override.trim() || act.busy}
                  onClick={() => void decide(blocked.decisions, override.trim())}>Approve anyway</button>
                <button type="button" className="btn btn-sm btn-ghost" onClick={() => { setBlocked(null); setOverride(""); }}>Cancel</button>
              </div>
            </Notice>
          )}
          {reasonMissing && <p id={`${id}-reason-err`} className="field-error" role="alert">Say why: a rejection is kept as negative knowledge.</p>}
          <ErrorBox error={act.error} />
        </Card>
      )}
      {rows.length > 0 && (
        <ul className="stack review-list" aria-label="Knowledge drafts">
          {rows.map((s) => (
            <li key={s.id}>
              {QUESTION_KINDS.has(s.kind) ? (
                <QuestionCard s={s} canDecide={pending && canDecide} selectable={pending && canDecide && !needsAnswer(s)}
                  selected={selected.has(s.id)} onSelect={(on) => toggle(s.id, on)} busy={act.busy} onDecide={(d) => void decide([d])} />
              ) : (
                <SuggestionCard s={s} selectable={pending && canDecide && s.kind !== "domain_candidate"} selected={selected.has(s.id)}
                  onSelect={(on) => toggle(s.id, on)} busy={act.busy} onEditApprove={(fields) => void decide([{ id: s.id, action: "edit", fields }])} />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const CHANGE_LABEL: Record<GoldenEffect["cases"][number]["change"], string> = {
  newly_failing: "newly failing", fixed: "fixed", still_failing: "still failing", still_passing: "still passing",
};

/** "Golden questions: 3 of 5 pass now, 4 of 5 with this batch (+20 points)." */
function goldenSummary(e: GoldenEffect): string {
  if (!e.evaluated) return "No golden questions to check yet: add some under Grounding so reviews show their effect.";
  const sign = e.delta_points > 0 ? "+" : "";
  return `Golden questions: ${e.baseline.passed} of ${e.baseline.total} pass now, ${e.projected.passed} of ${e.projected.total} `
    + `with this batch (${sign}${e.delta_points} points).`;
}

/** Per-question effect of a batch, newly failing first; columns and served query shown only where they change. */
function GoldenEffectPanel({ effect }: { effect: GoldenEffect }) {
  const changed = effect.cases.filter((c) => c.change === "newly_failing" || c.change === "fixed");
  return (
    <div className="stack small" aria-live="polite">
      <p>{goldenSummary(effect)}</p>
      {changed.length > 0 && (
        <ul className="plain-list" aria-label="Golden questions this batch changes">
          {changed.map((c) => (
            <li key={c.id}>
              <Tag tone={c.change === "fixed" ? "success" : "danger"}>{CHANGE_LABEL[c.change]}</Tag> {c.question}
              {c.columns_before.join(",") !== c.columns_after.join(",") && (
                <span className="muted"> · columns {c.columns_before.join(", ") || "none"} → {c.columns_after.join(", ") || "none"}</span>
              )}
              {c.served_before !== c.served_after && (
                <span className="muted"> · verified query {c.served_before ?? "none"} → {c.served_after ?? "none"}</span>
              )}
            </li>
          ))}
        </ul>
      )}
      {effect.errors.length > 0 && <p className="muted">{effect.errors.length} draft(s) in the selection cannot be applied as they stand.</p>}
    </div>
  );
}

/** A catalog field's previous value (`{value, origin}`), shown so a reviewer sees what approval replaces. */
function beforeText(before: unknown): string {
  const v = before && typeof before === "object" && "value" in before ? (before as { value: unknown }).value : before;
  return v === null || v === undefined ? "" : String(v);
}

function SuggestionCard({ s, selectable, selected, onSelect, busy, onEditApprove }: {
  s: KnowledgeSuggestion; selectable: boolean; selected: boolean; onSelect: (on: boolean) => void; busy: boolean;
  onEditApprove: (fields: Record<string, unknown>) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const editable = editableFields(s);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const begin = () => {
    setEdits(Object.fromEntries(editable.map((k) => [k, fieldText(s.fields[k])])));
    setEditing(true);
  };
  const main = primaryField(s.kind);
  const names = Object.keys(s.fields).sort((a, b) => (a === main ? -1 : b === main ? 1 : a.localeCompare(b)));
  return (
    <article className={`card suggestion ${selected ? "suggestion-selected" : ""}`} aria-label={`Draft: ${s.title}`}>
      <div className="card-body stack">
        <div className="suggestion-head">
          {selectable && (
            <input type="checkbox" id={`${id}-sel`} checked={selected} onChange={(e) => onSelect(e.target.checked)} aria-label={`Select ${s.title}`} />
          )}
          <div className="suggestion-title">
            <h2 className="h-md">{s.title}</h2>
            <span className="chip-row small">
              <Tag tone="info">{KIND_LABEL[s.kind] ?? s.kind}</Tag>
              <span className="muted">{s.origin} · {s.proposed_by} · {fmtDate(s.created_at)}</span>
              {s.status !== "pending" && <Tag tone={s.status === "approved" ? "success" : s.status === "rejected" ? "danger" : "neutral"}>{s.status}</Tag>}
            </span>
          </div>
          <div className="suggestion-confidence" title="The least confident field"><ConfidenceBar value={s.confidence} /></div>
        </div>
        <p className="small muted">Writes <code>{s.path}</code> · subject <code>{s.subject}</code>{s.revision ? <> · revision {s.revision}</> : null}</p>
        {s.kind === "domain_candidate" && <Notice tone="info">Choose a keyword from the table name or a non-sensitive column name, then approve with edits. That reviewed rule applies in this workspace on the next full crawl; the suggestion alone cannot change the catalog.</Notice>}
        {s.reason && <p className="small">Reason: {s.reason}</p>}
        {!editing && (
          <div className="table-wrap"><table className="table table-compact suggestion-fields">
            <caption className="sr-only">Fields of {s.title}</caption>
            <thead><tr><th scope="col">Field</th><th scope="col">Value</th><th scope="col">Confidence</th><th scope="col">Provenance</th></tr></thead>
            <tbody>
              {names.map((name) => {
                const f = s.fields[name];
                return (
                  <tr key={name}>
                    <th scope="row"><code>{name}</code></th>
                    <td className="small"><span className="clamp-3">{fieldText(f)}</span>
                      {f.before !== undefined && <div className="muted small">was: {beforeText(f.before) || "empty"}</div>}</td>
                    <td><ConfidenceBar value={f.confidence} /></td>
                    <td className="small">
                      <ul className="plain-list">{provenanceLine(f.provenance).map(([k, v]) => <li key={k}><span className="muted">{k}:</span> {v}</li>)}</ul>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table></div>
        )}
        {editing && (
          <form className="form" aria-label={`Edit ${s.title}`} onSubmit={(e) => { e.preventDefault(); onEditApprove(editedFields(s, edits)); }}>
            {editable.map((name) => (
              <div className="field" key={name}>
                <label htmlFor={`${id}-${name}`}><code>{name}</code>{Array.isArray(s.fields[name]?.value) ? " (comma-separated)" : ""}</label>
                <textarea id={`${id}-${name}`} rows={name === main ? 4 : 1} value={edits[name] ?? ""}
                  onChange={(e) => setEdits((x) => ({ ...x, [name]: e.target.value }))} />
              </div>
            ))}
            <p className="muted small">Edited fields are recorded as yours (provenance <code>human</code>, confidence 100%); the others keep their provenance.</p>
            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
              <button type="submit" className="btn btn-success" disabled={busy}>Approve with edits</button>
            </div>
          </form>
        )}
        {selectable && !editing && editable.length > 0 && (
          <div className="form-actions">
            <button type="button" className="btn btn-sm" onClick={begin}>Edit, then approve</button>
          </div>
        )}
      </div>
    </article>
  );
}

const listText = (f: KnowledgeSuggestion["fields"][string] | undefined) =>
  (Array.isArray(f?.value) ? (f!.value as unknown[]).map(String).filter(Boolean) : []);

/**
 * A draft that asks a person (Stream E): a glossary term the scan found (a code set, an abbreviation, a shared
 * business noun, a word Ask could not place) or a table/column nobody described. The question and the evidence
 * come first; a skeleton definition ("1 = ?") or a rule guess must be answered before it can be accepted.
 */
function QuestionCard({ s, canDecide, selectable, selected, onSelect, busy, onDecide }: {
  s: KnowledgeSuggestion; canDecide: boolean; selectable: boolean; selected: boolean; onSelect: (on: boolean) => void; busy: boolean;
  onDecide: (d: ReviewDecisionBody) => void;
}) {
  const id = useId();
  const glossary = s.kind === "glossary_term";
  const mustAnswer = needsAnswer(s);
  const lines = evidenceLines(s);
  const question = fieldText(s.fields.question);
  const synonyms = listText(s.fields.synonyms);
  const columns = listText(s.fields.mapped_columns);
  const guessFrom = String(s.fields.description?.provenance?.source ?? "");
  const [mode, setMode] = useState<"view" | "edit" | "reject">("view");
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [answer, setAnswer] = useState(fieldText(s.fields.description));
  const [why, setWhy] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const begin = () => {
    setEdits(Object.fromEntries(editableFields(s).map((k) => [k, fieldText(s.fields[k])])));
    setProblem(null);
    setMode("edit");
  };
  const acceptEdited = () => {
    const fields = editedFields(s, edits);
    if (mustAnswer && !("body" in fields)) {
      setProblem("Write the definition first: replace the “?” with what each part means.");
      return;
    }
    onDecide(Object.keys(fields).length ? { id: s.id, action: "edit", fields } : { id: s.id, action: "approve" });
  };
  const saveAnswer = () => {
    if (!answer.trim()) {
      setProblem("Write an answer, or skip the question.");
      return;
    }
    onDecide({ id: s.id, action: "edit", fields: { description: answer.trim() } });
  };
  const reject = () => {
    if (!why.trim()) {
      setProblem("Say why, so it is not suggested again for the wrong reason.");
      return;
    }
    onDecide({ id: s.id, action: "reject", reason: why.trim() });
  };
  const label = (name: string) => ({ body: "Definition", name: "Term", synonyms: "Also called (comma-separated)",
    mapped_columns: "Columns it describes (comma-separated)" } as Record<string, string>)[name] ?? name;

  return (
    <article className={`card suggestion ${selected ? "suggestion-selected" : ""}`} aria-label={`Question: ${s.title}`}>
      <div className="card-body stack">
        <div className="suggestion-head">
          {selectable && (
            <input type="checkbox" id={`${id}-sel`} checked={selected} onChange={(e) => onSelect(e.target.checked)} aria-label={`Select ${s.title}`} />
          )}
          <div className="suggestion-title">
            <h2 className="h-md">{s.title}</h2>
            <span className="chip-row small">
              <Tag tone="info">{KIND_LABEL[s.kind] ?? s.kind}</Tag>
              <span className="muted">{s.proposed_by.startsWith("model:") ? "drafted by AI" : "found by a scan"} · {fmtDate(s.created_at)}</span>
              {s.status !== "pending" && <Tag tone={s.status === "approved" ? "success" : s.status === "rejected" ? "danger" : "neutral"}>{s.status}</Tag>}
            </span>
          </div>
          <div className="suggestion-confidence" title="How sure the suggestion is"><ConfidenceBar value={s.confidence} /></div>
        </div>
        {question && <p><strong>{question}</strong></p>}
        {lines.length > 0 && (
          <div className="small">
            <span className="muted">Why this is asked:</span>
            <ul className="plain-list">{lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
          </div>
        )}
        {glossary && mode !== "edit" && (
          <dl className="kv small">
            <div className="kv-row">
              <dt>Definition</dt>
              <dd>{fieldText(s.fields.body) || "—"}{mustAnswer && <span className="muted"> (a draft: fill in the blanks)</span>}</dd>
            </div>
            {synonyms.length > 0 && <div className="kv-row"><dt>Also called</dt><dd>{synonyms.join(", ")}</dd></div>}
            {columns.length > 0 && <div className="kv-row"><dt>Columns</dt><dd>{columns.map((c) => <code key={c}>{c} </code>)}</dd></div>}
          </dl>
        )}
        {!glossary && !canDecide && (
          <p className="small"><span className="muted">Current best guess:</span> {fieldText(s.fields.description) || "none yet"}</p>
        )}
        {s.reason && <p className="small">Reason: {s.reason}</p>}
        {canDecide && glossary && mode === "view" && (
          <div className="form-actions">
            <button type="button" className="btn btn-success btn-sm" disabled={busy || mustAnswer}
              title={mustAnswer ? "Fill in the definition first (Edit & accept)" : undefined} onClick={() => onDecide({ id: s.id, action: "approve" })}>
              Accept</button>
            <button type="button" className="btn btn-sm" disabled={busy} onClick={begin}>Edit &amp; accept</button>
            <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={() => { setProblem(null); setMode("reject"); }}>Reject</button>
          </div>
        )}
        {canDecide && glossary && mode === "edit" && (
          <form className="form" aria-label={`Edit ${s.title}`} onSubmit={(e) => { e.preventDefault(); acceptEdited(); }}>
            {editableFields(s).map((name) => (
              <div className="field" key={name}>
                <label htmlFor={`${id}-${name}`}>{label(name)}</label>
                <textarea id={`${id}-${name}`} rows={name === "body" ? 4 : 1} value={edits[name] ?? ""}
                  onChange={(e) => { setEdits((x) => ({ ...x, [name]: e.target.value })); setProblem(null); }} />
              </div>
            ))}
            <p className="muted small">Accepted terms join the workspace glossary: Ask, analyses and the next crawl use them.</p>
            <div className="form-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setMode("view")}>Cancel</button>
              <button type="submit" className="btn btn-success" disabled={busy}>Accept with edits</button>
            </div>
          </form>
        )}
        {canDecide && !glossary && (
          <form className="form" aria-label={`Answer ${s.title}`} onSubmit={(e) => { e.preventDefault(); saveAnswer(); }}>
            <div className="field">
              <label htmlFor={`${id}-answer`}>Your answer</label>
              <textarea id={`${id}-answer`} rows={3} value={answer} placeholder="In a sentence: what it holds and how people use it."
                onChange={(e) => { setAnswer(e.target.value); setProblem(null); }} />
              {fieldText(s.fields.description) && (
                <p className="small muted">Pre-filled with {guessFrom === "model" ? "an AI draft" : "the current best guess"}: edit it or write
                  your own. Your answer is kept as yours; no crawl or model overwrites it.</p>
              )}
            </div>
            <div className="form-actions">
              <button type="submit" className="btn btn-success btn-sm" disabled={busy}>Save answer</button>
              {guessFrom === "model" && (
                <button type="button" className="btn btn-sm" disabled={busy} onClick={() => onDecide({ id: s.id, action: "approve" })}>
                  Accept the AI draft</button>
              )}
              <button type="button" className="btn btn-ghost btn-sm" disabled={busy}
                onClick={() => onDecide({ id: s.id, action: "reject", reason: "Skipped: no answer yet" })}>Skip</button>
            </div>
          </form>
        )}
        {canDecide && glossary && mode === "reject" && (
          <div className="form">
            <label htmlFor={`${id}-why`}>Why is this not a term here?</label>
            <input id={`${id}-why`} value={why} maxLength={2000} onChange={(e) => { setWhy(e.target.value); setProblem(null); }} />
            <div className="form-actions">
              <button type="button" className="btn btn-ghost btn-sm" onClick={() => setMode("view")}>Cancel</button>
              <button type="button" className="btn btn-danger btn-sm" disabled={busy} onClick={reject}>Reject</button>
            </div>
          </div>
        )}
        {problem && <p className="field-error" role="alert">{problem}</p>}
      </div>
    </article>
  );
}
