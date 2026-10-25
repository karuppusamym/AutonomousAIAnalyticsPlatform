import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type Approval, type Hypothesis, type Insight } from "../api";
import { buildBoard, caveatsOf, columnOf, modelOpinions, relevantApprovals, trustFacts, type ColumnId, type TrustState } from "../lib/board";
import { fmtNumber, fmtP, fmtValue } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { hypothesisIcon, toneFor } from "../lib/status";
import { buildInvestigationTree, type HypNode } from "../lib/tree";
import { to } from "../routes";
import { ConfidenceBar, EmptyState, ErrorBox, Loading, Notice, RecordTable, StateView, StatusBadge, Tag, TechnicalDetails, Value } from "./ui";
import { CitationsPanel } from "./Citations";
import { VerificationBadge, voidCause, WhyNumberButton } from "./WhyNumber";
import { methodLabel, methodsLabel } from "../lib/methods";
import { holdoutBadge, holdoutText, hypothesisStanding, strengthBadge, strengthText, type Badge } from "../lib/standing";

type Filter = ColumnId | "all";

/** Plain badges for a finding's standing (P8-15): "weak evidence", "confirmed on held-out data" / "not confirmed". */
function StandingBadges({ badges }: { badges: (Badge | null)[] }) {
  return <>{badges.filter((b): b is Badge => b !== null).map((b) => (
    <span key={b.label} title={b.title}><Tag tone={b.tone}>{b.label}</Tag></span>
  ))}</>;
}

/**
 * Keep the nodes that pass; a hidden node's visible follow-ups move up to its place, so filtering
 * by status or hiding superseded hypotheses never hides a follow-up that matches.
 */
function visibleNodes(nodes: HypNode[], keep: (h: Hypothesis) => boolean): HypNode[] {
  return nodes.flatMap((n) => {
    const children = visibleNodes(n.children, keep);
    return keep(n.hypothesis) ? [{ ...n, children }] : children;
  });
}

/**
 * The investigation's hypotheses in one view (P4-U03, spec v3 §9 Investigate): status counts that
 * filter, then question -> hypothesis -> follow-ups, each hypothesis carrying its test result and
 * finding cards. A finding card opens "Why trust this" and records an accept or reject signal for
 * calibration. Raw JSON appears only under "Technical details".
 */
