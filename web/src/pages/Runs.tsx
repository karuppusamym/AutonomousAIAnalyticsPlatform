import { useCallback } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";
import { BuildPanel } from "../components/BuildPanel";
import { DataThreadPanel } from "../components/DataThread";
import { ExperimentsPanel } from "../components/Ml";
import { NotebooksPanel } from "../components/Notebooks";
import { PreparePanel } from "../components/PreparePanel";
import { ProcessPanel } from "../components/ProcessMining";
import { StartWorkButton } from "../components/StartWork";
import { WorkflowPanel } from "../components/WorkflowPanel";
import { EmptyState, ErrorBox, Loading, PageHeader, StatusBadge, Tabs, Tag, Value } from "../components/ui";
import { durationBetween, fmtDate } from "../lib/format";
import { useExpertView } from "../lib/expert";
import { useAsync } from "../lib/hooks";
import { to, type WorkTab } from "../routes";

const TABS: { id: WorkTab; label: string }[] = [
  { id: "investigations", label: "Investigations" }, { id: "thread", label: "Data Thread" }, { id: "notebooks", label: "Notebooks" },
  { id: "experiments", label: "Experiments" },
  { id: "prepare", label: "Prepare data" }, { id: "builds", label: "dbt builds" }, { id: "workflows", label: "Workflows" },
  { id: "process", label: "Process" },
];
/** Engineering tabs, shown with expert tools on (or when a link opens one directly). */
const EXPERT_TABS = new Set<WorkTab>(["thread", "notebooks", "experiments", "builds", "workflows", "process"]);

/**
 * Work (spec v4 §15): everything started with Start work. Investigations and their Data Thread
 * (steps, branches, merge), notebooks (cells as steps), governed ML experiments, and the data-engineering panels (Prepare data, dbt builds) that arrive
 * as job kinds rather than new screens. Ask has its own entry because it is the quick box.
 */
export function WorkPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  // `?tab=ml` links (the first ML tab) open the Experiments tab, the one home for experiments
  const requested = params.get("tab") === "ml" ? "experiments" : params.get("tab");
  const tab = (TABS.some((t) => t.id === requested) ? requested : "investigations") as WorkTab;
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const { user } = useAuth();
  const expert = useExpertView();
  const shownTabs = TABS.filter((t) => expert || !EXPERT_TABS.has(t.id) || t.id === tab);
  const set = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      return next;
    });
  }, [setParams]);
  const selectJob = useCallback((id: string | null) => set({ job: id }), [set]);
  // Platform admins act with owner authority in any workspace (the server decides either way).
  const role = user?.is_admin ? "owner" : ws.data?.role;
  return (
    <div className="page">
      <PageHeader title="Investigations" subtitle={<>Deeper analyses that look for causes, trends and drivers. For a quick answer, use{" "}
        <Link to={to.ask(wsId)}>Ask a question</Link>.</>}
        actions={<StartWorkButton wsId={wsId} />} />
      <Tabs value={tab} onChange={(t) => setParams(t === "investigations" ? {} : { tab: t })} tabs={shownTabs} />
      <div className="tab-panel" role="tabpanel">
        {tab === "investigations" && <Investigations wsId={wsId} />}
        {tab === "thread" && <DataThreadPanel wsId={wsId} role={role} container={params.get("container")} branch={params.get("branch")}
          onChange={(patch) => set(patch as Record<string, string | null>)} />}
        {tab === "experiments" && <ExperimentsPanel wsId={wsId} role={role} selected={params.get("experiment")} newKind={params.get("new")}
          onSelect={(id) => set({ experiment: id, new: null })} onNew={(k) => set({ new: k, experiment: null })} />}
        {tab === "notebooks" && <NotebooksPanel wsId={wsId} role={role} selected={params.get("notebook")} onSelect={(id) => set({ notebook: id })} />}
        {tab === "prepare" && <PreparePanel wsId={wsId} role={role} recipe={params.get("recipe")}
          onSelectRecipe={(id) => set({ recipe: id })} pipeline={params.get("pipeline")} onSelectPipeline={(id) => set({ pipeline: id })} />}
        {tab === "builds" && <BuildPanel wsId={wsId} selected={params.get("job")} onSelect={selectJob}
          canDesignate={!!user?.is_admin || ws.data?.role === "owner"} />}
        {tab === "workflows" && <WorkflowPanel wsId={wsId} role={role} />}
        {tab === "process" && <ProcessPanel wsId={wsId} role={role} />}
      </div>
    </div>
  );
}

function Investigations({ wsId }: { wsId: string }) {
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const expert = useExpertView();
  return (
    <>
      <ErrorBox error={runs.error} onRetry={runs.reload} />
      {runs.loading && !runs.data && <Loading />}
      {runs.data?.length === 0 && <EmptyState title="No investigations yet">Press Start work once your data is connected.</EmptyState>}
      {!!runs.data?.length && (
        <div className="table-wrap card">
          <table className="table runs-table">
            <caption className="sr-only">Investigations</caption>
            <thead>
              <tr><th>Objective</th><th>Status</th><th>Started</th><th>Duration</th><th className="num">Cost</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {runs.data.map((r) => (
                <tr key={r.id}>
                  <td><Link to={to.run(wsId, r.id)} className="clamp-2">{r.objective}</Link>
                    {r.origin?.type === "schedule" && <div className="small"><Tag tone="info">scheduled</Tag></div>}
                    {r.origin?.type === "alert" && <div className="small"><Tag tone="warning">from an alert</Tag></div>}</td>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="small">{fmtDate(r.started_at ?? r.created_at)}</td>
                  <td className="small">{durationBetween(r.started_at, r.finished_at)}</td>
                  <td className="num"><Value value={r.cost_usd} format="usd" /></td>
                  <td>{expert && <><Link to={to.thread(wsId, "run", r.id)} className="btn btn-xs btn-ghost">Data Thread<span className="sr-only"> of {r.objective}</span></Link>
                    <Link to={to.runConsole(wsId, r.id)} className="btn btn-xs btn-ghost">Agent console<span className="sr-only"> for {r.objective}</span></Link></>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
