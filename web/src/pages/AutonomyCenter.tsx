import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { goalsApi } from "../api";
import { AutonomyOverview, GoalForm, GoalList } from "../components/Autonomy";
import { Card, ErrorBox, Loading, PageHeader, Tabs } from "../components/ui";
import { useAsync } from "../lib/hooks";
import { to } from "../routes";
import { MonitoringPage } from "./Monitoring";
import { SchedulesPage } from "./Schedules";

export type AutonomyView = "overview" | "goals" | "schedules" | "monitors";
const VIEWS: AutonomyView[] = ["overview", "goals", "schedules", "monitors"];
const GOAL_STATES = ["", "RUNNING", "WAITING_USER", "COMPLETED", "COMPLETED_WITH_LIMITATIONS", "EXPIRED", "FAILED", "CANCELLED"];

/**
 * The Autonomy Center (S11-T5): one place for what runs on its own — goals and their runs, queues,
 * exceptions, budgets, schedules, monitors and alert noise. It replaces the separate Schedules and
 * Monitors & alerts screens (their old URLs redirect here); `?view=` picks the section, and each
 * section keeps its own query (`?schedule=`, `?tab=alerts&alert=`).
 */
export function AutonomyCenterPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const raw = params.get("view") as AutonomyView | null;
  const view: AutonomyView = raw && VIEWS.includes(raw) ? raw : "overview";
  const setView = (v: AutonomyView) => setParams(v === "overview" ? {} : { view: v });
  return (
    <div className="page">
      <PageHeader title="Automations"
        subtitle="Goals, schedules and alerts that run on their own. Every run stays within its budget, and nothing is published or sent without an approval." />
      <Tabs value={view} onChange={setView} tabs={[
        { id: "overview", label: "Overview" },
        { id: "goals", label: "Goals" },
        { id: "schedules", label: "Schedules" },
        { id: "monitors", label: "Monitors & alerts" },
      ]} />
      <div className="tab-panel" role="tabpanel">
        {view === "overview" && <Overview wsId={wsId} onView={setView} />}
        {view === "goals" && <Goals wsId={wsId} state={params.get("state") ?? ""}
          onState={(s) => setParams(s ? { view: "goals", state: s } : { view: "goals" })} />}
        {view === "schedules" && <SchedulesPage embedded />}
        {view === "monitors" && <MonitoringPage embedded />}
      </div>
    </div>
  );
}

function Overview({ wsId, onView }: { wsId: string; onView: (v: AutonomyView) => void }) {
  const summary = useAsync(() => goalsApi.summary(wsId), [wsId]);
  if (summary.error && !summary.data) return <ErrorBox error={summary.error} onRetry={summary.reload} />;
  if (!summary.data) return <Loading label="Loading automations…" />;
  return <AutonomyOverview wsId={wsId} summary={summary.data} onView={onView} />;
}

function Goals({ wsId, state, onState }: { wsId: string; state: string; onState: (s: string) => void }) {
  const goals = useAsync(() => goalsApi.list(wsId, state || undefined), [wsId, state]);
  const navigate = useNavigate();
  return (
    <>
      <Card title="Start a goal">
        <GoalForm wsId={wsId} onStarted={(g) => navigate(to.run(wsId, g.run_id))} />
      </Card>
      <Card title="Goals" actions={
        <label className="inline-field small">State
          <select value={state} onChange={(e) => onState(e.target.value)} aria-label="Filter goals by state">
            {GOAL_STATES.map((s) => <option key={s} value={s}>{s ? s.toLowerCase().replace(/_/g, " ") : "all"}</option>)}
          </select>
        </label>}>
        <ErrorBox error={goals.error} onRetry={goals.reload} />
        {!goals.data && !goals.error && <Loading />}
        {goals.data && <GoalList wsId={wsId} goals={goals.data} />}
      </Card>
    </>
  );
}
