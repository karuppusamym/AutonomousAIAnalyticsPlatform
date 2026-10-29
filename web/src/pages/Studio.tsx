import { outputDisplayName } from "../lib/outputNames";
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type Artifact, type ArtifactDetail, type Insight, type ProcessAnalysis, type RecipeRun, type Run } from "../api";
import { useAuth } from "../auth";
import { ChartView } from "../components/Chart";
import { PublishCard } from "../components/DashboardPublish";
import { useExpertView } from "../lib/expert";
import { PublishedOutputs } from "../components/PublishedOutputs";
import { DashboardPreview } from "../components/DashboardPreview";
import { DashboardBuilder, NewBuilderDashboard } from "../components/DashboardBuilder";
import { DraftFromPrompt } from "../components/DashboardDraft";
import { ExistingDashboards } from "../components/ExistingDashboards";
import { DashboardGrouping } from "../components/DashboardGrouping";
import { LineageGraph } from "../components/LineageGraph";
import { Markdown } from "../components/Markdown";
import { ModelsOutput } from "../components/Ml";
import { MaterializationsOutput } from "../components/Pipelines";
import { ProcessAnalysisView } from "../components/ProcessMining";
import { VerificationBadge, voidCause } from "../components/WhyNumber";
import { StartWorkButton } from "../components/StartWork";
import { Card, CodeBlock, ConfidenceBar, EmptyState, ErrorBox, KeyValue, Loading, PageHeader, RecordTable, StatusBadge, TechnicalDetails } from "../components/ui";
import type { Preview } from "../lib/charts";
import { fmtDate, fmtNumber, fmtPct, optionLabel, shortHash } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { to, type OutputType } from "../routes";
import { FindingDetail } from "./Insights";
import { GenerateReportForm, NewReportDoc, ReportBuilder, ReportRow } from "./Reports";

/** Artifact type → output filter. Metrics are not outputs: their one home is Data (spec v4 §15). */
export function outputTypeOf(artifactType: string): OutputType | null {
  if (artifactType === "metric") return null;
  // ML experiment records (spec, split, trials, package, evaluation, card) live with their experiment in Work
  if (artifactType.startsWith("ml_")) return null;
  if (artifactType === "dashboard" || artifactType === "report" || artifactType === "dataset" || artifactType === "chart") return artifactType;
  if (artifactType === "report_document") return "report";  // CONV-04: a report shaped by conversation
  if (artifactType === "process_analysis") return "process";
  return "other";
}

export const OUTPUT_FILTERS: { id: OutputType; label: string }[] = [
  { id: "finding", label: "Findings" }, { id: "dashboard", label: "Dashboards" }, { id: "report", label: "Reports" },
  { id: "dataset", label: "Datasets" }, { id: "chart", label: "Charts" }, { id: "prepared", label: "Prepared data" },
  { id: "model", label: "Models & scoring" }, { id: "table", label: "Managed tables" }, { id: "process", label: "Process maps" }, { id: "other", label: "Other" },
];

/** Output types shown as their own panel rather than as rows of the one list (models and their scoring runs). */
const PANEL_TYPES = new Set<OutputType>(["model", "table"]);

type Item =
  | { kind: "finding"; id: string; at: string; finding: Insight }
  | { kind: "artifact"; id: string; at: string; artifact: Artifact; type: OutputType }
  | { kind: "prepared"; id: string; at: string; run: RecipeRun };

const typeOf = (i: Item): OutputType => (i.kind === "artifact" ? i.type : i.kind);
const analysisOf = (i: Item): string | null => i.kind === "finding" ? i.finding.run_id : i.kind === "artifact" ? i.artifact.run_id : null;

/**
 * Outputs (spec v4 §15): one list of what work produced, filtered by type: findings, dashboards,
 * reports, datasets, charts and prepared data. Dashboards and reports are filters of this list, not
 * second lists; opening a dashboard shows its preview and the publication request.
 */
