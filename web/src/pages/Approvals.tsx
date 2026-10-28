import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type Approval } from "../api";
import { ACTION_LABEL, ApprovalCard, previousApproved } from "../components/ApprovalsPanel";
import { EmptyState, ErrorBox, Loading, PageHeader, Tabs, StatusBadge } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { to } from "../routes";

type Filter = "pending" | "all";

/**
 * Operate → Approvals: the workspace inbox. Each card binds to its payload hash, plan hash and
 * policy version, and shows its payload against the last approved proposal for the same action
 * and destination (so the approver sees the change, not a blob). The whole history is loaded once
 * so the comparison works on the Pending tab too.
 */
export function ApprovalsPage() {
  const { wsId = "" } = useParams();
  const [filter, setFilter] = useState<Filter>("pending");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const list = useAsync(() => api.listApprovals(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const [decided, setDecided] = useState<Record<string, Approval>>({});
  const all = (list.data ?? []).map((a) => (decided[a.id] ? { ...a, ...decided[a.id], payload: a.payload } : a));
  const rows = (filter === "pending" ? all.filter((a) => a.status === "pending") : all)
    .filter((a) => `${ACTION_LABEL[a.action] ?? a.action} ${a.affected_assets.join(" ")} ${a.destination ?? ""}`.toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const open = rows.find((a) => a.id === selected) ?? rows[0];
  return (
    <div className="page">
      <PageHeader title="Approvals"
        subtitle="Review a proposal, inspect its evidence, then decide. Each approval covers exactly the content and policy version shown." />
      <div className="review-guide">
        <div><strong>1 · Agents check</strong><p>Verification and governance agents assess findings and publication proposals during an investigation.</p></div>
        <div><strong>2 · You review</strong><p>The selected proposal shows the automated evidence that was actually recorded. Missing evidence is labelled.</p></div>
        <div><strong>3 · Code enforces</strong><p>A person approves governed actions. An AI recommendation cannot grant permission; content and policy are checked again before execution.</p></div>
      </div>
      <Tabs value={filter} onChange={setFilter} tabs={[{ id: "pending", label: "Pending" }, { id: "all", label: "All" }]} />
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data && rows.length === 0 && !search && (
        <EmptyState title={filter === "pending" ? "Nothing waiting for approval" : "No approvals yet"}
          action={filter === "pending" && all.length > 0
            ? <button type="button" className="btn btn-sm" onClick={() => setFilter("all")}>Show decided approvals</button> : undefined}>
          Publishing, pinning and other changes outside the platform ask for an approval here; see <Link to={to.investigations(wsId)}>Investigations</Link>.
        </EmptyState>
      )}
      {/* With nothing to review and no search to clear, the queue and detail panes would only repeat the empty state. */}
      {(rows.length > 0 || !!search) && <div className="review-layout">
        <aside className="review-queue" aria-label="Approval proposals">
          <label className="field"><span>Find a proposal</span><input type="search" placeholder="Metric, action or destination…" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
          <p className="muted small">{rows.length} proposal{rows.length === 1 ? "" : "s"}</p>
          <ul className="list selectable">{rows.map((a) => <li key={a.id}>
            <button className={`list-button ${open?.id === a.id ? "active" : ""}`} aria-current={open?.id === a.id ? "true" : undefined} onClick={() => setSelected(a.id)}>
              <strong>{(a.affected_assets[0] ?? a.destination ?? ACTION_LABEL[a.action] ?? a.action).replace(/^metric:/, "").replace(/_/g, " ")}</strong>
              <span className="small muted">{ACTION_LABEL[a.action] ?? a.action}</span>
              <span className="chip-row"><StatusBadge status={a.status} /><span className="muted small">{fmtDate(a.created_at)}</span></span>
            </button></li>)}</ul>
        </aside>
        <div className="review-detail">
          {open ? <div key={open.id} className="stack">
            <ApprovalCard approval={open} previous={previousApproved(open, all)} currentPolicyVersion={ws.data?.policy_version}
              onDecided={(d) => setDecided((m) => ({ ...m, [d.id]: d }))} />
            {(open.run_id || typeof open.evidence?.run_id === "string") && <Link className="small" to={to.run(wsId, open.run_id ?? String(open.evidence.run_id))}>Open the investigation</Link>}
          </div> : <EmptyState title="No proposal selected">Select a proposal from the queue. Try a broader search if nothing matches.</EmptyState>}
        </div>
      </div>}
    </div>
  );
}
