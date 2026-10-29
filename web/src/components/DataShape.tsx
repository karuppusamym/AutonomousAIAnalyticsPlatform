import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type ShapeKind, type ShapePattern, type ShapeTable } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { Card, ErrorBox, Notice, Tag } from "./ui";

/** The patterns that open a next step; a table's own role (fact, dimension...) is context, not something to do. */
export const ACTIONABLE: ShapeKind[] = ["event_log", "time_series", "ml_candidate"];

const LABEL: Record<string, string> = {
  event_log: "Event log", time_series: "Trend over time", ml_candidate: "Prediction candidate",
};

interface Detail { targets?: { column: string; kind: string; why: string }[]; measures?: string[]; time_column?: string;
  mapping?: { case_column: string; activity_column: string; timestamp_column: string } }

/** Words a person can read for what a pattern found. */
export function patternSummary(p: ShapePattern): string {
  const d = p.detail as Detail;
  if (p.kind === "event_log" && d.mapping) return `case ${d.mapping.case_column}, step ${d.mapping.activity_column}, time ${d.mapping.timestamp_column}`;
  if (p.kind === "time_series" && d.time_column) return `${(d.measures ?? []).join(", ")} over ${d.time_column}`;
  if (p.kind === "ml_candidate") return (d.targets ?? []).slice(0, 3).map((t) => `${t.column} (${t.kind})`).join(", ");
  return "";
}

export function actionableRows(tables: ShapeTable[]): { table: ShapeTable; pattern: ShapePattern }[] {
  return tables.flatMap((table) => table.patterns.filter((p) => ACTIONABLE.includes(p.kind)).map((pattern) => ({ table, pattern })));
}

function NextStep({ wsId, p }: { wsId: string; p: ShapePattern }) {
  if (!p.next_step) return null;
  switch (p.next_step.action) {
    case "process_analysis": return <Link className="btn btn-sm btn-primary" to={to.work(wsId, "process")}>{p.next_step.label}</Link>;
    case "experiment": return <Link className="btn btn-sm btn-primary" to={to.work(wsId, "experiments")}>{p.next_step.label}</Link>;
    case "investigation": return <Link className="btn btn-sm btn-primary" to={to.investigations(wsId)}>{p.next_step.label}</Link>;
    default: return null;
  }
}

/**
 * Overview → "What you can do with this data": what the catalog's measurements say each selected table is good for, with the
 * evidence and the next step. A person confirms a pattern once or says it is not that; "Look for more" lets a model suggest
 * event logs for tables the rules did not recognise (code verifies each answer first).
 */
export function DataShapeCard({ wsId, role }: { wsId: string; role?: string | null }) {
  const canAct = roleAtLeast(role, "analyst");
  const shape = useAsync(() => (canAct ? api.dataShape(wsId) : Promise.resolve(null)), [wsId, canAct]);
  const [report, setReport] = useState<string | null>(null);
  const decide = useAction();
  const look = useAction();
  if (!canAct) return null;
  if (!shape.data || !Array.isArray(shape.data.tables)) return null;  // a secondary card: it appears when it has something to say, and never blocks the page
  const rows = actionableRows(shape.data.tables);
  const workspace = shape.data.workspace ?? [];
  const editor = roleAtLeast(role, "editor");
  const mark = (assetId: string, kind: ShapeKind, decision: "confirm" | "dismiss") =>
    void decide.run(async () => { await api.markDataShape(wsId, { asset_id: assetId, kind, decision }); await shape.reload(); });
  const lookForMore = () => void look.run(async () => {
    const r = await api.proposeDataShape(wsId);
    setReport(r.skipped ? `Nothing to look at: ${r.skipped}.` : `Looked at ${r.considered} table(s): ${r.proposed} suggestion(s) verified`
      + (r.rejected.length ? `, ${r.rejected.length} rejected because the columns did not check out.` : "."));
    await shape.reload();
  });
  return (
    <Card title="What you can do with this data" label="What you can do with this data">
      {workspace.length > 0 && (
        <p className="muted" data-testid="shape-workspace">
          {workspace.map((w) => w.label).join(" · ")}. {sentence(workspace[0].reasons[0])}
        </p>
      )}
      {rows.length === 0 && <p className="muted">No clear pattern yet. Select more tables in Data, or profile them, and it will fill in.</p>}
      <ul className="stack" aria-label="Detected patterns">
        {rows.map(({ table, pattern: p }) => (
          <li key={`${table.asset_id}-${p.kind}`} className="row-between">
            <span>
              <strong>{table.business_name || table.name}</strong> <Tag>{LABEL[p.kind]}</Tag>{" "}
              {p.state === "confirmed" && <Tag tone="success">confirmed</Tag>}
              {p.state === "proposed" && <Tag tone="warning">suggested by a model, checked in code</Tag>}
              <span className="block small muted">{patternSummary(p)}{p.reasons[0] ? ` · ${p.reasons[0]}` : ""}</span>
            </span>
            <span className="chip-row">
              <NextStep wsId={wsId} p={p} />
              {editor && p.state !== "confirmed" && <button type="button" className="btn btn-sm" disabled={decide.busy}
                aria-label={`Confirm ${LABEL[p.kind]} for ${table.name}`} onClick={() => mark(table.asset_id, p.kind, "confirm")}>Yes, this</button>}
              {editor && <button type="button" className="btn btn-sm btn-ghost" disabled={decide.busy}
                aria-label={`Not a ${LABEL[p.kind]}: ${table.name}`} onClick={() => mark(table.asset_id, p.kind, "dismiss")}>Not this</button>}
            </span>
          </li>
        ))}
      </ul>
      <ErrorBox error={decide.error ?? look.error} />
      {report && <Notice>{report}</Notice>}
      {editor && (
        <div className="chip-row">
          <button type="button" className="btn btn-sm" disabled={look.busy} onClick={lookForMore}>
            {look.busy ? "Looking…" : "Look for more"}
          </button>
          <span className="small muted">A model reads column names, types and counts only; nothing is shown until code checks it.</span>
        </div>
      )}
    </Card>
  );
}

/** The server writes reasons as lower-case clauses; shown alone, one starts a sentence. */
function sentence(text: string | undefined): string {
  if (!text) return "";
  const t = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(t) ? t : `${t}.`;
}
