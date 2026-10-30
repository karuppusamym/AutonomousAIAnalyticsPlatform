/**
 * Admin control plane screens (platform settings, prompts, token savings). Admin-only: the API
 * refuses non-admins, and the Admin page does not render these for them.
 */
import { useId, useMemo, useState } from "react";
import { api, type LLMMode, type PlatformSettings, type PromptTemplate, type RungSpend, type SettingsDocument, type SettingsVersion, type TokenSavings } from "../api";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, Stat, StateView, StatusBadge, Tag, Value } from "../components/ui";
import { fmtDate, fmtNumber, fmtPct, fmtUsd } from "../lib/format";
import { useAction, useAsync, type AsyncState } from "../lib/hooks";
import { ModelReadiness } from "../components/ModelReadiness";
import {
  LIMIT_SECTIONS, LLM_MODES, MODE_TEXT, SECTION_LABELS, fieldSchema, humanKey, modeOf, pendingChanges, presetEffect, savedShare,
  settingsPatch, statusCount, validateLimits, withPurposeMode,
} from "../lib/settings";

/** Deep copy of plain JSON-like data that keeps NaN (an emptied number input) so validation can flag it. */
function clone<T>(v: T): T {
  if (Array.isArray(v)) return v.map((x) => clone(x)) as T;
  if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clone(x)])) as T;
  return v;
}
const fmtSetting = (v: unknown) => (v === undefined ? "—" : typeof v === "string" ? v : JSON.stringify(v));

