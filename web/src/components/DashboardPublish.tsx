import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type Artifact, type CollectionDashboard, type CollectionRequest } from "../api";
import { fmtDate, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { Card, ErrorBox, KeyValue, Loading, StatusBadge, TechnicalDetails } from "./ui";

const KIND: Record<CollectionDashboard["kind"], string> = {
  executive: "Executive overview", operational: "Operations view", topic: "Topic view", other: "Dashboard",
};

const same = (a: string[], b: string[]) => a.length === b.length && a.every((k, i) => k === b[i]);

function whereTo(destination: string | null): string {
  if (destination === "preview") return "The BI tool isn’t available here, so this publishes to the in-app preview.";
  if (destination === "superset") return "Publishes to Superset.";
  return "No publishing destination is available for this workspace.";
}

/**
 * Publishing a dashboard (P4-U05), shown in Outputs when a dashboard is open. Publishing writes outside the
 * platform, so it is always a request that an approver decides in the approval inbox. BI-VIEWS-02: a person picks
 * which of the investigation's dashboards to publish (by default the one open); the approval covers exactly that
 * selection, and choosing other dashboards needs its own approval.
 */
export function PublishCard({ wsId, artifact }: { wsId: string; artifact: Artifact }) {
  const published = artifact.status === "published";
  return (
    <Card title="Publish" actions={<StatusBadge status={artifact.status} />}>
      {published && (
        <p className="small">Published to {artifact.platform ?? "BI"}{artifact.external_url && <> — <a href={artifact.external_url} target="_blank" rel="noreferrer noopener">open ↗</a></>}.</p>
      )}
      {!published && <p className="small muted">
        Publishing puts dashboards in the BI tool where others can see them. Someone with approval rights says yes first in
        the approval inbox; nothing is published before that.
      </p>}
      {artifact.run_id && <CollectionPublish wsId={wsId} runId={artifact.run_id} artifact={artifact} />}
      {!published && <WholeInvestigation wsId={wsId} artifact={artifact} />}
    </Card>
  );
}

/** Choose some of the investigation's dashboards and publish only those, under their own approval. */
function CollectionPublish({ wsId, runId, artifact }: { wsId: string; runId: string; artifact: Artifact }) {
  const data = useAsync(() => api.runCollections(runId), [runId]);
  const act = useAction();
  const [chosen, setChosen] = useState<string[] | null>(null);
  const [urls, setUrls] = useState<Record<string, string>>({});
  if (data.error) return <ErrorBox error={data.error} onRetry={data.reload} />;
  if (!data.data) return <Loading label="Loading this investigation’s dashboards…" />;
  const { dashboards, requests, destination } = data.data;
  if (!dashboards.length) return null;
  const own = dashboards.find((d) => d.artifact_id === artifact.id)?.key;
  const selected = [...(chosen ?? (own ? [own] : []))].sort();
  const titleOf = (key: string) => dashboards.find((d) => d.key === key)?.title ?? key;
  const current = requests.find((r) => same(r.collection, selected) && ["pending", "approved", "executed"].includes(r.status));
  const toggle = (key: string, on: boolean) => setChosen(on ? [...selected, key] : selected.filter((k) => k !== key));
  const request = async () => {
    if (await act.run(() => api.requestCollection(runId, selected))) await data.reload();
  };
  const publish = async (r: CollectionRequest) => {
    const done = await act.run(() => api.publishCollection(r.approval_id));
    if (done) { setUrls(done.urls); await data.reload(); }
  };
  return (
    <section className="stack" aria-label="Publish chosen dashboards">
      <h3>Publish on its own</h3>
      <fieldset className="stack">
        <legend className="small">Choose the dashboards to publish together</legend>
        {dashboards.map((d) => (
          <label key={d.key} className="check">
            <input type="checkbox" checked={selected.includes(d.key)} onChange={(e) => toggle(d.key, e.target.checked)} />{" "}
            <span>{d.title}</span>{" "}
            <span className="muted small">{KIND[d.kind]} · {d.charts} chart{d.charts === 1 ? "" : "s"}{d.status === "published" ? " · published" : ""}</span>
          </label>
        ))}
      </fieldset>
      <p className="small muted">{whereTo(destination)} Changing the selection needs a new approval.</p>
      {!current && (
        <button type="button" className="btn btn-primary btn-sm" disabled={act.busy || !selected.length || !destination}
          onClick={() => void request()}>
          {act.busy ? "Asking…" : `Ask for approval to publish ${selected.length} dashboard${selected.length === 1 ? "" : "s"}`}
        </button>
      )}
      {current && <RequestState wsId={wsId} request={current} busy={act.busy} onPublish={() => void publish(current)} />}
      {current?.status === "executed" && (
        <ul className="small">
          {current.collection.map((key) => {
            const url = urls[key] ?? dashboards.find((d) => d.key === key)?.external_url;
            return <li key={key}>{titleOf(key)}{url && <> — <a href={url} target="_blank" rel="noreferrer noopener">open ↗</a></>}</li>;
          })}
        </ul>
      )}
      <ErrorBox error={act.error} />
      {requests.filter((r) => r !== current).length > 0 && (
        <details>
          <summary className="small">Earlier publishing requests ({requests.filter((r) => r !== current).length})</summary>
          <ul className="list compact">
            {requests.filter((r) => r !== current).map((r) => (
              <li key={r.approval_id} className="list-item">
                <span className="small">{r.collection.map(titleOf).join(", ")}</span>
                <span className="chip-row"><StatusBadge status={r.status} /><span className="muted small">{fmtDate(r.created_at)}</span></span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

function RequestState({ wsId, request, busy, onPublish }: { wsId: string; request: CollectionRequest; busy: boolean; onPublish: () => void }) {
  const failed = request.publication && request.publication.status !== "succeeded" ? request.publication : null;
  return (
    <div className="stack">
      {request.status === "pending" && <p className="small">Waiting for approval. <Link to={to.approvals(wsId)}>Open the approval inbox</Link></p>}
      {request.status === "approved" && (
        <>
          <p className="small">Approved. Publishing checks once more that nothing changed since the approval.</p>
          {failed && <p className="small warn-text">The last try did not finish ({failed.error ?? failed.status}). You can try again.</p>}
          <p><button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={onPublish}>
            {busy ? "Publishing…" : "Publish now"}</button></p>
        </>
      )}
      {request.status === "executed" && <p className="small">Published.</p>}
      <TechnicalDetails>
        <p className="small">Approval <code>{request.approval_id}</code> · {request.status} · expires {fmtDate(request.expires_at)} ·
          payload hash <code title={request.payload_hash}>{shortHash(request.payload_hash, 16)}</code></p>
      </TechnicalDetails>
    </div>
  );
}

/** The investigation's own publication proposal, which covers every dashboard it produced. */
function WholeInvestigation({ wsId, artifact }: { wsId: string; artifact: Artifact }) {
  const act = useAction();
  const [proposal, setProposal] = useState<Approval | null>(null);
  const request = async () => {
    const r = await act.run(() => api.publishDashboard(artifact.id));
    if (r) setProposal(r);
  };
  return (
    <section className="stack" aria-label="Publish everything from this investigation">
      <h3>Or publish everything from this investigation</h3>
      <p className="small muted">Opens the investigation’s own publishing request, which covers all of its dashboards together.</p>
      {!proposal && (
        <p><button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void request()}>
          {act.busy ? "Checking…" : "Request publication"}</button></p>
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
    </section>
  );
}
