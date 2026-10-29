import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  api, type DataPack, type DeskRequest, type Member, type RequestActionType, type RequestAnswer, type RequestPriority,
} from "../api";
import { useAuth } from "../auth";
import { Card, CodeBlock, EmptyState, ErrorBox, Field, JsonView, KeyValue, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag } from "../components/ui";
import { fmtDate, fmtUsd, plural } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";

type View = "mine" | "queue" | "all";

/** Product spec §4: what each class is, in the words the requester and the analyst see. */
export const CLASS_LABELS: Record<string, string> = {
  R1: "Certified metric", R2: "Ad-hoc slice", R3: "Explain a change", R4: "Recurring report", R5: "Monitoring",
  R6: "Dashboard", R7: "Definition", R8: "Data request or export", R9: "Novel or strategic",
};
const PRIORITIES: RequestPriority[] = ["low", "normal", "high", "urgent"];
/** States the desk is still working on by itself: the detail refreshes until it settles. */
const WORKING = new Set(["new", "triaged", "auto_answering"]);
const OPEN = new Set(["new", "triaged", "auto_answering", "auto_answered", "auto_with_review", "in_investigation", "escalated", "answered", "reopened"]);

const VIA: Record<RequestAnswer["via"], string> = {
  auto: "Answered automatically",
  auto_with_review: "Prepared automatically, reviewed by an analyst",
  human: "Answered by an analyst",
};

/** Time left to the SLA (or how late), for a queue chip; null once the request is closed. */
export function slaText(r: Pick<DeskRequest, "sla_due_at" | "sla_breached" | "state">, now = Date.now()): { text: string; tone: string } | null {
  if (!r.sla_due_at || !OPEN.has(r.state) || r.state === "auto_answered" || r.state === "answered") return null;
  const left = new Date(r.sla_due_at).getTime() - now;
  const h = Math.abs(left) / 3_600_000;
  const span = h >= 24 ? `${Math.round(h / 24)} d` : h >= 1 ? `${Math.round(h)} h` : `${Math.max(1, Math.round(h * 60))} min`;
  if (left < 0 || r.sla_breached) return { text: `SLA missed by ${span}`, tone: "danger" };
  return { text: `SLA in ${span}`, tone: left < 2 * 3_600_000 ? "warning" : "neutral" };
}

function who(id: string | null | undefined, members: Member[], me?: string): string {
  if (!id) return "nobody yet";
  if (id === me) return "you";
  const m = members.find((x) => x.user_id === id);
  return m ? m.name || m.email : id;
}

/**
 * Work → Request desk (spec S11-T1, S13): what people asked of the analysts, how each request was triaged and answered,
 * and the analyst queue with SLA, assignment and the prepared data pack. The server decides every transition and role;
 * this screen only offers what the person's role allows.
 */
export function DeskPage() {
  const { wsId = "", requestId } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const analyst = roleAtLeast(ws.data?.role, "analyst") || !!user?.is_admin;
  const view: View = (params.get("view") as View) || (analyst ? "queue" : "mine");
  const list = useAsync(() => api.listRequests({ view, workspace_id: wsId }), [wsId, view]);
  const rows = list.data ?? [];
  const selected = requestId ?? null;

  const open = (id: string) => navigate(to.desk(wsId, id, { view }));
  const created = (r: DeskRequest) => {
    void list.reload();
    navigate(to.desk(wsId, r.id, { view: "mine" }));
  };

  return (
    <div className="page">
      <PageHeader title="Requests"
        subtitle="Ask for a number, an explanation, a definition or a report. The desk answers what it can through the governed paths and hands the rest to an analyst, with the context already gathered." />
      <NewRequest wsId={wsId} onCreated={created} />
      <Tabs<View> value={view} onChange={(v) => setParams((p) => { p.set("view", v); return p; })}
        tabs={[{ id: "mine", label: "My requests" }, ...(analyst ? [{ id: "queue" as View, label: "Analyst queue" }, { id: "all" as View, label: "All requests" }] : [])]} />
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && !selected && (
        <EmptyState title={view === "queue" ? "Nothing waiting for an analyst" : "No requests yet"}>
          {view === "queue" ? "Escalated requests, answers prepared for review and reopened requests appear here, oldest SLA first."
            : "Requests you make above appear here with their state and answer."}
        </EmptyState>
      )}
      {(rows.length > 0 || selected) && <div className="review-layout">
        <aside className="review-queue" aria-label="Requests">
          <p className="muted small">{plural(rows.length, "request")}</p>
          <ul className="list selectable">{rows.map((r) => {
            const sla = slaText(r);
            return <li key={r.id}>
              <button type="button" className={`list-button ${selected === r.id ? "active" : ""}`} aria-current={selected === r.id ? "true" : undefined}
                onClick={() => open(r.id)}>
                <strong>{r.text.length > 90 ? `${r.text.slice(0, 90)}…` : r.text}</strong>
                <span className="chip-row">
                  <StatusBadge status={r.state} />
                  {r.class && <Tag>{r.class} · {CLASS_LABELS[r.class]}</Tag>}
                  {r.priority !== "normal" && <Tag tone={r.priority === "urgent" || r.priority === "high" ? "warning" : "neutral"}>{r.priority}</Tag>}
                  {sla && <Tag tone={sla.tone}>{sla.text}</Tag>}
                </span>
                <span className="small muted">{fmtDate(r.created_at)}{view !== "mine" && r.assignee_id ? ` · assigned to ${who(r.assignee_id, ws.data?.members ?? [], user?.id)}` : ""}</span>
              </button>
            </li>;
          })}</ul>
        </aside>
        <div className="review-detail">
          {selected ? <RequestDetail key={selected} id={selected} wsId={wsId} analyst={analyst} members={ws.data?.members ?? []}
            onChanged={() => void list.reload()} />
            : <EmptyState title="No request selected">Select a request to see its answer, timeline and next step.</EmptyState>}
        </div>
      </div>}
    </div>
  );
}