export function SettingsEditor() {
  const id = useId();
  const doc = useAsync(() => api.adminSettings(), []);
  const models = useAsync(() => api.models(), []);
  const kinds = useAsync(() => api.sourceKinds(), []);
  const history = useAsync(() => api.settingsHistory(), []);
  const [draft, setDraft] = useState<PlatformSettings | null>(null);
  const [loadedVersion, setLoadedVersion] = useState<number | null>(null);
  const [note, setNote] = useState("");
  const [noteError, setNoteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [purposeSearch, setPurposeSearch] = useState("");
  const act = useAction();

  // A new server version (load, save, preset, rollback) resets the draft to what is now effective.
  if (doc.data && doc.data.version !== loadedVersion) {
    setLoadedVersion(doc.data.version);
    setDraft(clone(doc.data.settings));
  }

  const reloadAll = async () => {
    await Promise.all([doc.reload(), models.reload(), history.reload(), kinds.reload()]);
  };

  const saved = doc.data?.settings;
  const changes = useMemo(() => (saved && draft ? pendingChanges(saved, draft) : []), [saved, draft]);
  const limitErrors = useMemo(() => (draft ? validateLimits(draft, doc.data?.schema) : {}), [draft, doc.data?.schema]);

  if (doc.error) return <ErrorBox error={doc.error} onRetry={doc.reload} />;
  if (!doc.data || !draft || !saved) return <Loading />;
  const d = doc.data;

  const update = (fn: (s: PlatformSettings) => void) => {
    setNotice(null);
    setDraft((prev) => {
      const next = clone(prev ?? d.settings);
      fn(next);
      return next;
    });
  };

  const save = async () => {
    if (!note.trim()) {
      setNoteError("A note is required: say why the settings change.");
      return;
    }
    setNoteError(null);
    if (Object.keys(limitErrors).length) return;
    const patch = settingsPatch(saved, draft);
    const r = await act.run(() => api.updateSettings(patch, note.trim()));
    if (r) {
      setNote("");
      setNotice(r.changes?.length ? `Saved as version ${r.version} (${r.changes.length} change${r.changes.length === 1 ? "" : "s"}).` : "Nothing changed.");
      await reloadAll();
    }
  };

  const applyPreset = async (name: string) => {
    const purposes = Object.keys(models.data?.effective ?? models.data?.routing ?? {});
    const effect = presetEffect(saved.llm.purpose_modes, d.presets[name] ?? {}, purposes);
    const lines = effect.slice(0, 12).map((x) => `  ${x.purpose}: ${x.from} → ${x.to}`).join("\n");
    const msg = `Apply the “${name}” preset? It replaces every per-purpose model mode.\n\n`
      + (effect.length ? `${effect.length} purpose(s) change:\n${lines}${effect.length > 12 ? "\n  …" : ""}` : "No purpose changes mode.")
      + (changes.length ? "\n\nYour unsaved edits will be discarded." : "");
    if (!window.confirm(msg)) return;
    const r = await act.run(() => api.applyPreset(name));
    if (r) {
      setNotice(`Preset “${name}” applied as version ${r.version}.`);
      await reloadAll();
    }
  };

  const rollback = async (version: number) => {
    if (!window.confirm(`Roll the platform settings back to version ${version}? This creates a new version with that document.`)) return;
    const r = await act.run(() => api.rollbackSettings(version));
    if (r) {
      setNotice(`Rolled back to version ${version} (now version ${r.version}).`);
      await reloadAll();
    }
  };

  const effective = models.data?.effective ?? {};
  const purposes = Object.keys(effective).length ? Object.keys(effective).sort() : Object.keys(models.data?.routing ?? {}).sort();
  const allowlist = models.data?.allowlist ?? [];

  return (
    <div className="stack settings-editor">
      <ModelReadiness />
      <Notice tone="info">
        Version <strong>{d.version}</strong> is in effect. Changes apply to every workspace within seconds; workspace policy may only
        tighten them. Every save is versioned and audited, and can be rolled back.
      </Notice>
      {notice && <Notice tone="success">{notice}</Notice>}
      <ErrorBox error={act.error} />

      <Card title="Presets" actions={<span className="muted small">A preset replaces the per-purpose modes; pilot_low_cost also sets the enabled models and daily cap, air_gapped turns JEV off.</span>}>
        <div className="chip-row">
          {Object.keys(d.presets).map((p) => (
            <button key={p} type="button" className="btn btn-sm" onClick={() => void applyPreset(p)} disabled={act.busy}>{humanKey(p)}</button>
          ))}
        </div>
      </Card>

      <Card title="Model use per purpose" actions={<input type="search" aria-label="Search model purposes" placeholder="Search purposes…" value={purposeSearch} onChange={(e) => setPurposeSearch(e.target.value)} />}>
        <p className="muted small">
          <strong>off</strong>: {MODE_TEXT.off}; <strong>auto</strong>: {MODE_TEXT.auto}; <strong>always</strong>: {MODE_TEXT.always}.
          A purpose without a rule path has no fallback: turning it off disables that capability.
        </p>
        <ErrorBox error={models.error} onRetry={models.reload} />
        {!models.data && !models.error && <Loading />}
        {models.data && (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>Purpose</th><th>Profile</th><th>Rule path</th><th>Connection</th><th>Effective mode</th><th>Requested mode</th></tr></thead>
              <tbody>
                {purposes.filter((p) => humanKey(p).toLowerCase().includes(purposeSearch.toLowerCase()) || p.includes(purposeSearch.toLowerCase())).map((p) => {
                  const eff = effective[p];
                  const mode = modeOf(draft.llm.purpose_modes, p);
                  const changed = mode !== modeOf(saved.llm.purpose_modes, p);
                  const risky = mode === "off" && eff && !eff.deterministic_path;
                  return (
                    <tr key={p} className={eff?.decision_model ? "row-jev" : undefined}>
                      <td><strong>{humanKey(p)}</strong><div className="muted small">{p}</div></td>
                      <td className="small"><code>{eff?.profile ?? models.data?.routing[p]}</code></td>
                      <td>{eff?.deterministic_path ? <Tag tone="success">rule path</Tag> : <Tag>model only</Tag>}</td>
                      <td><StatusBadge status={eff?.available ? "ok" : "failed"} label={eff?.available ? "available" : "unavailable"} />
                        {!eff?.available && <div className="muted small route-reason">{eff?.unavailable_reason ?? "See Models & routing for provider configuration."}</div>}</td>
                      <td className="small">{eff?.mode ?? "—"}</td>
                      <td>
                        <select aria-label={`Mode for ${p}`} value={mode}
                          onChange={(e) => update((s) => { s.llm.purpose_modes = withPurposeMode(saved.llm.purpose_modes, s.llm.purpose_modes, p, e.target.value as LLMMode); })}>
                          {LLM_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
                        </select>
                        {changed && <span className="tag tag-warning">changed</span>}
                        {risky && <div className="field-error">No rule path: this capability stops working.</div>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Features">
        <div className="toggle-group">
          {Object.entries(draft.features).map(([k, v]) => (
            <label key={k} className="switch">
              <input type="checkbox" role="switch" checked={!!v} aria-label={`Feature ${humanKey(k)}`}
                onChange={(e) => update((s) => { s.features[k] = e.target.checked; })} />
              <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
              <span className="small">{humanKey(k)}</span>
            </label>
          ))}
        </div>
      </Card>

      <Card title="Limits">
        <div className="grid-2">
          {LIMIT_SECTIONS.map((section) => {
            const values = (draft as unknown as Record<string, Record<string, unknown>>)[section] ?? {};
            const scalars = Object.entries(values).filter(([, v]) => ["number", "boolean", "string"].includes(typeof v));
            if (!scalars.length) return null;
            return (
              <fieldset key={section} className="autonomy">
                <legend>{SECTION_LABELS[section] ?? section}</legend>
                {scalars.map(([k, v]) => {
                  const fs = fieldSchema(d.schema, section, k);
                  const fid = `${id}-${section}-${k}`;
                  const setVal = (val: unknown) => update((s) => { (s as unknown as Record<string, Record<string, unknown>>)[section][k] = val; });
                  if (typeof v === "boolean") {
                    return (
                      <label key={k} className="toggle small">
                        <input type="checkbox" checked={v} onChange={(e) => setVal(e.target.checked)} /> {humanKey(k)}
                      </label>
                    );
                  }
                  if (fs.enum) {
                    return (
                      <Field key={k} label={humanKey(k)} htmlFor={fid}>
                        <select id={fid} value={String(v)} onChange={(e) => setVal(e.target.value)}>
                          {fs.enum.map((o) => <option key={o} value={o}>{o}</option>)}
                        </select>
                      </Field>
                    );
                  }
                  if (typeof v === "string") {
                    return <Field key={k} label={humanKey(k)} htmlFor={fid}><input id={fid} value={v} onChange={(e) => setVal(e.target.value)} /></Field>;
                  }
                  const err = limitErrors[`${section}.${k}`];
                  const range = fs.minimum !== undefined || fs.maximum !== undefined ? `${fs.minimum ?? "…"} – ${fs.maximum ?? "…"}` : undefined;
                  return (
                    <Field key={k} label={humanKey(k)} htmlFor={fid} hint={range}>
                      <input id={fid} type="number" value={Number.isFinite(v as number) ? String(v) : ""} min={fs.minimum} max={fs.maximum}
                        step={fs.type === "integer" ? 1 : "any"} aria-invalid={!!err}
                        onChange={(e) => setVal(e.target.value === "" ? Number.NaN : Number(e.target.value))} />
                      {err && <div className="field-error" role="alert">{err}</div>}
                    </Field>
                  );
                })}
              </fieldset>
            );
          })}
        </div>
      </Card>

      <div className="grid-2">
        <Card title="Enabled source kinds">
          <p className="muted small">None checked = every kind in the catalog may be registered.</p>
          <ErrorBox error={kinds.error} onRetry={kinds.reload} />
          <div className="toggle-group">
            {(kinds.data ?? []).map((k) => (
              <label key={k.kind} className="toggle small">
                <input type="checkbox" checked={draft.sources.enabled_kinds.includes(k.kind)}
                  onChange={(e) => update((s) => {
                    const cur = s.sources.enabled_kinds;
                    s.sources.enabled_kinds = e.target.checked ? [...cur, k.kind] : cur.filter((x) => x !== k.kind);
                  })} /> {k.label} <span className="muted">({k.execution_mode}{k.driver_installed ? "" : ", no driver"})</span>
              </label>
            ))}
          </div>
        </Card>
        <Card title="Models and cache">
          <fieldset className="autonomy">
            <legend>Disabled models (removed from the allowlist at runtime)</legend>
            <div className="toggle-group">
              {allowlist.map((m) => (
                <label key={m} className="toggle small">
                  <input type="checkbox" checked={draft.llm.disabled_models.includes(m)}
                    onChange={(e) => update((s) => {
                      const cur = s.llm.disabled_models;
                      s.llm.disabled_models = e.target.checked ? [...cur, m] : cur.filter((x) => x !== m);
                    })} /> <code>{m}</code>
                </label>
              ))}
            </div>
          </fieldset>
          <fieldset className="autonomy">
            <legend>Cacheable purposes</legend>
            <div className="toggle-group">
              {purposes.map((p) => (
                <label key={p} className="toggle small">
                  <input type="checkbox" checked={draft.llm.cacheable_purposes.includes(p)}
                    onChange={(e) => update((s) => {
                      const cur = s.llm.cacheable_purposes;
                      s.llm.cacheable_purposes = e.target.checked ? [...cur, p] : cur.filter((x) => x !== p);
                    })} /> {p}
                </label>
              ))}
            </div>
          </fieldset>
          {(Object.keys(draft.llm.routing_overrides).length > 0 || Object.keys(draft.llm.profile_models).length > 0) && (
            <p className="muted small">Routing overrides: {JSON.stringify(draft.llm.routing_overrides)} · profile models:{" "}
              {JSON.stringify(draft.llm.profile_models)} (edit through the API).</p>
          )}
        </Card>
      </div>

      <Card title={`Save changes${changes.length ? ` (${changes.length})` : ""}`}>
        {changes.length === 0 ? <p className="muted small">No unsaved changes.</p> : (
          <ul className="list compact" aria-label="Pending changes">
            {changes.map((c) => (
              <li key={c.path} className="list-item small"><code>{c.path}</code><span>{fmtSetting(c.from)} → <strong>{fmtSetting(c.to)}</strong></span></li>
            ))}
          </ul>
        )}
        <Field label="Note (required)" htmlFor={`${id}-note`} hint="Recorded with the version and in the audit log.">
          <input id={`${id}-note`} value={note} onChange={(e) => setNote(e.target.value)} aria-invalid={!!noteError} maxLength={500}
            placeholder="Why this change?" />
          {noteError && <div className="field-error" role="alert">{noteError}</div>}
        </Field>
        <div className="form-actions">
          <button type="button" className="btn btn-ghost" disabled={!changes.length || act.busy}
            onClick={() => { setDraft(clone(saved)); setNoteError(null); }}>Discard</button>
          <button type="button" className="btn btn-primary" onClick={() => void save()}
            disabled={!changes.length || act.busy || Object.keys(limitErrors).length > 0}>{act.busy ? "Saving…" : "Save settings"}</button>
        </div>
      </Card>

      <SettingsHistory doc={d} history={history} onRollback={rollback} busy={act.busy} />
    </div>
  );
}

function SettingsHistory({ doc, history, onRollback, busy }: {
  doc: SettingsDocument; history: AsyncState<SettingsVersion[]>; onRollback: (v: number) => void; busy: boolean;
}) {
  return (
    <Card title="History">
      <ErrorBox error={history.error} onRetry={history.reload} />
      {history.data?.length === 0 && <EmptyState title="No saved versions yet">The defaults are in effect.</EmptyState>}
      {!!history.data?.length && (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead><tr><th>Version</th><th>Note</th><th>By</th><th>When</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {history.data.map((h) => (
                <tr key={h.version}>
                  <td>v{h.version} {h.version === doc.version && <Tag tone="success">current</Tag>}</td>
                  <td className="small">{h.note || <span className="muted">—</span>}</td>
                  <td className="small"><code>{h.created_by ?? "—"}</code></td>
                  <td className="small">{fmtDate(h.created_at)}</td>
                  <td>{h.version !== doc.version && (
                    <button type="button" className="btn btn-xs" onClick={() => onRollback(h.version)} disabled={busy}>Roll back to v{h.version}</button>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export function PromptsView() {
  const list = useAsync(() => api.prompts(), []);
  const [q, setQ] = useState("");
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  const needle = q.trim().toLowerCase();
  const rows = list.data.filter((p) => !needle || p.name.toLowerCase().includes(needle) || p.text.toLowerCase().includes(needle));
  const edited = list.data.filter((p) => p.edited).length;
  return (
    <Card title={`Prompt templates (${rows.length})`} actions={
      <input type="search" placeholder="Filter prompts…" aria-label="Filter prompts" value={q} onChange={(e) => setQ(e.target.value)} />}>
      <p className="muted small">
        The instructions the agents send when a purpose calls a model. An administrator can edit a template; the edit keeps the
        template's <code>{"{placeholders}"}</code> and its untrusted-context rule, is saved as a new platform settings version (see
        history and roll back under Platform settings), and every model call records the exact text it sent. Models still only
        propose: SQL, numbers and approvals go through the same checks whatever a prompt says.
        {edited > 0 && <> <strong>{edited} edited.</strong></>}
      </p>
      {rows.length === 0 ? <EmptyState title="No prompts match" /> : (
        <div className="stack">{rows.map((p) => <PromptRow key={p.name} p={p} onSaved={list.reload} />)}</div>
      )}
    </Card>
  );
}

function PromptRow({ p, onSaved }: { p: PromptTemplate; onSaved: () => void }) {
  const id = useId();
  const act = useAction();
  const [draft, setDraft] = useState<string | null>(null);
  const [saved, setSaved] = useState<number | null>(null);
  const missing = draft === null ? [] : p.placeholders.filter((name) => !draft.includes(`{${name}}`));
  const save = async () => {
    if (draft === null) return;
    const r = await act.run(() => api.editPrompt(p.name, draft));
    if (r) { setDraft(null); setSaved(r.version); onSaved(); }
  };
  const reset = async () => {
    const r = await act.run(() => api.resetPrompt(p.name));
    if (r) { setDraft(null); setSaved(r.version); onSaved(); }
  };
  return (
    <details className="prompt">
      <summary>
        <code>{p.name}</code> <Tag tone="info">{p.version}</Tag> {p.edited && <Tag tone="warning">edited</Tag>}{" "}
        <span className="muted small">{p.text.length.toLocaleString()} chars</span>
      </summary>
      {p.placeholders.length > 0 && (
        <p className="small muted">Keep these placeholders: {p.placeholders.map((name) => <code key={name}>{`{${name}}`}</code>)}</p>
      )}
      {draft === null ? (
        <>
          <pre className="json">{p.text}</pre>
          <div className="chip-row">
            <button type="button" className="btn btn-sm" onClick={() => { setDraft(p.text); setSaved(null); }}>Edit</button>
            {p.edited && <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void reset()}>Reset to built-in</button>}
          </div>
          {p.edited && (
            <details>
              <summary className="small">Built-in text</summary>
              <pre className="json">{p.built_in}</pre>
            </details>
          )}
        </>
      ) : (
        <form className="stack" aria-label={`Edit ${p.name}`} onSubmit={(e) => { e.preventDefault(); void save(); }}>
          <Field label="Prompt text" htmlFor={`${id}-text`}>
            <textarea id={`${id}-text`} rows={14} value={draft} spellCheck={false} onChange={(e) => setDraft(e.target.value)} />
          </Field>
          {missing.length > 0 && <Notice tone="warning"><p>Missing placeholder{missing.length > 1 ? "s" : ""}:{" "}
            {missing.map((name) => <code key={name}>{`{${name}}`}</code>)}. Saving will be refused.</p></Notice>}
          <div className="chip-row">
            <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || draft === p.text || !draft.trim()}>Save</button>
            <button type="button" className="btn btn-sm" onClick={() => { setDraft(null); act.setError(null); }}>Cancel</button>
          </div>
        </form>
      )}
      <ErrorBox error={act.error} />
      {saved !== null && <p className="small muted" role="status">Saved as platform settings version {saved}.</p>}
    </details>
  );
}

export const SAVINGS_DAYS = [7, 30, 90, 365];

function contextHits(c: NonNullable<TokenSavings["context_cache"]>): number {
  return Object.values(c.by_kind).flatMap((byPurpose) => Object.values(byPurpose)).reduce((n, x) => n + (x.hits ?? 0), 0);
}

export function TokenSavingsView() {
  const [days, setDays] = useState(30);
  const s = useAsync(() => api.tokenSavings(days), [days]);
  const rows = useMemo(() => Object.entries(s.data?.by_purpose ?? {}).sort((a, b) => b[1].tokens_saved - a[1].tokens_saved || a[0].localeCompare(b[0])),
    [s.data]);
  const t = s.data?.totals;
  return (
    <div className="stack">
      <ModelReadiness />
      <div className="toolbar">
        <label className="inline-field small">Period
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Period in days">
            {SAVINGS_DAYS.map((d) => <option key={d} value={d}>last {d} days</option>)}
          </select>
        </label>
        <span className="muted small">Avoided tokens are estimates for skipped calls. Spend uses provider cost or the versioned model price table.</span>
      </div>
      <ErrorBox error={s.error} onRetry={s.reload} />
      {!s.data && !s.error && <Loading />}
      {t && (
        <>
          {t.calls === 0 && <Notice tone="info"><strong>No provider calls recorded in this period.</strong> The {t.deterministic_skips} rule-based steps and {t.cache_hits} cache hits did not incur a model charge. Estimated avoided tokens are hypothetical usage, not tokens sent to an LLM.</Notice>}
          {s.data?.cost_complete === false && <Notice tone="warning">
            Spend is incomplete: {s.data.missing_price?.reduce((n, row) => n + row.calls, 0) ?? 0} model call(s) had no provider cost or price-table entry.
            Update the model price table before using this total as a budget report.
          </Notice>}
          <div className="stats-row usage-summary">
            <Stat label="Recorded model cost" value={s.data?.cost_complete === false ? <>Incomplete</> : <Value value={t.cost_usd} format="usd" />} hint={s.data?.cost_complete === false ? "Some calls have unknown prices" : "Actual calls only"} />
            <Stat label="Model calls" value={<Value value={t.calls} format="int" />} />
            <Stat label="Tokens used" value={<Value value={t.tokens_used} format="int" />} />
            <Stat label="Estimated tokens avoided" value={<Value value={t.tokens_saved} format="int" />} hint={`${fmtPct(t.saved_share)} of actual plus estimated tokens`} />
          </div>
          <div className="stats-row usage-secondary">
            <Stat label="Cache hits" value={<Value value={t.cache_hits} format="int" />} />
            <Stat label="Deterministic skips" value={<Value value={t.deterministic_skips} format="int" />} />
            <Stat label="Refused (oversize)" value={<Value value={t.refused} format="int" />} />
            {t.cached_input_tokens !== undefined && (
              <Stat label="Prompt-cache tokens" value={<Value value={t.cached_input_tokens} format="int" />}
                hint={t.cached_input_share != null
                  ? `${fmtPct(t.cached_input_share)} of input served from the provider's cache${t.cached_input_saved_usd ? `, saving ${fmtUsd(t.cached_input_saved_usd)}` : ""}`
                  : "Input served from the provider's cache"} />
            )}
            {s.data?.context_cache && (
              <Stat label="Context reused" value={<Value value={contextHits(s.data.context_cache)} format="int" />}
                hint={`times a compiled context or retrieval was reused instead of rebuilt${s.data.context_cache.shared ? " (shared across workers)" : " (this process)"}`} />
            )}
          </div>
          <Card title="By purpose">
            {rows.length === 0 ? <EmptyState title="No model activity in this period" /> : (
              <div className="table-wrap">
                <table className="table table-compact">
                  <thead>
                    <tr><th>Purpose</th><th className="num">Calls</th><th className="num">Tokens used</th><th className="num">Tokens saved</th>
                      <th className="num">Cost</th><th className="num">Cache hits</th><th className="num">Deterministic skips</th>
                      <th className="num">Refused</th><th className="num">Saved share</th></tr>
                  </thead>
                  <tbody>
                    {rows.map(([p, r]) => (
                        <tr key={p}>
                          <td><code>{p}</code></td>
                          <td className="num">{fmtNumber(r.calls, 0)}</td>
                          <td className="num">{fmtNumber(r.tokens_used, 0)}</td>
                          <td className="num">{fmtNumber(r.tokens_saved, 0)}</td>
                          <td className="num">{r.cost_complete === false ? "Incomplete" : <Value value={r.cost_usd} format="usd" />}</td>
                          <td className="num">{fmtNumber(statusCount(r, "cache_hit"), 0)}</td>
                          <td className="num">{fmtNumber(statusCount(r, "skipped"), 0)}</td>
                          <td className="num">{fmtNumber(statusCount(r, "refused"), 0)}</td>
                          <td className="num">{fmtPct(savedShare(r))}</td>
                        </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
          <details className="card diagnostics"><summary>Execution details and model breakdown</summary><div className="card-body stack">
          <SpendBreakdown title="By rung" by={s.data?.by_rung} keyLabel="Rung"
            missing="The server does not report the execution-ladder rung (L0 cache … L5 strong model) per call yet." />
          <Card title="Escalations (small answer failed a check, large tier answered)">
            {Object.keys(s.data?.escalations ?? {}).length === 0 ? <EmptyState title="No escalations in this period" /> : (
              <div className="table-wrap">
                <table className="table table-compact">
                  <thead><tr><th>Purpose</th><th className="num">Calls</th><th className="num">Cost</th><th>Reasons</th><th>Models</th></tr></thead>
                  <tbody>
                    {Object.entries(s.data?.escalations ?? {}).map(([p, e]) => (
                      <tr key={p}>
                        <td><code>{p}</code></td>
                        <td className="num">{fmtNumber(e.calls, 0)}</td>
                        <td className="num"><Value value={e.cost_usd} format="usd" /></td>
                        <td className="small">{Object.entries(e.by_reason).map(([k, n]) => `${k} ${n}`).join(", ")}</td>
                        <td className="small">{Object.entries(e.by_model).map(([k, n]) => `${k} ${n}`).join(", ")}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
          <SpendBreakdown title="By model" by={s.data?.by_model} keyLabel="Model"
            missing="Not in this report; the Usage tab lists calls and spend per provider and model." />
          </div></details>
        </>
      )}
    </div>
  );
}

/** Optional breakdowns: rendered when the server reports them, otherwise an explicit unknown state. */
function SpendBreakdown({ title, by, keyLabel, missing }: { title: string; by: Record<string, RungSpend> | null | undefined; keyLabel: string; missing: string }) {
  const rows = Object.entries(by ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <Card title={title}>
      {!by ? <StateView kind="unknown" title={`${title.replace(/^By /, "Spend by ")} not reported`}>{missing}</StateView> : rows.length === 0 ? (
        <EmptyState title="No model activity in this period" />
      ) : (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead><tr><th>{keyLabel}</th><th className="num">Calls</th><th className="num">Tokens used</th><th className="num">Tokens saved</th><th className="num">Cost</th></tr></thead>
            <tbody>
              {rows.map(([k, r]) => (
                <tr key={k}>
                  <td><code>{k}</code></td>
                  <td className="num"><Value value={r.calls} format="int" /></td>
                  <td className="num"><Value value={r.tokens_used} format="int" /></td>
                  <td className="num"><Value value={r.tokens_saved} format="int" /></td>
                  <td className="num">{r.cost_complete === false ? "Incomplete" : <Value value={r.cost_usd} format="usd" />}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
