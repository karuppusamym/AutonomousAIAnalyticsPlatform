import { Link } from "react-router-dom";
import type { Insight, RunDetail } from "../api";
import { caveatsOf } from "../lib/board";
import { TERMINAL_RUN } from "../lib/status";
import { to } from "../routes";
import { Markdown } from "./Markdown";
import { StatusBadge } from "./ui";

/** A stale or rejected result must never be promoted by the executive summary. */
export function findingStanding(finding: Insight): "verified" | "review" | "excluded" {
  if (["rejected", "superseded"].includes(finding.status.toLowerCase())) return "excluded";
  const badge = finding.verification_state?.badge;
  if (badge === "superseded") return "excluded";
  if (badge) return badge === "verified" && finding.verified ? "verified" : "review";
  return finding.verified ? "verified" : "review";
}

export function InvestigationOverview({ run, onExplore, onPlan, onApprovals, onContext }: {
  run: RunDetail; onExplore: () => void; onPlan: () => void; onApprovals: () => void; onContext?: () => void;
}) {
  const verified = run.insights.filter((i) => findingStanding(i) === "verified");
  const review = run.insights.filter((i) => findingStanding(i) === "review");
  const pending = run.approvals.filter((a) => a.status === "pending");
  const completed = run.tasks.filter((t) => t.status === "COMPLETED").length;
  const failed = run.tasks.filter((t) => t.status === "FAILED").length;
  const terminal = TERMINAL_RUN.has(run.status);
  const findings = [...verified, ...review].slice(0, 3);
  const caveats = [...new Set([...verified, ...review].flatMap(caveatsOf))];
  const stopped = ["FAILED", "CANCELLED", "REJECTED"].includes(run.status);
  const meaningWaiting = run.tasks.some(t => t.status === "WAITING_USER" && Array.isArray(t.output?.meaning_questions) && t.output.meaning_questions.length > 0);
  const next = stopped
    ? { title: "Review where the investigation stopped", text: "Results may be incomplete. Inspect the steps and any errors before using the findings.", action: "Inspect steps", click: onPlan }
    : meaningWaiting
      ? { title: "A business definition needs review", text: "Context resolution could not settle a material meaning. Review the remaining questions before testing continues.", action: "Review context", click: onContext ?? onPlan }
    : pending.length
      ? { title: "A decision is waiting", text: "Review the proposed action and its evidence. Nothing is approved by viewing this summary.", action: "Review approvals", click: onApprovals }
      : run.status === "PAUSED"
        ? { title: "Ready when you are", text: "Inspect completed work, then use Resume above to continue the investigation.", action: "Review steps", click: onPlan }
        : !terminal
          ? { title: "Investigation in progress", text: "Findings and their verification can change as the remaining steps finish.", action: "Follow progress", click: onPlan }
          : review.length
            ? { title: "Check the remaining evidence", text: "Some findings need review or renewed verification. Open their evidence before relying on them.", action: "Explore findings", click: onExplore }
            : verified.length
              ? { title: "Review the findings with your team", text: "Open the supporting evidence and limitations before deciding what to do next.", action: "Explore findings", click: onExplore }
              : { title: "No verified conclusion yet", text: "Review the tested explanations and data limitations before changing the question or starting another investigation.", action: "Review investigation", click: onExplore };

  return (
    <section className="investigation-overview" aria-label="Investigation overview">
      <div className="investigation-overview-head">
        <div><p className="investigation-eyebrow">INVESTIGATION BRIEF</p><h2>The answer, with the evidence</h2>
          <p className="muted">What we know, what needs checking, and your next step.</p></div>
        <StatusBadge status={run.status} />
      </div>
      <dl className="investigation-metrics">
        <div><dt>Verified findings</dt><dd>{verified.length}</dd></div>
        <div><dt>Findings to check</dt><dd>{review.length}</dd></div>
        <div><dt>Steps completed</dt><dd>{completed}<span> / {run.tasks.length}</span></dd></div>
        <div><dt>Decisions waiting</dt><dd>{pending.length}</dd></div>
      </dl>
      <div className="investigation-brief-grid">
        <div className="investigation-brief-main">
          <h3>What we found</h3>
          {!findings.length && <p className="muted">{terminal ? "No current findings are available for review." : "Findings will appear here as the investigation tests possible explanations."}</p>}
          {findings.length > 0 && <ul className="investigation-findings" aria-label="Findings at a glance">
            {findings.map((i) => <li key={i.id}>
              <div className="investigation-finding-title"><strong>{i.title}</strong>
                <StatusBadge status={findingStanding(i) === "verified" ? "verified" : "pending"}
                  label={findingStanding(i) === "verified" ? "verified" : "needs checking"} /></div>
              <p>{i.finding}</p>
              <Link className="investigation-evidence-link" to={to.findings(run.workspace_id, i.id)} aria-label={`Open evidence for ${i.code}`}>
                Open evidence <span aria-hidden="true">↗</span>
              </Link>
            </li>)}
          </ul>}
          {run.summary?.summary_markdown && <details className="investigation-narrative"><summary>Recorded run narrative</summary>
            <p className="small muted">Written during this run. Use the current verification badges above when assessing each finding.</p>
            <Markdown text={run.summary.summary_markdown} /></details>}
          {run.insights.length > 3 && <button type="button" className="btn btn-sm btn-ghost" onClick={onExplore}>Explore all findings and hypotheses</button>}
        </div>
        <aside className="investigation-next" aria-label="Suggested next step">
          <p className="investigation-eyebrow">NEXT STEP</p><h3>{next.title}</h3><p>{next.text}</p>
          <button type="button" className="btn btn-primary btn-sm" onClick={next.click}>{next.action}</button>
          <div className="investigation-progress">
            <label htmlFor="investigation-progress">Execution progress</label>
            <progress id="investigation-progress" max={Math.max(run.tasks.length, 1)} value={completed} />
            <p className="small muted">{run.tasks.length ? `${completed} of ${run.tasks.length} steps completed` : "Waiting for the investigation plan"}{failed > 0 ? ` · ${failed} failed` : ""}. Progress does not measure confidence.</p>
          </div>
        </aside>
      </div>
      {caveats.length > 0 && <details className="investigation-limitations"><summary>Limitations to keep in mind ({caveats.length})</summary>
        <ul>{caveats.map((c) => <li key={c}>{c}</li>)}</ul></details>}
      <p className="small muted investigation-disclaimer">Verification describes the recorded checks. An association alone does not establish a cause.</p>
    </section>
  );
}