function NewRequest({ wsId, onCreated }: { wsId: string; onCreated: (r: DeskRequest) => void }) {
  const [text, setText] = useState("");
  const [priority, setPriority] = useState<RequestPriority>("normal");
  const action = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const out = await action.run(() => api.createRequest({ workspace_id: wsId, text: text.trim(), priority, channel: "ui" }));
    if (out) {
      setText("");
      onCreated(out.request);
    }
  };
  return (
    <Card title="New request" label="New request">
      <form className="stack" onSubmit={submit}>
        <Field label="What do you need?" htmlFor="desk-text"
          hint="For example: “Refund count by merchant last month”, “Why did failures rise this week?”, “What counts as an active customer?”">
          <textarea id="desk-text" rows={2} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} />
        </Field>
        <div className="toolbar">
          <label className="field"><span>Priority</span>
            <select value={priority} onChange={(e) => setPriority(e.target.value as RequestPriority)}>
              {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </label>
          <button type="submit" className="btn btn-primary" disabled={action.busy || text.trim().length < 3}>
            {action.busy ? "Sending…" : "Send to the desk"}
          </button>
        </div>
        <ErrorBox error={action.error} />
      </form>
    </Card>
  );
}

function RequestDetail({ id, wsId, analyst, members, onChanged }: {
  id: string; wsId: string; analyst: boolean; members: Member[]; onChanged: () => void;
}) {
  const { user } = useAuth();
  const req = useAsync(() => api.getRequest(id), [id]);
  const r = req.data;
  // While the desk is still triaging or answering, look again shortly: "running" is never shown as done.
  useEffect(() => {
    if (!r || !WORKING.has(r.state)) return;
    const t = setTimeout(() => void req.reload(), 2000);
    return () => clearTimeout(t);
  }, [r, req]);
  if (req.error) return <ErrorBox error={req.error} onRetry={req.reload} />;
  if (!r) return <Loading />;
  const mine = r.requester_id === user?.id;
  const sla = slaText(r);
  const changed = (next: DeskRequest) => {
    req.setData(next);
    onChanged();
  };
  return (
    <div className="stack">
      <Card title={r.text} label="Request">
        <div className="chip-row">
          <StatusBadge status={r.state} />
          {r.class && <Tag>{r.class} · {CLASS_LABELS[r.class]}</Tag>}
          <Tag>{r.priority} priority</Tag>
          {sla && <Tag tone={sla.tone}>{sla.text}</Tag>}
          {r.resolution && <Tag tone="info">resolved: {r.resolution}</Tag>}
        </div>
        <KeyValue items={[
          ["Asked", `${fmtDate(r.created_at)} via ${r.channel}${mine ? " (by you)" : ` by ${who(r.requester_id, members, user?.id)}`}`],
          ["Triage", r.triage?.class ? `${r.triage.answered_by === "rules" ? "Rules" : r.triage.answered_by === "fallback" ? "No rule or model was sure" : `Model (${r.triage.answered_by})`}: ${r.triage.reason ?? ""}` : "Not triaged yet"],
          ["Assigned to", who(r.assignee_id, members, user?.id)],
          ["SLA due", r.sla_due_at ? fmtDate(r.sla_due_at) : "—"],
          ["First answer", r.first_answer_at ? fmtDate(r.first_answer_at) : "not yet"],
          ["Analyst minutes", r.human_minutes != null ? String(r.human_minutes) : "—"],
          ["Model cost", fmtUsd(r.cost_usd)],
        ]} />
        {(r.triage?.flags ?? []).length > 0 && <p className="small">Flags: {(r.triage.flags ?? []).map((f) => <Tag key={f} tone="warning">{f}</Tag>)}</p>}
        {WORKING.has(r.state) && <Notice>The desk is still working on this request. This page refreshes until it has an answer or a person takes it.</Notice>}
      </Card>
      {r.original && <Notice>
        This is the same request as an earlier one, so it follows that one instead (state: {r.original.state}).{" "}
        <Link to={to.desk(wsId, r.original.id)}>Open the earlier request</Link>
      </Notice>}
      <AnswerCard r={r} wsId={wsId} />
      {mine && (r.state === "auto_answered" || r.state === "answered") && <RequesterActions r={r} onChanged={changed} />}
      {analyst && <AnalystActions r={r} members={members} onChanged={changed} />}
      {analyst && r.data_pack && <DataPackCard pack={r.data_pack} wsId={wsId} />}
      <Timeline r={r} members={members} />
    </div>
  );
}

