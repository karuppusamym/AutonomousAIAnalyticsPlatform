import { Fragment, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, subscribeRunEvents, type ConsoleCost, type EventStreamHandle, type StreamInfo, type FeedbackResponse, type Publication, type RunDetail, type RunEvent, type RunOrigin, type RunTask,
  type WorkspacePolicy } from "../api";
import { ApprovalsPanel } from "../components/ApprovalsPanel";
import { GoalRunPanel, PlanVersions } from "../components/Autonomy";
import { ChangesPanel } from "../components/ChangesPanel";
import { InvestigationBoard } from "../components/InvestigationBoard";
import { InvestigationActions } from "../components/InvestigationActions";
import { RunGrounding } from "../components/ContextGrounding";
import { findingStanding, InvestigationOverview } from "../components/InvestigationOverview";
import { isSavedRunAnswer, SavedEvidenceAnswer } from "../components/SavedEvidenceAnswer";
import { RunLineage } from "../components/RunLineage";
import { VerificationBadge, WhyState } from "../components/WhyNumber";
import { Card, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag, TechnicalDetails, Value } from "../components/ui";
import { durationBetween, fmtDate, fmtTime, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { TERMINAL_RUN } from "../lib/status";

/** Chat kinds; rejecting a finding is done on its card, where the target is unambiguous. */
const FEEDBACK_KINDS = [
  { id: "", label: "Let the platform decide" },
  { id: "redirect", label: "Redirect — change focus / filters" },
  { id: "add_context", label: "Add context — business definitions" },
  { id: "deeper_analysis", label: "Deeper analysis" },
  { id: "question", label: "Question about the results" },
];

type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export function RunViewPage() {
  const { wsId = "", runId = "" } = useParams();
  const run = useAsync(() => api.getRun(wsId, runId), [wsId, runId]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [stream, setStream] = useState<{ state: StreamState; error?: string; info?: StreamInfo; lastUpdate?: Date }>({ state: "connecting" });
  const handleRef = useRef<EventStreamHandle | null>(null);
  const [tab, setTab] = useState<"tasks" | "events" | "lineage" | "hypotheses" | "versions">("hypotheses");
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const control = useAction();
  const investigationRef = useRef<HTMLDivElement | null>(null);
  const approvalsRef = useRef<HTMLDivElement | null>(null);
  const showInvestigation = (target: "tasks" | "hypotheses") => {
    setTab(target);
    requestAnimationFrame(() => {
      investigationRef.current?.scrollIntoView({ block: "start" });
      investigationRef.current?.focus({ preventScroll: true });
    });
  };
  const reload = run.reload;

  // Coalesce bursts of events into one refetch of the run detail.
  const scheduleRefresh = useCallback(() => {
    if (refreshTimer.current) return;
    refreshTimer.current = setTimeout(() => {
      refreshTimer.current = null;
      void reload();
    }, 800);
  }, [reload]);

  useEffect(() => {
    setEvents([]);
    const h = subscribeRunEvents(wsId, runId, {
      onEvent: (ev) => {
        setEvents((prev) => (prev.length > 500 ? [...prev.slice(-400), ev] : [...prev, ev]));
        setStream((s) => ({ ...s, lastUpdate: new Date() }));
        scheduleRefresh();
      },
      onEnd: () => scheduleRefresh(),
      onStatus: (state, error, info) => {
        setStream((s) => ({ state, error, info, lastUpdate: state === "open" ? new Date() : s.lastUpdate }));
        // Back after a drop: missed events replay from the cursor, and the detail is re-read so nothing shown is stale.
        if (state === "open" && info?.resumed) scheduleRefresh();
      },
    });
    handleRef.current = h;
    return () => {
      handleRef.current = null;
      h.close();
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    };
  }, [wsId, runId, scheduleRefresh]);

  if (run.error && !run.data) return <div className="page"><ErrorBox error={run.error} onRetry={run.reload} /></div>;
  if (!run.data) return <div className="page"><Loading label="Loading investigation…" /></div>;
  const r = run.data;
  const terminal = TERMINAL_RUN.has(r.status);
  const doControl = async (action: "pause" | "resume" | "cancel") => {
    if (action === "cancel" && !window.confirm("Cancel this investigation? Work in progress stops; nothing unapproved is published.")) return;
    const res = await control.run(() => api.controlRun(wsId, runId, action));
    if (res) void run.reload();
  };
  const pending = r.approvals.filter((a) => a.status === "pending");
  const pub = r.summary?.publication;
  const pinnedContext = r.capabilities?.analysis_context as { definition?: { version?: number; key?: string }; spec?: { purpose?: string; business_description?: string; metric_names?: string[] } } | undefined;

  return (
    <div className="page">
      <PageHeader
        title={<span className="run-title">{r.objective}</span>}
        subtitle={<>
          <StatusBadge status={r.status} /> <OriginBadge wsId={wsId} origin={r.origin} />
        </>}
        actions={<>
          {/* A finished investigation has nothing to pause or stream: its controls leave rather than sit disabled. */}
          {!terminal && <>
            <StreamIndicator state={stream.state} error={stream.error} info={stream.info} lastUpdate={stream.lastUpdate}
              onReconnect={() => handleRef.current?.reconnectNow()} />
            {r.status === "PAUSED"
              ? <button type="button" className="btn btn-sm" onClick={() => doControl("resume")} disabled={control.busy}>Resume</button>
              : <button type="button" className="btn btn-sm" onClick={() => doControl("pause")} disabled={control.busy}>Pause</button>}
            <button type="button" className="btn btn-sm btn-danger" onClick={() => doControl("cancel")} disabled={control.busy}>Cancel</button>
          </>}
          <Link to={to.thread(wsId, "run", runId)} className="btn btn-sm btn-ghost">Data Thread</Link>
          <Link to={to.runConsole(wsId, runId)} className="btn btn-sm btn-ghost">Agent console</Link>
        </>}
      />
      <ErrorBox error={control.error} />
      {r.error && <Notice tone="danger"><strong>Investigation error:</strong> {r.error}</Notice>}
      <InvestigationOverview run={r} onExplore={() => showInvestigation("hypotheses")} onPlan={() => showInvestigation("tasks")}
        onContext={() => {const el=document.getElementById("run-grounding");el?.scrollIntoView({block:"start"});el?.focus({preventScroll:true});}}
        onApprovals={() => {
          approvalsRef.current?.scrollIntoView({ block: "start" });
          approvalsRef.current?.focus({ preventScroll: true });
        }} />
      <RedirectChat key={r.id} wsId={wsId} run={r} disabled={["FAILED", "CANCELLED", "REJECTED"].includes(r.status)} onDone={() => void run.reload()} />
      {!!r.origin?.guided_template && <Card title="Guided investigation context"><p className="small">Started from {String((r.origin.guided_template as Record<string, unknown>).title)} · template v{String((r.origin.guided_template as Record<string, unknown>).version)}.</p>
        {!!r.origin.reviewed_context && <p className="small">Reviewed brief v{String((r.origin.reviewed_context as Record<string, unknown>).version)} · {String((r.origin.reviewed_context as Record<string, unknown>).fact_count ?? 0)} reviewed facts pinned.</p>}
        <p className="small muted">Reviewed business facts were pinned at start; later edits apply to future investigations.</p></Card>}
      <InvestigationActions wsId={wsId} run={r} />
      <div id="run-grounding" tabIndex={-1}><RunGrounding wsId={wsId} runId={runId} refreshKey={r.tasks.map(t=>`${t.key}:${t.status}`).join("|")} onRecheck={() => void run.reload()} /></div>
      {pending.length > 0 && <Notice tone="warning">{pending.length} approval{pending.length > 1 ? "s" : ""} waiting — see the Approvals panel.</Notice>}
      {pinnedContext && <Card title={`Analysis context · ${pinnedContext.definition?.key ?? "saved"} v${pinnedContext.definition?.version ?? "?"}`}>
        <p><strong>Purpose:</strong> {pinnedContext.spec?.purpose}</p>
        {pinnedContext.spec?.business_description && <p className="muted small">{pinnedContext.spec.business_description}</p>}
        {!!pinnedContext.spec?.metric_names?.length && <p className="small">Relevant metrics: {pinnedContext.spec.metric_names.join(", ")}</p>}
        <p className="small"><Link to={to.data(wsId, "contexts")}>View analysis contexts</Link></p>
      </Card>}

      <div className="stats-row card card-body">
        <KeyValue items={[
          ["Started", fmtDate(r.started_at ?? r.created_at)],
          ["Duration", durationBetween(r.started_at, r.finished_at)],
          ["Tokens", <Value key="t" value={r.tokens} format="int" />],
          ["Cost", <Value key="c" value={r.cost_usd} format="usd" />],
          ["Tasks", `${r.tasks.filter((t) => t.status === "COMPLETED").length}/${r.tasks.length} done`],
          ["Hypotheses", String(r.hypotheses.length)],
          ["Findings", `${r.insights.filter((i) => findingStanding(i) === "verified").length} verified / ${r.insights.length}`],
        ]} />
        <TechnicalDetails>
          <p className="small">Investigation <code>{r.id}</code> · autonomy level L{r.autonomy_level} · plan version {r.plan_version}
            {r.plan_hash ? <> (<code title={r.plan_hash}>{shortHash(r.plan_hash, 10)}</code>)</> : null} · iteration {r.iteration}</p>
        </TechnicalDetails>
      </div>

      {r.summary?.changes && <ChangesPanel changes={r.summary.changes} wsId={wsId} reportArtifactId={r.summary.report_artifact_id} />}
      {!r.summary?.changes && r.summary?.report_artifact_id && (
        <Notice tone="info">A report was generated for this investigation: <Link to={to.reports(wsId, r.summary.report_artifact_id)}>open it in Outputs</Link>.</Notice>
      )}

      {pub && (
        <Card title="Result summary" actions={r.summary?.summary_source ? <span className="muted small">source: {r.summary.summary_source}</span> : undefined}>
          {r.summary?.cancel_outcome && <p className="muted small">Cancel outcome: {r.summary.cancel_outcome}</p>}
          {pub && <PublicationBox pub={pub} onRolledBack={run.reload} />}
        </Card>
      )}

      <div className="run-grid">
        <div className="run-main" ref={investigationRef} tabIndex={-1} aria-label="Investigation details">
          <Tabs value={tab} onChange={setTab} tabs={[
            { id: "tasks", label: `Plan & tasks (${r.tasks.length})` },
            { id: "events", label: `Live events (${events.length})` },
            { id: "lineage", label: "Lineage" },
            { id: "hypotheses", label: `Hypotheses (${r.hypotheses.length})` },
            { id: "versions", label: `Plan versions (${r.plan_version})` },
          ]} />
          <div className="tab-panel card card-body" role="tabpanel">
            {tab === "hypotheses" && <InvestigationBoard wsId={wsId} runId={runId} objective={r.objective} hypotheses={r.hypotheses} insights={r.insights} approvals={r.approvals}
              readOnly={["FAILED", "CANCELLED", "REJECTED"].includes(r.status)} onChanged={() => void run.reload()} />}
            {tab === "tasks" && <TaskBoard tasks={r.tasks} />}
            {tab === "events" && <EventFeed events={events} />}
            {tab === "lineage" && <RunLineage wsId={wsId} runId={runId} />}
            {tab === "versions" && <PlanVersions runId={runId} current={r.plan_version} />}
          </div>
        </div>
        <aside className="run-side">
          <GoalRunPanel runId={runId} events={events} refreshKey={`${r.status}:${r.plan_version}`} />
          <NumbersPanel wsId={wsId} runId={runId} findings={r.insights.length} />
          <CostMeter wsId={wsId} run={r} />
          <div ref={approvalsRef} tabIndex={-1} aria-label="Investigation approvals"><Card title={`Approvals${pending.length ? ` (${pending.length} pending)` : ""}`}>
            <ApprovalsPanel approvals={r.approvals} onDecided={() => void run.reload()} />
          </Card></div>
          <Card title="Authorized scope">
            <KeyValue items={[
              ["Assets", ((r.scope.assets as string[] | undefined) ?? []).join(", ") || "—"],
              ["Denied columns", String(((r.scope.denied_columns as string[] | undefined) ?? []).length)],
              ["Max rows", String(r.scope.max_rows ?? "—")],
              ["Policy version", String(r.scope.policy_version ?? r.policy_version)],
            ]} />
          </Card>
        </aside>
      </div>
    </div>
  );
}

/** Where a run came from: a schedule firing or an alert investigation (user runs show nothing). */
export function OriginBadge({ wsId, origin }: { wsId: string; origin: RunOrigin | null | undefined }) {
  if (!origin || !origin.type || origin.type === "user") return null;
  if (origin.type === "schedule") {
    return (
      <span className="origin">
        <Tag tone="info">scheduled</Tag>{" "}
        {origin.schedule_id && <Link className="small" to={to.schedules(wsId, origin.schedule_id)}>schedule</Link>}
        {origin.previous_run_id && <> · <Link className="small" to={to.run(wsId, origin.previous_run_id)}>previous investigation</Link></>}
        {origin.publish && <span className="muted small"> · publish: {origin.publish}</span>}
      </span>
    );
  }
  if (origin.type === "alert") {
    return (
      <span className="origin">
        <Tag tone="warning">{origin.automatic ? "alert investigation (automatic)" : "alert investigation"}</Tag>{" "}
        {origin.alert_id && <Link className="small" to={to.monitoring(wsId, { tab: "alerts", alert: origin.alert_id })}>alert</Link>}
      </span>
    );
  }
  return <Tag>{origin.type}</Tag>;
}

/**
 * The live-update state (workbench-ux §3): a dropped stream says "Reconnecting" with the attempt,
 * when it retries and the time of the last update, and offers to reconnect now; it resumes from the
 * persisted cursor, so no event is lost or shown twice.
 */
export function StreamIndicator({ state, error, info, lastUpdate, onReconnect }: {
  state: StreamState; error?: string; info?: StreamInfo; lastUpdate?: Date; onReconnect?: () => void;
}) {
  const label = state === "open" ? "Live" : state === "connecting" ? "Connecting" : state === "reconnecting" ? "Reconnecting" : "Not live";
  const since = lastUpdate ? lastUpdate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : null;
  const detail = state === "reconnecting"
    ? [info?.attempt ? `attempt ${info.attempt}` : null, info?.retryInMs ? `retry in ${Math.ceil(info.retryInMs / 1000)} s` : null,
      since ? `last update ${since}` : "no update yet"].filter(Boolean).join(" · ")
    : state === "closed" && error ? error : null;
  return (
    <span className={`stream stream-${state}`} role="status" title={error ?? label}>
      <span className="stream-dot" aria-hidden="true" />{label}
      {detail && <span className="small muted"> ({detail})</span>}
      {state === "reconnecting" && onReconnect && (
        <button type="button" className="btn btn-xs btn-ghost" onClick={onReconnect}>Reconnect now</button>
      )}
    </span>
  );
}

function PublicationBox({ pub, onRolledBack }: { pub: Publication; onRolledBack: () => void }) {
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  const urls = Object.entries(pub.urls ?? {});
  const rollback = async () => {
    if (!pub.publication_id || !window.confirm("Roll back this publication? Published dashboards are removed from the destination.")) return;
    const r = await act.run(() => api.rollback(pub.publication_id!));
    if (r) {
      setDone("Publication rolled back.");
      onRolledBack();
    }
  };
  return (
    <div className="publication">
      <h3>Publication <StatusBadge status={pub.status} /></h3>
      {urls.length === 0 ? <p className="muted small">No URLs returned.</p> : (
        <ul>{urls.map(([k, u]) => <li key={k}><a href={u} target="_blank" rel="noreferrer noopener">{k}</a> <span className="muted small">{u}</span></li>)}</ul>
      )}
      {pub.publication_id && (
        <button type="button" className="btn btn-sm btn-danger" onClick={rollback} disabled={act.busy}>{act.busy ? "Rolling back…" : "Roll back publication"}</button>
      )}
      <ErrorBox error={act.error} />
      {done && <Notice tone="success">{done}</Notice>}
    </div>
  );
}

function TaskBoard({ tasks }: { tasks: RunTask[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (!tasks.length) return <EmptyState title="No plan yet">The supervisor is planning.</EmptyState>;
  const sorted = [...tasks].sort((a, b) => a.seq - b.seq || a.key.localeCompare(b.key));
  const counts = sorted.reduce<Record<string, number>>((acc, t) => ({ ...acc, [t.status]: (acc[t.status] ?? 0) + 1 }), {});
  return (
    <>
      <div className="chip-row">{Object.entries(counts).map(([s, n]) => <StatusBadge key={s} status={s} label={`${s} · ${n}`} />)}</div>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th className="num">#</th><th>Task</th><th>Agent</th><th>Status</th><th className="num">Attempts</th><th>Duration</th><th>Error</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>
            {sorted.map((t) => (
              <Fragment key={t.id}>
                <tr>
                  <td className="num">{t.seq}</td>
                  <td><code>{t.key}</code><div className="muted small">{t.title}</div></td>
                  <td>{t.agent_id}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td className="num">{t.attempts}</td>
                  <td className="small">{durationBetween(t.started_at, t.finished_at)}</td>
                  <td className="small warn-text clamp-2">{t.error ?? ""}</td>
                  <td><button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
                    {open === t.id ? "Hide" : "Details"}</button></td>
                </tr>
                {open === t.id && (
                  <tr className="row-detail"><td colSpan={8}><TaskDetail task={t} /></td></tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function TaskDetail({ task }: { task: RunTask }) {
  const detail = useAsync(() => api.agentRun(task.id), [task.id]);
  return (
    <div className="task-detail">
      <p className="small muted">Depends on: {task.depends_on.join(", ") || "—"} · plan v{task.plan_version}</p>
      <div className="grid-2">
        <TechnicalDetails value={task.input} label="Input (technical details)" />
        <TechnicalDetails value={task.output} label="Output (technical details)" />
      </div>
      <ErrorBox error={detail.error} />
      {detail.data && (
        <>
          <p className="small">
            {detail.data.messages.length} messages · {detail.data.tool_calls.length} tool calls · {detail.data.model_calls.length} model calls ·{" "}
            {detail.data.queries.length} queries
          </p>
          <ul className="timeline small">
            {detail.data.messages.map((m) => <li key={m.id}><span className="muted">{fmtTime(m.created_at)} [{m.kind}]</span> {m.content}</li>)}
          </ul>
        </>
      )}
    </div>
  );
}

function describeEvent(ev: RunEvent): string {
  const p = ev.payload ?? {};
  const pick = (...keys: string[]) => keys.map((k) => p[k]).find((v) => v !== undefined && v !== null && v !== "");
  const main = pick("content", "message", "text", "title", "statement", "code", "key", "status", "reason", "summary");
  return main !== undefined ? String(main) : "";
}

function EventFeed({ events }: { events: RunEvent[] }) {
  if (!events.length) return <EmptyState title="Waiting for events…" />;
  const shown = [...events].reverse().slice(0, 300);
  return (
    <ol className="events" aria-live="polite" aria-relevant="additions">
      {shown.map((ev) => (
        <li key={ev.id} className="event">
          <span className="event-time muted small">{fmtTime(ev.created_at)}</span>
          <span className={`event-type type-${ev.type.split(".")[0]}`}>{ev.type}</span>
          <span className="event-text">{describeEvent(ev)}</span>
          {ev.actor && <span className="muted small">{ev.actor}</span>}
          <span className="event-payload"><TechnicalDetails value={ev.payload} label="payload" /></span>
        </li>
      ))}
    </ol>
  );
}

/**
 * Redirect by chat: the run's instructions as a conversation, and a composer that sends feedback.
 * JEV classifies the message when no kind is chosen; a redirect replans the run.
 */
function RedirectChat({ wsId, run, disabled, onDone }: { wsId: string; run: RunDetail; disabled: boolean; onDone: () => void }) {
  const [text, setText] = useState("");
  const [kind, setKind] = useState("question");
  const [resp, setResp] = useState<FeedbackResponse | null>(null);
  const [sentText, setSentText] = useState("");
  const sending = useRef(false);
  const messageRef = useRef<HTMLTextAreaElement | null>(null);
  const logRef = useRef<HTMLOListElement | null>(null);
  const act = useAction();
  const history = run.instructions ?? [];
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [history.length, resp]);
  const suggestedFinding = run.insights.find((i) => findingStanding(i) === "verified") ?? run.insights.find((i) => findingStanding(i) !== "excluded");
  const suggestions = ["Summarize the findings", "What are the limitations?", "Which findings need checking?",
    ...(suggestedFinding ? [`How was ${suggestedFinding.code} checked?`] : [])];
  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || disabled || sending.current) return;
    sending.current = true;
    const submitted = text.trim();
    const r = await act.run(() => api.feedback(wsId, run.id, { text: text.trim(), kind: kind || null, target_type: null, target_id: null }));
    sending.current = false;
    if (r) {
      setResp(r);
      setSentText(submitted);
      setText("");
      onDone();
    }
  };
  return (
    <Card title="Continue the investigation" label="Investigation conversation" className="investigation-conversation">
      <p className="small muted">Ask about saved findings, checks and limitations. Choose Deeper analysis below when you want the agents to investigate further.</p>
      <div className="conversation-suggestions" role="group" aria-label="Suggested follow-up questions">
        {suggestions.map((q) => <button key={q} type="button" className="btn btn-sm" disabled={disabled || act.busy}
          onClick={() => { setText(q); setKind("question"); messageRef.current?.focus(); }}>{q}</button>)}
      </div>
      <ol ref={logRef} className="chat-log" aria-label="Instructions sent to this investigation">
        {history.length === 0 && <li className="muted small">No instructions yet. Tell the agents what to focus on, exclude or explain.</li>}
        {history.map((ins, i) => (
          <li key={i} className="chat-msg chat-user">
            <span className="chat-who">You</span> <Tag>{String(ins.kind ?? "feedback").replace(/_/g, " ")}</Tag>
            <p>{String(ins.text ?? "")}</p>
            {isSavedRunAnswer(ins.answer) && <div className="chat-agent"><span className="chat-who">Investigator</span><SavedEvidenceAnswer answer={ins.answer} wsId={wsId} /></div>}
          </li>
        ))}
        {resp && !history.some((ins) => ins.feedback_id === resp.feedback_id) && (
          <li className="chat-msg chat-agent" aria-live="polite">
            <span className="chat-who">Supervisor</span>
            <p><strong>You:</strong> {sentText}</p>
            {resp.answer ? <SavedEvidenceAnswer answer={resp.answer} wsId={wsId} /> : <>
            <p className="small">
              Read as <strong title={`classified by ${resp.classified_by}`}>{resp.kind.replace(/_/g, " ")}</strong>
              .
              {resp.interpretation && <> {resp.interpretation.summary}</>}
              {resp.replan && <> Replanned to plan v{resp.replan.plan_version}.</>}
            </p>
            {resp.interpretation && resp.interpretation.filters.length > 0 && (
              <ul className="small">{resp.interpretation.filters.map((f, i) => (
                <li key={i}><code>{String(f.asset ?? "")}.{String(f.column)} {String(f.op)} {JSON.stringify(f.value)}</code></li>
              ))}</ul>
            )}
            {resp.note && <Notice tone="warning">{resp.note}</Notice>}
            </>}
          </li>
        )}
      </ol>
      <form className="form" onSubmit={submit}>
        <Field label="Message" htmlFor="fb-text">
          <textarea id="fb-text" ref={messageRef} rows={3} value={text} onChange={(e) => setText(e.target.value)} disabled={disabled || act.busy}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit(); }}
            placeholder="What did we find, and what should I check before trusting it?" />
        </Field>
        <Field label="What would you like to do?" htmlFor="fb-kind" hint="Questions explain saved evidence. Deeper analysis can restart steps and invalidate pending approvals.">
          <select id="fb-kind" value={kind} onChange={(e) => setKind(e.target.value)} disabled={disabled || act.busy}>
            {FEEDBACK_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || disabled || !text.trim()}>{act.busy ? "Sending…" : "Send"}</button>
        </div>
      </form>
      {run.constraints && Object.keys(run.constraints).length > 0 && <TechnicalDetails value={run.constraints} label="Compiled constraints (technical details)" />}
    </Card>
  );
}

/**
 * Live cost meter: tokens and spend against the workspace run budgets, tokens avoided by the
 * cache and deterministic paths, and the ladder rungs when the server reports them. A missing
 * number is unknown, never 0.
 */
function CostMeter({ wsId, run }: { wsId: string; run: RunDetail }) {
  const policy = useAsync(() => api.getWorkspace(wsId).then((w) => w.policy as WorkspacePolicy), [wsId]);
  const cost = useAsync<ConsoleCost | null>(() => api.console(wsId, run.id).then((c) => c.cost).catch(() => null), [wsId, run.id, run.tokens, run.status]);
  const c = cost.data ?? null;
  const rungs = c?.by_rung ? Object.entries(c.by_rung).sort(([a], [b]) => a.localeCompare(b)) : [];
  return (
    <Card title="Cost">
      <Meter label="Tokens" value={run.tokens} max={policy.data?.run_token_budget} format="int" />
      <Meter label="Spend" value={run.cost_usd} max={policy.data?.run_cost_budget_usd} format="usd" />
      <KeyValue items={[
        ["Tokens avoided", <Value key="s" value={c?.tokens_saved} format="int" />],
        ["Cache hits", <Value key="h" value={c?.cache_hits} format="int" />],
        ["Answered by rules", <Value key="d" value={c?.deterministic_skips} format="int" />],
        ["Model calls", <Value key="m" value={c?.model_calls} format="int" />],
      ]} />
      <TechnicalDetails label="Spend by model tier">
      {rungs.length ? (
        <ul className="rung-list small" aria-label="Spend by rung">
          {rungs.map(([rung, v]) => (
            <li key={rung}><code>{rung}</code> <Value value={v.calls} format="int" suffix="calls" /> · <Value value={v.cost_usd} format="usd" />
              {v.tokens_saved !== undefined && <> · <Value value={v.tokens_saved} format="int" suffix="avoided" /></>}</li>
          ))}
        </ul>
      ) : <p className="muted small">Tier breakdown not reported by this server.</p>}
      </TechnicalDetails>
    </Card>
  );
}

function Meter({ label, value, max, format }: { label: string; value: number | null | undefined; max: number | null | undefined; format: "int" | "usd" }) {
  const known = typeof value === "number" && Number.isFinite(value);
  const hasMax = typeof max === "number" && max > 0;
  const pct = known && hasMax ? Math.min(100, (value / max) * 100) : null;
  const tone = pct === null ? "neutral" : pct >= 90 ? "danger" : pct >= 70 ? "warning" : "success";
  return (
    <div className="meter">
      <div className="meter-head small">
        <span>{label}</span>
        <span><Value value={value} format={format} />{hasMax && <span className="muted"> of <Value value={max} format={format} /></span>}</span>
      </div>
      {pct !== null && (
        <div className="confidence-track" role="meter" aria-label={`${label} used of run budget`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)}>
          <div className={`confidence-fill fill-${tone}`} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

/**
 * Every number the investigation reported, traced (P7-08): how many still hold, and which findings
 * are void and why. Each finding's numbers open "Why this number?" from its card or its Outputs page.
 */
function NumbersPanel({ wsId, runId, findings }: { wsId: string; runId: string; findings: number }) {
  const why = useAsync(() => (findings ? api.whyRun(wsId, runId) : Promise.resolve(null)), [wsId, runId, findings]);
  if (!findings) return null;
  const d = why.data;
  const held = d?.by_state?.ok ?? 0;
  const voided = (d?.findings ?? []).filter((f) => f.verification_state?.badge === "void");
  return (
    <Card title="Numbers">
      {why.error && <p className="muted small">Could not trace the numbers: {why.error}</p>}
      {!d && !why.error && <Loading />}
      {d && (
        <>
          <p className="small">{d.numbers === 0 ? "No numbers reported yet." : held === d.numbers ? `All ${d.numbers} reported numbers still hold.`
            : `${held} of ${d.numbers} reported numbers still hold.`}</p>
          {Object.entries(d.by_state).filter(([k, n]) => k !== "ok" && n > 0).map(([k, n]) => (
            <p key={k} className="small"><WhyState state={k} /> {n}</p>
          ))}
          {voided.map((f) => (
            <p key={f.subject.id} className="small"><strong>{f.subject.code}</strong> <VerificationBadge state={f.verification_state} /></p>
          ))}
        </>
      )}
    </Card>
  );
}
