import { useState, type FormEvent } from "react";
import {
  api, errorMessage, saveBlob, type BuilderClarify, type BuilderDashboard, type BuilderDocument, type BuilderOp, type BuilderTurn,
  type BuilderVersion,
} from "../api";
import { formatKpi, type Preview } from "../lib/charts";
import { useExpertView } from "../lib/expert";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { ChartView } from "./Chart";
import { answeredByText } from "./DashboardDraft";
import { cellStyle, ROW_PX } from "./DashboardPreview";
import { applyViewFilters, ExploreBar, RowsPanel, TileControls, TileStatus, useDashboardExplore, type Explore } from "./DashboardExplore";
import { DashboardSummary } from "./DashboardSummary";
import { EmbeddedDashboard } from "./EmbeddedDashboard";
import { ErrorBoundary } from "./ErrorBoundary";
import { Markdown } from "./Markdown";
import { Card, EmptyState, ErrorBox, Loading, Notice, StatusBadge, Tag } from "./ui";

/** One line per typed op, in plain words (the op list is each version's diff). */
export function describeOp(op: BuilderOp): string {
  const o = op as Record<string, unknown>;
  switch (op.op) {
    case "chart.set_type": return `${o.chart_id} → ${o.type} chart`;
    case "chart.set_encoding": return `${o.chart_id}: ${["x", "y", "series", "color", "aggregation"].filter((k) => o[k] != null)
      .map((k) => `${k} = ${Array.isArray(o[k]) ? (o[k] as string[]).join(", ") : String(o[k])}`).join("; ")}`;
    case "dashboard.add_filter": return `filter ${o.field} ${o.operator ?? "eq"} ${JSON.stringify(o.value)}`;
    case "query.extend_period": return `compare ${String(o.compare).toUpperCase()} on ${(o.chart_ids as string[]).join(", ")}`;
    case "metric.bind": return `bind ${o.metric} as ${o.viz ?? "kpi"}`;
    case "chart.add": return `add chart ${(o.chart as { id?: string })?.id ?? ""}`;
    case "chart.from_question": return `add a chart: “${String(o.question ?? "")}”${o.type ? ` as ${String(o.type)}` : ""}`;
    case "chart.remove": return `remove ${o.chart_id}`;
    case "layout.move": return `move ${o.ref} to row ${Number(o.y) + 1}, column ${Number(o.x) + 1}`;
    case "text.set": return `text ${o.block_id}`;
    case "publish.request": return `request publication (${(o.audience as string[] | undefined)?.join(", ") ?? "viewers"})`;
    case "revert": return `revert to v${o.to_version}`;
    case "fork": return `draft copied from the published dashboard`;
    case "create": {
      const origin = o.origin as { kind?: string; prompt?: string; question?: string } | undefined;
      if (origin?.kind === "prompt") return `drafted from “${origin.prompt ?? ""}”`;
      if (origin?.kind === "ask_turn") return `started from the Ask answer “${origin.question ?? ""}”`;
      return "created";
    }
    default: return op.op;
  }
}

/** What changed between two versions, element by element (the version diff in the history panel). */
export function diffDocuments(before: BuilderDocument, after: BuilderDocument): string[] {
  const out: string[] = [];
  const index = (d: BuilderDocument) => new Map<string, unknown>([
    ...d.charts.map((c) => [`chart ${c.id}`, c] as [string, unknown]),
    ...d.kpis.map((k) => [`KPI ${k.id}`, k] as [string, unknown]),
    ...d.text_blocks.map((t) => [`text ${t.id}`, t] as [string, unknown]),
    ...d.filters.map((f) => [`filter ${f.id}`, f] as [string, unknown]),
  ]);
  const a = index(before), b = index(after);
  for (const [k, v] of b) {
    if (!a.has(k)) out.push(`added ${k}`);
    else if (JSON.stringify(a.get(k)) !== JSON.stringify(v)) out.push(`changed ${k}`);
  }
  for (const k of a.keys()) if (!b.has(k)) out.push(`removed ${k}`);
  const place = (d: BuilderDocument) => new Map(d.layout.items.map((i) => [i.ref, `${i.x},${i.y},${i.w},${i.h}`]));
  const pa = place(before), pb = place(after);
  for (const [ref, p] of pb) if (pa.has(ref) && pa.get(ref) !== p) out.push(`moved ${ref}`);
  if (before.title !== after.title) out.push("changed the title");
  return out;
}