export function InvestigationBoard({ wsId, runId, objective, hypotheses, insights, approvals, readOnly, onChanged }: {
  wsId: string; runId: string; objective: string; hypotheses: Hypothesis[]; insights: Insight[]; approvals: Approval[]; readOnly: boolean;
  onChanged: () => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [showSuperseded, setShowSuperseded] = useState(false);
  const [trustFor, setTrustFor] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  if (hypotheses.length === 0 && insights.length === 0) {
    return <EmptyState title="No hypotheses yet">The investigator proposes hypotheses after context, metadata and profiling.</EmptyState>;
  }
  const board = buildBoard(hypotheses, insights);
  const counted = board.columns.filter((c) => c.items.length > 0);
  const active = counted.reduce((n, c) => n + c.items.length, 0);
  const keep = (h: Hypothesis) => {
    const col = columnOf(h.status);
    if (col === null) return showSuperseded && filter === "all";
    return filter === "all" || col === filter;
  };
  const questions = buildInvestigationTree(objective, hypotheses, insights)
    .map((q) => ({ question: q.question, nodes: visibleNodes(q.hypotheses, keep) }))
    .filter((q) => q.nodes.length > 0);
  // A heading earns its place when it groups hypotheses; one question per hypothesis reads better as a
  // flat list with the question inside each row, and a lone question that is the objective repeats the title.
  const titled = questions.some((q) => q.nodes.length > 1) && !(questions.length === 1 && questions[0].question === objective);
  const hypById = new Map(hypotheses.map((h) => [h.id, h]));
  const open = insights.find((i) => i.id === trustFor) ?? null;
  const openTrust = (id: string, el: HTMLElement) => {
    opener.current = el;
    setTrustFor(id);
  };
  const cardProps = { wsId, runId, readOnly, onChanged, onTrust: openTrust };

  return (
    <div className="board-wrap">
      <div className="hyp-toolbar">
        <div className="seg" role="radiogroup" aria-label="Show hypotheses">
          <button type="button" role="radio" aria-checked={filter === "all"} className={`seg-btn ${filter === "all" ? "active" : ""}`}
            onClick={() => setFilter("all")}>All ({active})</button>
          {counted.map((c) => (
            <button key={c.id} type="button" role="radio" aria-checked={filter === c.id} title={c.hint}
              className={`seg-btn ${filter === c.id ? "active" : ""}`} onClick={() => setFilter(c.id)}>
              <span className={`tone-${toneFor(c.id)}`} aria-hidden="true">{hypothesisIcon(c.id)}</span> {c.label} ({c.items.length})
            </button>
          ))}
        </div>
        {board.superseded.length > 0 && filter === "all" && (
          <button type="button" className="btn btn-xs btn-ghost" aria-expanded={showSuperseded} onClick={() => setShowSuperseded((v) => !v)}>
            {showSuperseded ? "Hide" : "Show"} superseded hypotheses ({board.superseded.length})
          </button>
        )}
      </div>
      {questions.length === 0 && <p className="muted small">No hypotheses with this status.</p>}
      {questions.map((q) => (
        <section key={q.question} className="hyp-question" aria-label={titled ? `Question: ${q.question}` : "Hypotheses"}>
          {titled && <h3 className="tree-question-head"><span className="tree-kind">Question</span> {q.question}</h3>}
          <ul className="hyp-rows">
            {q.nodes.map((n) => (
              <HypothesisRow key={n.hypothesis.id} node={n} question={titled || q.question === objective ? undefined : q.question} {...cardProps} />
            ))}
          </ul>
        </section>
      ))}
      {board.orphans.length > 0 && filter === "all" && (
        <section className="board-orphans" aria-labelledby="orphans-h">
          <h3 id="orphans-h">Findings without a hypothesis</h3>
          <ul className="board-cards board-cards-row">
            {board.orphans.map((i) => <li key={i.id}><FindingCard insight={i} {...cardProps} /></li>)}
          </ul>
        </section>
      )}
      {open && (
        <TrustDrawer insight={open} hypothesis={open.hypothesis_id ? hypById.get(open.hypothesis_id) ?? null : null}
          approvals={approvals} wsId={wsId}
          onClose={() => {
            setTrustFor(null);
            opener.current?.focus();
          }} />
      )}
    </div>
  );
}

type CardProps = { wsId: string; runId: string; readOnly: boolean; onChanged: () => void; onTrust: (id: string, el: HTMLElement) => void };

/** One line per hypothesis that reads without opening it; open it for the evidence, findings and follow-ups. */
function HypothesisRow({ node, question, ...rest }: { node: HypNode; question?: string } & CardProps) {
  const h = node.hypothesis;
  const r = h.result;
  const col = columnOf(h.status);
  const highlights = r?.highlights ? Object.entries(r.highlights).filter(([, v]) => v !== null && v !== undefined && typeof v !== "object") : [];
  const standing = hypothesisStanding(h, node.findings);
  const supported = h.status === "supported" || node.findings.length > 0;
  const strengthLine = supported ? strengthText(standing.strength) : null;
  const holdoutLine = holdoutText(standing.holdout);
  return (
    <li className={`hyp-row hyp-row-${col ?? "superseded"}`}>
      <article aria-label={`Hypothesis ${h.code}`}>
        {/* Rejected and superseded rows start closed: what was supported, or is still open, comes first. */}
        <details open={col !== null && col !== "rejected"}>
          <summary>
            <span className={`hyp-icon tone-${toneFor(h.status)}`} aria-hidden="true">{hypothesisIcon(h.status)}</span>
            <strong>{h.code}</strong>
            <span className="hyp-row-statement">{h.statement}</span>
            <span className="hyp-row-tags">
              <Tag tone={h.priority === "high" ? "danger" : h.priority === "medium" ? "warning" : "neutral"}>{h.priority}</Tag>
              <StatusBadge status={h.status} />
              {supported && <StandingBadges badges={[strengthBadge(standing.strength), holdoutBadge(standing.holdout, standing.validation)]} />}
              {node.findings.length > 0 && <span className="muted small">{node.findings.length} finding{node.findings.length > 1 ? "s" : ""}</span>}
            </span>
            <span className="hyp-row-result muted small">
              <span title={r?.test ?? h.methods.join(", ")}>{r?.test ? methodLabel(r.test) : (methodsLabel(h.methods) || "method pending")}</span>
              {r && <> · n <Value value={r.n} format="int" /> · q {r.p_adjusted === null || r.p_adjusted === undefined ? <Value value={null} /> : fmtP(r.p_adjusted)}
                {r.effect_size !== null && r.effect_size !== undefined && <> · <span title={r.effect_label ?? undefined}>{r.effect_label ? methodLabel(r.effect_label) : "effect"}</span> {fmtNumber(r.effect_size, 3)}</>}</>}
            </span>
          </summary>
          <div className="hyp-row-body">
            {question && <p className="small"><span className="tree-kind">Question</span> {question}</p>}
            {h.conclusion && <p className="small">{h.conclusion}</p>}
            {!!h.provenance?.rationale && <p className="small"><strong>Why this test:</strong> {h.provenance.rationale}</p>}
            {!!h.provenance?.mapping_receipts?.length && <details><summary className="small">Definitions used for this test</summary><ul className="list small">{h.provenance.mapping_receipts.map((r,i)=><li key={i}>{r.term||r.id}{r.revision?` · v${r.revision}`:""}: {r.columns.join(", ")}</li>)}</ul></details>}
            {(strengthLine || holdoutLine) && (
              <ul className="small standing" aria-label={`How firm ${h.code} is`}>
                {strengthLine && <li><span className="muted">Strength:</span> {strengthLine}</li>}
                {holdoutLine && <li><span className="muted">Held-out check:</span> {holdoutLine}</li>}
              </ul>
            )}
            {highlights.length > 0 && (
              <ul className="highlights small">
                {highlights.map(([k, v]) => <li key={k}><span className="muted">{k.replace(/_/g, " ")}:</span> {fmtValue(v)}</li>)}
              </ul>
            )}
            {r?.warnings?.length ? <p className="small warn-text">⚠ {r.warnings.join("; ")}</p> : null}
            {r?.groups?.length ? (
              <details className="groups">
                <summary className="small">Groups ({r.groups.length})</summary>
                <RecordTable records={r.groups} maxRows={30} />
              </details>
            ) : null}
            {node.findings.length > 0 && (
              <ul className="finding-list" aria-label={`Findings for ${h.code}`}>
                {node.findings.map((i) => <li key={i.id}><FindingCard insight={i} {...rest} /></li>)}
              </ul>
            )}
            {node.children.length > 0 && (
              <ul className="hyp-rows hyp-children" aria-label={`Follow-ups of ${h.code}`}>
                {node.children.map((c) => <HypothesisRow key={c.hypothesis.id} node={c} {...rest} />)}
              </ul>
            )}
            <p className="muted small">Proposed by {h.origin} · iteration {h.iteration}</p>
          </div>
        </details>
      </article>
    </li>
  );
}

type Signal = { kind: "accepted" | "rejected"; note?: string } | { kind: "refused"; message: string };

function FindingCard({ insight: i, wsId, runId, readOnly, onChanged, onTrust }: { insight: Insight } & CardProps) {
  const act = useAction();
  const [signal, setSignal] = useState<Signal | null>(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const reasonId = useId();
  const rejected = i.status === "rejected";

  const accept = async () => {
    try {
      await api.findingOutcome(i.id, "accept");
      setSignal({ kind: "accepted" });
    } catch (err) {
      if (err instanceof ApiError && (err.status === 400 || err.status === 403 || err.status === 422)) {
        setSignal({ kind: "refused", message: err.message });
      } else {
        act.setError(err instanceof Error ? err.message : String(err));
      }
    }
  };
  const reject = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.feedback(wsId, runId, { text: reason.trim(), kind: "reject_finding", target_type: "insight", target_id: i.id }));
    if (r) {
      setSignal({ kind: "rejected", note: r.replan ? `Replanned to plan v${r.replan.plan_version}.` : undefined });
      setRejecting(false);
      setReason("");
      onChanged();
    }
  };

  return (
    <article className={`finding-card ${i.verified ? "finding-verified" : ""}`} aria-label={`Finding ${i.code}`}>
      <div className="finding-head">
        <span className="tree-kind">Finding</span>
        <Link to={to.findings(wsId, i.id)}><strong>{i.code}</strong> {i.title}</Link>
      </div>
      <div className="chip-row small">
        {i.verification_state?.badge === "void" ? <VerificationBadge state={i.verification_state} showCause={false} />
          : <StatusBadge status={i.status} label={i.verified ? "verified" : i.status.replace(/_/g, " ")} />}
        {i.verified && <StandingBadges badges={[strengthBadge(i.strength), holdoutBadge(i.holdout, i.validation)]} />}
        <span className="muted">n</span> <Value value={i.population_size || null} format="int" />
      </div>
      {voidCause(i.verification_state) && <p className="small void-cause">Why void: {voidCause(i.verification_state)}</p>}
      <p className="small">{i.finding}</p>
      <ConfidenceBar value={i.confidence} />
      <div className="finding-actions">
        <button type="button" className="btn btn-xs" onClick={(e) => onTrust(i.id, e.currentTarget)}>Why trust this</button>
        <WhyNumberButton insightId={i.id} />
        {!readOnly && !rejected && signal?.kind !== "accepted" && (
          <button type="button" className="btn btn-xs btn-success" disabled={act.busy} onClick={() => void accept()}>Accept</button>
        )}
        {!readOnly && !rejected && !rejecting && (
          <button type="button" className="btn btn-xs btn-danger" disabled={act.busy} onClick={() => setRejecting(true)}>Reject</button>
        )}
      </div>
      {rejecting && (
        <form className="form finding-reject" onSubmit={reject}>
          <label htmlFor={reasonId} className="small">Why is {i.code} wrong or not useful?</label>
          <textarea id={reasonId} rows={2} value={reason} required onChange={(e) => setReason(e.target.value)}
            placeholder="The Network group was reorganised in August; the comparison is not like for like." />
          <p className="muted small">Rejecting marks the finding and its hypothesis rejected and replans the run. It is recorded as negative knowledge.</p>
          <div className="form-actions">
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => setRejecting(false)}>Cancel</button>
            <button type="submit" className="btn btn-xs btn-danger" disabled={act.busy || !reason.trim()}>Reject finding</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error} />
      {signal?.kind === "accepted" && <p className="small success-text" role="status">Accepted — signal recorded for calibration.</p>}
      {signal?.kind === "rejected" && <p className="small" role="status">Rejected. {signal.note}</p>}
      {signal?.kind === "refused" && <StateView kind="refused" title="Accept not recorded">{signal.message}</StateView>}
    </article>
  );
}

