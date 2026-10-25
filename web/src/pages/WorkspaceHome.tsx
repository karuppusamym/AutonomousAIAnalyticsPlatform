import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type CatalogAsset, type Insight, type Run, type Source, type WorkspaceDetail } from "../api";
import { BriefSummary } from "../components/Brief";
import { DataShapeCard } from "../components/DataShape";
import { WelcomeCard } from "../components/Guide";
import { UsageGuide } from "../components/UsageGuide";
import { useTour } from "../components/Tour";
import { StartWorkButton } from "../components/StartWork";
import { WorkModesSettings } from "../components/WorkModes";
import { ReviewedContext } from "../components/ReviewedContext";
import { PilotScorecard } from "../components/PilotScorecard";
import { Card, ErrorBox, Field, Loading, Notice, PageHeader, Stat, Value } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync, type AsyncState } from "../lib/hooks";
import { TERMINAL_RUN } from "../lib/status";
import { roleAtLeast, to } from "../routes";

/** A source not checked for this long is shown as stale on the Overview. */
export const STALE_AFTER_DAYS = 7;

export interface ChecklistStep {
  id: "connect" | "select" | "brief" | "goal" | "start";
  label: string;
  hint: string;
  done: boolean;
}

/**
 * The first-run checklist (workbench-ux §2), derived only from real state so it resumes wherever the
 * person left off: connect or upload → select tables → review the data brief → describe the goal →
 * start work (the outcome and the preflight are chosen in Start work).
 */
export function firstRunSteps(state: { sources: Source[]; catalog: CatalogAsset[]; objective: string; runs: Run[] }): ChecklistStep[] {
  return [
    { id: "connect", label: "Connect or upload data", hint: "A database, a warehouse, ServiceNow or a file.", done: state.sources.length > 0 },
    { id: "select", label: "Choose the tables to use", hint: "Only selected tables are read, and only through the governed gateway.",
      done: state.sources.some((s) => s.status === "ready") },
    { id: "brief", label: "Review what the data means", hint: "Row grain, entities, joins and key definitions, in the catalog.",
      done: state.catalog.length > 0 },
    { id: "goal", label: "Describe what you want to know", hint: "One or two sentences; it pre-fills new work.", done: state.objective.trim().length >= 10 },
    { id: "start", label: "Start your first piece of work", hint: "Pick what to do; you see what it will read, spend and need from you before it starts.",
      done: state.runs.length > 0 },
  ];
}

export function nextStep(steps: ChecklistStep[]): ChecklistStep | undefined {
  return steps.find((s) => !s.done);
}

/**
 * Overview, in two states (spec v4 §15). Before the first piece of work: a checklist from real state
 * where only the next step has a button. After it: only what needs the person, and one Start work.
 * Counters and cost are secondary, behind "At a glance".
 */
export function WorkspaceHomePage() {
  const { wsId = "" } = useParams();
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);

  if (ws.error) return <div className="page"><ErrorBox error={ws.error} onRetry={ws.reload} /></div>;
  if (!ws.data) return <div className="page"><Loading /></div>;
  const w = ws.data;
  const firstRun = runs.data !== undefined && runs.data.length === 0;
  const checked = (sources.data ?? []).map((s) => s.last_discovered_at).filter((d): d is string => !!d).sort().pop();

  return (
    <div className="page">
      <PageHeader title={w.name} subtitle={<>{w.objective || w.description || undefined}
        {checked && <span className="block small">Data last checked {fmtDate(checked)}.</span>}</>}
        actions={firstRun ? undefined : <StartWorkButton wsId={wsId} />} />
      <ErrorBox error={runs.error ?? sources.error} onRetry={() => { void runs.reload(); void sources.reload(); }} />
      <UsageGuide wsId={wsId} />
      {runs.data === undefined && !runs.error && <Loading />}
      {runs.data && runs.data.length > 0 && <WelcomeCard wsId={wsId} />}
      {firstRun && <FirstRun ws={w} sources={sources} onChanged={ws.reload} />}
      {runs.data && runs.data.length > 0 && (
        <>
          <NeedsYou wsId={wsId} runs={runs.data} sources={sources.data} />
          <AtAGlance ws={w} runs={runs.data} />
          <DataShapeCard wsId={wsId} role={w.role} />
          <ReviewedContext wsId={wsId} role={w.role} />
          <PilotScorecard wsId={wsId} role={w.role} runs={runs.data} />
        </>
      )}
      {w.role === "owner" && !!runs.data?.length && <WorkModesSettings wsId={wsId} collapsed />}
    </div>
  );
}