function BuilderGrid({ doc, focus, onFocus, explore }: {
  doc: BuilderDocument; focus: string | null; onFocus: (id: string) => void; explore: Explore;
}) {
  const expert = useExpertView();
  if (!doc.layout.items.length) {
    return <EmptyState title="Empty dashboard">Ask for a chart below, e.g. “add incidents by priority”.</EmptyState>;
  }
  const charts = new Map(doc.charts.map((c) => [c.id, c]));
  const kpis = new Map(doc.kpis.map((k) => [k.id, k]));
  const texts = new Map(doc.text_blocks.map((t) => [t.id, t]));
  return (
    <div className="dash-grid builder-grid" style={{ gridAutoRows: `minmax(${ROW_PX}px, auto)` }} aria-label="Dashboard layout">
      {doc.layout.items.map((item) => {
        const style = cellStyle({ chart: item.ref, row: item.y, col: item.x, width: item.w, height: item.h });
        const chart = charts.get(item.ref), kpi = kpis.get(item.ref), text = texts.get(item.ref);
        const selected = focus === item.ref;
        const name = chart?.title ?? kpi?.title ?? item.ref;
        const tile = explore.tiles.get(item.ref);
        const preview = chart ? explore.preview(chart) : undefined;
        const controls = !!chart && explore.plans.has(chart.id) && !!chart.preview?.rows?.length;
        return (
          <div key={item.ref} className={`dash-cell builder-cell${selected ? " selected" : ""}`} style={style} role="group" aria-label={name}>
            <div className="cell-head">
              <span className="dash-cell-title grow">{chart?.title}
                {chart?.labels?.includes("experimental") && <Tag tone="warning">experimental</Tag>}
                {chart?.labels?.includes("ad_hoc") && <Tag tone="warning">new calculation</Tag>}
                {chart?.query?.compare && chart.query.compare !== "none" && <Tag tone="info">{chart.query.compare.toUpperCase()}</Tag>}
                {(chart || kpi) && <TileStatus explore={explore} id={item.ref} drilled={chart ? explore.drills[chart.id] : undefined} />}
              </span>
              <button type="button" className="btn btn-xs cell-select" aria-pressed={selected} onClick={() => onFocus(item.ref)}
                aria-label={`Select ${name} for the next request`} title={`Select ${item.ref} for the next request`}>
                {selected ? "Selected" : "Select"}
              </button>
            </div>
            {chart && (
              <>
                {preview?.rows?.length ? (
                  <ErrorBoundary label={`Chart ${chart.id}`}>
                    <ChartView type={chart.type === "area" ? "line" : chart.type} preview={preview as Preview}
                      height={Math.max(100, item.h * ROW_PX - 58)} showTable={false}
                      onSelect={(sel) => explore.select(chart, sel)} />
                  </ErrorBoundary>
                ) : explore.drills[chart.id]?.loading ? <span className="muted small">Loading…</span>
                  : <span className="muted small">{chart.type} · {chart.encoding.x ?? "—"} × {chart.encoding.y.join(", ")} (no preview yet)</span>}
                {controls && <TileControls explore={explore} chart={chart} />}
                {expert && <span className="muted small">{chart.dataset_ref.kind === "metric" ? `metric ${chart.dataset_ref.name}@${chart.dataset_ref.version}`
                  : chart.dataset_ref.kind === "verified_query" ? `verified query ${chart.dataset_ref.name}` : `query ${chart.dataset_ref.handle_id}`}
                  {chart.provenance?.sources?.length ? ` · ${chart.provenance.sources.join(", ")}` : ""}</span>}
              </>
            )}
            {kpi && (
              <div className="kpi">
                <div className="kpi-value">{tile?.status === "filtered" && tile.value !== undefined
                  ? formatKpi(typeof tile.value === "number" ? tile.value : null) : (kpi.value ?? "—")}{kpi.unit ? ` ${kpi.unit}` : ""}</div>
                <div className="kpi-label">{kpi.title} {expert && <span className="muted small">{kpi.metric.name}@{kpi.metric.version}</span>}</div>
              </div>
            )}
            {text && <Markdown text={text.markdown || "_Empty text block_"} />}
          </div>
        );
      })}
    </div>
  );
}