const TRUST_ICON: Record<TrustState, string> = { pass: "✓", fail: "✗", unknown: "?", info: "•" };
const TRUST_TONE: Record<TrustState, string> = { pass: "success", fail: "danger", unknown: "neutral", info: "info" };

/** "Why trust this": a modal drawer with the finding's verification evidence, check by check. */
function TrustDrawer({ insight: i, hypothesis, approvals, wsId, onClose }: {
  insight: Insight; hypothesis: Hypothesis | null; approvals: Approval[]; wsId: string; onClose: () => void;
}) {
  const detail = useAsync(() => api.getInsight(i.id), [i.id]);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab" || !panelRef.current) return;
      const f = panelRef.current.querySelectorAll<HTMLElement>("a[href], button:not([disabled]), summary, [tabindex]:not([tabindex='-1'])");
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const facts = trustFacts(i, hypothesis, detail.data ?? null);
  const caveats = caveatsOf(i);
  const opinions = modelOpinions(i);
  const gates = relevantApprovals(approvals);
  const deterministicOk = i.verification?.verified ?? i.verified;

  return (
    <div className="drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} ref={panelRef}>
        <header className="drawer-head">
          <h2 id={titleId}>Why trust {i.code}?</h2>
          <button type="button" className="btn btn-sm btn-ghost" ref={closeRef} onClick={onClose}>Close</button>
        </header>
        <div className="drawer-body">
          <p className="small">{i.finding}</p>
          {deterministicOk
            ? <Notice tone="success">Verified: every deterministic check passed. Model opinions below only adjusted confidence.</Notice>
            : <Notice tone="warning">Not verified: at least one deterministic check failed or was not recorded. Confidence is capped.</Notice>}
          <h3>Evidence</h3>
          <ul className="trust-list">
            {facts.map((f) => (
              <li key={f.id} className={`trust-item trust-${f.state}`}>
                <span className={`badge badge-${TRUST_TONE[f.state]}`} role="img" aria-label={f.state === "pass" ? "passed" : f.state === "fail" ? "failed" : "not reported"}>
                  <span aria-hidden="true">{TRUST_ICON[f.state]}</span>
                </span>
                <div>
                  <div><strong>{f.label}</strong>: {f.value}</div>
                  {f.detail && <div className="muted small">{f.detail}</div>}
                </div>
              </li>
            ))}
          </ul>
          {detail.loading && !detail.data && <Loading label="Loading query evidence…" />}
          {detail.error && <p className="muted small">Query evidence unavailable: {detail.error}</p>}
          <h3>Citations</h3>
          <CitationsPanel load={() => api.insightCitations(i.id)} deps={[i.id]} workspaceId={wsId} />
          <h3>Caveats</h3>
          {caveats.length ? <ul className="small">{caveats.map((c) => <li key={c}>{c}</li>)}</ul> : <p className="muted small">No data-quality or population caveats recorded.</p>}
          <h3>Approvals on this investigation</h3>
          {gates.length ? (
            <ul className="small">
              {gates.map((a) => (
                <li key={a.id}><StatusBadge status={a.status} /> {a.action.replace(/_/g, " ")} · policy version {a.policy_version}</li>
              ))}
            </ul>
          ) : <p className="muted small">Nothing from this investigation needed an approval.</p>}
          <p className="small"><Link to={to.findings(wsId, i.id)}>Open the full evidence (queries, lineage)</Link></p>
          <TechnicalDetails value={{ verification: i.verification, evidence: i.evidence, approvals: gates.map((a) => ({ id: a.id, payload_hash: a.payload_hash })) }}>
            <h3 className="small">Model opinions <span className="muted">(adjust confidence only)</span></h3>
            {opinions.length ? (
              <ul className="small">{opinions.map((o) => <li key={o.label}><span className="tag tag-jev">{o.label}</span> {o.value}</li>)}</ul>
            ) : <p className="muted small">None recorded.</p>}
          </TechnicalDetails>
        </div>
      </div>
    </div>
  );
}
