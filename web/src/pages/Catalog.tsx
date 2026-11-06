import { Fragment, Suspense, useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { lazyWithReload } from "../lib/lazyReload";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { to, type DataTab } from "../routes";
import { api, type CatalogAsset, type CatalogColumn, type Relationship } from "../api";
import { ColumnCurationForm } from "../components/ColumnCuration";
import { KpiEditor } from "../components/KpiEditor";
import { ColumnProfilePanel, hasProfile, profileLine, profileMetaText } from "../components/ProfileView";
import { ConfidenceBar, EmptyState, ErrorBox, Field, Loading, Notice, PageHeader, StatusBadge, Tag, Card, Tabs } from "../components/ui";
import { fmtDate, fmtNumber, fmtPct } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { useExpertView } from "../lib/expert";

// The studio's other tabs load on demand (the force-graph renderer among them), keeping the main bundle small.
const KnowledgeDocs = lazyWithReload(() => import("../components/KnowledgeDocs").then((m) => ({ default: m.KnowledgeDocs })));
const ContextGrounding = lazyWithReload(() => import("../components/ContextGrounding").then((m) => ({ default: m.ContextGrounding })));
const ReviewQueue = lazyWithReload(() => import("../components/ReviewQueue").then((m) => ({ default: m.ReviewQueue })));
const DefinitionsPanel = lazyWithReload(() => import("../components/DefinitionsPanel").then((m) => ({ default: m.DefinitionsPanel })));
const AnalysisContexts = lazyWithReload(() => import("../components/AnalysisContexts").then((m) => ({ default: m.AnalysisContexts })));
const KnowledgeTransfer = lazyWithReload(() => import("../components/KnowledgeTransfer").then((m) => ({ default: m.KnowledgeTransfer })));
const BriefPanel = lazyWithReload(() => import("../components/Brief").then((m) => ({ default: m.BriefPanel })));
const ReadinessPanel = lazyWithReload(() => import("../components/Brief").then((m) => ({ default: m.ReadinessPanel })));

/** Who wrote a description: the source system, a rule, a model, or a person (a person always wins). */
export const ORIGIN_LABELS: Record<string, { label: string; tone: string; title: string }> = {
  source: { label: "source", tone: "info", title: "Comment from the source system" },
  rule: { label: "rule", tone: "neutral", title: "Written by the deterministic catalog rules" },
  model: { label: "model", tone: "jev", title: "Filled in by a model where rules were not confident" },
  user: { label: "user", tone: "success", title: "Curated by a person — never overwritten by the crawler" },
};

export function OriginBadge({ origin }: { origin: string | null | undefined }) {
  if (!origin) return null;
  const o = ORIGIN_LABELS[origin] ?? { label: origin, tone: "neutral", title: origin };
  return <span className={`tag tag-${o.tone}`} title={o.title}>{o.label}</span>;
}

const EDITOR_ROLES = new Set(["editor", "owner"]);

type StudioTab = DataTab;
const TABS: { id: StudioTab; label: string }[] = [
  { id: "catalog", label: "Catalog" }, { id: "brief", label: "Brief & readiness" }, { id: "documents", label: "Documents" },
  { id: "grounding", label: "Context & mappings" },
  { id: "contexts", label: "Analysis contexts" }, { id: "review", label: "Review queue" }, { id: "metrics", label: "Metrics" }, { id: "definitions", label: "Definitions" },
  { id: "transfer", label: "Import & export" },
];
/** Tabs for data engineers, shown with expert tools on (or when a link opens one directly). */
const EXPERT_TABS = new Set<StudioTab>(["grounding", "contexts", "transfer"]);

/**
 * Data → Catalog & definitions (spec v4 §15): one screen, seven tabs. The crawled catalog; the
 * workspace brief (facts, open questions, glossary aliases) and readiness checks (P4-04); the
 * workspace's knowledge documents; the review queue of drafted descriptions; metrics (their one
 * home); definitions (joins to confirm, data model changes, versioned definitions and the graph);
 * and bundle import/export.
 */
export function CatalogPage() {
  const { wsId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const tab = (TABS.some((t) => t.id === params.get("tab")) ? params.get("tab") : "catalog") as StudioTab;
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const expert = useExpertView();
  const shownTabs = TABS.filter((t) => expert || !EXPERT_TABS.has(t.id) || t.id === tab);
  const canEdit = EDITOR_ROLES.has(ws.data?.role ?? "");
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
  const switchTab = (t: StudioTab) => setParams(t === "catalog" ? {} : { tab: t });

  return (
    <div className="page">
      <PageHeader title="Data dictionary"
        subtitle={<>What your data means: tables and columns, business terms, metrics and documents. Connect data
          under <Link to={to.sources(wsId)}>Data sources</Link>.</>} />
      <div className="catalog-section-picker"><Field label="Choose a data section" htmlFor="catalog-section"><select id="catalog-section" value={tab} onChange={e=>switchTab(e.target.value as StudioTab)}>{shownTabs.map(t=><option key={t.id} value={t.id}>{t.label}</option>)}</select></Field></div>
      <div className="catalog-section-tabs"><Tabs value={tab} onChange={switchTab} tabs={shownTabs} /></div>
      <div className="tab-panel">
        <Suspense fallback={<Loading />}>
          {tab === "catalog" && <CatalogTab wsId={wsId} canEdit={canEdit} />}
          {tab === "grounding" && <ContextGrounding wsId={wsId} canEdit={canEdit} />}
          {tab === "contexts" && <AnalysisContexts wsId={wsId} canEdit={canEdit} />}
          {tab === "brief" && (
            <div className="stack">
              <BriefPanel wsId={wsId} role={ws.data?.role} />
              <ReadinessPanel wsId={wsId} />
            </div>
          )}
          {tab === "documents" && <KnowledgeDocs wsId={wsId} params={params} set={set} />}
          {tab === "review" && <ReviewQueue wsId={wsId} canDecide={canEdit}
            onOpenDocument={(path) => setParams({ tab: "documents", path })} />}
          {tab === "definitions" && <DefinitionsPanel wsId={wsId} role={ws.data?.role} />}
          {tab === "metrics" && <KpiEditor wsId={wsId} selected={params.get("kpi")} onSelect={(k) => set({ kpi: k })} />}
          {tab === "transfer" && <KnowledgeTransfer wsId={wsId} canEdit={canEdit}
            onBrowse={(packId) => setParams({ tab: "documents", pack: packId })} />}
        </Suspense>
      </div>
    </div>
  );
}

function CatalogTab({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const [draftQ, setDraftQ] = useState("");
  const [q, setQ] = useState("");
  const [domain, setDomain] = useState("");
  const [role, setRole] = useState("");
  const [deprecated, setDeprecated] = useState(false);
  const list = useAsync(() => api.catalog(wsId, { q, domain, role, include_deprecated: deprecated }), [wsId, q, domain, role, deprecated]);
  // Option lists grow from everything seen so far, so a filter never hides its own alternatives.
  const [known, setKnown] = useState<{ domains: string[]; roles: string[] }>({ domains: [], roles: [] });
  useEffect(() => {
    if (!list.data) return;
    setKnown((k) => ({
      domains: [...new Set([...k.domains, ...list.data!.map((a) => a.domain).filter((x): x is string => !!x)])].sort(),
      roles: [...new Set([...k.roles, ...list.data!.map((a) => a.role).filter((x): x is string => !!x)])].sort(),
    }));
  }, [list.data]);
  const [open, setOpen] = useState<string | null>(null);
  // Joins load with the first opened table and are shared by every row (one request per workspace).
  const rels = useAsync(() => (open ? api.relationships(wsId) : Promise.resolve(null)), [wsId, open !== null]);

  const search = (e: FormEvent) => {
    e.preventDefault();
    setQ(draftQ.trim());
  };
  const onCurated = (a: Partial<CatalogAsset> & { id: string }) =>
    list.setData((prev) => prev?.map((x) => (x.id === a.id ? { ...x, ...a } : x)));

  return (
    <div className="stack">
      <p className="muted small">Every crawled table with its business meaning, role, grain and sensitive columns. Select a table in Sources and run profiling to see measured column details.</p>
      <Card>
        <form className="form-inline catalog-filters" onSubmit={search} role="search" aria-label="Search the catalog">
          <label className="inline-field">
            <span className="sr-only">Search</span>
            <input type="search" value={draftQ} onChange={(e) => setDraftQ(e.target.value)} placeholder="Search tables, columns, descriptions…"
              aria-label="Search tables, columns and descriptions" />
          </label>
          <button type="submit" className="btn btn-sm btn-primary">Search</button>
          <label className="inline-field small">Domain
            <select value={domain} onChange={(e) => setDomain(e.target.value)} aria-label="Domain">
              <option value="">All</option>
              {known.domains.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <label className="inline-field small">Role
            <select value={role} onChange={(e) => setRole(e.target.value)} aria-label="Role">
              <option value="">All</option>
              {known.roles.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label className="toggle small">
            <input type="checkbox" checked={deprecated} onChange={(e) => setDeprecated(e.target.checked)} /> Include deprecated
          </label>
        </form>
      </Card>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && (
        <EmptyState title={q || domain || role ? "No tables match" : "The catalog is empty"}>
          {q || domain || role ? "Try a broader search." : "Crawl a source to populate it."}
        </EmptyState>
      )}
      {!!list.data?.length && (
        <Card title={`${list.data.length} table${list.data.length === 1 ? "" : "s"}`}>
          <div className="table-wrap" data-tour="catalog-table">
            <table className="table">
              <thead>
                <tr><th>Table</th><th>Role</th><th>Domain</th><th>Grain</th><th>Confidence</th><th>Description</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {list.data.map((a) => (
                  <Fragment key={a.id}>
                    <tr className={open === a.id ? "row-active" : undefined}>
                      <td><code>{a.fq}</code>{a.business_name && <div className="small">{a.business_name}</div>}
                        <div className="muted small">{fmtNumber(a.row_count)} rows · {a.columns.length} columns</div></td>
                      <td className="small">{a.role ?? <span className="muted">—</span>}</td>
                      <td className="small">{a.domain ?? <span className="muted">—</span>}</td>
                      <td className="small">{a.grain ?? <span className="muted">—</span>}</td>
                      <td>{a.confidence != null ? <ConfidenceBar value={a.confidence} /> : <span className="muted">—</span>}</td>
                      <td className="small"><span className="clamp-2">{a.description ?? <span className="muted">No description</span>}</span>
                        <OriginBadge origin={a.description ? a.description_origin : null} /></td>
                      <td>
                        <div className="chip-row">
                          {a.reviewed ? <Tag tone="success">reviewed</Tag> : <Tag>unreviewed</Tag>}
                          {a.lifecycle !== "active" && <StatusBadge status={a.lifecycle === "deprecated" ? "superseded" : a.lifecycle} label={a.lifecycle} />}
                          {a.selected && <Tag tone="info">selected</Tag>}
                        </div>
                      </td>
                      <td>
                        <button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === a.id} aria-controls={`cat-${a.id}`}
                          onClick={() => setOpen((o) => (o === a.id ? null : a.id))}>
                          {open === a.id ? "Hide" : `Columns (${a.columns.length})`}
                        </button>
                      </td>
                    </tr>
                    {open === a.id && (
                      <tr className="row-detail" id={`cat-${a.id}`}>
                        <td colSpan={8}>
                          <AssetCuration asset={a} canEdit={canEdit} onCurated={onCurated} />
                          <AssetRelationships fq={a.fq} rels={rels.data ?? null} error={rels.error} onRetry={rels.reload} />
                          {profileMetaText(a.profile_meta, fmtDate) &&
                            <p className="muted small">{profileMetaText(a.profile_meta, fmtDate)}</p>}
                          <CatalogColumns columns={a.columns} rowCount={a.profile_meta?.rows_profiled}
                            curate={canEdit ? { wsId, assetId: a.id, fq: a.fq } : undefined} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

export function AssetCuration({ asset, canEdit, onCurated }: {
  asset: CatalogAsset; canEdit: boolean; onCurated: (a: Partial<CatalogAsset> & { id: string }) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [businessName, setBusinessName] = useState(asset.business_name ?? "");
  const [description, setDescription] = useState(asset.description ?? "");
  const [reviewed, setReviewed] = useState(asset.reviewed);
  const [guidelines, setGuidelines] = useState(asset.guidelines?.text ?? "");
  const act = useAction();

  const begin = () => {
    setBusinessName(asset.business_name ?? "");
    setDescription(asset.description ?? "");
    setReviewed(asset.reviewed);
    setGuidelines(asset.guidelines?.text ?? "");
    setEditing(true);
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const body: { business_name?: string; description?: string; reviewed?: boolean; guidelines?: string } = {};
    if (businessName !== (asset.business_name ?? "")) body.business_name = businessName;
    if (description !== (asset.description ?? "")) body.description = description;
    if (reviewed !== asset.reviewed) body.reviewed = reviewed;
    if (guidelines !== (asset.guidelines?.text ?? "")) body.guidelines = guidelines;
    if (!Object.keys(body).length) {
      setEditing(false);
      return;
    }
    const r = await act.run(() => api.curateAsset(asset.id, body));
    if (r) {
      onCurated({ id: asset.id, business_name: r.business_name, description: r.description, description_origin: r.description_origin,
        reviewed: r.reviewed, guidelines: r.guidelines ?? null });
      setEditing(false);
    }
  };
  const markReviewed = async () => {
    const r = await act.run(() => api.curateAsset(asset.id, { reviewed: !asset.reviewed }));
    if (r) onCurated({ id: asset.id, reviewed: r.reviewed });
  };

  return (
    <div className="stack curation">
      {asset.description && (
        <p className="small">{asset.description} <OriginBadge origin={asset.description_origin} /></p>
      )}
      {asset.guidelines?.text && (
        <div className="small">
          <strong>Guidelines for agents</strong>
          <p className="msg-body">{asset.guidelines.text}</p>
          {asset.guidelines.at && <p className="muted small">Written by a person, {fmtDate(asset.guidelines.at)}.</p>}
        </div>
      )}
      <p className="muted small">Last crawled {fmtDate(asset.last_crawled_at)}.</p>
      {canEdit && !editing && (
        <div className="chip-row">
          <button type="button" className="btn btn-sm" onClick={begin}>Edit description</button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => void markReviewed()} disabled={act.busy}>
            {asset.reviewed ? "Mark unreviewed" : "Mark reviewed"}
          </button>
        </div>
      )}
      {!canEdit && <p className="muted small">Editors and owners can curate names and descriptions.</p>}
      {editing && (
        <form className="form" onSubmit={save} aria-label={`Curate ${asset.fq}`}>
          <Notice tone="info">A person's curation always wins: once you edit a description, crawler rules and models never overwrite it.</Notice>
          <Field label="Business name" htmlFor={`${id}-bn`}>
            <input id={`${id}-bn`} value={businessName} maxLength={200} onChange={(e) => setBusinessName(e.target.value)} />
          </Field>
          <Field label="Description" htmlFor={`${id}-desc`}>
            <textarea id={`${id}-desc`} rows={3} value={description} maxLength={4000} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          <Field label="Guidelines for agents" htmlFor={`${id}-guide`}
            hint="Notes an agent should follow when it reads this table, for example which date column means what. Sent with the table's context; people only.">
            <textarea id={`${id}-guide`} rows={3} value={guidelines} maxLength={4000} onChange={(e) => setGuidelines(e.target.value)} />
          </Field>
          <label className="toggle small">
            <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} /> Reviewed
          </label>
          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Saving…" : "Save"}</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error} />
    </div>
  );
}

/** Where a join came from, in words a reader can check. */
const RELATIONSHIP_ORIGINS: Record<string, string> = {
  declared: "Declared by the source system (a foreign key)",
  discovered: "Found by measuring the data (containment and key uniqueness)",
  review: "Confirmed by a person in the review queue",
  user: "Added by a person",
  context: "From workspace context",
};

/**
 * The joins a table takes part in (review P1-4): where each one came from, its measured confidence and, once
 * validated, the exact join as a runnable statement, the same one `context.lookup` hands to agents.
 */
export function AssetRelationships({ fq, rels, error, onRetry }: {
  fq: string; rels: Relationship[] | null; error?: string | null; onRetry?: () => void;
}) {
  if (error) return <ErrorBox error={error} onRetry={onRetry} />;
  if (!rels) return null;
  const mine = rels.filter((r) => r.from_asset === fq || r.to_asset === fq)
    .sort((a, b) => Number(b.validated) - Number(a.validated) || b.confidence - a.confidence || a.id.localeCompare(b.id));
  if (!mine.length) return <p className="muted small">No joins recorded for this table yet.</p>;
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        <caption className="sr-only">{`Joins of ${fq}`}</caption>
        <thead><tr><th>Join</th><th>Origin</th><th>Confidence</th><th>Status</th><th>Join SQL</th></tr></thead>
        <tbody>
          {mine.map((r) => {
            const fromCols = (r.evidence?.from_columns as string[] | undefined) ?? [r.from_column];
            const toCols = (r.evidence?.to_columns as string[] | undefined) ?? [r.to_column];
            return (
              <tr key={r.id}>
                <td className="small"><code>{r.from_asset}.{fromCols.join(", ")}</code> → <code>{r.to_asset}.{toCols.join(", ")}</code>
                  <div className="muted small">{r.cardinality.replace(/_/g, " ")}</div></td>
                <td className="small"><span className="tag tag-info" title={RELATIONSHIP_ORIGINS[r.origin] ?? r.origin}>{r.origin}</span></td>
                <td><ConfidenceBar value={r.confidence} /></td>
                <td>{r.validated ? <Tag tone="success">validated</Tag> : <Tag>not validated</Tag>}</td>
                <td className="small">{r.join_sql ? <pre><code>{r.join_sql}</code></pre> : <span className="muted">Shown once validated</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The columns of a table; with `curate`, an editor curates each column's tags, unit and glossary alias
 * in place (P4-07). The one home of column curation is here, in Data.
 */
export function CatalogColumns({ columns, curate, rowCount }: { columns: CatalogColumn[]; rowCount?: number | null;
  curate?: { wsId: string; assetId: string; fq: string } }) {
  const [open, setOpen] = useState<string | null>(null);
  const [profileOpen, setProfileOpen] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  if (!columns.length) return <p className="muted small">No columns recorded.</p>;
  return (
    <div className="table-wrap" tabIndex={curate ? 0 : undefined}>
      {saved && <Notice tone="success">{saved}</Notice>}
      <table className="table table-compact">
        <caption className="sr-only">Columns</caption>
        <thead><tr><th>Column</th><th>Type</th><th>Role</th><th>Unit</th><th>Tags</th><th>PII</th><th>Glossary</th><th>Description</th>
          {curate && <th><span className="sr-only">Curate</span></th>}</tr></thead>
        <tbody>
          {columns.map((c) => (
            <Fragment key={c.name}>
            <tr>
              <td><code>{c.name}</code>{c.business_name && <div className="muted small">{c.business_name}</div>}
                {hasProfile(c.profile) && <div><button type="button" className="btn btn-xs btn-ghost"
                  aria-expanded={profileOpen === c.name} onClick={() => setProfileOpen((old) => old === c.name ? null : c.name)}>
                  {profileOpen === c.name ? "Hide profile" : "View profile"}</button></div>}
              </td>
              <td className="small">{c.data_type}</td>
              <td className="small">{c.role ?? <span className="muted">—</span>}</td>
              <td className="small">{c.unit ?? <span className="muted">—</span>}</td>
              <td>
                {c.tags.length ? c.tags.map((t) => <Tag key={t} tone={t === "pii" || t === "restricted" ? "danger" : "warning"}>{t}</Tag>) : <span className="muted">—</span>}
                {c.tags.length > 0 && <div className="muted small" title="Crawler tags only ever tighten; owner tags are never removed.">by {c.tags_origin}</div>}
              </td>
              <td className="small">
                {c.pii?.category ? (
                  <span title={(c.pii.reasons ?? []).join("; ")}>{c.pii.category} <span className="muted">({c.pii.sensitivity}, {fmtPct(c.pii.confidence, 0)})</span></span>
                ) : <span className="muted">—</span>}
              </td>
              <td className="small">
                {c.glossary ? (
                  <span className="tag tag-info" title={c.glossary.reason ?? `glossary term ${c.glossary.term_id}`}>
                    {c.glossary.term ?? c.glossary.term_id}
                  </span>
                ) : <span className="muted">—</span>}
              </td>
              <td className="small clamp-2">{c.description ?? <span className="muted">—</span>}</td>
              {curate && (
                <td><button type="button" className="btn btn-xs btn-ghost" aria-expanded={open === c.name}
                  onClick={() => { setSaved(null); setOpen((o) => (o === c.name ? null : c.name)); }} aria-label={`Curate ${c.name}`}>Curate</button></td>
              )}
            </tr>
            {profileOpen === c.name && hasProfile(c.profile) && (
              <tr className="row-detail"><td colSpan={curate ? 9 : 8}>
                <strong>{c.business_name || c.name}</strong>
                <p className="muted small">{profileLine(c.profile, rowCount)}</p>
                <ColumnProfilePanel profile={c.profile}
                  sensitive={c.tags.some((t) => ["pii", "restricted", "sensitive"].includes(t))} rowCount={rowCount} />
              </td></tr>
            )}
            {curate && open === c.name && (
              <tr className="row-detail"><td colSpan={9}>
                <ColumnCurationForm wsId={curate.wsId} assetId={curate.assetId} fq={curate.fq} column={c} onCancel={() => setOpen(null)}
                  onDone={(msg) => { setOpen(null); setSaved(msg); }} />
              </td></tr>
            )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