export function OutputsPage() {
  const { wsId = "", insightId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const requested = params.get("type") as OutputType | null;
  const type: OutputType | "" = insightId ? "finding" : OUTPUT_FILTERS.some((f) => f.id === requested) ? requested! : "";
  const requestedSelection = insightId ?? params.get("artifact") ?? params.get("dashboard") ?? params.get("run");
  const analysis = params.get("analysis");
  const [search, setSearch] = useState("");
  const artifacts = useAsync(() => api.listArtifacts(wsId), [wsId]);
  const findings = useAsync(() => api.listInsights(wsId), [wsId]);
  const prepared = useAsync(() => api.listRecipeRuns(wsId), [wsId]);
  const runs = useAsync(() => api.listRuns(wsId), [wsId]);
  const models = useAsync(() => api.modelVersions(wsId), [wsId]);
  const tables = useAsync(() => api.materializations(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const { user } = useAuth();
  const role = user?.is_admin ? "owner" : ws.data?.role;
  const set = useCallback((patch: Record<string, string | null>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v === null || v === "") next.delete(k);
        else next.set(k, v);
      }
      return next;
    });
  }, [setParams]);

  const items = useMemo<Item[]>(() => {
    const out: Item[] = [];
    for (const f of findings.data ?? []) out.push({ kind: "finding", id: f.id, at: f.created_at, finding: f });
    for (const a of artifacts.data ?? []) {
      const t = outputTypeOf(a.type);
      if (t) out.push({ kind: "artifact", id: a.id, at: a.created_at, artifact: a, type: t });
    }
    for (const r of Array.isArray(prepared.data) ? prepared.data : []) {
      if (r.mode === "materialize" && r.status === "succeeded") out.push({ kind: "prepared", id: r.id, at: r.created_at, run: r });
    }
    return out.sort((a, b) => b.at.localeCompare(a.at));
  }, [findings.data, artifacts.data, prepared.data]);
  const panel = type !== "" && PANEL_TYPES.has(type);
  const runById = new Map((runs.data ?? []).map((r) => [r.id, r]));
  const linkedFinding = items.find((i): i is Extract<Item, { kind: "finding" }> => i.id === insightId && i.kind === "finding");
  const activeAnalysis = panel ? null : insightId ? linkedFinding?.finding.run_id ?? analysis : analysis;
  const scopedItems = items.filter((i) => !activeAnalysis || analysisOf(i) === activeAnalysis);
  const counts = new Map<OutputType, number>();
  for (const i of scopedItems) counts.set(typeOf(i), (counts.get(typeOf(i)) ?? 0) + 1);
  if (!activeAnalysis) {
    const modelNames = new Set((Array.isArray(models.data) ? models.data : []).map((m) => m.name)).size;
    if (modelNames) counts.set("model", modelNames);
    const tableNames = new Set((Array.isArray(tables.data) ? tables.data : []).map((m) => `${m.schema_name}.${m.table_name}`)).size;
    if (tableNames) counts.set("table", tableNames);
  }
  const shown = scopedItems.filter((i) => (!type || typeOf(i) === type) &&
    `${i.kind === "finding" ? i.finding.title : i.kind === "artifact" ? `${outputDisplayName(i.artifact)} ${i.artifact.name}` : i.run.recipe_name} ${runById.get(analysisOf(i) ?? "")?.objective ?? ""}`.toLowerCase().includes(search.toLowerCase()));
  // Open the newest visible output on first visit so the detail pane is useful immediately.
  const selected = requestedSelection && shown.some((i) => i.id === requestedSelection)
    ? requestedSelection : shown[0]?.id ?? null;
  const analysisRuns = (runs.data ?? []).filter((r) => items.some((i) => analysisOf(i) === r.id));
  const unknownRuns = [...new Set(items.map(analysisOf).filter((id): id is string => !!id && !runById.has(id)))];
  const open = selected ? items.find((i) => i.id === selected) : undefined;
  const selectedForPicker = shown.some((i) => i.id === selected) ? selected ?? "" : "";
  const loading = (artifacts.loading && !artifacts.data) || (findings.loading && !findings.data);
  const error = artifacts.error ?? findings.error;
  const filters = OUTPUT_FILTERS.filter((f) => counts.has(f.id) || f.id === type || f.id === "report" || f.id === "dashboard");

  return (
    <div className="page outputs-page">
      <PageHeader title="Dashboards & reports" subtitle="Everything your investigations produced: dashboards, reports, findings and datasets. Pick one to see it and where its numbers came from." />
      {!insightId && (!type || type === "dashboard" || type === "report") && <PublishedOutputs wsId={wsId} artifacts={artifacts.data ?? []}
        onSelect={(a) => { setSearch(""); set({ type: a.type, artifact: a.id, analysis: a.run_id, dashboard: null, run: null }); }} />}
      <nav className="chip-row type-filter" aria-label="Filter outputs by type">
        <Link className={`chip ${type ? "" : "active"}`} aria-current={type ? undefined : "page"} to={to.outputs(wsId, { analysis: activeAnalysis ?? undefined })}>All ({scopedItems.length})</Link>
        {filters.map((f) => (
          <Link key={f.id} className={`chip ${f.id === type ? "active" : ""}`} aria-current={f.id === type ? "page" : undefined}
            to={to.outputs(wsId, { type: f.id, analysis: PANEL_TYPES.has(f.id) ? undefined : activeAnalysis ?? undefined })}>{f.label} ({counts.get(f.id) ?? 0})</Link>
        ))}
      </nav>
      <section className="card outputs-controls" aria-label="Choose outputs">
        <div className="outputs-control">
          <label htmlFor="output-investigation">Investigation</label>
          <select id="output-investigation" value={activeAnalysis ?? ""} onChange={(e) => set({
            analysis: e.target.value || null, artifact: null, dashboard: null, run: null,
          })}>
            <option value="">All investigations</option>
            {analysisRuns.map((r) => <option key={r.id} value={r.id}>{optionLabel(r.objective, 70)} · {items.filter((i) => analysisOf(i) === r.id).length} outputs</option>)}
            {unknownRuns.map((id) => <option key={id} value={id}>Earlier investigation · {id}</option>)}
          </select>
          <span className="muted small">Scope the list by the question that produced the output.</span>
        </div>
        <div className="outputs-control">
          <label htmlFor="output-item">Output item</label>
          <select id="output-item" value={selectedForPicker} disabled={!shown.length} onChange={(e) => {
            const item = shown.find((i) => i.id === e.target.value);
            if (!item) {
              set({ artifact: null, dashboard: null, run: null });
            } else if (item.kind === "finding") {
              navigate(to.findings(wsId, item.id));
            } else if (item.kind === "prepared") {
              set({ run: item.id, artifact: null, dashboard: null });
            } else {
              set({ artifact: item.id, dashboard: null, run: null });
            }
          }}>
            <option value="">{shown.length ? "Select an output" : "No matching outputs"}</option>
            {shown.map((item) => <option key={`${item.kind}-${item.id}`} value={item.id}>
              {item.kind === "finding" ? `${item.finding.code} · ${item.finding.title}`
                : item.kind === "prepared" ? `${item.run.recipe_name} · prepared data`
                  : `${outputDisplayName(item.artifact)} · ${TYPE_WORD[item.type] || item.artifact.type.replace(/_/g, " ")}`}
            </option>)}
          </select>
          <span className="muted small">Choose an item to open its detail and lineage.</span>
        </div>
      </section>
      {type === "report" && (
        <details className="card generate-report">
          <summary>Generate a report</summary>
          <div className="card-body">
            <GenerateReportForm wsId={wsId} onCreated={(a) => { void artifacts.reload(); set({ artifact: a.id }); }} />
          </div>
        </details>
      )}
      {type === "report" && (role === "owner" || role === "editor") && (
        <details className="card generate-report" open={!items.some((i) => i.kind === "artifact" && i.artifact.type === "report_document")}>
          <summary>Write a report by asking</summary>
          <div className="card-body">
            <p className="small muted">Start with a title, then say what it should contain: “add a section on EMEA backlog”, “include the
              resolution trend chart”. It uses only charts, numbers and findings that are already verified.</p>
            <NewReportDoc wsId={wsId} onCreated={(id) => { void artifacts.reload(); set({ artifact: id }); }} />
          </div>
        </details>
      )}
      {type === "dashboard" && role && role !== "viewer" && role !== "approver" && (
        <details className="card generate-report">
          <summary>Build a dashboard</summary>
          <div className="card-body">
            <p className="small muted">Describe it and a draft is built from your data, or start empty. Then change it by asking:
              every change is a new version you can undo.</p>
            <DraftFromPrompt wsId={wsId} onCreated={(id) => { void artifacts.reload(); set({ artifact: id }); }} />
            <details>
              <summary className="small">Start an empty dashboard instead</summary>
              <NewBuilderDashboard wsId={wsId} onCreated={(id) => { void artifacts.reload(); set({ artifact: id }); }} />
            </details>
          </div>
        </details>
      )}
      {type === "dashboard" && (
        <details className="card generate-report">
          <summary>Group published dashboards under this workspace’s name</summary>
          <div className="card-body"><DashboardGrouping wsId={wsId} role={role} /></div>
        </details>
      )}
      {type === "dashboard" && (
        <details className="card generate-report">
          <summary>Import an existing BI dashboard</summary>
          <div className="card-body"><ExistingDashboards wsId={wsId} role={role} /></div>
        </details>
      )}
      {type === "model" && <ModelsOutput wsId={wsId} role={role} />}
      {type === "table" && <MaterializationsOutput wsId={wsId} role={role} table={params.get("table")} />}
      {!panel && <ErrorBox error={error} onRetry={() => { void artifacts.reload(); void findings.reload(); }} />}
      {!panel && loading && <Loading />}
      {!panel && !loading && !error && items.length === 0 && (
        <EmptyState title="No outputs yet" action={<StartWorkButton wsId={wsId} />}>
          Findings, dashboards, reports and prepared data appear here once work produces them.
        </EmptyState>
      )}
      {!panel && !loading && (error || items.length > 0) && (
        <div className="outputs-layout">
          <div className="split-list">
            <div className="outputs-list-heading"><h2>{activeAnalysis ? runById.get(activeAnalysis)?.objective ?? "Investigation outputs" : "Recent outputs"}</h2><span className="muted small">{shown.length} item{shown.length === 1 ? "" : "s"}</span></div>
            <label className="field output-search"><span className="sr-only">Search outputs</span><input type="search" placeholder="Find an output…" value={search} onChange={(e) => setSearch(e.target.value)} /></label>
            <span className="small muted">{shown.length === 1 ? "1 output" : `${shown.length} outputs`} · newest first</span>
            {shown.length === 0 && <EmptyState title="No outputs for this selection">Choose another investigation or output type.</EmptyState>}
            <ul className="list selectable" aria-label="Outputs">
              {shown.map((i) => {
                const active = i.id === selected;
                return (
                  <li key={`${i.kind}-${i.id}`}>
                    {i.kind === "finding"
                      ? <Link to={to.findings(wsId, i.id)} className={`list-button ${active ? "active" : ""}`} aria-current={active ? "true" : undefined}>
                        <OutputRow item={i} run={runById.get(analysisOf(i) ?? "")} /></Link>
                      : <button type="button" className={`list-button ${active ? "active" : ""}`} aria-current={active ? "true" : undefined}
                        onClick={() => set(i.kind === "prepared" ? { run: i.id, artifact: null, dashboard: null } : { artifact: i.id, dashboard: null, run: null })}>
                        <OutputRow item={i} run={runById.get(analysisOf(i) ?? "")} /></button>}
                  </li>
                );
              })}
            </ul>
          </div>
          <div className="split-detail output-detail">
            {open && analysisOf(open) && <div className="output-context">
              <span className="small muted">Investigation question</span>
              <strong>{runById.get(analysisOf(open) ?? "")?.objective ?? "Earlier investigation"}</strong>
              <Link className="small" to={to.run(wsId, analysisOf(open)!)}>Open investigation</Link>
            </div>}
            {open?.kind === "finding" ? <FindingDetail id={open.id} wsId={wsId} />
              : open?.kind === "prepared" ? <PreparedView run={open.run} wsId={wsId} />
                : selected ? <ArtifactView id={selected} wsId={wsId} role={role ?? null} onSelect={(id) => set({ artifact: id })} />
                  : <EmptyState title="Select an output">A dashboard shows a live preview from its charts&apos; data before anything is published.</EmptyState>}
          </div>
        </div>
      )}
    </div>
  );
}