interface Turn { text: string; result?: BuilderTurn; error?: string }

/**
 * The builder chat panel (S08-T3, shared with S11-T4). What a person types becomes typed operations
 * through the `dashboard_design` ladder; the server validates and applies them and every applied batch is
 * a new version. Refusals carry the reason and, where there is one, a suggested op to apply instead.
 */
export function BuilderChatPanel({ dashboard, focus, onUpdated }: {
  dashboard: BuilderDashboard; focus: string | null; onUpdated: (d: BuilderDashboard, turn: BuilderTurn) => void;
}) {
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const turn = async (said: string, fn: () => Promise<BuilderTurn>) => {
    setBusy(true);
    try {
      const result = await fn();
      setTurns((t) => [...t, { text: said, result }]);
      onUpdated(result.dashboard, result);
    } catch (err) {
      setTurns((t) => [...t, { text: said, error: errorMessage(err) }]);
    } finally {
      setBusy(false);
    }
  };
  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    const said = text.trim();
    if (!said) return;
    setText("");
    await turn(said, () => api.builderMessage(dashboard.id, { text: said, focus, base_version: dashboard.version }));
  };
  const applySuggestion = (op: BuilderOp) =>
    turn(`apply: ${describeOp(op)}`, () => api.builderOps(dashboard.id, [op], dashboard.version));
  const askFor = (question: string) => {
    const said = `add ${question}`;
    return turn(said, () => api.builderMessage(dashboard.id, { text: said, focus, base_version: dashboard.version }));
  };

  return (
    <Card title="Change it by asking" label="Dashboard builder chat">
      <ol className="chat-log builder-chat" aria-live="polite">
        {turns.map((t, i) => (
          <li key={i} className="chat-msg">
            <p><strong>You:</strong> {t.text}</p>
            {t.error && <p className="warn-text small">{t.error}</p>}
            {t.result && <TurnResult turn={t.result} onSuggestion={(op) => void applySuggestion(op)} onAsk={(q) => void askFor(q)} busy={busy} />}
          </li>
        ))}
      </ol>
      <form className="row" onSubmit={(e) => void send(e)}>
        <label htmlFor="builder-say" className="sr-only">Describe a change</label>
        <input id="builder-say" className="grow" value={text} onChange={(e) => setText(e.target.value)} maxLength={4000}
          placeholder={focus ? `Change ${focus}: e.g. “make it a bar chart”` : "e.g. “add resolution time by priority”, “filter region = EMEA”"} />
        <button type="submit" className="btn btn-primary" disabled={busy || !text.trim()}>{busy ? "Working…" : "Send"}</button>
      </form>
      {focus && <p className="muted small">Requests about “it” refer to <code>{focus}</code>.</p>}
    </Card>
  );
}

function isClarify(s: BuilderOp | BuilderClarify | undefined): s is BuilderClarify {
  return !!s && typeof (s as BuilderClarify).clarify === "string";
}

