import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type BiDashboardImport } from "../api";
import { fmtDate, optionLabel } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { needsRole, to } from "../routes";
import { Card, CodeBlock, EmptyState, ErrorBox, Loading, RecordTable, StatusBadge } from "./ui";

/**
 * Existing-dashboard mode (v1 §35, N-2): import a dashboard built in the BI tool, see whether each chart's numbers
 * re-execute to the same values through the governed gateway, what is wrong, and what the platform proposes. Nothing
 * here writes to the BI tool: a write-back proposal becomes an approval decided in the approval inbox.
 */
export function ExistingDashboards({ wsId, role }: { wsId: string; role?: string | null }) {
  const candidates = useAsync(() => api.biDashboardCandidates(wsId), [wsId]);
  const imports = useAsync(() => api.biDashboardImports(wsId), [wsId]);
  const [openId, setOpenId] = useState<string | null>(null);
  const act = useAction();
  const blocked = needsRole(role, "editor", "Importing a dashboard");
  if (blocked) return <p className="small muted">{blocked}</p>;
  const importOne = async (id: string) => {
    const r = await act.run(() => api.importBiDashboard(wsId, id));
    if (r) {
      setOpenId(r.id);
      void imports.reload();
      void candidates.reload();
    }
  };
  return (
    <div className="stack">
      <p className="small muted">
        Import a dashboard someone built in the BI tool. Every chart is re-run through the governed gateway and compared with
        the numbers the dashboard shows; definitions are checked against the approved metrics. Changes are only proposed.
      </p>
      <ErrorBox error={candidates.error ?? act.error} onRetry={candidates.error ? candidates.reload : undefined} />
      {candidates.loading && !candidates.data && <Loading />}
      {candidates.data && candidates.data.length === 0 && <EmptyState title="No dashboards to import">The BI tool has no dashboard this workspace may read.</EmptyState>}
      {candidates.data && candidates.data.length > 0 && (
        <ul className="list compact" aria-label="BI dashboards">
          {candidates.data.map((d) => (
            <li key={String(d.id)} className="list-item">
              <span><strong>{d.title ?? d.slug ?? d.id}</strong> {d.last_import && <StatusBadge status={d.last_import.status} label={`v${d.last_import.version} ${d.last_import.status}`} />}</span>
              <span className="chip-row">
                <a className="small" href={d.url} target="_blank" rel="noreferrer noopener">open ↗</a>
                <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void importOne(String(d.id))}>
                  {d.last_import ? "Import again" : "Import & verify"}</button>
              </span>
            </li>
          ))}
        </ul>
      )}
      {(imports.data ?? []).length > 0 && (
        <label className="field">
          <span>Imported dashboards</span>
          <select value={openId ?? ""} onChange={(e) => setOpenId(e.target.value || null)}>
            <option value="">Select an import</option>
            {(imports.data ?? []).map((i) => <option key={i.id} value={i.id}>{optionLabel(i.title, 60)} · v{i.version} · {i.status} · {fmtDate(i.created_at)}</option>)}
          </select>
        </label>
      )}
      {openId && <ImportDetail wsId={wsId} importId={openId} role={role} />}
    </div>
  );
}

