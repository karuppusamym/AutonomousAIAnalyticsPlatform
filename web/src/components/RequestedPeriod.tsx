import type { RunDetail } from "../api";

interface Window { label: string; requested?: string; last_day?: string; start?: string }
interface PeriodConstraint { current: Window; baseline?: Window | null; columns?: Record<string, string | null> }
interface TableFacts { asset: string; column?: string | null; windows?: Record<string, { label: string; rows: number }>; facts?: string[] }

/** PILOT-FIX-01: a guided run's typed period and baseline, the date column each table is filtered on, and the
 * data facts a reader must see (incomplete period, empty months, future-dated rows), and the requested comparison on
 * every record once REV has run. Nothing when the run has none. */
export function RequestedPeriod({ run }: { run: RunDetail }) {
  const period = (run.constraints as { period?: PeriodConstraint } | undefined)?.period;
  if (!period?.current) return null;
  const output = run.tasks.find((t) => t.key === "hypotheses")?.output as { period?: { tables?: TableFacts[] } } | undefined;
  const tables = output?.period?.tables ?? [];
  const facts = tables.flatMap((t) => t.facts ?? []);
  const verify = run.tasks.find((t) => t.key === "verify")?.output as { period_comparison?: { hypothesis: string; sentence: string }[] } | undefined;
  const comparisons = verify?.period_comparison ?? [];
  const columns = Object.entries(period.columns ?? {}).filter(([, c]) => c).map(([a, c]) => `${a.split(".").pop()}.${c}`);
  const rows = (key: "current" | "baseline") => tables.reduce((n, t) => n + (t.windows?.[key]?.rows ?? 0), 0);
  const said = (w: Window) => `${w.label}${w.requested ? ` (${w.requested})` : ""}`;
  return (
    <section className="investigation-period" aria-label="Requested period">
      <h3>Period analysed</h3>
      <ul>
        <li><strong>Requested:</strong> {said(period.current)}{tables.length > 0 && <> · {rows("current").toLocaleString()} records</>}</li>
        <li><strong>Compared with:</strong> {period.baseline ? <>{said(period.baseline)}{tables.length > 0 && <> · {rows("baseline").toLocaleString()} records</>}</> : "no baseline"}</li>
        {columns.length > 0 && <li className="small muted">Dated by {columns.join(", ")}; every test reads only these dates.</li>}
      </ul>
      {comparisons.length > 0 && <div aria-label="Requested comparison">
        <p><strong>What changed between the two periods</strong> <span className="small muted">(every record in each period)</span></p>
        <ul>{comparisons.map((c) => <li key={c.hypothesis}>{c.sentence}</li>)}</ul>
      </div>}
      {facts.length > 0 && <div role="note" aria-label="Data facts about the period">
        <p><strong>Data facts</strong></p>
        <ul>{facts.map((f) => <li key={f}>{f}</li>)}</ul>
      </div>}
    </section>
  );
}
