import { useId, useMemo, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { to } from "../routes";
import { api, type Asset, type Dict, type DiscoveredAsset, type Source, type SourceColumn, type SourceUpdatePatch } from "../api";
import { CrawlPanel } from "../components/CrawlPanel";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, PageHeader, StatusBadge, Tag, TechnicalDetails } from "../components/ui";
import { crawlStatsSummary } from "../lib/crawls";
import { fmtDate, fmtNumber, fmtPct, fmtValue } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import {
  NUMBER_FIELDS, buildSourceConfig, defaultKind, executionModeText, fieldHint, fieldLabel, groupKinds, suggestedSecretRef,
  validateSourceForm, valuesFromConfig, type SourceFormErrors,
} from "../lib/sourceKinds";

/** Same key/value pairs regardless of key order (source config has no nested objects). */
function configEqual(a: Dict, b: Dict): boolean {
  const norm = (x: Dict) => JSON.stringify(Object.keys(x).sort().map((k) => [k, x[k]]));
  return norm(a) === norm(b);
}
const TOGGLE_TAGS = ["pii", "restricted", "sensitive"] as const;

export function SourcesPage() {
  const { wsId = "" } = useParams();
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const assets = useAsync(() => api.listAssets(wsId), [wsId]);
  const rels = useAsync(() => api.relationships(wsId), [wsId]);
  const [showAdd, setShowAdd] = useState(false);
  const [activeAsset, setActiveAsset] = useState<string | null>(null);

  const reloadAll = () => {
    void sources.reload();
    void assets.reload();
    void rels.reload();
  };
  const asset = assets.data?.find((a) => a.id === activeAsset) ?? null;

  return (
    <div className="page">
      <PageHeader title="Data sources"
        subtitle={<>Connect a database, warehouse, application or files, and choose which tables may be read. What the data means is
          kept in the <Link to={to.catalog(wsId)}>Data dictionary</Link>.</>}
        actions={<button type="button" className="btn btn-primary" onClick={() => setShowAdd((s) => !s)}>{showAdd ? "Close" : "Add source"}</button>} />
      {showAdd && <AddSource wsId={wsId} onAdded={() => { setShowAdd(false); reloadAll(); }} />}
      <ErrorBox error={sources.error} onRetry={sources.reload} />
      {sources.loading && !sources.data && <Loading />}
      {sources.data?.length === 0 && <EmptyState title="No sources yet">Add a database, warehouse, file or ServiceNow instance.</EmptyState>}
      <div className="stack" data-tour="source-list">
        {sources.data?.map((s) => (
          <SourceCard key={s.id} wsId={wsId} source={s} assets={(assets.data ?? []).filter((a) => a.source_id === s.id)}
            onChanged={reloadAll} onOpenAsset={setActiveAsset} activeAsset={activeAsset} />
        ))}
      </div>
      {asset && <AssetDetail asset={asset} onTagged={(col) => assets.setData((prev) => prev?.map((a) => a.id !== asset.id ? a : {
        ...a, columns: a.columns.map((c) => (c.name === col.name ? { ...c, tags: col.tags } : c)),
      }))} onClose={() => setActiveAsset(null)} />}
      <Card title="Relationships">
        <ErrorBox error={rels.error} />
        {rels.data?.length === 0 && <EmptyState title="No relationships discovered yet">They are found by metadata discovery and profiling.</EmptyState>}
        {!!rels.data?.length && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>From</th><th>To</th><th>Cardinality</th><th>Confidence</th><th>Validated</th><th>Origin</th></tr></thead>
              <tbody>
                {rels.data.map((r) => (
                  <tr key={r.id}>
                    <td><code>{r.from_asset}.{r.from_column}</code></td>
                    <td><code>{r.to_asset}.{r.to_column}</code></td>
                    <td>{r.cardinality}</td>
                    <td className="num">{fmtPct(r.confidence, 0)}</td>
                    <td>{r.validated ? <StatusBadge status="validated" /> : <span className="muted">no</span>}</td>
                    <td>{r.origin}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

export function SourceCard({ wsId, source, assets, onChanged, onOpenAsset, activeAsset }: {
  wsId: string; source: Source; assets: Asset[]; onChanged: () => void; onOpenAsset: (id: string) => void; activeAsset: string | null;
}) {
  const [editing, setEditing] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredAsset[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set(assets.filter((a) => a.selected).map((a) => a.name)));
  const [dirty, setDirty] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [crawlKey, setCrawlKey] = useState(0);
  const discoverAct = useAction();
  const selectAct = useAction();

  // Keep selection in sync with server state until the user edits it.
  const serverSelected = useMemo(() => assets.filter((a) => a.selected).map((a) => a.name).sort().join("|"), [assets]);
  const [lastServer, setLastServer] = useState(serverSelected);
  if (!dirty && serverSelected !== lastServer) {
    setLastServer(serverSelected);
    setSelected(new Set(serverSelected ? serverSelected.split("|") : []));
  }

  const names = useMemo(() => {
    const byName = new Map<string, { name: string; rows: number | null; cols: number; description: string | null }>();
    for (const a of assets) byName.set(a.name, { name: a.name, rows: a.row_count, cols: a.columns.length, description: a.description ?? a.business_name });
    for (const d of discovered ?? []) if (!byName.has(d.name)) byName.set(d.name, { name: d.name, rows: d.row_count, cols: d.columns.length, description: d.description });
    return [...byName.values()].sort((a, b) => a.name.localeCompare(b.name));
  }, [assets, discovered]);

  const discover = async () => {
    const r = await discoverAct.run(() => api.discover(wsId, source.id));
    if (r) {
      setDiscovered(r.assets);
      const summary = crawlStatsSummary(r.stats);
      setResult(`Discovered ${r.assets.length} assets${summary ? ` (${summary})` : ""}.`);
      setCrawlKey((k) => k + 1);
      onChanged();
    }
  };
  const save = async () => {
    const r = await selectAct.run(() => api.selectAssets(wsId, source.id, [...selected]));
    if (r) {
      setDirty(false);
      setResult(`Selected ${r.selected.length} assets${r.loaded.length ? `; loaded ${r.loaded.length} into staging` : ""}.`);
      onChanged();
    }
  };
  const toggle = (name: string) => {
    setDirty(true);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <Card title={<>{source.name} <span className="muted small">· {source.kind} · {source.execution_mode}</span></>}
      actions={<>
        <StatusBadge status={source.status} />
        {!editing && <button type="button" className="btn btn-sm btn-ghost" onClick={() => setEditing(true)}>Edit</button>}
        <button type="button" className="btn btn-sm" onClick={discover} disabled={discoverAct.busy}>{discoverAct.busy ? "Discovering…" : "Discover"}</button>
      </>}>
      {editing && <EditSource wsId={wsId} source={source} onClose={() => setEditing(false)}
        onSaved={() => { setEditing(false); onChanged(); }} />}
      <p className="muted small">Last discovered {fmtDate(source.last_discovered_at)}</p>
      {(source.secret_ref || source.staging_schema) && (
        <TechnicalDetails label="Connection details">
          <p className="small">
            {source.secret_ref ? <>Credentials read from <code>{source.secret_ref}</code> (never stored). </> : null}
            {source.staging_schema ? <>Staged copy in schema <code>{source.staging_schema}</code>.</> : null}
          </p>
        </TechnicalDetails>
      )}
      {source.last_error && <Notice tone="danger">{source.last_error}</Notice>}
      <ErrorBox error={discoverAct.error ?? selectAct.error} />
      {result && <Notice tone="success">{result}</Notice>}
      {names.length === 0 ? <EmptyState title="No assets discovered">Press Discover to list tables.</EmptyState> : (
        <>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th>Asset</th><th className="num">Rows</th><th className="num">Columns</th><th>Description</th><th><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>
                {names.map((n) => {
                  const a = assets.find((x) => x.name === n.name);
                  return (
                    <tr key={n.name} className={a && a.id === activeAsset ? "row-active" : undefined}>
                      <td><input type="checkbox" aria-label={`Select ${n.name}`} checked={selected.has(n.name)} onChange={() => toggle(n.name)} /></td>
                      <td><code>{a?.fq ?? n.name}</code></td>
                      <td className="num">{fmtNumber(n.rows)}</td>
                      <td className="num">{n.cols}</td>
                      <td className="muted small clamp-1">{n.description ?? ""}</td>
                      <td>{a && <button type="button" className="btn btn-xs btn-ghost" onClick={() => onOpenAsset(a.id)}>Columns</button>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="form-actions">
            <span className="muted small">{selected.size} selected{dirty ? " (unsaved)" : ""}</span>
            <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={selectAct.busy || selected.size === 0}>
              {selectAct.busy ? "Saving & loading…" : "Save selection"}
            </button>
          </div>
        </>
      )}
      <CrawlPanel wsId={wsId} source={source} refreshKey={crawlKey} onFinished={onChanged} />
    </Card>
  );
}

/**
 * Rename a source or correct its connection: host, port, tables and other connection fields, and the
 * secret reference. Only the fields actually changed are sent (the server rejects an edit with nothing
 * to change). The kind is fixed here — a different kind is a different connector, so it needs a new
 * source; move table selection over afterward.
 */
function EditSource({ wsId, source, onClose, onSaved }: { wsId: string; source: Source; onClose: () => void; onSaved: () => void }) {
  const id = useId();
  const kinds = useAsync(() => api.sourceKinds(), []);
  const kind = kinds.data?.find((k) => k.kind === source.kind) ?? null;
  const [name, setName] = useState(source.name);
  const [values, setValues] = useState<Record<string, string> | null>(null);
  const [secretRef, setSecretRef] = useState(source.secret_ref ?? "");
  const [errors, setErrors] = useState<SourceFormErrors>({});
  const act = useAction();

  // Seed the form from the source's own config once the kind spec (which fields it has) is known.
  if (kind && values === null) setValues(valuesFromConfig(kind, source.config));

  const setValue = (f: string, v: string) => setValues((prev) => ({ ...prev, [f]: v }));
  const err = (k: string) => errors[k] && <div className="field-error" role="alert">{errors[k]}</div>;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!kind || !values) return;
    const errs = validateSourceForm(kind, name, values, kind.secret_field ? secretRef : "");
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const patch: SourceUpdatePatch = {};
    if (name.trim() !== source.name) patch.name = name.trim();
    const config = buildSourceConfig(kind, values);
    if (!configEqual(config, source.config)) patch.config = config;
    const ref = secretRef.trim();
    if (kind.secret_field && ref !== (source.secret_ref ?? "")) {
      if (ref) patch.secret_ref = ref;
      else patch.clear_secret_ref = true;
    }
    if (Object.keys(patch).length === 0) {
      setErrors({ name: "Nothing changed yet." });
      return;
    }
    const ok = await act.run(() => api.updateSource(wsId, source.id, patch));
    if (ok) onSaved();
  };

  const fieldInput = (f: string, required: boolean) => (
    <Field key={f} label={`${fieldLabel(f)}${required ? " *" : ""}`} htmlFor={`${id}-${f}`} hint={fieldHint(f)}>
      <input id={`${id}-${f}`} value={values?.[f] ?? ""} onChange={(e) => setValue(f, e.target.value)} aria-invalid={!!errors[f]}
        aria-required={required || undefined} inputMode={NUMBER_FIELDS.has(f) ? "numeric" : undefined} autoComplete="off" spellCheck={false} />
      {err(f)}
    </Field>
  );

  return (
    <Card title="Edit source" className="edit-source">
      <ErrorBox error={kinds.error} onRetry={kinds.reload} />
      {kinds.loading && !kind && <Loading label="Loading connection fields…" />}
      {kind && values && (
        <form className="form" onSubmit={submit} noValidate aria-label={`Edit ${source.name}`}>
          <Notice tone="info">
            Kind: {kind.label} · dialect <code>{kind.dialect}</code>. To connect with a different kind, add a new source instead.
            {source.status !== "registered" && <> Changing the connection here marks it <strong>registered</strong> again until you discover it.</>}
          </Notice>
          <Field label="Name *" htmlFor={`${id}-name`}>
            <input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} aria-invalid={!!errors.name} />
            {err("name")}
          </Field>
          {kind.required.length > 0 && <div className="form-row">{kind.required.map((f) => fieldInput(f, true))}</div>}
          {kind.optional.length > 0 && (
            <details className="optional-fields">
              <summary className="small">Optional settings ({kind.optional.length})</summary>
              <div className="form-row">{kind.optional.map((f) => fieldInput(f, false))}</div>
            </details>
          )}
          {kind.secret_field ? (
            <Field label={`Secret reference (${kind.secret_field})`} htmlFor={`${id}-secret`}
              hint={<>A reference, never the secret itself: <code>env:NAME</code> or <code>file:/path</code>. Clear it to remove the reference
                (refused if this kind needs one).</>}>
              <input id={`${id}-secret`} value={secretRef} onChange={(e) => setSecretRef(e.target.value)} placeholder="env:NAME"
                autoComplete="off" spellCheck={false} aria-invalid={!!errors.secret_ref} />
              {err("secret_ref")}
            </Field>
          ) : <p className="muted small">This kind needs no credential.</p>}
          <ErrorBox error={act.error} />
          <div className="form-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Saving…" : "Save"}</button>
          </div>
        </form>
      )}
    </Card>
  );
}

function AssetDetail({ asset, onTagged, onClose }: { asset: Asset; onTagged: (c: SourceColumn) => void; onClose: () => void }) {
  const act = useAction();
  const toggleTag = async (col: SourceColumn, tag: string) => {
    const tags = col.tags.includes(tag) ? col.tags.filter((t) => t !== tag) : [...col.tags, tag];
    const updated = await act.run(() => api.tagColumn(asset.id, col.name, tags));
    if (updated) onTagged(updated);
  };
  return (
    <Card title={<>Columns of <code>{asset.fq}</code></>} actions={<button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>Close</button>}>
      <p className="muted small">{fmtNumber(asset.row_count)} rows · source table <code>{asset.source_name}</code>. PII and restricted columns are
        withheld from agents and SQL according to workspace policy.</p>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table table-compact">
          <thead>
            <tr><th>Column</th><th>Type</th><th>Semantic</th><th>Tags</th><th className="num">Null rate</th><th className="num">Distinct</th><th>Top values</th><th>Sensitivity</th></tr>
          </thead>
          <tbody>
            {asset.columns.map((c) => (
              <tr key={c.name}>
                <td><code>{c.name}</code>{c.is_key && <Tag tone="info">key</Tag>}{c.business_name && <div className="muted small">{c.business_name}</div>}</td>
                <td className="small">{c.data_type}</td>
                <td>{c.semantic_type ?? <span className="muted">—</span>}</td>
                <td>{c.tags.length ? c.tags.map((t) => <Tag key={t} tone={t === "pii" || t === "restricted" ? "danger" : "warning"}>{t}</Tag>) : <span className="muted">—</span>}</td>
                <td className="num">{c.profile?.null_rate !== undefined ? fmtPct(c.profile.null_rate) : "—"}</td>
                <td className="num">{c.profile?.distinct !== undefined ? fmtNumber(c.profile.distinct) : "—"}</td>
                <td className="small">
                  {(c.profile?.top_values ?? []).slice(0, 3).map((tv, i) => (
                    <div key={i} className="clamp-1">{fmtValue(tv.value)} <span className="muted">({fmtNumber(tv.count)})</span></div>
                  ))}
                  {!c.profile?.top_values?.length && <span className="muted">not profiled</span>}
                </td>
                <td>
                  <div className="toggle-group">
                    {TOGGLE_TAGS.map((t) => (
                      <label key={t} className="toggle small">
                        <input type="checkbox" checked={c.tags.includes(t)} disabled={act.busy} onChange={() => toggleTag(c, t)} /> {t}
                      </label>
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

export function AddSource({ wsId, onAdded }: { wsId: string; onAdded: () => void }) {
  const id = useId();
  const kinds = useAsync(() => api.sourceKinds(), []);
  const [kindId, setKindId] = useState("");
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [secretRef, setSecretRef] = useState("");
  const [mode, setMode] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [errors, setErrors] = useState<SourceFormErrors>({});
  const act = useAction();

  const all = kinds.data ?? [];
  const groups = useMemo(() => groupKinds(all), [all]);
  const kind = all.find((k) => k.kind === (kindId || defaultKind(all))) ?? null;

  const chooseKind = (k: string) => {
    const spec = all.find((x) => x.kind === k);
    setKindId(k);
    setValues(spec?.default_port ? { port: String(spec.default_port) } : {});
    setSecretRef(spec ? suggestedSecretRef(spec) : "");
    setMode("");
    setFile(null);
    setErrors({});
  };
  // First render with the catalog loaded: seed the defaults of the default kind.
  if (kind && !kindId) chooseKind(kind.kind);

  const acceptsUpload = !!kind && kind.category === "file" && kind.required.includes("path");
  const setValue = (f: string, v: string) => setValues((prev) => ({ ...prev, [f]: v }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!kind) return;
    const effective = acceptsUpload && file ? { ...values, path: values.path || "(upload)" } : values;
    const errs = validateSourceForm(kind, name || file?.name || "", effective, kind.secret_field ? secretRef : "");
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const ok = await act.run(async () => {
      let vals = values;
      if (acceptsUpload && file) {
        const up = await api.upload(wsId, file);
        vals = { ...values, path: up.path };
      }
      const config = buildSourceConfig(kind, vals, mode || undefined);
      return api.addSource(wsId, {
        kind: kind.kind, name: name.trim() || (file?.name ?? kind.label), config,
        secret_ref: kind.secret_field ? secretRef.trim() || null : null,
      });
    });
    if (ok) onAdded();
  };

  const err = (k: string) => errors[k] && <div className="field-error" role="alert">{errors[k]}</div>;
  const fieldInput = (f: string, required: boolean) => (
    <Field key={f} label={`${fieldLabel(f)}${required ? " *" : ""}`} htmlFor={`${id}-${f}`} hint={fieldHint(f)}>
      <input id={`${id}-${f}`} value={values[f] ?? ""} onChange={(e) => setValue(f, e.target.value)} aria-invalid={!!errors[f]}
        aria-required={required || undefined} inputMode={NUMBER_FIELDS.has(f) ? "numeric" : undefined}
        type={f === "instance_url" ? "url" : "text"} autoComplete="off" spellCheck={false} />
      {err(f)}
    </Field>
  );

  return (
    <Card title="Add source">
      <ErrorBox error={kinds.error} onRetry={kinds.reload} />
      {kinds.loading && !kinds.data && <Loading label="Loading source kinds…" />}
      {kind && (
        <form className="form" onSubmit={submit} noValidate aria-label="Add source">
          <div className="form-row">
            <Field label="Kind" htmlFor={`${id}-kind`} hint={kind.docs || undefined}>
              <select id={`${id}-kind`} value={kind.kind} onChange={(e) => chooseKind(e.target.value)}>
                {groups.map((g) => (
                  <optgroup key={g.category} label={g.label}>
                    {g.kinds.map((k) => (
                      <option key={k.kind} value={k.kind} disabled={!k.enabled}>
                        {k.label}{!k.enabled ? " (disabled by admin)" : !k.driver_installed ? " (driver not installed)" : ""}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            </Field>
            <Field label="Name *" htmlFor={`${id}-name`}>
              <input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} placeholder="Production ITSM" aria-invalid={!!errors.name} />
              {err("name")}
            </Field>
          </div>
          <div className="chip-row" aria-label="Kind details">
            <Tag tone={kind.execution_mode === "pushdown" ? "info" : "neutral"}>{kind.execution_mode}</Tag>
            <span className="muted small">{executionModeText(kind.execution_mode)} Dialect <code>{kind.dialect}</code>.</span>
            {kind.driver_installed ? <Tag tone="success">driver installed</Tag> : <Tag tone="warning">driver missing</Tag>}
          </div>
          {!kind.enabled && <Notice tone="warning">This kind is disabled by the platform administrator.</Notice>}
          {!kind.driver_installed && (
            <Notice tone="warning">
              The driver for {kind.label} is not installed on the server, so connecting will fail until it is.
              {kind.install_hint && <> Install it with <code>{kind.install_hint}</code>.</>}
            </Notice>
          )}
          {kind.required.length > 0 && <div className="form-row">{kind.required.filter((f) => !(acceptsUpload && f === "path")).map((f) => fieldInput(f, true))}</div>}
          {acceptsUpload && (
            <div className="form-row">
              <Field label="Upload a file" htmlFor={`${id}-file`} hint="Uploaded to the workspace (max 200 MB) and registered with config.path.">
                <input id={`${id}-file`} type="file" accept=".csv,.parquet,.xlsx,.db,.sqlite,.sqlite3,.duckdb"
                  onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
              </Field>
              <Field label={`…or a server path${file ? "" : " *"}`} htmlFor={`${id}-path`}>
                <input id={`${id}-path`} value={values.path ?? ""} onChange={(e) => setValue("path", e.target.value)} disabled={!!file}
                  aria-invalid={!!errors.path} autoComplete="off" />
                {err("path")}
              </Field>
            </div>
          )}
          {kind.optional.length > 0 && (
            <details className="optional-fields">
              <summary className="small">Optional settings ({kind.optional.length})</summary>
              <div className="form-row">{kind.optional.map((f) => fieldInput(f, false))}</div>
            </details>
          )}
          {kind.execution_mode === "pushdown" && (
            <Field label="Execution mode" htmlFor={`${id}-mode`} hint="Pushdown needs the administrator to allow it; otherwise the source is staged.">
              <select id={`${id}-mode`} value={mode} onChange={(e) => setMode(e.target.value)}>
                <option value="">Pushdown (default)</option>
                <option value="staged">Staged snapshot</option>
              </select>
            </Field>
          )}
          {kind.secret_field ? (
            <Field label={`Secret reference (${kind.secret_field})`} htmlFor={`${id}-secret`}
              hint={<>A reference, never the secret itself: <code>env:NAME</code> or <code>file:/path</code>. The server resolves it at connect
                time. Secrets never go in config.</>}>
              <input id={`${id}-secret`} value={secretRef} onChange={(e) => setSecretRef(e.target.value)} placeholder="env:NAME"
                autoComplete="off" spellCheck={false} aria-invalid={!!errors.secret_ref} />
              {err("secret_ref")}
            </Field>
          ) : <p className="muted small">This kind needs no credential.</p>}
          <ErrorBox error={act.error} />
          <div className="form-actions">
            <button type="submit" className="btn btn-primary" disabled={act.busy || !kind.enabled}>{act.busy ? "Adding…" : "Add source"}</button>
          </div>
        </form>
      )}
    </Card>
  );
}