const TYPE_WORD: Record<OutputType, string> = {
  finding: "finding", dashboard: "dashboard", report: "report", dataset: "dataset", chart: "chart", prepared: "prepared data",
  model: "model", table: "managed table", process: "process map", other: "",
};

function OutputRow({ item: i, run }: { item: Item; run?: Run }) {
  if (i.kind === "finding") {
    const f = i.finding;
    const cause = voidCause(f.verification_state);
    return (
      <>
        <span className="list-button-head"><span className="clamp-2"><strong>{f.code}</strong> {f.title}</span><span className="muted small">finding</span></span>
        <span className="chip-row">
          {f.verification_state ? <VerificationBadge state={f.verification_state} showCause={false} />
            : <StatusBadge status={f.verified ? "verified" : f.status} label={f.verified ? "verified" : f.status} />}
        </span>
        {cause && <span className="small void-cause">Why void: {cause}</span>}
        <ConfidenceBar value={f.confidence} />
        <span className="muted small clamp-1">{run?.objective ?? `Investigation ${f.run_id}`}</span>
      </>
    );
  }
  if (i.kind === "prepared") {
    return (
      <>
        <span className="list-button-head"><span className="clamp-1">{i.run.recipe_name}</span>
          <span className="muted small">prepared data · v{i.run.recipe_version}</span></span>
        <span className="chip-row"><StatusBadge status={i.run.status} /><span className="muted small">{fmtDate(i.run.finished_at ?? i.run.created_at)}</span></span>
      </>
    );
  }
  const a = i.artifact;
  return (
    <>
      <span className="list-button-head"><span className="clamp-1">{outputDisplayName(a)}</span>
        <span className="muted small">{TYPE_WORD[i.type] || a.type.replace(/_/g, " ")} · v{a.version}</span></span>
      <span className="chip-row"><StatusBadge status={a.status} />{a.platform && <span className="tag">{a.platform}</span>}
        <span className="muted small">{fmtDate(a.created_at)}</span></span>
      <span className="muted small clamp-1">{run?.objective ?? (a.run_id ? `Investigation ${a.run_id}` : "Workspace output")}</span>
    </>
  );
}

