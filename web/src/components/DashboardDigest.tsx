import { useId, useState } from "react";
import { Link } from "react-router-dom";
import { api, type BuilderDashboard, type Schedule } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { describeCron, digestDashboards, withDigest } from "../lib/schedules";
import { to } from "../routes";
import { ErrorBox, Notice } from "./ui";

function deliveryText(s: Schedule): string {
  const n = s.config?.deliver_to?.length ?? 0;
  return n ? `sent to its ${n} delivery destination${n === 1 ? "" : "s"} (each sends only while its approval stands)`
    : "saved under Reports; it has no delivery destination, so nothing is sent";
}

/**
 * PILOT-FIX-07: "Send a summary of this dashboard" with a schedule. Each successful run of the schedule saves the
 * dashboard's summary under Reports and queues it for the schedule's own delivery destinations; the server checks
 * each destination's approval immediately before sending. This only edits the schedule's `digest_dashboards`.
 */
export function DashboardDigest({ dashboard, canEdit }: { dashboard: BuilderDashboard; canEdit: boolean }) {
  const id = useId();
  const ws = dashboard.workspace_id;
  const schedules = useAsync(() => api.listSchedules(ws), [ws]);
  const act = useAction();
  const [choice, setChoice] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const list = Array.isArray(schedules.data) ? schedules.data : [];
  const sending = list.filter((s) => digestDashboards(s.config).includes(dashboard.id));
  const others = list.filter((s) => !digestDashboards(s.config).includes(dashboard.id));

  const change = async (s: Schedule, on: boolean) => {
    setNotice(null);
    const saved = await act.run(async () => {
      // the whole config is replaced: start from the schedule as it is now, not as this page loaded it
      const fresh = (await api.listSchedules(ws)).find((x) => x.id === s.id) ?? s;
      return api.updateSchedule(s.id, { config: withDigest(fresh.config, dashboard.id, on) });
    });
    if (!saved) return;
    setChoice("");
    setNotice(on ? `Added. Each time “${saved.name}” runs, a summary of this dashboard is ${deliveryText(saved)}.`
      : `“${saved.name}” no longer sends a summary of this dashboard.`);
    void schedules.reload();
  };

  return (
    <section className="stack small" aria-labelledby={`${id}-h`}>
      <h3 id={`${id}-h`}>Send a summary of this dashboard</h3>
      <ErrorBox error={schedules.error} onRetry={() => void schedules.reload()} />
      {sending.length > 0 && (
        <ul className="plain-list">
          {sending.map((s) => (
            <li key={s.id}>
              With <Link to={to.schedules(ws, s.id)}>{s.name}</Link> ({describeCron(s.cron)}): {deliveryText(s)}.
              {canEdit && <> <button type="button" className="btn btn-sm btn-ghost" disabled={act.busy}
                aria-label={`Stop sending with ${s.name}`} onClick={() => void change(s, false)}>Stop sending</button></>}
            </li>
          ))}
        </ul>
      )}
      {schedules.data && list.length === 0 && (
        <p className="muted">No schedules yet. <Link to={to.schedules(ws)}>Create a schedule</Link>, then choose it here.</p>
      )}
      {canEdit && others.length > 0 && (
        <div className="chip-row">
          <label htmlFor={`${id}-sch`}>Schedule</label>
          <select id={`${id}-sch`} value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">Choose a schedule…</option>
            {others.map((s) => <option key={s.id} value={s.id}>{s.name} ({describeCron(s.cron)})</option>)}
          </select>
          <button type="button" className="btn btn-sm" disabled={act.busy || !choice}
            onClick={() => { const s = others.find((x) => x.id === choice); if (s) void change(s, true); }}>Add to schedule</button>
        </div>
      )}
      {!canEdit && sending.length === 0 && schedules.data && list.length > 0 && <p className="muted">No schedule sends this summary. An editor can add one.</p>}
      <ErrorBox error={act.error} />
      {notice && <Notice tone="success">{notice}</Notice>}
    </section>
  );
}
