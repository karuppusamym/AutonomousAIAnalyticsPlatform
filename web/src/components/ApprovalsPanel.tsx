import { useState } from "react";
import { api, type Approval } from "../api";
import { diffJson, outline, preview } from "../lib/diff";
import { fmtDate, fmtPct, shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { EmptyState, ErrorBox, KeyValue, StatusBadge, Tag, TechnicalDetails } from "./ui";

export const ACTION_LABEL: Record<string, string> = {
  publish_dashboard: "Publish dashboards",
  "dashboard.collection_publish": "Publish chosen dashboards on their own",
  "dashboard.grouping_update": "Rename published dashboards under the workspace name",
  execute_plan: "Run an investigation plan",
  mcp_tool_call: "Call an external MCP tool",
  elt_build: "Build with dbt (write to a target schema)",
  "semantic_metric.approve": "Approve a KPI definition",
  "ml.promote": "Promote a model version to champion",
  "ml.rollback": "Roll a model back to its previous champion",
  "ml.score": "Score data with the champion model (writes a managed table)",
  pipeline_materialize: "Materialize a pipeline's output (writes a managed table)",
  "step.pin_tile": "Pin a verified step to a dashboard tile",
  "step.pin_schedule": "Pin a verified step to a schedule",
};

function GovernanceReview({ evidence }: { evidence: Record<string, unknown> }) {
  const review = evidence.governance_review as { ok?: boolean; problems?: string[] } | undefined;
  const policy = evidence.policy as { decision?: string; reasons?: string[]; risk_tier?: string; effective_autonomy?: number } | undefined;
  const jev = evidence.jev_consequential as number | null | undefined;
  return (
    <div className="evidence">
      <h3>Automated review</h3>
      {!review && !policy && !Object.hasOwn(evidence, "problem") && <p className="muted small">No automated review result was attached to this proposal. Approval still runs the server validation checks.</p>}
      {Object.hasOwn(evidence, "problem") && <p className="small">Proposal checks: <StatusBadge status={evidence.problem ? "failed" : "ok"} label={evidence.problem ? "needs attention" : "no problem recorded"} />{evidence.problem ? ` ${String(evidence.problem)}` : ""}</p>}
      {Array.isArray(evidence.conflicts) && evidence.conflicts.length > 0 && <p className="warn-text small">{evidence.conflicts.length} conflicting definition(s). Review them in Data → Metrics before approving.</p>}
      {typeof evidence.proposed_via === "string" && <p className="small muted">Proposed by {evidence.proposed_via.replace("agent:", "the ").replace(/_/g, " ")}{evidence.proposed_via.startsWith("agent:") ? " agent" : ""}.</p>}
      {review && (
        <p className="small">
          Governance review: <StatusBadge status={review.ok ? "ok" : "failed"} label={review.ok ? "passed" : "failed"} />
          {review.problems?.length ? <span className="warn-text"> {review.problems.join("; ")}</span> : null}
        </p>
      )}
      {policy && (
        <p className="small">
          Policy decision: <StatusBadge status={policy.decision} />{" "}
          {policy.reasons?.length ? <span className="muted">{policy.reasons.join(", ")}</span> : null}
        </p>
      )}
      <TechnicalDetails value={evidence} label="Technical details">
        {jev !== undefined && jev !== null && (
          <p className="small">JEV consequential-action probability: <strong>{fmtPct(jev)}</strong> <span className="muted">(escalates risk only; never grants)</span></p>
        )}
      </TechnicalDetails>
    </div>
  );
}

/**
 * What the approver signs: the payload against the last approved proposal for the same action and
 * destination, path by path. A first proposal lists its contents instead.
 */
export function PayloadDiff({ approval, previous }: { approval: Approval; previous?: Approval | null }) {
  if (!approval.payload) return <p className="muted small">The content is not included in this view; see Technical details.</p>;
  if (!previous?.payload) {
    const items = outline(approval.payload);
    return (
      <div className="payload-diff">
        <p className="small">Nothing was approved before for this action and destination. The proposal contains:</p>
        {items.length ? <ul className="small">{items.map((i) => <li key={i.key}><code>{i.key}</code> {i.summary}</li>)}</ul> : <p className="muted small">An empty payload.</p>}
      </div>
    );
  }
  const changes = diffJson(previous.payload, approval.payload);
  return (
    <div className="payload-diff">
      <p className="small">
        Compared with the last approved proposal ({fmtDate(previous.created_at)}):{" "}
        {changes.length === 0 ? <strong>identical content</strong> : <strong>{changes.length} change{changes.length > 1 ? "s" : ""}</strong>}
      </p>
      {changes.length > 0 && (
        <div className="table-wrap">
          <table className="table table-compact diff-table">
            <caption className="sr-only">Payload changes</caption>
            <thead><tr><th scope="col">Path</th><th scope="col">Change</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path} className={`diff-${c.kind}`}>
                  <td><code>{c.path}</code></td>
                  <td><Tag tone={c.kind === "added" ? "success" : c.kind === "removed" ? "danger" : "warning"}>{c.kind}</Tag></td>
                  <td className="small diff-before">{preview(c.before)}</td>
                  <td className="small diff-after">{preview(c.after)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function ApprovalCard({ approval, onDecided, previous, currentPolicyVersion }: {
  approval: Approval; onDecided: (a: Approval) => void; previous?: Approval | null; currentPolicyVersion?: number | null;
}) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const pending = approval.status === "pending";
  const expired = new Date(approval.expires_at).getTime() < Date.now();
  const policyMoved = pending && typeof currentPolicyVersion === "number" && currentPolicyVersion !== approval.policy_version;
  const decide = async (approve: boolean) => {
    const r = await act.run(() => (approve ? api.approve(approval.id, reason) : api.reject(approval.id, reason)));
    if (r) onDecided(r);
  };
  return (
    <article className={`approval ${pending ? "approval-pending" : ""}`}>
      <header className="approval-head">
        <h2 className="card-title">{ACTION_LABEL[approval.action] ?? approval.action}</h2>
        <StatusBadge status={approval.status} />
        <Tag tone={approval.risk_tier === "high" ? "danger" : approval.risk_tier === "medium" ? "warning" : "neutral"}>risk: {approval.risk_tier}</Tag>
      </header>
      <KeyValue items={[
        ["Destination", approval.destination ?? "—"],
        ["Policy version", <span key="v">v{approval.policy_version}{typeof currentPolicyVersion === "number" && <span className="muted"> (current v{currentPolicyVersion})</span>}</span>],
        ["Affected", approval.affected_assets.join(", ") || "—"],
        ["Expires", <span key="e" className={expired && pending ? "warn-text" : undefined}>{fmtDate(approval.expires_at)}{expired && pending ? " (expired)" : ""}</span>],
        ...(approval.decided_at ? [["Decided", `${fmtDate(approval.decided_at)}${approval.reason ? ` — ${approval.reason}` : ""}`] as [string, string]] : []),
      ]} />
      {policyMoved && (
        <p className="small warn-text" role="status">The workspace policy changed since this was requested; execution re-verifies against the current policy and will refuse a mismatch.</p>
      )}
      {approval.action === "semantic_metric.approve" && !!approval.payload?.definition && <div className="review-proposal">
        <h3>Metric definition</h3>
        <p><strong>{String(approval.payload.metric ?? "Metric").replace(/_/g, " ")}</strong></p>
        <TechnicalDetails value={approval.payload.definition} label="Expression, dataset and dimensions" />
        {typeof approval.evidence?.expression === "string" && <pre className="json">{approval.evidence.expression}</pre>}
      </div>}
      {/* What is being approved is the decision itself: open while it waits, folded once decided. */}
      <details className="proposal-diff" open={pending}><summary>Proposed content and changes</summary><PayloadDiff approval={approval} previous={previous} /></details>
      <GovernanceReview evidence={approval.evidence ?? {}} />
      {pending && (
        <div className="approval-actions">
          <label className="sr-only" htmlFor={`reason-${approval.id}`}>Reason</label>
          <input id={`reason-${approval.id}`} placeholder="Reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
          <button type="button" className="btn btn-success btn-sm" disabled={act.busy || expired || policyMoved} onClick={() => decide(true)}>Approve</button>
          <button type="button" className="btn btn-danger btn-sm" disabled={act.busy} onClick={() => decide(false)}>Reject</button>
        </div>
      )}
      <p className="muted small">The approval covers exactly this content and policy version; any change after approval invalidates it.</p>
      <TechnicalDetails>
        <p className="small">Payload hash <code title={approval.payload_hash}>{shortHash(approval.payload_hash, 16)}</code> · plan hash{" "}
          <code title={approval.plan_hash ?? ""}>{shortHash(approval.plan_hash, 16)}</code></p>
      </TechnicalDetails>
      <ErrorBox error={act.error} />
    </article>
  );
}

/** The last approved (or executed) proposal of the same action and destination before this one. */
export function previousApproved(a: Approval, all: Approval[]): Approval | null {
  const prior = all.filter((x) => x.id !== a.id && x.action === a.action && x.destination === a.destination
    && (x.status === "approved" || x.status === "executed") && x.created_at < a.created_at);
  prior.sort((x, y) => y.created_at.localeCompare(x.created_at));
  return prior[0] ?? null;
}

export function ApprovalsPanel({ approvals, onDecided }: { approvals: Approval[]; onDecided: (a: Approval) => void }) {
  if (!approvals.length) return <EmptyState title="No approvals for this investigation" />;
  const sorted = [...approvals].sort((a, b) => (a.status === "pending" ? -1 : 0) - (b.status === "pending" ? -1 : 0));
  return <div className="approvals">{sorted.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={onDecided} previous={previousApproved(a, approvals)} />)}</div>;
}