/** A materialized recipe run: its outputs, and a link back to the recipe in Work. */
function PreparedView({ run, wsId }: { run: RecipeRun; wsId: string }) {
  const outputs = Object.entries(run.outputs ?? {});
  return (
    <Card title={<>{run.recipe_name} <span className="muted small">· prepared data · v{run.recipe_version}</span></>} actions={<StatusBadge status={run.status} />}>
      <KeyValue items={[["Finished", fmtDate(run.finished_at)], ["Engine", run.engine ?? "—"], ["Outputs", String(outputs.length)]]} />
      {outputs.length > 0 && (
        <RecordTable records={outputs.map(([name, o]) => ({ output: name, ...(o && typeof o === "object" ? o as Record<string, unknown> : { value: o }) }))} />
      )}
      <p className="small"><Link to={to.work(wsId, "prepare", { recipe: run.recipe_id })}>Open the recipe in Investigations → Prepare data</Link></p>
      <TechnicalDetails value={run} />
    </Card>
  );
}

function ArtifactView({ id, wsId, role, onSelect }: { id: string; wsId: string; role: string | null; onSelect: (id: string) => void }) {
  const d = useAsync(() => api.getArtifact(id), [id]);
  const expert = useExpertView();
  if (d.error) return <ErrorBox error={d.error} onRetry={d.reload} />;
  if (!d.data) return <Loading />;
  const a = d.data;
  return (
    <div className="stack">
      <Card title={<>{outputDisplayName(a)} <span className="muted small">· {a.type.replace(/_/g, " ")} · v{a.version}</span></>} actions={<>
        <StatusBadge status={a.status} />
        {a.external_url && <a className="btn btn-sm" href={a.external_url} target="_blank" rel="noreferrer noopener">Open in {a.platform ?? "BI"} ↗</a>}
      </>}>
        <KeyValue items={[
          ["Created by", a.creator_agent ? `agent:${a.creator_agent}` : a.creator_user ? `user:${a.creator_user}` : "—"],
          ["Investigation", a.run_id ? <Link key="r" to={to.run(wsId, a.run_id)}>Open the investigation</Link> : "—"],
          ["Updated", fmtDate(a.updated_at)],
          ...(a.external_id ? [["External id", `${a.platform}:${a.external_id}`] as [string, string]] : []),
        ]} />
        <TechnicalDetails><p className="small">Content hash <code title={a.content_hash}>{shortHash(a.content_hash, 16)}</code></p></TechnicalDetails>
      </Card>
      <ArtifactContent a={a} wsId={wsId} role={role} onSelect={onSelect} />
      <Card title={`Versions (${a.versions.length})`}>
        <ul className="list compact">
          {a.versions.map((v) => (
            <li key={v.id} className="list-item">
              <span><strong>v{v.version}</strong></span>
              <span className="muted small">{v.created_by ?? "—"} · {fmtDate(v.created_at)}</span>
            </li>
          ))}
        </ul>
      </Card>
      {expert ? (
        <Card title="Lineage">
          <LineageGraph lineage={a.lineage} focus={{ type: a.type, id: a.id }}
            onSelect={(_, nodeId) => { if (nodeId.startsWith("art_")) onSelect(nodeId); }} />
        </Card>
      ) : (
        <details className="card at-a-glance">
          <summary>Where these numbers came from</summary>
          <div className="card-body">
            <LineageGraph lineage={a.lineage} focus={{ type: a.type, id: a.id }}
              onSelect={(_, nodeId) => { if (nodeId.startsWith("art_")) onSelect(nodeId); }} />
          </div>
        </details>
      )}
    </div>
  );
}

