import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type Artifact } from "../api";
import { fmtDate, shortHash } from "../lib/format";
import { useAction } from "../lib/hooks";
import { to } from "../routes";
import { Card, ErrorBox, KeyValue, StatusBadge, TechnicalDetails } from "./ui";

/**
 * Publishing a dashboard (P4-U05), shown in Outputs when a dashboard is open. Publishing stays
 * proposal-based: the button fetches the investigation's pending, hash-bound publication proposal and
 * sends the person to the approval inbox to decide it; nothing is written from here.
 */
export function PublishCard({ wsId, artifact }: { wsId: string; artifact: Artifact }) {
  const act = useAction();
  const [proposal, setProposal] = useState<Approval | null>(null);
  const published = artifact.status === "published";
  const request = async () => {
    const r = await act.run(() => api.publishDashboard(artifact.id));
    if (r) setProposal(r);
  };
  return (
    <Card title="Publish" actions={<StatusBadge status={artifact.status} />}>
      {published ? (
        <p className="small">Published to {artifact.platform ?? "BI"}{artifact.external_url && <> — <a href={artifact.external_url} target="_blank" rel="noreferrer noopener">open ↗</a></>}.</p>
      ) : (
        <>
          <p className="small muted">
            Publishing writes outside the platform, so it is a proposal that an approver decides in the approval inbox. The proposal covers the investigation’s reviewed bundle, which may include several dashboards and datasets.
            This button opens an existing pending proposal; a new draft needs to be included in that reviewed bundle.
          </p>
          {!proposal && (
            <button type="button" className="btn btn-primary btn-sm" disabled={act.busy} onClick={() => void request()}>
              {act.busy ? "Checking…" : "Request publication"}</button>
          )}
        </>
      )}
      {proposal && (
        <div className="stack">
          <KeyValue items={[
            ["Proposal", <code key="i">{proposal.id}</code>],
            ["Status", <StatusBadge key="s" status={proposal.status} />],
            ["Destination", proposal.destination ?? "—"],
            ["Expires", fmtDate(proposal.expires_at)],
          ]} />
          <TechnicalDetails><p className="small">Payload hash <code title={proposal.payload_hash}>{shortHash(proposal.payload_hash, 16)}</code></p></TechnicalDetails>
          <p><Link className="btn btn-sm" to={to.approvals(wsId)}>Decide in the approvals inbox</Link></p>
        </div>
      )}
      <ErrorBox error={act.error} />
    </Card>
  );
}
