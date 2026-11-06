/**
 * Declared quality rules on one catalog table (P1-7): the trust signal, the rules with their latest result, a run
 * through the gateway, and recommendations from the stored profile (they land in the Review queue as drafts; an
 * owner approves them there). Every decision is made again by the server.
 */
import { useState } from "react";
import { api, type QualityRecommendation, type QualityRuleRow, type QualityTrust } from "../api";
import { fmtDate, fmtPct } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { ErrorBox, Loading, Notice, Tag } from "./ui";

const LEVELS: Record<string, { tone: string; label: string; title: string }> = {
  trusted: { tone: "success", label: "quality: trusted", title: "Every declared rule passed on its last run" },
  partial: { tone: "info", label: "quality: partial", title: "Rules that ran passed; some have not run yet" },
  unverified: { tone: "neutral", label: "quality: not yet run", title: "Rules are declared but none has run" },
  at_risk: { tone: "warning", label: "quality: at risk", title: "A warning rule failed or could not run" },
  failing: { tone: "danger", label: "quality: failing", title: "A critical rule failed" },
};

export function QualityTrustBadge({ trust }: { trust: QualityTrust | null | undefined }) {
  if (!trust || trust.level === "none") return null;
  const l = LEVELS[trust.level] ?? { tone: "neutral", label: `quality: ${trust.level}`, title: trust.level };
  return <span className={`tag tag-${l.tone}`} title={`${l.title} · ${trust.passing}/${trust.rules} rules pass`}>{l.label}</span>;
}

function resultTag(r: QualityRuleRow) {
  if (!r.last_status) return <Tag>not run</Tag>;
  const tone = r.last_status === "passed" ? "success" : r.last_status === "error" ? "warning" : "danger";
  return <Tag tone={tone}>{r.last_status}{r.last_pass_rate !== null ? ` · ${fmtPct(r.last_pass_rate)}` : ""}</Tag>;
}

export function QualityRulesPanel({ wsId, assetId, fq, trust, canEdit }: {
  wsId: string; assetId: string; fq: string; trust: QualityTrust | null | undefined; canEdit: boolean;
}) {
  const rules = useAsync(() => api.qualityRules(wsId, fq), [wsId, fq]);
  const run = useAction();
  const rec = useAction();
  const toggle = useAction();
  const [recommended, setRecommended] = useState<QualityRecommendation | null>(null);
  const active = (rules.data ?? []).filter((r) => r.status === "active");

  return (
    <section className="stack" aria-label={`Quality rules on ${fq}`}>
      <h4>Quality rules <QualityTrustBadge trust={trust} /></h4>
      <ErrorBox error={rules.error} onRetry={rules.reload} />
      {rules.loading && !rules.data && <Loading label="Loading rules…" />}
      {rules.data && rules.data.length === 0 && (
        <p className="muted small">No rule is declared on this table. {canEdit
          ? "Recommend rules from its profile, then approve the ones you want in the Review queue."
          : "Editors and owners can declare rules."}</p>
      )}
      {!!rules.data?.length && (
        <div className="table-wrap">
          <table className="table table-compact">
            <caption className="sr-only">Declared quality rules</caption>
            <thead><tr><th>Rule</th><th>Dimension</th><th>Severity</th><th>Threshold</th><th>Last result</th><th>Origin</th>
              {canEdit && <th><span className="sr-only">Status</span></th>}</tr></thead>
            <tbody>{rules.data.map((r) => (
              <tr key={r.id} className={r.status !== "active" ? "muted" : undefined}>
                <td><code>{r.name}</code>{r.definition.description && <div className="muted small">{r.definition.description}</div>}</td>
                <td className="small">{r.dimension}</td>
                <td className="small">{r.severity}</td>
                <td className="small">{fmtPct(r.threshold)}</td>
                <td>{resultTag(r)}{r.last_run_at && <div className="muted small">{fmtDate(r.last_run_at)}</div>}</td>
                <td className="small">{r.origin}</td>
                {canEdit && (
                  <td><button type="button" className="btn btn-xs btn-ghost" disabled={toggle.busy}
                    onClick={() => void toggle.run(async () => {
                      await api.setQualityRuleStatus(r.id, r.status === "active" ? "disabled" : "active");
                      await rules.reload();
                    })}>{r.status === "active" ? "Disable" : "Enable"}</button></td>
                )}
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <div className="chip-row">
        {active.length > 0 && (
          <button type="button" className="btn btn-sm" disabled={run.busy}
            onClick={() => void run.run(async () => { const out = await api.runQualityRules(wsId, [fq]); await rules.reload(); return out; })}>
            {run.busy ? "Running…" : `Run ${active.length} rule${active.length === 1 ? "" : "s"} now`}
          </button>
        )}
        {canEdit && (
          <button type="button" className="btn btn-sm btn-ghost" disabled={rec.busy}
            onClick={() => void rec.run(async () => setRecommended(await api.recommendQualityRules(wsId, assetId)))}>
            {rec.busy ? "Reading profile…" : "Recommend rules from profile"}
          </button>
        )}
      </div>
      {recommended && (
        <Notice tone={recommended.queued.length ? "success" : "info"}>
          {recommended.queued.length
            ? `${recommended.queued.length} draft rule${recommended.queued.length === 1 ? "" : "s"} added to the Review queue. Nothing runs until an owner approves.`
            : "No new rule to recommend from this profile."}
        </Notice>
      )}
      <ErrorBox error={run.error ?? rec.error ?? toggle.error} />
    </section>
  );
}
