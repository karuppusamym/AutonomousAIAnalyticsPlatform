import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { session, type Run } from "../api";
import { fmtNumber, fmtPct, optionLabel } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { valueApi, type PilotInput, type PilotReview } from "../lib/investigationValue";
import { roleAtLeast, to } from "../routes";
import { Card, ErrorBox, Field, Notice } from "./ui";

const blank = (): PilotInput => ({ run_id: "", evidence_kind: "rehearsal", task: "", baseline_minutes: 0, assisted_minutes: 0,
  accuracy: "not_reviewed", usefulness: 3, would_use_again: false, notes: "" });

export function PilotScorecard({ wsId, role, runs }: { wsId: string; role?: string; runs: Run[] }) {
  const results = useAsync(() => valueApi.reviews(wsId), [wsId]);
  const act = useAction();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<PilotReview | undefined>();
  const [draft, setDraft] = useState<PilotInput>(blank);
  const [notice, setNotice] = useState("");
  const set = (patch: Partial<PilotInput>) => setDraft((d) => ({ ...d, ...patch }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const saved = await act.run(() => valueApi.saveReview(wsId, draft, editing));
    if (saved) { setOpen(false); setEditing(undefined); setDraft(blank()); setNotice("Assessment saved. Rehearsals remain separate from real pilot evidence."); await results.reload(); }
  };
  const edit = (r: PilotReview) => { setEditing(r); setDraft({ run_id: r.run_id, evidence_kind: r.evidence_kind, task: r.task,
    baseline_minutes: r.baseline_minutes, assisted_minutes: r.assisted_minutes, accuracy: r.accuracy, usefulness: r.usefulness,
    would_use_again: r.would_use_again, notes: r.notes }); setOpen(true); act.setError(null); };
  return <Card title="Pilot scorecard" label="Pilot scorecard">
    <p className="small muted">Measure usefulness with analysts: record the same task's manual baseline, assisted time, accuracy and willingness to use it again. These are self-reported observations.</p>
    <ErrorBox error={results.error} onRetry={results.reload} />
    <div className="grid-2">{(["real_pilot", "rehearsal"] as const).map((kind) => {
      const s = results.data?.summary[kind];
      return <section key={kind} className="pilot-summary" aria-label={kind === "real_pilot" ? "Real pilot results" : "Rehearsal results"}>
        <h3>{kind === "real_pilot" ? "Real analyst pilot" : "Demo rehearsals"}</h3>
        {!s?.assessments ? <p className="muted small">No assessments recorded. No benefit or accuracy claim yet.</p> : <>
          <p className="small">{s.assessments} assessment{s.assessments === 1 ? "" : "s"} · {s.reviewers} reviewer{s.reviewers === 1 ? "" : "s"} · {s.investigations} investigation{s.investigations === 1 ? "" : "s"}</p>
          <dl className="value-score-metrics"><div><dt>Time saved</dt><dd>{fmtNumber(s.minutes_saved, 1)} min ({fmtPct(s.time_saved_fraction, 0)})</dd></div>
            <div><dt>Rated correct</dt><dd>{s.correct_fraction == null ? "Not reviewed" : <>{fmtPct(s.correct_fraction, 0)} <span className="small muted">of {s.accuracy_reviewed} reviewed</span></>}</dd></div>
            <div><dt>Usefulness</dt><dd>{fmtNumber(s.average_usefulness, 1)} / 5</dd></div><div><dt>Would use again</dt><dd>{fmtPct(s.would_use_again_fraction, 0)}</dd></div></dl>
        </>}
      </section>;
    })}</div>
    {results.data?.at_limit && <Notice tone="warning">Showing the latest 1,000 assessments; this summary covers those assessments only.</Notice>}
    <details><summary>Pilot protocol and recorded assessments</summary>
      <ol className="small"><li>Choose three to five analysts and ten representative questions.</li><li>Record each question's manual time baseline before using the app.</li><li>Run the same question with the same data and record assisted time, including verification and corrections.</li><li>Have each analyst rate accuracy and usefulness. Review failures before claiming value.</li></ol>
      <ul className="value-action-list">{results.data?.reviews.map((r) => <li key={r.id}><strong>{r.task}</strong><p className="small">{r.evidence_kind === "rehearsal" ? "Rehearsal" : "Real pilot"} · {r.accuracy.replace(/_/g, " ")} · {r.baseline_minutes} → {r.assisted_minutes} min</p>
        <Link className="small" to={to.run(wsId, r.run_id)}>Open assessed investigation</Link>{r.reviewer_id === session.user?.id && <button type="button" className="btn btn-xs" onClick={() => edit(r)}>Edit my assessment</button>}</li>)}</ul>
    </details>
    {roleAtLeast(role, "analyst") && !open && <button type="button" className="btn btn-sm" onClick={() => { setEditing(undefined); setDraft(blank()); setOpen(true); act.setError(null); }}>Record an assessment</button>}
    {open && <form className="form value-form" aria-label="Pilot assessment" onSubmit={submit}>
      <Field label="Assessment type" htmlFor={`${id}-kind`}><select id={`${id}-kind`} disabled={!!editing} value={draft.evidence_kind} onChange={(e) => set({ evidence_kind: e.target.value as PilotInput["evidence_kind"] })}><option value="rehearsal">Demo rehearsal (excluded from real pilot results)</option><option value="real_pilot">Real analyst pilot observation</option></select></Field>
      <Field label="Investigation assessed" htmlFor={`${id}-run`} hint="Include failed or inconclusive investigations so the score is not biased toward successes."><select id={`${id}-run`} disabled={!!editing} required value={draft.run_id} onChange={(e) => set({ run_id: e.target.value })}><option value="">Choose a finished investigation or one with findings</option>{runs.filter((r) => ["COMPLETED", "WAITING_USER", "FAILED", "CANCELLED", "REJECTED"].includes(r.status)).map((r) => <option key={r.id} value={r.id}>{optionLabel(r.objective, 70)} · {r.status.toLowerCase().replace(/_/g, " ")}</option>)}</select></Field>
      <Field label="Task measured" htmlFor={`${id}-task`}><input id={`${id}-task`} required minLength={5} maxLength={500} value={draft.task} onChange={(e) => set({ task: e.target.value })} /></Field>
      <div className="grid-2"><Field label="Manual baseline (minutes)" htmlFor={`${id}-baseline`}><input id={`${id}-baseline`} required type="number" min="0.1" max="100000" step="0.1" value={draft.baseline_minutes} onChange={(e) => set({ baseline_minutes: Number(e.target.value) })} /></Field>
        <Field label="Assisted time including review (minutes)" htmlFor={`${id}-assisted`}><input id={`${id}-assisted`} required type="number" min="0" max="100000" step="0.1" value={draft.assisted_minutes} onChange={(e) => set({ assisted_minutes: Number(e.target.value) })} /></Field></div>
      <Field label="Accuracy assessment" htmlFor={`${id}-accuracy`}><select id={`${id}-accuracy`} value={draft.accuracy} onChange={(e) => set({ accuracy: e.target.value as PilotInput["accuracy"] })}><option value="not_reviewed">Not reviewed yet</option><option value="correct">Correct</option><option value="partly_correct">Partly correct</option><option value="incorrect">Incorrect</option></select></Field>
      <Field label="Usefulness (1 low, 5 high)" htmlFor={`${id}-useful`}><select id={`${id}-useful`} value={draft.usefulness} onChange={(e) => set({ usefulness: Number(e.target.value) })}>{[1, 2, 3, 4, 5].map((n) => <option key={n}>{n}</option>)}</select></Field>
      <Field label="Would you use it again?" htmlFor={`${id}-again`}><select id={`${id}-again`} value={String(draft.would_use_again)} onChange={(e) => set({ would_use_again: e.target.value === "true" })}><option value="false">No</option><option value="true">Yes</option></select></Field>
      <Field label="Review notes and corrections needed" htmlFor={`${id}-notes`}><textarea id={`${id}-notes`} maxLength={3000} value={draft.notes} onChange={(e) => set({ notes: e.target.value })} /></Field>
      <ErrorBox error={act.error} onRetry={() => { setOpen(false); void results.reload(); }} />
      <div className="btn-row"><button className="btn btn-primary btn-sm" disabled={act.busy}>Save assessment</button><button type="button" className="btn btn-sm" onClick={() => setOpen(false)}>Cancel</button></div>
    </form>}
    {notice && <Notice tone="success">{notice}</Notice>}
  </Card>;
}