function TurnResult({ turn, onSuggestion, onAsk, busy }: {
  turn: BuilderTurn; onSuggestion: (op: BuilderOp) => void; onAsk: (question: string) => void; busy: boolean;
}) {
  const ops = turn.proposal?.ops ?? [];
  const added = (turn.applied ?? []).filter((o) => o.op === "chart.from_question" && o.result);
  return (
    <div className="stack small">
      {ops.length > 0 && (
        <p>Ops ({turn.proposal?.answered_by === "rules" ? "rules" : "model, validated"}): {ops.map((o, i) => <Tag key={i}>{describeOp(o)}</Tag>)}</p>
      )}
      {turn.status === "applied" && <p className="ok-text">Applied: now version {turn.dashboard.version}.</p>}
      {added.map((o, i) => (
        <p key={i}>Added a {o.result?.chart_type ?? "chart"} for “{String(o.question)}”, {answeredByText(o.result ?? {})}.</p>
      ))}
      {turn.status === "unchanged" && <p className="muted">Nothing changed.</p>}
      {turn.forked_from && <Notice tone="info">The dashboard is published, so this edit went to a draft copy; viewers still see the approved version.</Notice>}
      {turn.approval && <Notice tone="info">Publication requested: approval {turn.approval.id} waits in the approval inbox.</Notice>}
      {(turn.refused ?? []).map((r, i) => isClarify(r.suggestion) ? (
        <div key={i} className="stack" role="status" aria-label="Clarifying question">
          <p>I could not chart that from governed data: {r.reason}. {r.suggestion.clarify}</p>
          {(r.suggestion.examples ?? []).length > 0 && (
            <div className="row wrap">
              <span className="muted">Try:</span>
              {r.suggestion.examples!.map((q) => (
                <button key={q} type="button" className="btn btn-sm" disabled={busy} onClick={() => onAsk(q)}>{q}</button>
              ))}
            </div>
          )}
        </div>
      ) : (
        <div key={i} className="warn-text">
          Not applied — {r.reason}
          {r.suggestion && <> <button type="button" className="btn btn-sm" disabled={busy} onClick={() => onSuggestion(r.suggestion as BuilderOp)}>
            Apply instead: {describeOp(r.suggestion as BuilderOp)}</button></>}
        </div>
      ))}
      {(turn.proposal?.notes ?? []).map((n, i) => <p key={i} className="muted">{n}</p>)}
    </div>
  );
}

function History({ dashboard, onUpdated }: { dashboard: BuilderDashboard; onUpdated: (d: BuilderDashboard) => void }) {
  const versions = useAsync(() => api.builderVersions(dashboard.id), [dashboard.id, dashboard.version]);
  const [diff, setDiff] = useState<{ version: number; lines: string[] } | null>(null);
  const act = useAction();
  const compare = async (v: BuilderVersion) => {
    const old = await act.run(() => api.builderVersion(dashboard.id, v.version));
    if (old) setDiff({ version: v.version, lines: diffDocuments(old.document, dashboard.document) });
  };
  const revert = async (v: BuilderVersion) => {
    const r = await act.run(() => api.builderRevert(dashboard.id, v.version, dashboard.version));
    if (r) onUpdated(r.dashboard);
  };
  return (
    <Card title="Versions">
      <ErrorBox error={versions.error ?? act.error} />
      {versions.loading && !versions.data ? <Loading /> : (
        <ol className="list compact">
          {(versions.data ?? []).map((v) => (
            <li key={v.version} className="list-item">
              <span><strong>v{v.version}</strong> {v.diff.map((o, i) => <Tag key={i}>{describeOp(o)}</Tag>)}</span>
              <span className="muted small">{v.created_by ?? "—"} · {fmtDate(v.created_at)}</span>
              {v.version !== dashboard.version && <span className="row">
                <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void compare(v)}>Compare</button>
                <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void revert(v)}>Revert to v{v.version}</button>
              </span>}
            </li>
          ))}
        </ol>
      )}
      {diff && (
        <div className="stack small" aria-label={`Changes since v${diff.version}`}>
          <strong>Since v{diff.version}:</strong>
          {diff.lines.length ? <ul>{diff.lines.map((l) => <li key={l}>{l}</li>)}</ul> : <span className="muted">no differences</span>}
        </div>
      )}
    </Card>
  );
}