function FirstRun({ ws, sources, onChanged }: { ws: WorkspaceDetail; sources: AsyncState<Source[]>; onChanged: () => void }) {
  const catalog = useAsync(() => api.catalog(ws.id), [ws.id]);
  const tour = useTour();
  if (!sources.data || (!catalog.data && !catalog.error)) return <Loading />;
  const steps = firstRunSteps({ sources: sources.data, catalog: catalog.data ?? [], objective: ws.objective ?? "", runs: [] });
  const next = nextStep(steps);
  const action: Record<ChecklistStep["id"], ReactNode> = {
    connect: <Link className="btn btn-primary btn-sm" to={to.sources(ws.id)}>Connect data</Link>,
    select: <Link className="btn btn-primary btn-sm" to={to.sources(ws.id)}>Choose tables</Link>,
    brief: <BriefStep wsId={ws.id} />,
    goal: <GoalForm ws={ws} onSaved={onChanged} />,
    start: <StartWorkButton wsId={ws.id} className="btn btn-primary btn-sm" />,
  };
  return (
    <Card title="Get started">
      <p className="muted small">Pick up where you left off: each step is checked against what is already in the workspace.{" "}
        <button type="button" className="btn-link" onClick={() => tour.start("platform", ws.id)}>New here? Take the 2-minute tour</button>
      </p>
      <ol className="checklist" aria-label="Getting started">
        {steps.map((s, k) => {
          const isNext = s.id === next?.id;
          return (
            <li key={s.id} className={`checklist-step ${s.done ? "done" : isNext ? "next" : "later"}`} aria-current={isNext ? "step" : undefined}>
              <span className="checklist-mark" aria-hidden="true">{s.done ? "✓" : k + 1}</span>
              <div className="checklist-body">
                <strong>{s.label}</strong><span className="sr-only">{s.done ? " (done)" : isNext ? " (next)" : " (later)"}</span>
                {isNext && <p className="muted small">{s.hint}</p>}
                {isNext && action[s.id]}
              </div>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

/**
 * Onboarding step 3: the data brief as drafted from the catalog (facts and open questions), read-only
 * here; it is reviewed at its one home, Data → Brief & readiness. The catalog stays one click away.
 */
function BriefStep({ wsId }: { wsId: string }) {
  const brief = useAsync(() => api.brief(wsId), [wsId]);
  return (
    <div className="stack">
      {brief.error && <p className="small warn-text">The brief could not be loaded: {brief.error}</p>}
      {brief.data && <BriefSummary wsId={wsId} brief={brief.data} />}
      {!brief.data && <Link className="btn btn-primary btn-sm" to={to.catalog(wsId)}>Review the catalog</Link>}
    </div>
  );
}

/** The brief's one required field: what the person wants to know. Needs the editor role. */
function GoalForm({ ws, onSaved }: { ws: WorkspaceDetail; onSaved: () => void }) {
  const [objective, setObjective] = useState(ws.objective ?? "");
  const act = useAction();
  useEffect(() => setObjective(ws.objective ?? ""), [ws.objective]);
  if (!roleAtLeast(ws.role, "editor")) return <p className="small">Ask an editor of this workspace to describe the goal.</p>;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (await act.run(() => api.updateWorkspace(ws.id, { objective: objective.trim() }))) onSaved();
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Describe the goal">
      <Field label="Goal" htmlFor="overview-goal" hint="At least 10 characters.">
        <textarea id="overview-goal" rows={2} value={objective} onChange={(e) => setObjective(e.target.value)}
          placeholder="Why are incident resolution times rising, and which groups drive SLA breaches?" />
      </Field>
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || objective.trim().length < 10}>Save goal</button>
      </div>
    </form>
  );
}

type Need = { id: string; label: string; count: number | undefined; error?: string | null; href: string; hint: string };

const count = <T,>(s: { data?: T[]; error: string | null }, pick: (x: T) => boolean = () => true) =>
  (s.error || !Array.isArray(s.data) ? undefined : s.data.filter(pick).length);

/**
 * What needs the person: each item is fetched on its own, and one that could not be loaded shows as
 * unknown, never as 0 ("no open alerts" and "could not check alerts" are different facts).
 */
function NeedsYou({ wsId, runs, sources }: { wsId: string; runs: Run[]; sources: Source[] | undefined }) {
  const approvals = useAsync(() => api.listApprovals(wsId, "pending"), [wsId]);
  const alerts = useAsync(() => api.listAlerts(wsId, "open"), [wsId]);
  const insights = useAsync(() => api.listInsights(wsId), [wsId]);
  const questions = useAsync(() => api.relationshipCandidates(wsId, "pending"), [wsId]);
  const knowledge = useAsync(() => api.suggestionSummary(wsId), [wsId]);
  const staleBefore = Date.now() - STALE_AFTER_DAYS * 86_400_000;
  const stale = sources?.filter((s) => s.last_error || !s.last_discovered_at || Date.parse(s.last_discovered_at) < staleBefore);
  const needs: Need[] = [
    { id: "approvals", label: "Pending approvals", count: count(approvals), error: approvals.error, href: to.approvals(wsId), hint: "Waiting for a decision" },
    { id: "alerts", label: "Open alerts", count: count(alerts), error: alerts.error, href: to.monitoring(wsId, { tab: "alerts" }), hint: "A monitor fired" },
    { id: "void", label: "Void findings", count: count<Insight>(insights, (i) => i.verification_state?.badge === "void"), error: insights.error,
      href: to.outputs(wsId, { type: "finding" }), hint: "Their data or definition changed; re-run to verify again" },
    { id: "waiting", label: "Investigations waiting for you", count: runs.filter((r) => r.status === "WAITING_USER" || r.status === "PAUSED").length,
      href: to.investigations(wsId), hint: "Paused or asking a question" },
    { id: "questions", label: "Open data questions", count: count(questions), error: questions.error, href: to.data(wsId, "definitions"),
      hint: "Measured joins to confirm" },
    { id: "meaning", label: "Questions about your data", count: knowledge.error || !knowledge.data ? undefined : knowledge.data.questions,
      error: knowledge.error, href: to.data(wsId, "review"), hint: meaningHint(knowledge.data?.questions) },
    { id: "stale", label: "Stale sources", count: stale?.length, href: to.sources(wsId), hint: `Not checked in ${STALE_AFTER_DAYS} days, or failing` },
  ];
  const shown = needs.filter((n) => n.count === undefined || n.count > 0);
  const loading = [approvals, alerts, insights, questions, knowledge].some((s) => s.loading && !s.data && !s.error);
  return (
    <Card title="What needs you">
      {!loading && shown.length === 0 && <Notice tone="success">Nothing needs you right now.</Notice>}
      <div className="stats-row">
        {shown.map((n) => (
          <Link key={n.id} to={n.href} className="stat-link">
            <Stat label={n.label} value={<Value value={n.count} format="int" unknownTitle={n.error ?? "Loading"} />} hint={n.hint} />
          </Link>
        ))}
      </div>
    </Card>
  );
}

/** Glossary terms and descriptions only a person can give: good answers are what every later answer stands on. */
export function meaningHint(n: number | undefined): string {
  if (n === undefined) return "Glossary terms and descriptions to confirm";
  return `${n} ${n === 1 ? "question" : "questions"} about your data ${n === 1 ? "needs" : "need"} an answer`;
}

/** Counters and cost: secondary, collapsed. */
function AtAGlance({ ws, runs }: { ws: WorkspaceDetail; runs: Run[] }) {
  const running = runs.filter((r) => !TERMINAL_RUN.has(r.status)).length;
  const cost = runs.reduce<number | undefined>((sum, r) => (r.cost_usd == null || sum === undefined ? undefined : sum + r.cost_usd), 0);
  return (
    <details className="card at-a-glance">
      <summary>At a glance</summary>
      <div className="card-body stats-row">
        <Stat label="Investigations in progress" value={<Value value={running} format="int" />} />
        <Stat label="Investigations" value={<Value value={ws.counts?.runs} format="int" />} />
        <Stat label="Verified findings" value={<Value value={ws.counts?.verified_insights} format="int" />} />
        <Stat label="Dashboards" value={<Value value={ws.counts?.dashboard} format="int" />} />
        <Stat label="Spend on these investigations" value={<Value value={cost} format="usd" />} />
      </div>
    </details>
  );
}