function AnswerCard({ r, wsId }: { r: DeskRequest; wsId: string }) {
  const a = r.answer;
  if (!a) {
    const waiting = r.state === "auto_with_review" ? "An answer is prepared and waiting for an analyst's review."
      : r.state === "in_investigation" ? "An investigation is running; an analyst reviews its findings before they are shared."
        : r.state === "escalated" || r.state === "reopened" ? "An analyst has this request." : null;
    return waiting ? <Card title="Answer" label="Answer"><p className="muted">{waiting}</p></Card> : null;
  }
  return (
    <Card title="Answer" label="Answer">
      <div className="chip-row">
        <Tag tone={a.via === "human" ? "info" : "success"}>{VIA[a.via]}</Tag>
        {a.governance && <Tag tone={a.governance === "governed" ? "success" : "warning"}>{a.governance === "governed" ? "governed" : "ad hoc"}</Tag>}
        {a.answered_by && <Tag>answered by {a.answered_by}</Tag>}
        {a.edited && <Tag>edited by the analyst</Tag>}
      </div>
      {a.text && <p style={{ whiteSpace: "pre-wrap" }}>{a.text}</p>}
      {a.definition && <p><strong>{a.name}:</strong> {a.definition}</p>}
      {a.explanation && !a.text && <p>{a.explanation}</p>}
      <p className="chip-row small">
        {a.thread_id && <Link to={`${to.ask(wsId)}?thread=${encodeURIComponent(a.thread_id)}`}>Open the answer in Ask (SQL, numbers, evidence)</Link>}
        {a.run_id && <Link to={to.run(wsId, a.run_id)}>Open the investigation</Link>}
        {r.run_ids.filter((x) => x !== a.run_id).map((x) => <Link key={x} to={to.run(wsId, x)}>Investigation {x}</Link>)}
      </p>
      {(a.certified ?? []).length > 0 && <p className="small muted">Certified for reuse: {(a.certified ?? []).map((c) => String(c.as ?? c.kind ?? "")).join(", ")}</p>}
    </Card>
  );
}

function RequesterActions({ r, onChanged }: { r: DeskRequest; onChanged: (r: DeskRequest) => void }) {
  const action = useAction();
  const [reason, setReason] = useState("");
  const [reopening, setReopening] = useState(false);
  const act = async (type: RequestActionType, payload = {}) => {
    const out = await action.run(() => api.requestAction(r.id, type, payload));
    if (out) onChanged(out);
  };
  return (
    <Card title="Does this answer your request?" label="Your decision">
      {!reopening ? <div className="toolbar">
        <button type="button" className="btn btn-primary" disabled={action.busy} onClick={() => act("accept")}>Accept the answer</button>
        <button type="button" className="btn" disabled={action.busy} onClick={() => setReopening(true)}>Reopen…</button>
      </div> : <div className="stack">
        <Field label="What is wrong or missing?" htmlFor="reopen-reason"><textarea id="reopen-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
        <div className="toolbar">
          <button type="button" className="btn btn-primary" disabled={action.busy} onClick={() => act("reopen", { reason })}>Reopen and send to an analyst</button>
          <button type="button" className="btn btn-ghost" onClick={() => setReopening(false)}>Cancel</button>
        </div>
      </div>}
      <ErrorBox error={action.error} />
    </Card>
  );
}

