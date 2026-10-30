import { useNavigate } from "react-router-dom";
import { api, type BuilderDashboard } from "../api";
import { useExpertView } from "../lib/expert";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { DashboardDigest } from "./DashboardDigest";
import { Card, ErrorBox, Loading } from "./ui";

/**
 * The dashboard's story (CONV-05): what it shows, its notable facts and what changed since the previous version.
 * Facts are computed on the server from the chart previews and the version diff; "Turn this into a report" makes
 * a report document of them that can be shaped further by asking.
 */
export function DashboardSummary({ dashboard, canEdit = false }: { dashboard: BuilderDashboard; canEdit?: boolean }) {
  const expert = useExpertView();
  const navigate = useNavigate();
  const story = useAsync(() => api.dashboardStory(dashboard.id), [dashboard.id, dashboard.version]);
  const act = useAction();
  const toReport = async () => {
    const r = await act.run(() => api.storyToReport(dashboard.id));
    if (r) navigate(to.outputs(r.workspace_id, { type: "report", artifact: r.id }));
  };
  const s = story.data;
  return (
    <Card title="Summary" label="Dashboard summary" actions={
      <button type="button" className="btn btn-sm" disabled={act.busy || !s} onClick={() => void toReport()}>Turn this into a report</button>}>
      {story.loading && !s ? <Loading label="Reading the dashboard…" /> : s ? (
        <div className="stack small">
          <p>{s.shows}</p>
          {s.facts.length > 0 && <ul className="story-facts">{s.facts.map((f, i) => <li key={i}>{f.text}</li>)}</ul>}
          <strong>What changed</strong>
          {s.changes.length ? <ul className="story-changes">{s.changes.map((f, i) => <li key={i}>{f.text}</li>)}</ul>
            : <p className="muted">{s.previous_version == null ? "No earlier version to compare with yet." : "Nothing changed since the previous version."}</p>}
          {expert && <p className="muted">Version {s.version}{s.previous_version != null ? ` compared with v${s.previous_version}` : ""} · worded by {s.wording === "rules" ? "templates" : "a model, numbers checked"} · <code>{s.story_hash.slice(0, 12)}</code></p>}
        </div>
      ) : <ErrorBox error={story.error} onRetry={() => void story.reload()} />}
      <ErrorBox error={act.error} />
      <DashboardDigest dashboard={dashboard} canEdit={canEdit} />
    </Card>
  );
}
