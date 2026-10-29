import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type GroupingChange } from "../api";
import { fmtDate, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { ErrorBox, Loading, TechnicalDetails } from "./ui";

export const GROUPING_ACTION = "dashboard.grouping_update";

/**
 * BI-VIEWS-02: dashboards published before the `Workspace / Topic` naming keep older titles in the BI tool. This
 * lists them with the title they would get and asks for an approval to rename the chosen ones; the rename happens
 * only after a person approves, and only if nobody changed those titles in the BI tool since.
 */
export function DashboardGrouping({ wsId, role }: { wsId: string; role?: string | null }) {
  const plan = useAsync(() => api.dashboardGrouping(wsId), [wsId]);
  const requests = useAsync(() => api.listApprovals(wsId), [wsId]);
  const act = useAction();
  const [unchosen, setUnchosen] = useState<number[]>([]);
  const [done, setDone] = useState<GroupingChange[] | null>(null);
  if (plan.error) return <ErrorBox error={plan.error} onRetry={plan.reload} />;
  if (!plan.data) return <Loading />;
  const p = plan.data;
  if (!p.available) {
    return (
      <div className="stack">
        <p className="small muted">The BI tool isn’t connected here, so there are no published dashboards to group.</p>
        {p.reason && <TechnicalDetails><p className="small">{p.reason}</p></TechnicalDetails>}
      </div>
    );
  }
  const canAsk = !role || !["viewer", "approver"].includes(role);
  const open = (requests.data ?? []).filter((a: Approval) => a.action === GROUPING_ACTION && ["pending", "approved"].includes(a.status));
  const toChange = p.dashboards.filter((d) => !d.grouped);
  const chosen = toChange.filter((d) => !unchosen.includes(d.id)).map((d) => d.id);
  const reload = async () => { await plan.reload(); await requests.reload(); };
  const ask = async () => {
    if (await act.run(() => api.requestGrouping(wsId, chosen))) await reload();
  };
  const apply = async (a: Approval) => {
    const r = await act.run(() => api.applyGrouping(wsId, a.id));
    if (r) { setDone(r.renamed); await reload(); }
  };
  return (
    <div className="stack">
      <p className="small muted">
        New dashboards are named “{p.group}{p.separator}Topic”. Dashboards published earlier can be renamed the same way so
        they appear together in the BI tool. Only titles change: charts, data, filters and who can see them stay as they are.
      </p>
      {done && <p className="small" role="status">Renamed {done.length} dashboard{done.length === 1 ? "" : "s"}.</p>}
      {open.map((a) => {
        const changes = ((a.payload?.changes as GroupingChange[] | undefined) ?? []);
        return (
          <div key={a.id} className="stack" aria-label="Rename request">
            <p className="small">
              {a.status === "pending" ? <>Waiting for approval to rename {changes.length} dashboard{changes.length === 1 ? "" : "s"}. <Link to={to.approvals(wsId)}>Open the approval inbox</Link></>
                : <>Approved: rename {changes.length} dashboard{changes.length === 1 ? "" : "s"}. The titles are checked again first; if someone changed one in the BI tool, ask again.</>}
            </p>
            <ul className="small">{changes.map((c) => <li key={c.id}>{c.from} → <strong>{c.to}</strong></li>)}</ul>
            {a.status === "approved" && canAsk && (
              <p><button type="button" className="btn btn-primary btn-sm" disabled={act.busy} onClick={() => void apply(a)}>
                {act.busy ? "Renaming…" : "Rename now"}</button></p>
            )}
            <TechnicalDetails>
              <p className="small">Approval <code>{a.id}</code> · {a.status} · expires {fmtDate(a.expires_at)} · payload hash <code title={a.payload_hash}>{shortHash(a.payload_hash, 16)}</code></p>
            </TechnicalDetails>
          </div>
        );
      })}
      {toChange.length === 0 ? (
        <p className="small">All {p.dashboards.length} published dashboard{p.dashboards.length === 1 ? " is" : "s are"} already grouped under “{p.group}”.</p>
      ) : (
        <>
          <fieldset className="stack">
            <legend className="small">Dashboards to rename</legend>
            {toChange.map((d) => (
              <label key={d.id} className="check">
                <input type="checkbox" checked={chosen.includes(d.id)} disabled={!canAsk}
                  onChange={(e) => setUnchosen(e.target.checked ? unchosen.filter((i) => i !== d.id) : [...unchosen, d.id])} />{" "}
                <span>{d.from} → <strong>{d.to}</strong></span>
                {d.url && <> <a className="small" href={d.url} target="_blank" rel="noreferrer noopener" aria-label={`Open ${d.from} in the BI tool`}>open ↗</a></>}
              </label>
            ))}
          </fieldset>
          {p.dashboards.length > toChange.length && <p className="small muted">{p.dashboards.length - toChange.length} already grouped.</p>}
          {canAsk && (
            <p><button type="button" className="btn btn-sm" disabled={act.busy || !chosen.length} onClick={() => void ask()}>
              {act.busy ? "Asking…" : `Ask for approval to rename ${chosen.length}`}</button></p>
          )}
        </>
      )}
      <ErrorBox error={act.error} />
    </div>
  );
}
