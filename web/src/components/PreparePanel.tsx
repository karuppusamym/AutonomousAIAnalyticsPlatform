import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, type Recipe, type RecipeRun, type Source } from "../api";
import { parsePolicy } from "../lib/policy";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { PipelinesCard } from "./Pipelines";
import { Card, DataTable, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, TechnicalDetails } from "./ui";

const FILE_KINDS = new Set(["csv"]);

/**
 * Work → Prepare data (P6-04..07): load a file into a file source, and preview, publish and run
 * recipes. A preview writes nothing; a run keeps the last good output when a check fails. What a run
 * produced is listed in Outputs.
 */
export function PreparePanel({ wsId, role, recipe, onSelectRecipe, pipeline = null, onSelectPipeline = () => undefined }: {
  wsId: string; role: string | undefined; recipe: string | null; onSelectRecipe: (id: string | null) => void;
  pipeline?: string | null; onSelectPipeline?: (id: string | null) => void;
}) {
  const canEdit = roleAtLeast(role, "editor");
  return (
    <div className="stack">
      <p className="muted small">Load a file, or build a recipe of joins, filters and checks. Previews write nothing; results appear in
        {" "}<Link to={to.outputs(wsId, { type: "prepared" })}>Results → Prepared data</Link>.</p>
      <div className="grid-2">
        <IngestCard wsId={wsId} canEdit={canEdit} />
        <RecipesCard wsId={wsId} canEdit={canEdit} selected={recipe} onSelect={onSelectRecipe} />
      </div>
      <PipelinesCard wsId={wsId} role={role} selected={pipeline} onSelect={onSelectPipeline} />
    </div>
  );
}

function IngestCard({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const id = useId();
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const fileSources = (sources.data ?? []).filter((s: Source) => FILE_KINDS.has(s.kind));
  const [sourceId, setSourceId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [table, setTable] = useState("");
  const [mode, setMode] = useState<"replace" | "append" | "merge">("replace");
  const [keys, setKeys] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const act = useAction();
  const target = sourceId || fileSources[0]?.id || "";
  const keyList = keys.split(",").map((k) => k.trim()).filter(Boolean);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!file || !target || !table.trim()) return;
    setDone(null);
    const r = await act.run(async () => {
      const up = await api.upload(wsId, file);
      return api.ingestFile(wsId, target, { path: up.path, table: table.trim(), mode, keys: mode === "merge" ? keyList : [] });
    });
    if (r) setDone(`Loaded into ${r.asset} (${r.mode}).`);
  };
  return (
    <Card title="Load a file">
      {sources.loading && !sources.data && <Loading />}
      <ErrorBox error={sources.error} onRetry={sources.reload} />
      {sources.data && fileSources.length === 0 && (
        <Notice tone="info">Files load into a file source. <Link to={to.sources(wsId)}>Add a file source in Data → Sources</Link> first.</Notice>
      )}
      {!canEdit && <p className="muted small">Loading files needs the editor role in this workspace.</p>}
      {fileSources.length > 0 && canEdit && (
        <form className="form" onSubmit={submit} aria-label="Load a file">
          {fileSources.length > 1 && (
            <Field label="Into" htmlFor={`${id}-src`}>
              <select id={`${id}-src`} value={target} onChange={(e) => setSourceId(e.target.value)}>
                {fileSources.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </Field>
          )}
          <Field label="File" htmlFor={`${id}-file`} hint="CSV, JSON, Excel or Parquet, up to 200 MB.">
            <input id={`${id}-file`} type="file" accept=".csv,.json,.xlsx,.xls,.parquet" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          </Field>
          <Field label="Table name" htmlFor={`${id}-table`}>
            <input id={`${id}-table`} value={table} onChange={(e) => setTable(e.target.value)} placeholder="orders" />
          </Field>
          <Field label="If the table exists" htmlFor={`${id}-mode`}>
            <select id={`${id}-mode`} value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
              <option value="replace">Replace it</option>
              <option value="append">Add the rows</option>
              <option value="merge">Merge on key columns</option>
            </select>
          </Field>
          {mode === "merge" && (
            <Field label="Key columns" htmlFor={`${id}-keys`} hint="Comma-separated target column names.">
              <input id={`${id}-keys`} value={keys} onChange={(e) => setKeys(e.target.value)} />
            </Field>
          )}
          <ErrorBox error={act.error} />
          {done && <Notice tone="success">{done}</Notice>}
          <div className="form-actions">
            <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !file || !table.trim() || (mode === "merge" && !keyList.length)}>
              {act.busy ? "Loading…" : "Load file"}</button>
          </div>
        </form>
      )}
    </Card>
  );
}

function RecipesCard({ wsId, canEdit, selected, onSelect }: { wsId: string; canEdit: boolean; selected: string | null; onSelect: (id: string | null) => void }) {
  const recipes = useAsync(() => api.listRecipes(wsId), [wsId]);
  const list = Array.isArray(recipes.data) ? recipes.data : [];
  const open = list.find((r) => r.id === selected) ?? null;
  return (
    <Card title="Recipes">
      <ErrorBox error={recipes.error} onRetry={recipes.reload} />
      {recipes.loading && !recipes.data && <Loading />}
      {recipes.data && list.length === 0 && <EmptyState title="No recipes yet">A recipe joins, filters and checks data into a new table.</EmptyState>}
      {list.length > 0 && (
        <ul className="list selectable" aria-label="Recipes">
          {list.map((r) => (
            <li key={r.id}>
              <button type="button" className={`list-button ${r.id === selected ? "active" : ""}`} aria-current={r.id === selected ? "true" : undefined}
                onClick={() => onSelect(r.id === selected ? null : r.id)}>
                <span className="list-button-head"><span className="clamp-1">{r.name}</span><span className="muted small">v{r.version}</span></span>
                <span className="chip-row"><StatusBadge status={r.status} /><span className="muted small">{fmtDate(r.published_at ?? r.created_at)}</span></span>
              </button>
            </li>
          ))}
        </ul>
      )}
      {open && <RecipeDetail wsId={wsId} recipe={open} canEdit={canEdit} onChanged={recipes.reload} />}
      {canEdit && <NewRecipe wsId={wsId} onSaved={(r) => { void recipes.reload(); onSelect(r.id); }} />}
    </Card>
  );
}