function ArtifactContent({ a, wsId, role, onSelect }: { a: ArtifactDetail; wsId: string; role: string | null; onSelect: (id: string) => void }) {
  const c = a.content as Record<string, unknown>;
  switch (a.type) {
    case "chart":
      return (
        <Card title={String(c.title ?? a.name)} actions={<span className="tag">{String(c.chart_type)}</span>}>
          {c.description ? <p className="muted small">{String(c.description)}</p> : null}
          <ChartView type={String(c.chart_type)} preview={c.preview as Preview} title={String(c.title ?? "")} height={320} />
          <KeyValue items={[
            ["Intent", String(c.intent ?? "—")], ["Metric", String(c.metric ?? "—")], ["Dimension", String(c.dimension ?? "—")],
            ["Series", String(c.series ?? "—")], ["Findings", ((c.insight_codes as string[]) ?? []).join(", ") || "—"], ["Rationale", String(c.rationale ?? "—")],
          ]} />
        </Card>
      );
    case "dashboard":
      if (c.schema_version === "dashboard.v2") return <DashboardBuilder artifactId={a.id} role={role} onSwitch={onSelect} />;  // S08 builder
      return <><PublishCard wsId={wsId} artifact={a} /><DashboardPreview wsId={wsId} artifact={a} /></>;
    case "report":
      return <ReportRow wsId={wsId} report={a} />;
    case "report_document":
      return <ReportBuilder docId={a.id} role={role} onSwitch={onSelect} />;  // CONV-04
    case "process_analysis":
      return <ProcessAnalysisView wsId={wsId} analysis={{ ...(a.content as unknown as ProcessAnalysis), artifact: { id: a.id, name: a.name, version: a.version } }} />;
    case "dataset":
      return (
        <Card title="Dataset definition">
          {c.description ? <p>{String(c.description)}</p> : null}
          <CodeBlock code={String(c.sql ?? "")} />
          <KeyValue items={[["Time column", String(c.time_column ?? "—")], ["Rows", fmtNumber(c.row_count)],
            ["Source assets", ((c.source_assets as string[]) ?? []).join(", ") || "—"]]} />
          {Array.isArray(c.columns) && <RecordTable records={c.columns as Record<string, unknown>[]} maxRows={100} />}
        </Card>
      );
    case "metric":
      return (
        <Card title={String(c.display_name ?? c.name ?? a.name)} actions={<StatusBadge status={String(c.status ?? "")} />}>
          <p>{String(c.definition ?? "")}</p>
          <p className="small">Metric definitions are edited in <Link to={to.data(wsId, "metrics")}>Data → Metrics</Link>.</p>
        </Card>
      );
    case "narrative":
      return <Card title="Narrative"><Markdown text={String(c.markdown ?? "")} /></Card>;
    case "profile": {
      const cols = Array.isArray(c.columns) ? (c.columns as Record<string, unknown>[]) : [];
      return (
        <Card title={`Profile — ${fmtNumber(c.row_count)} rows`}>
          <RecordTable maxRows={200} records={cols.map((col) => ({
            column: col.name, type: col.data_type, semantic: col.semantic_type, null_rate: fmtPct(col.null_rate),
            distinct: col.distinct, min: col.min, max: col.max, mean: col.mean,
          }))} />
          <TechnicalDetails value={c} label="Full profile" />
        </Card>
      );
    }
    case "quality_report": {
      const issues = Array.isArray(c.issues) ? (c.issues as Record<string, unknown>[]) : [];
      return (
        <Card title={`Quality report — ${issues.length} issues`}>
          <RecordTable maxRows={200} records={issues.map((i) => ({ severity: i.severity, code: i.code, asset: i.asset, column: i.column, message: i.message }))} />
          <TechnicalDetails value={c} label="Full report" />
        </Card>
      );
    }
    default:
      return <Card title="Content"><TechnicalDetails value={c} label="Content" /></Card>;
  }
}