function ImportDetail({ wsId, importId, role }: { wsId: string; importId: string; role?: string | null }) {
  const d = useAsync(() => api.biDashboardImport(wsId, importId), [wsId, importId]);
  const act = useAction();
  const [chosen, setChosen] = useState<string[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const imp: BiDashboardImport = d.data;
  const writeBlocked = needsRole(role, "editor", "Requesting a change to the BI tool");
  const requested = imp.applied.filter((a) => a.status === "requested");
  const request = async () => {
    const r = await act.run(() => api.requestBiDashboardUpdate(wsId, imp.id, chosen));
    if (r) { setMessage(`Approval ${r.approval_id} requested; an approver decides it in the approval inbox.`); setChosen([]); void d.reload(); }
  };
  const apply = async (approvalId: string) => {
    const r = await act.run(() => api.applyBiDashboardUpdate(wsId, imp.id, approvalId));
    if (r) { setMessage(r.status === "applied" ? `Applied: ${(r.changed ?? []).join(", ")}. Import again to verify.` : `Failed: ${r.error}`); void d.reload(); }
  };
  const propose = async (proposalId: string) => {
    const r = await act.run(() => api.proposeBiDashboardMetric(wsId, imp.id, proposalId));
    if (r) setMessage(`Metric ${r.metric} ${r.status === "proposed" ? "proposed to the semantic layer" : "already proposed"}.`);
  };
  return (
    <Card title={<>{imp.title} <span className="muted small">· import v{imp.version}</span></>}
      actions={<>{imp.drift && <StatusBadge status="changed" label="changed since last import" />}<StatusBadge status={imp.status} /></>}>
      {imp.url && <p className="small"><a href={imp.url} target="_blank" rel="noreferrer noopener">Open in {imp.destination} ↗</a></p>}
      <h3>Chart verification</h3>
      <RecordTable records={(imp.verification ?? []).map((v) => ({
        chart: v.name, type: v.viz_type, result: v.status,
        compared: v.comparison?.compared ?? "—", differing: v.comparison?.mismatches.length ?? "—", note: v.reason ?? "",
      }))} />
      <h3>Findings ({imp.findings.length})</h3>
      {imp.findings.length === 0 ? <p className="small muted">Nothing to flag.</p> : (
        <ul className="list compact">{imp.findings.map((f) => (
          <li key={f.id} className="list-item"><span><StatusBadge status={f.severity} label={f.severity} /> {f.message}</span></li>
        ))}</ul>
      )}
      <h3>Proposals</h3>
      {imp.proposals.length === 0 ? <p className="small muted">No changes proposed.</p> : (
        <ul className="list compact">{imp.proposals.map((p) => (
          <li key={p.id} className="list-item">
            <span>
              {p.write_back && <input type="checkbox" aria-label={`Choose ${p.title}`} disabled={!!writeBlocked} checked={chosen.includes(p.id)}
                onChange={(e) => setChosen((c) => e.target.checked ? [...c, p.id] : c.filter((x) => x !== p.id))} />} {p.title}
            </span>
            <span className="chip-row">
              <span className="tag">{p.write_back ? "changes the BI tool" : p.platform ? "semantic proposal" : "advice"}</span>
              {p.platform && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void propose(p.id)}>Propose metric</button>}
            </span>
          </li>
        ))}</ul>
      )}
      {writeBlocked && <p className="small muted">{writeBlocked}</p>}
      <p>
        <button type="button" className="btn btn-primary btn-sm" disabled={!chosen.length || act.busy || !!writeBlocked} onClick={() => void request()}>
          Request approval for {chosen.length || "no"} change{chosen.length === 1 ? "" : "s"}</button>
      </p>
      {requested.length > 0 && (
        <div className="stack">
          <p className="small">Requested changes apply only after approval, and only if the dashboard has not changed since this import.
            {" "}<Link to={to.approvals(wsId)}>Open the approval inbox</Link></p>
          {requested.map((a) => (
            <p key={a.approval_id} className="small"><code>{a.approval_id}</code>{" "}
              <button type="button" className="btn btn-sm" disabled={act.busy || !!writeBlocked} onClick={() => void apply(a.approval_id)}>Apply approved change</button></p>
          ))}
        </div>
      )}
      {message && <p className="small" role="status">{message}</p>}
      <ErrorBox error={act.error} />
      {(imp.verification ?? []).some((v) => v.sql) && (
        <details><summary className="small">Governed SQL per chart</summary>
          {(imp.verification ?? []).filter((v) => v.sql).map((v) => <div key={String(v.chart_id)}><p className="small">{v.name}</p><CodeBlock code={v.sql ?? ""} /></div>)}
        </details>
      )}
    </Card>
  );
}