const GATE_WORDS: Record<string, string> = { passed: "passed", warned: "warned", failed: "failed" };

function RunResult({ run }: { run: RecipeRun }) {
  const gates = Object.entries(run.gates ?? {});
  const preview = Object.entries(run.preview ?? {});
  return (
    <section className="stack" aria-label={`${run.mode === "preview" ? "Preview" : "Run"} result`}>
      <p className="small"><StatusBadge status={run.status} /> {run.mode === "preview" ? "Preview — nothing was written." : "Run finished."}
        {run.error && <> {run.error}</>}</p>
      {gates.map(([name, g]) => (
        <div key={name} className="small">
          <strong>{name}</strong>: {g.kept_rows} rows kept, {g.dropped_rows} set aside{g.blocked ? "; blocked by a failing check (the last good output stays)" : ""}.
          <ul>
            {g.gates.map((x) => <li key={x.gate}>{x.gate.replace(/_/g, " ")}: {GATE_WORDS[x.status] ?? x.status}{x.failed_rows ? ` (${x.failed_rows} rows)` : ""}</li>)}
          </ul>
        </div>
      ))}
      {preview.map(([name, p]) => (
        <div key={name}>
          <h4 className="small">{name} — {p.row_count} rows{p.would_block ? " (a run would be blocked)" : ""}</h4>
          <DataTable columns={p.columns} rows={p.rows} maxRows={20} caption={`Preview of ${name}`} />
        </div>
      ))}
      <TechnicalDetails value={{ id: run.id, spec_hash: run.spec_hash, engine: run.engine, plan: run.plan, preflight: run.preflight }} />
    </section>
  );
}

function RecipeDetail({ wsId, recipe, canEdit, onChanged }: { wsId: string; recipe: Recipe; canEdit: boolean; onChanged: () => void }) {
  const act = useAction();
  const [result, setResult] = useState<RecipeRun | null>(null);
  const runs = useAsync(() => api.listRecipeRuns(wsId, recipe.id), [wsId, recipe.id]);
  const exec = async (mode: "preview" | "materialize") => {
    const r = await act.run(() => api.runRecipe(wsId, recipe.id, { mode }));
    if (r) {
      setResult(r);
      void runs.reload();
    }
  };
  const publish = async () => {
    const r = await act.run(() => api.publishRecipe(wsId, recipe.id));
    if (r) onChanged();
  };
  const published = recipe.status === "published";
  return (
    <div className="stack recipe-detail">
      <h3>{recipe.name} <span className="muted small">v{recipe.version}</span></h3>
      <div className="form-actions">
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void exec("preview")}>Preview</button>
        {canEdit && !published && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void publish()}>Publish</button>}
        {canEdit && published && <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void exec("materialize")}>Run</button>}
      </div>
      {canEdit && !published && <p className="muted small">Publish the recipe to run it; a draft can only be previewed.</p>}
      <ErrorBox error={act.error} />
      {result && <RunResult run={result} />}
      {Array.isArray(runs.data) && runs.data.length > 0 && (
        <details>
          <summary>Earlier runs ({runs.data.length})</summary>
          <ul className="list compact">
            {runs.data.map((r) => (
              <li key={r.id} className="list-item"><span>{r.mode} · {fmtDate(r.created_at)}</span><StatusBadge status={r.status} /></li>
            ))}
          </ul>
        </details>
      )}
      <TechnicalDetails value={recipe.spec} label="Recipe specification" />
    </div>
  );
}

/** Creating a recipe is a specialist step: the spec is JSON, under Advanced. */
function NewRecipe({ wsId, onSaved }: { wsId: string; onSaved: (r: Recipe) => void }) {
  const id = useId();
  const [text, setText] = useState("");
  const act = useAction();
  const parsed = text.trim() ? parsePolicy(text) : { value: undefined, error: undefined };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!parsed.value) return;
    const r = await act.run(() => api.saveRecipe(wsId, parsed.value!));
    if (r) {
      setText("");
      onSaved(r);
    }
  };
  return (
    <details className="advanced">
      <summary>Advanced: new recipe from a specification</summary>
      <form className="form" onSubmit={save} aria-label="New recipe">
        <Field label="Recipe specification (JSON)" htmlFor={`${id}-spec`}>
          <textarea id={`${id}-spec`} className="mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} aria-invalid={!!parsed.error} />
        </Field>
        {parsed.error && <p className="warn-text small" role="alert">Invalid JSON: {parsed.error}</p>}
        <ErrorBox error={act.error} />
        <div className="form-actions"><button type="submit" className="btn btn-sm" disabled={act.busy || !parsed.value}>Save draft</button></div>
      </form>
    </details>
  );
}