function Actions({ dashboard, role, onUpdated }: { dashboard: BuilderDashboard; role: string | null; onUpdated: (d: BuilderDashboard) => void }) {
  const act = useAction();
  const [note, setNote] = useState<string | null>(null);
  const [approvalId, setApprovalId] = useState("");
  const canEdit = role === "owner" || role === "editor" || role === "analyst";
  const request = async () => {
    const r = await act.run(() => api.builderPublishRequest(dashboard.id, { destination: "superset", audience: ["role:viewer"] }));
    if (r) {
      setApprovalId(r.approval_id);
      setNote(`Publication requested (approval ${r.approval_id}, ${r.risk_tier} risk). An approver decides it in the approval inbox.`);
      const d = await api.builderDashboard(dashboard.id);
      onUpdated(d);
    }
  };
  const publish = async () => {
    const r = await act.run(() => api.builderPublish(dashboard.id, approvalId));
    if (r) {
      setNote(`Published${r.url ? `: ${r.url}` : ""}.`);
      onUpdated(await api.builderDashboard(dashboard.id));
    }
  };
  const exportAs = async (format: "csv" | "xlsx" | "pdf") => {
    const r = await act.run(() => api.builderExportRequest(dashboard.id, format));
    if (!r) return;
    if (r.status === "approval_required") {
      setNote(`Exporting ${r.sensitivity} data needs approval ${r.approval?.id}; download it here once approved.`);
      setApprovalId(r.approval?.id ?? "");
      return;
    }
    const file = await act.run(() => api.builderExport(dashboard.id, format));
    if (file) saveBlob(file);
  };
  const exportApproved = async (format: "csv" | "xlsx" | "pdf") => {
    const file = await act.run(() => api.builderExport(dashboard.id, format, approvalId));
    if (file) saveBlob(file);
  };
  return (
    <Card title="Publish and export" actions={<StatusBadge status={dashboard.status} />}>
      <p className="small muted">Publishing and exports of sensitive data are side effects: each needs an approval bound to this exact version.
        Exports carry a watermark with your name and the time.</p>
      <div className="row wrap">
        {canEdit && <button type="button" className="btn btn-primary btn-sm" disabled={act.busy || dashboard.status === "published"}
          onClick={() => void request()}>Request publication</button>}
        {(["csv", "xlsx", "pdf"] as const).map((f) => (
          <button key={f} type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void exportAs(f)}>Export {f.toUpperCase()}</button>
        ))}
      </div>
      {approvalId && (
        <div className="row wrap">
          <span className="small">Approval <code>{approvalId}</code> approved?</span>
          {canEdit && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void publish()}>Publish now</button>}
          {(["csv", "xlsx", "pdf"] as const).map((f) => (
            <button key={f} type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void exportApproved(f)}>Download {f.toUpperCase()}</button>
          ))}
        </div>
      )}
      {note && <p className="small">{note}</p>}
      <ErrorBox error={act.error} />
    </Card>
  );
}

/** A builder dashboard (schema dashboard.v2) open in Outputs: grid, chat panel, versions, publish and export. */
export function DashboardBuilder({ artifactId, role, onSwitch }: {
  artifactId: string; role: string | null; onSwitch?: (artifactId: string) => void;
}) {
  const dash = useAsync(() => api.builderDashboard(artifactId), [artifactId]);
  if (dash.loading && !dash.data) return <Loading label="Loading dashboard…" />;
  if (!dash.data) return <ErrorBox error={dash.error} onRetry={() => void dash.reload()} />;
  const d = dash.data;
  const updated = (next: BuilderDashboard) => {
    if (next.id !== d.id && onSwitch) onSwitch(next.id);  // an edit of a published dashboard forked a draft
    dash.setData(next);
  };
  return <BuilderView dashboard={d} role={role} onUpdated={updated} />;
}

