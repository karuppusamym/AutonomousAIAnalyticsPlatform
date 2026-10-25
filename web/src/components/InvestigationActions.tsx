import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type RunDetail } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { valueApi, type ActionInput, type InvestigationAction } from "../lib/investigationValue";
import { fmtValue } from "../lib/format";
import { roleAtLeast, to } from "../routes";
import { Card, ErrorBox, Field, Notice, StatusBadge } from "./ui";
import { findingStanding } from "./InvestigationOverview";

const empty = (): ActionInput => ({ title: "", next_step: "", owner_id: null, due_date: null, impact_assessment: "",
  success_measure: "", open_questions: "", status: "proposed", resolution_note: "" });

export function InvestigationActions({ wsId, run }: { wsId: string; run: RunDetail }) {
  const list = useAsync(() => valueApi.actions(wsId, run.id), [wsId, run.id]);
  const workspace = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const act = useAction();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState<InvestigationAction | undefined>();
  const [insight, setInsight] = useState(run.insights[0]?.id ?? "");
  const [draft, setDraft] = useState<ActionInput>(empty);
  const [notice, setNotice] = useState("");
  const selected = run.insights.find((i) => i.id === insight);
  const set = (patch: Partial<ActionInput>) => setDraft((d) => ({ ...d, ...patch }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const saved = await act.run(() => valueApi.saveAction(wsId, insight, draft, editing));
    if (saved) { setNotice("Follow-up saved. This records a team action; it does not execute changes in another system."); setOpen(false); setEditing(undefined); setDraft(empty()); await list.reload(); }
  };
  const start = (action?: InvestigationAction) => {
    setEditing(action); setOpen(true); act.setError(null);
    const finding = run.insights.find((i) => findingStanding(i) === "verified") ?? run.insights[0];
    setInsight(action?.insight_id ?? finding?.id ?? "");
    setDraft(action ? { title: action.title, next_step: action.next_step, owner_id: action.owner_id, due_date: action.due_date,
      impact_assessment: action.impact_assessment, success_measure: action.success_measure, open_questions: action.open_questions,
      status: action.status, resolution_note: action.resolution_note } : { ...empty(),
        title: finding ? `Review: ${finding.title}`.slice(0, 300) : "",
        next_step: finding && findingStanding(finding) === "verified"
          ? `Review the evidence behind ${finding.code} with the responsible team. Check alternative explanations before changing the process.`
          : "Recheck the supporting finding and resolve its verification issues before acting on it.",
        success_measure: "Compare the same outcome measure and population with the agreed baseline in the next period.",
        open_questions: finding?.caveats.join("\n") || "" });
  };
  const editable = roleAtLeast(workspace.data?.role, "analyst");
  return <Card title="Turn findings into follow-up" label="Investigation actions">
    <p className="small muted">Record the decision, owner, impact assessment and remaining questions. Estimates are entered by your team and are not measured benefits.</p>
    <ErrorBox error={list.error} onRetry={list.reload} />
    <ErrorBox error={workspace.error} onRetry={workspace.reload} />
    {list.data?.length === 0 && <p className="muted small">No follow-up actions yet.</p>}
    <ul className="value-action-list">{list.data?.map((a) => <li key={a.id}>
      <div className="value-action-heading"><strong>{a.title}</strong><StatusBadge status={a.status} /></div>
      <p>{a.next_step}</p><p className="small muted">Owner: {workspace.data?.members.find((m) => m.user_id === a.owner_id)?.name || (a.owner_id ? "Workspace member" : "Unassigned")} · Due: {a.due_date || "Not set"}</p>
      {a.verification_state?.badge !== "verified" && <Notice tone="warning">Supporting finding needs review ({a.verification_state?.badge ?? "unknown"}). Review it before relying on this action.</Notice>}
      {a.impact_assessment && <p className="small"><strong>Team impact assessment:</strong> {a.impact_assessment}</p>}
      <p className="small"><strong>Success measure:</strong> {a.success_measure}</p>
      {a.open_questions && <p className="small"><strong>Unresolved:</strong> {a.open_questions}</p>}
      {a.resolution_note && <p className="small"><strong>Resolution:</strong> {a.resolution_note}</p>}
      <div className="btn-row"><Link className="btn btn-xs btn-ghost" to={to.findings(wsId, a.insight_id)}>Review supporting evidence</Link>
        {editable && <button type="button" className="btn btn-xs" onClick={() => start(a)}>Edit follow-up</button>}</div>
    </li>)}</ul>
    {editable && run.insights.length > 0 && !open && <button type="button" className="btn btn-sm" onClick={() => start()}>Create follow-up action</button>}
    {!run.insights.length && <p className="small muted">Actions become available when the investigation produces findings.</p>}
    {open && <form className="form value-form" aria-label="Follow-up action" onSubmit={submit}>
      {!editing && <p className="small muted">Suggested draft from the selected finding. Review the action, owner and success measure before saving.</p>}
      <Field label="Supporting finding" htmlFor={`${id}-finding`}><select id={`${id}-finding`} value={insight} disabled={!!editing} onChange={(e) => {
        const finding = run.insights.find((i) => i.id === e.target.value);
        setInsight(e.target.value);
        if (finding) set({ title: `Review: ${finding.title}`.slice(0, 300),
          next_step: findingStanding(finding) === "verified"
            ? `Review the evidence behind ${finding.code} with the responsible team. Check alternative explanations before changing the process.`
            : "Recheck the supporting finding and resolve its verification issues before acting on it.",
          open_questions: finding.caveats.join("\n") });
      }}>
        {run.insights.map((i) => <option key={i.id} value={i.id}>{i.code} · {i.title}</option>)}</select></Field>
      {selected && Object.keys(selected.business_impact ?? {}).length > 0 && <details><summary>Recorded business impact</summary><p className="small">{fmtValue(selected.business_impact)}</p></details>}
      <Field label="Action title" htmlFor={`${id}-title`}><input id={`${id}-title`} required minLength={5} maxLength={300} value={draft.title} onChange={(e) => set({ title: e.target.value })} /></Field>
      <Field label="Recommended next step" htmlFor={`${id}-next`}><textarea id={`${id}-next`} required minLength={5} maxLength={2000} value={draft.next_step} onChange={(e) => set({ next_step: e.target.value })} /></Field>
      <div className="grid-2"><Field label="Responsible owner" htmlFor={`${id}-owner`}><select id={`${id}-owner`} value={draft.owner_id ?? ""} onChange={(e) => set({ owner_id: e.target.value || null })}>
        <option value="">Unassigned</option>{workspace.data?.members.map((m) => <option key={m.user_id} value={m.user_id}>{m.name || m.email || m.user_id}</option>)}</select></Field>
        <Field label="Due date" htmlFor={`${id}-due`}><input id={`${id}-due`} type="date" value={draft.due_date ?? ""} onChange={(e) => set({ due_date: e.target.value || null })} /></Field></div>
      <Field label="Impact assessment (your estimate)" htmlFor={`${id}-impact`}><textarea id={`${id}-impact`} maxLength={2000} value={draft.impact_assessment} onChange={(e) => set({ impact_assessment: e.target.value })} /></Field>
      <Field label="How will success be measured?" htmlFor={`${id}-success`}><input id={`${id}-success`} required minLength={3} maxLength={1000} value={draft.success_measure} onChange={(e) => set({ success_measure: e.target.value })} /></Field>
      <Field label="Unresolved questions" htmlFor={`${id}-questions`}><textarea id={`${id}-questions`} maxLength={2000} value={draft.open_questions} onChange={(e) => set({ open_questions: e.target.value })} /></Field>
      <Field label="Action status" htmlFor={`${id}-status`}><select id={`${id}-status`} value={draft.status} onChange={(e) => set({ status: e.target.value as ActionInput["status"] })}>
        <option value="proposed">Proposed</option><option value="in_progress">In progress</option><option value="done">Done</option><option value="dismissed">Dismissed</option></select></Field>
      <Field label="Resolution note" htmlFor={`${id}-resolution`} hint="Required with an owner before marking an action done. Current verified evidence is also required."><textarea id={`${id}-resolution`} maxLength={2000} value={draft.resolution_note} onChange={(e) => set({ resolution_note: e.target.value })} /></Field>
      <ErrorBox error={act.error} onRetry={() => { setOpen(false); void list.reload(); }} />
      <div className="btn-row"><button className="btn btn-primary btn-sm" disabled={act.busy || !insight}>Save follow-up</button><button type="button" className="btn btn-sm" onClick={() => setOpen(false)}>Cancel</button></div>
    </form>}
    {notice && <Notice tone="success">{notice}</Notice>}
  </Card>;
}
