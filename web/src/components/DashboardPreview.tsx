import { useMemo, type CSSProperties } from "react";
import { api, type ArtifactDetail } from "../api";
import type { Preview } from "../lib/charts";
import { useAsync } from "../lib/hooks";
import { ChartView } from "./Chart";
import { ErrorBoundary } from "./ErrorBoundary";
import { Markdown } from "./Markdown";
import { Card, EmptyState, ErrorBox, Loading } from "./ui";

export interface LayoutCell {
  kind?: "chart" | "filters" | "markdown";
  chart: string | null;
  row: number;
  col: number;
  width: number;
  height: number;
}

interface ChartContent {
  key: string;
  title: string;
  chart_type: string;
  preview?: Preview & { error?: string };
  description?: string;
}

export const ROW_PX = 46;

/** CSS grid placement for a 12-column layout cell (0-based row/col from skills/dashboards.py). */
export function cellStyle(c: LayoutCell): CSSProperties {
  const col = Math.max(0, Math.min(11, c.col));
  const width = Math.max(1, Math.min(12 - col, c.width));
  return {
    gridColumn: `${col + 1} / span ${width}`,
    gridRow: `${c.row + 1} / span ${Math.max(1, c.height)}`,
    minHeight: Math.max(1, c.height) * ROW_PX,
  };
}

/**
 * Renders a DashboardSpec artifact: content.layout cells on a 12-column grid, each chart cell drawn
 * from the matching chart artifact's content.preview (same run, artifact name == chart key).
 */
export function DashboardPreview({ wsId, artifact }: { wsId: string; artifact: ArtifactDetail }) {
  const content = artifact.content as {
    title?: string; audience?: string; description?: string; charts?: string[]; layout?: LayoutCell[];
    native_filters?: string[]; summary_markdown?: string;
  };
  const charts = useAsync(
    () => (artifact.run_id ? api.listArtifacts(wsId, { type: "chart", run_id: artifact.run_id }) : Promise.resolve([])),
    [wsId, artifact.run_id],
  );
  const byKey = useMemo(() => {
    const m = new Map<string, ChartContent>();
    for (const a of charts.data ?? []) m.set(a.name, a.content as unknown as ChartContent);
    return m;
  }, [charts.data]);

  const layout: LayoutCell[] = content.layout?.length
    ? content.layout
    : (content.charts ?? []).map((k, i) => ({ kind: "chart", chart: k, row: i * 4, col: 0, width: 12, height: 4 }));
  const rowHeight = (c: LayoutCell) => Math.max(1, c.height) * ROW_PX - 44;

  return (
    <Card title={content.title ?? artifact.name} actions={<>
      {content.audience && <span className="tag tag-info">{content.audience}</span>}
      {artifact.external_url && <a className="btn btn-sm" href={artifact.external_url} target="_blank" rel="noreferrer noopener">Open published ↗</a>}
    </>}>
      {artifact.banner && <p className="alert alert-warning" role="alert">Needs re-verification: {artifact.banner.reason}.
        {artifact.banner.request_id && <> A Request Desk task is open for it.</>}</p>}
      {content.description && <p className="muted small">{content.description}</p>}
      <ErrorBox error={charts.error} />
      {charts.loading && !charts.data ? <Loading label="Loading charts…" /> : layout.length === 0 ? <EmptyState title="Empty dashboard" /> : (
        <div className="dash-grid" style={{ gridAutoRows: `${ROW_PX}px` }} aria-label="Dashboard preview">
          {layout.map((cell, i) => {
            const kind = cell.kind ?? (cell.chart ? "chart" : "markdown");
            if (kind === "filters") {
              return (
                <div key={i} className="dash-cell dash-filters" style={cellStyle(cell)}>
                  {/* Not interactive here: a filter that changed its value without re-querying would show wrong numbers (workbench-ux §4). */}
                  <span className="muted small">Filters, applied once published (not interactive in this preview):</span>
                  {(content.native_filters ?? []).map((f) => <span key={f} className="tag tag-neutral">{f}</span>)}
                  {!content.native_filters?.length && <span className="muted small">none</span>}
                </div>
              );
            }
            if (kind === "markdown") {
              return (
                <div key={i} className="dash-cell dash-md" style={cellStyle(cell)}>
                  {content.summary_markdown ? <Markdown text={content.summary_markdown} /> : <span className="muted small">No summary text.</span>}
                </div>
              );
            }
            const ch = cell.chart ? byKey.get(cell.chart) : undefined;
            return (
              <div key={i} className="dash-cell" style={cellStyle(cell)}>
                {!ch ? <p className="muted small">Chart <code>{cell.chart}</code> not found in this run.</p> : ch.preview?.error ? (
                  <p className="warn-text small">{ch.title}: preview failed — {ch.preview.error}</p>
                ) : (
                  <>
                    {ch.chart_type !== "kpi" && <div className="dash-cell-title">{ch.title}</div>}
                    <ErrorBoundary label={`Chart ${ch.key}`}>
                      <ChartView type={ch.chart_type} preview={ch.preview} title={ch.chart_type === "kpi" ? ch.title : undefined}
                        height={Math.max(120, rowHeight(cell))} showTable={false} />
                    </ErrorBoundary>
                  </>
                )}
              </div>
            );
          })}
        </div>
      )}
      {content.summary_markdown && !layout.some((c) => c.kind === "markdown") && (
        <div className="dash-summary"><h3>Summary</h3><Markdown text={content.summary_markdown} /></div>
      )}
    </Card>
  );
}
