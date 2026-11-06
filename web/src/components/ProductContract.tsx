/**
 * A data product's contract and access (P2-9): the freshness SLA and quality-rule refs it promises, their state
 * now (breaches first), and consumer access requests. A request is an approval bound to (product, version,
 * requester, role); an approver decides it here and the server applies the role or MCP grant after re-verifying
 * the binding. The requester never approves their own request (the server refuses).
 */
import { useState } from "react";
import { api, type ProductContractCheck } from "../api";
import { fmtDate, fmtPct } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { ErrorBox, Loading, Notice, Tag } from "./ui";

const APPROVERS = new Set(["approver", "owner"]);

function checkTone(c: ProductContractCheck): string {
  if (c.state === "ok") return "success";
  if (c.state === "unknown") return "neutral";
  return c.severity === "critical" ? "danger" : "warning";
}

export function ProductContract({ ws, toolId }: { ws: string; toolId: string }) {
  const workspace = useAsync(() => api.getWorkspace(ws), [ws]);
  const status = useAsync(() => api.productContract(ws, toolId), [ws, toolId]);
  const requests = useAsync(() => api.productAccessRequests(ws, toolId), [ws, toolId]);
  const ask = useAction();
  const decide = useAction();
  const [reason, setReason] = useState("");
  const [sent, setSent] = useState<string | null>(null);
  const s = status.data;
  const canDecide = APPROVERS.has(workspace.data?.role ?? "");

  return (
    <section className="stack" aria-label="Contract and access">
      <h3>Contract</h3>
      <ErrorBox error={status.error} onRetry={status.reload} />
      {!s && !status.error && <Loading label="Checking the contract…" />}
      {s && !s.has_contract && <p className="muted small">This product promises no freshness or quality contract.</p>}
      {s?.has_contract && (
        <>
          <div className="chip-row" aria-label="Contract state">
            {s.ok ? <Tag tone="success">contract met</Tag> : <Tag tone="danger">{s.breaches.length} breach{s.breaches.length === 1 ? "" : "es"}</Tag>}
            {s.unknown > 0 && <Tag>{s.unknown} not yet checked</Tag>}
            {s.contract?.freshness && <Tag tone="info">fresh within {s.contract.freshness.max_age_hours} h</Tag>}
            {s.contract?.min_pass_rate != null && <Tag tone="info">pass rate ≥ {fmtPct(s.contract.min_pass_rate)}</Tag>}
          </div>
          <ul className="plain-list small">
            {[...s.breaches, ...s.checks.filter((c) => c.state !== "breach")].map((c) => (
              <li key={c.key}><Tag tone={checkTone(c)}>{c.state === "breach" ? c.severity : c.state}</Tag> {c.message}</li>
            ))}
          </ul>
          {s.checked_at && <p className="muted small">Checked {fmtDate(s.checked_at)} from catalog load times and the rules' latest runs.</p>}
        </>
      )}

      <h3>Access</h3>
      <div className="form-inline">
        <label className="inline-field small">Why you need it
          <input value={reason} maxLength={1000} onChange={(e) => setReason(e.target.value)} aria-label="Reason for access" />
        </label>
        <button type="button" className="btn btn-sm" disabled={ask.busy}
          onClick={() => void ask.run(async () => {
            const r = await api.requestProductAccess(ws, toolId, { reason: reason || undefined });
            setSent(`Request ${r.approval_id} sent (${r.risk_tier} risk): an approver decides it.`);
            await requests.reload();
          })}>Request access</button>
      </div>
      {sent && <Notice tone="success">{sent}</Notice>}
      <ErrorBox error={ask.error ?? decide.error ?? requests.error} />
      {!!requests.data?.length && (
        <div className="table-wrap">
          <table className="table table-compact">
            <caption className="sr-only">Access requests</caption>
            <thead><tr><th>Requester</th><th>Asks for</th><th>Version</th><th>Risk</th><th>Status</th><th>Reason</th>
              {canDecide && <th><span className="sr-only">Decide</span></th>}</tr></thead>
            <tbody>{requests.data.map((r) => (
              <tr key={r.approval_id}>
                <td className="small"><code>{r.requester}</code></td>
                <td className="small">{r.grant ? `MCP grant for ${r.grant.client_id}` : `role ${r.role}`}</td>
                <td className="small"><code>{r.version}</code></td>
                <td className="small">{r.risk_tier}</td>
                <td><Tag tone={r.status === "executed" ? "success" : r.status === "pending" ? "warning" : "neutral"}>{r.status}</Tag></td>
                <td className="small">{r.reason || <span className="muted">—</span>}</td>
                {canDecide && (
                  <td>{r.status === "pending" && (
                    <div className="chip-row">
                      <button type="button" className="btn btn-xs btn-primary" disabled={decide.busy}
                        onClick={() => void decide.run(async () => { await api.decideProductAccess(r.approval_id, true); await requests.reload(); })}>
                        Approve</button>
                      <button type="button" className="btn btn-xs btn-ghost" disabled={decide.busy}
                        onClick={() => void decide.run(async () => { await api.decideProductAccess(r.approval_id, false); await requests.reload(); })}>
                        Reject</button>
                    </div>
                  )}</td>
                )}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}