const CERTIFY_AS = [
  { id: "verified_query", label: "Verified query (the next similar request is answered automatically)" },
  { id: "glossary", label: "Glossary entry" },
  { id: "metric", label: "Metric suggestion (the metric's owner decides)" },
  { id: "playbook", label: "Playbook" },
] as const;

function AnalystActions({ r, members, onChanged }: { r: DeskRequest; members: Member[]; onChanged: (r: DeskRequest) => void }) {
  const { user } = useAuth();
  const action = useAction();
  const [text, setText] = useState("");
  const [minutes, setMinutes] = useState("");
  const [reason, setReason] = useState("");
  const [dupOf, setDupOf] = useState("");
  const [certifyAs, setCertifyAs] = useState<string>("verified_query");
  const analysts = useMemo(() => members.filter((m) => roleAtLeast(m.role, "analyst")), [members]);
  const act = async (type: RequestActionType, payload: Record<string, unknown> = {}) => {
    const out = await action.run(() => api.requestAction(r.id, type, payload));
    if (out) {
      setText("");
      setReason("");
      onChanged(out);
    }
  };
  const answerable = ["escalated", "auto_with_review", "reopened"].includes(r.state);
  const review = r.state === "auto_with_review";
  const closed = r.state === "accepted" || r.state === "declined";
  const certifiable = (r.state === "answered" || r.state === "accepted") && r.answer && r.answer.via !== "auto";
  const mins = minutes.trim() === "" ? NaN : Number(minutes);
  return (
    <Card title="Analyst" label="Analyst actions">
      {!closed && <div className="toolbar">
        <label className="field"><span>Assignee</span>
          <select value={r.assignee_id ?? ""} disabled={action.busy}
            onChange={(e) => e.target.value && act("assign", { assignee_id: e.target.value })}>
            <option value="" disabled>Nobody yet</option>
            {analysts.map((m) => <option key={m.user_id} value={m.user_id}>{m.user_id === user?.id ? "Me" : m.name || m.email} ({m.role})</option>)}
          </select>
        </label>
        {r.assignee_id !== user?.id && user && <button type="button" className="btn btn-sm" disabled={action.busy} onClick={() => act("assign", { assignee_id: user.id })}>Assign to me</button>}
      </div>}
      {answerable && <div className="stack">
        <Field label={review ? "Edit the prepared answer (optional)" : "Your answer"} htmlFor="desk-answer"
          hint={review ? "Leave empty to approve the prepared answer as it is." : "Say what you found and where the numbers come from."}>
          <textarea id="desk-answer" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
        </Field>
        <Field label="Minutes you spent" htmlFor="desk-minutes" hint="Recorded as human time; the Value screen weighs it against the class baseline.">
          <input id="desk-minutes" type="number" min={0} max={43200} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
        </Field>
        <div className="toolbar">
          <button type="button" className="btn btn-primary" disabled={action.busy || Number.isNaN(mins) || (!review && !text.trim())}
            onClick={() => act("answer", { text: text.trim() || undefined, human_minutes: mins })}>
            {review && !text.trim() ? "Approve the prepared answer" : "Send the answer"}
          </button>
        </div>
      </div>}
      {!closed && <details>
        <summary>Escalate, decline or link as a duplicate</summary>
        <div className="stack">
          <Field label="Reason" htmlFor="desk-reason"><input id="desk-reason" value={reason} onChange={(e) => setReason(e.target.value)} /></Field>
          <div className="toolbar">
            {(review || r.state === "in_investigation") && <button type="button" className="btn" disabled={action.busy} onClick={() => act("escalate", { reason })}>Take it over (escalate)</button>}
            <button type="button" className="btn btn-danger" disabled={action.busy || !reason.trim()} onClick={() => act("decline", { reason })}>Decline</button>
          </div>
          <Field label="Duplicate of request id" htmlFor="desk-dup"><input id="desk-dup" value={dupOf} onChange={(e) => setDupOf(e.target.value)} /></Field>
          <button type="button" className="btn btn-sm" disabled={action.busy || !dupOf.trim()} onClick={() => act("mark_duplicate", { of: dupOf.trim() })}>Link as duplicate</button>
        </div>
      </details>}
      {certifiable && <div className="stack">
        <Field label="Certify this answer for reuse" htmlFor="desk-certify" hint="Certified knowledge goes to the context ledger with this request as its source.">
          <select id="desk-certify" value={certifyAs} onChange={(e) => setCertifyAs(e.target.value)}>
            {CERTIFY_AS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
          </select>
        </Field>
        <button type="button" className="btn" disabled={action.busy} onClick={() => act("certify", { as: certifyAs })}>Certify</button>
      </div>}
      <ErrorBox error={action.error} />
    </Card>
  );
}

function DataPackCard({ pack, wsId }: { pack: DataPack; wsId: string }) {
  const p = pack.proposal;
  return (
    <Card title="Prepared for the analyst" label="Data pack">
      <p><strong>Why it is here:</strong> {pack.reason}</p>
      {pack.refusal && <Notice tone="warning">{pack.refusal.title ?? pack.refusal.kind}: {pack.refusal.message}{pack.refusal.remedy ? ` ${pack.refusal.remedy}` : ""}</Notice>}
      {pack.missing.length > 0 && <p className="small">Missing: {pack.missing.map((m) => (typeof m === "string" ? m : JSON.stringify(m))).join("; ")}</p>}
      {p && <div className="stack">
        <h3>Draft {p.kind}</h3>
        <p>{p.summary} Still needed: {p.needs.join(", ")}.</p>
        <p className="small muted">{p.approval} The desk did not create it.</p>
        <JsonView value={p.body} label={`Draft ${p.kind} (${p.endpoint})`} />
        <Link to={p.kind === "schedule" ? to.schedules(wsId) : to.monitoring(wsId)}>
          Open {p.kind === "schedule" ? "Schedules" : "Monitors & alerts"} to create it
        </Link>
      </div>}
      {pack.candidate_sql.length > 0 && <div className="stack">
        <h3>Candidate SQL</h3>
        {pack.candidate_sql.map((c, i) => <div key={i}>
          <CodeBlock code={c.sql} label={`${c.from}${c.status ? ` · ${c.status}` : ""}`} />
          {c.error && <p className="small muted">Refused: {c.error}</p>}
        </div>)}
      </div>}
      {pack.context_receipts.length > 0 && <div>
        <h3>Context the workspace already holds</h3>
        <ul className="list">{pack.context_receipts.map((c) => <li key={`${c.kind}:${c.id}`}>
          <strong>{c.kind === "asset" ? c.asset : c.name}</strong>{c.kind === "context_entry" && c.trusted === false ? " (unreviewed)" : ""}
          {c.excerpt && <span className="muted small"> — {c.excerpt}</span>}
        </li>)}</ul>
      </div>}
      {pack.evidence.length > 0 && <p className="chip-row small">Evidence: {pack.evidence.map((e) => (
        e.kind === "ask_turn" && e.thread_id ? <Link key={e.id} to={`${to.ask(wsId)}?thread=${encodeURIComponent(e.thread_id)}`}>Ask turn ({e.status})</Link>
          : e.kind === "run" ? <Link key={e.id} to={to.run(wsId, e.id)}>Investigation</Link>
            : <Tag key={e.id}>{e.kind} {e.id}</Tag>
      ))}</p>}
    </Card>
  );
}

function Timeline({ r, members }: { r: DeskRequest; members: Member[] }) {
  const { user } = useAuth();
  if (!r.timeline?.length) return null;
  const actor = (a: string) => (a.startsWith("user:") ? who(a.slice(5), members, user?.id) : a.startsWith("desk") ? "the desk" : a);
  return (
    <Card title="Timeline" label="Timeline">
      <ol className="list">{r.timeline.map((e) => <li key={e.seq}>
        <span className="chip-row">
          <strong>{e.type.replace(/_/g, " ")}</strong>
          {e.to_state && <StatusBadge status={e.to_state} />}
          <span className="small muted">{fmtDate(e.created_at)} · {actor(e.actor)}</span>
        </span>
        {typeof e.payload.reason === "string" && e.payload.reason && <span className="small muted">{e.payload.reason}</span>}
      </li>)}</ol>
    </Card>
  );
}