function BuilderView({ dashboard: d, role, onUpdated: updated }: {
  dashboard: BuilderDashboard; role: string | null; onUpdated: (d: BuilderDashboard) => void;
}) {
  const [focus, setFocus] = useState<string | null>(null);
  const explore = useDashboardExplore(d);
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const canEdit = role === "owner" || role === "editor" || role === "analyst";
  const apply = async () => {
    if (!explore.view) return;
    setApplying(true);
    setApplyError(null);
    try {
      const turn = await applyViewFilters(d, explore.view);
      if (turn.status === "refused") setApplyError((turn.refused ?? []).map((r) => r.reason).join("; "));
      else {
        explore.clear();
        updated(turn.dashboard);
      }
    } catch (err) {
      setApplyError(errorMessage(err));
    } finally {
      setApplying(false);
    }
  };
  return (
    <div className="stack dashboard-builder">
      <Card title={<>{d.document.title} <span className="muted small">· v{d.version}</span></>} actions={<>
        {d.document.sensitivity && <Tag tone={d.document.sensitivity === "internal" || d.document.sensitivity === "public" ? "neutral" : "warning"}>{d.document.sensitivity}</Tag>}
        {d.document.filters.map((f) => <Tag key={f.id} tone="info">{f.value == null ? `${f.field.split(".").pop()}: any` : `${f.field} ${f.op} ${JSON.stringify(f.value)}`}</Tag>)}
      </>}>
        {d.document.charts.length > 0 && <ExploreBar explore={explore} canEdit={canEdit} onApply={() => void apply()} applying={applying} />}
        <ErrorBox error={applyError ?? explore.error} />
        <BuilderGrid doc={d.document} focus={focus} onFocus={(id) => setFocus(focus === id ? null : id)} explore={explore} />
        <RowsPanel explore={explore} charts={d.document.charts} />
      </Card>
      <ErrorBoundary label="Summary" resetKey={`${d.id}@${d.version}`}><DashboardSummary dashboard={d} /></ErrorBoundary>
      {d.status === "published" && d.platform === "superset" && (
        <Card title="Published in Superset" label="Published dashboard in Superset">
          <EmbeddedDashboard artifactId={d.id} title={d.document.title} />
        </Card>
      )}
      <BuilderChatPanel dashboard={d} focus={focus} onUpdated={(next) => updated(next)} />
      <History dashboard={d} onUpdated={updated} />
      <Actions dashboard={d} role={role} onUpdated={updated} />
    </div>
  );
}

/** Start a new, empty builder dashboard (Outputs › Dashboards). */
export function NewBuilderDashboard({ wsId, onCreated }: { wsId: string; onCreated: (artifactId: string) => void }) {
  const [title, setTitle] = useState("");
  const act = useAction();
  const create = async (e: FormEvent) => {
    e.preventDefault();
    const id = title.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
    const doc: BuilderDocument = { id: /^[a-z]/.test(id) ? id : `d_${id || Date.now()}`, title: title.trim(), layout: { items: [] },
      charts: [], kpis: [], text_blocks: [], filters: [] };
    const r = await act.run(() => api.createBuilderDashboard(wsId, doc));
    if (r) onCreated(r.id);
  };
  return (
    <form className="row" onSubmit={(e) => void create(e)}>
      <label htmlFor="new-dashboard-title" className="sr-only">Dashboard title</label>
      <input id="new-dashboard-title" className="grow" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Dashboard title" maxLength={300} />
      <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !title.trim()}>Create</button>
      <ErrorBox error={act.error} />
    </form>
  );
}
