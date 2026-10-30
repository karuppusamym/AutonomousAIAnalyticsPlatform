import { useState, type ReactNode } from "react";
import {
  ApiError, api, type Dict, type EffectiveModels, type ModelEvaluation, type ProviderTest, type ProviderView,
  type SwitchGateResult,
} from "../api";
import { Card, EmptyState, ErrorBox, Loading, Notice, StatusBadge, Tabs, Tag, TechnicalDetails } from "../components/ui";
import { fmtDate, fmtUsd } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";

/**
 * Admin › Models (spec S05 §6, task S05-T4): the one home of model configuration, inside Platform settings.
 * Every view reads the admin models API; nothing here decides a route — the server resolves the layers
 * (file ⊕ platform ⊕ tenant ⊕ workspace, each lower layer only narrowing) and the router enforces them.
 */
type View = "purposes" | "providers" | "profiles" | "layers" | "presets" | "spend" | "evaluations" | "airgap";

const VIEWS: { id: View; label: string }[] = [
  { id: "purposes", label: "Purposes" }, { id: "providers", label: "Providers" }, { id: "profiles", label: "Profiles" },
  { id: "layers", label: "Budgets & overrides" }, { id: "presets", label: "Presets" }, { id: "spend", label: "Spend" },
  { id: "evaluations", label: "Evaluations" }, { id: "airgap", label: "Air-gap" },
];

const LAYER_TONE: Record<string, string> = { file: "neutral", platform: "info", tenant: "warning", workspace: "success" };

export function family(model: string): string {
  return model.split("/", 1)[0];
}

export function LayerTag({ layer }: { layer?: string }) {
  if (!layer) return null;
  return <Tag tone={LAYER_TONE[layer] ?? "neutral"}>{layer}</Tag>;
}

/** Families the owner blocked (cost or compliance): never routed, whatever a preset or override says. */
export function BlockedFamilies({ families }: { families: string[] }) {
  if (!families.length) return null;
  return (
    <Notice tone="warning">
      <strong>Blocked on this platform:</strong>{" "}
      {families.map((f) => <span key={f} className="tag tag-danger" data-blocked-family={f}>{f}/* blocked</span>)}{" "}
      No route, preset, tenant or workspace override can use these families.
    </Notice>
  );
}

/** The 409 the model-switch gate answers with: per purpose, the current and new model's suite scores. */
export function switchGateOf(failure: unknown): SwitchGateResult | null {
  if (failure instanceof ApiError && failure.details && typeof failure.details === "object") {
    const gate = (failure.details as { model_switch_gate?: SwitchGateResult }).model_switch_gate;
    if (gate && Array.isArray(gate.evaluations)) return gate;
  }
  return null;
}

function score(e: ModelEvaluation | null | undefined): string {
  if (!e) return "—";
  if (e.status !== "scored" || e.score == null) return e.reason ? `unavailable: ${e.reason}` : "unavailable";
  return `${Math.round(e.score * 100)}% (${e.passed}/${e.cases})${e.reused ? " · stored" : ""}`;
}

/** Shows why a save was blocked and lets a platform administrator activate it anyway, with a reason. */
export function SwitchGatePanel({ gate, busy, onOverride }: { gate: SwitchGateResult; busy: boolean; onOverride: (reason: string) => void }) {
  const [reason, setReason] = useState("");
  return (
    <Card title="Model switch blocked by the evaluation gate" label="Model switch gate">
      <p className="muted small">A change of a purpose's primary model runs its golden suite on the current and the new model.
        A regression beyond the tolerance{gate.tolerance != null ? ` (${Math.round(gate.tolerance * 100)} points)` : ""}, or a model
        that cannot be evaluated, blocks the save.</p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Purpose</th><th>Current</th><th>New</th><th>Verdict</th></tr></thead>
          <tbody>
            {gate.evaluations.map((e) => (
              <tr key={e.purpose}>
                <td><code>{e.purpose}</code></td>
                <td className="small">{e.from ?? "—"}<div className="muted small">{score(e.current)}</div></td>
                <td className="small">{e.to}<div className="muted small">{score(e.new)}</div></td>
                <td><StatusBadge status={e.verdict === "passed" ? "ok" : "failed"} label={e.verdict} />
                  {e.reason && <div className="muted small">{e.reason}</div>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <label className="field">
        <span>Override reason (platform administrators; audited)</span>
        <input type="text" value={reason} onChange={(ev) => setReason(ev.target.value)} aria-label="Override reason"
          placeholder="Why this model may go live despite the gate" />
      </label>
      <button type="button" className="btn btn-danger" disabled={busy || !reason.trim()} onClick={() => onOverride(reason.trim())}>
        Activate anyway
      </button>
    </Card>
  );
}

export function ModelsAdmin({ purposes }: { purposes: ReactNode }) {
  const [view, setView] = useState<View>("purposes");
  const effective = useAsync(() => api.modelsEffective(), []);
  return (
    <div className="stack">
      {effective.data && <BlockedFamilies families={effective.data.blocked_families ?? []} />}
      <Tabs value={view} onChange={setView} tabs={VIEWS} />
      {view === "purposes" && purposes}
      {view === "providers" && <ProvidersView />}
      {view === "profiles" && <ProfilesView effective={effective.data} error={effective.error} />}
      {view === "layers" && <LayersView />}
      {view === "presets" && <PresetsView />}
      {view === "spend" && <SpendView />}
      {view === "evaluations" && <EvaluationsView effective={effective.data} />}
      {view === "airgap" && <AirgapView />}
    </div>
  );
}

// ------------------------------------------------------------------------------------------ providers
function ProvidersView() {
  const list = useAsync(() => api.modelProviders(), []);
  const act = useAction();
  const [tests, setTests] = useState<Record<string, ProviderTest>>({});
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  const test = async (p: ProviderView) => {
    const r = await act.run(() => api.testModelProvider(p.provider));
    if (r) setTests((t) => ({ ...t, [p.provider]: r }));
  };
  return (
    <Card title={`Providers (${list.data.providers.length})`}>
      <p className="muted small">Keys are referenced by environment variable, never shown. Test sends one fixed, billable prompt
        through the router and records it as a model call.</p>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Provider</th><th>Endpoint</th><th>Capabilities</th><th>Key</th><th>Last 24 h</th><th>Health</th><th /></tr></thead>
          <tbody>
            {list.data.providers.map((p) => {
              const t = tests[p.provider];
              return (
                <tr key={p.provider}>
                  <td><strong>{p.provider}</strong> <Tag tone={p.kind === "decision" ? "info" : "neutral"}>{p.type}</Tag>
                    <div className="muted small">{p.egress}{p.region ? ` · ${p.region}` : " · region unknown"}</div>
                    {p.profiles.length > 0 && <div className="muted small">profiles: {p.profiles.join(", ")}</div>}</td>
                  <td className="small"><code>{p.base_url || "—"}</code>{p.base_url_env && <div className="muted small">env {p.base_url_env}</div>}</td>
                  <td className="small">{Object.keys(p.capabilities).length
                    ? Object.entries(p.capabilities).map(([k, v]) => `${k}: ${String(v)}`).join(" · ") : <span className="muted">not declared</span>}</td>
                  <td>{p.key_required ? <StatusBadge status={p.key_present ? "ok" : "failed"} label={p.key_present ? "set" : "missing"} />
                    : <span className="muted small">none needed</span>}{p.api_key_env && <div className="muted small">{p.api_key_env}</div>}</td>
                  <td className="small">{p.stats_24h.calls} calls
                    {p.stats_24h.error_rate != null && <> · {Math.round(p.stats_24h.error_rate * 100)}% errors</>}
                    {p.stats_24h.latency_ms_p50 != null && <div className="muted small">p50 {p.stats_24h.latency_ms_p50} ms · p95 {p.stats_24h.latency_ms_p95} ms</div>}</td>
                  <td className="small">{p.cooldown ? <StatusBadge status="warning" label={`cooling down ${p.cooldown.remaining_seconds}s`} />
                    : p.last_success_at ? <>ok {fmtDate(p.last_success_at)}</> : <span className="muted">no success yet</span>}
                    {p.last_error && <div className="muted small">last error {fmtDate(p.last_error.at)}</div>}</td>
                  <td>
                    <button type="button" className="btn btn-sm" disabled={act.busy || p.kind === "decision"} onClick={() => void test(p)}>Test</button>
                    {t && <div className="small">{t.probed ? <StatusBadge status={t.ok ? "ok" : "failed"}
                      label={t.ok ? `${t.model} · ${t.latency_ms} ms` : (t.code ?? "failed")} /> : <span className="muted">{t.detail}</span>}
                    {t.error && <div className="muted small">{t.error}</div>}</div>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------------------ profiles
function ProfilesView({ effective, error }: { effective: EffectiveModels | null | undefined; error?: string | null }) {
  const models = useAsync(() => api.models(), []);
  if (error || models.error) return <ErrorBox error={error ?? models.error} onRetry={models.reload} />;
  if (!effective || !models.data) return <Loading />;
  const blocked = new Set(effective.blocked_families ?? []);
  const allowed = new Set(effective.allowlist);
  return (
    <Card title="Profiles">
      <p className="muted small">A profile is an ordered model list plus parameters. Struck-through models are not routed here:
        blocked family, not on the effective allowlist, or outside the residency rule.</p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Profile</th><th>Provider</th><th>Models (fallback order)</th><th>Escalation</th><th>Source</th></tr></thead>
          <tbody>
            {Object.entries(models.data.profiles).map(([name, p]) => (
              <tr key={name}>
                <td><code>{name}</code></td>
                <td className="small">{p.provider}</td>
                <td className="small">{(effective.profile_models[name] ?? p.models).map((m) => {
                  const isBlocked = blocked.has(family(m));
                  const off = isBlocked || !allowed.has(m);
                  return (
                    <span key={m} className={`tag ${isBlocked ? "tag-danger" : ""}`} style={off ? { textDecoration: "line-through" } : undefined}
                      title={effective.excluded_models[m]?.reason}>{m}{isBlocked ? " · blocked" : ""}</span>
                  );
                })}</td>
                <td className="small">{(p.escalation_models ?? []).join(" → ") || "—"}</td>
                <td><LayerTag layer={effective.sources[`profile_models.${name}`]} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------------------------------ layers
function LayersView() {
  const [workspace, setWorkspace] = useState("");
  const [applied, setApplied] = useState<string | undefined>(undefined);
  const scope: "tenant" | "workspace" = applied ? "workspace" : "tenant";
  const eff = useAsync(() => api.modelsEffective(applied), [applied]);
  const doc = useAsync(() => api.modelOverride(scope, applied), [scope, applied]);
  const act = useAction();
  const [draft, setDraft] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const gate = switchGateOf(act.failure);
  const save = async (override?: string) => {
    if (!doc.data) return;
    let parsed: Dict;
    try {
      parsed = JSON.parse(draft ?? JSON.stringify(doc.data.document ?? {}));
    } catch {
      act.setError("The override is not valid JSON.");
      return;
    }
    const r = await act.run(() => api.putModelOverride(scope, doc.data!.version, parsed, note, applied, override));
    if (r) {
      setDraft(null);
      setNote("");
      doc.reload();
      eff.reload();
    }
  };
  const rollback = async (version: number) => {
    const r = await act.run(() => api.rollbackModelOverride(scope, version, applied));
    if (r) {
      doc.reload();
      eff.reload();
    }
  };
  return (
    <div className="stack">
      <Card title="Scope">
        <form className="row" onSubmit={(e) => { e.preventDefault(); setApplied(workspace.trim() || undefined); setDraft(null); }}>
          <label className="field"><span>Workspace id (empty = your tenant)</span>
            <input type="text" value={workspace} onChange={(e) => setWorkspace(e.target.value)} aria-label="Workspace id" placeholder="ws_…" /></label>
          <button type="submit" className="btn btn-sm">Show</button>
        </form>
        <p className="muted small">Effective configuration = models file ⊕ platform settings ⊕ tenant override ⊕ workspace override.
          A tenant or workspace override can only narrow: allowlists intersect, residency shrinks, budgets take the minimum,
          a purpose can lose model rungs but never gain one.</p>
      </Card>
      {eff.error ? <ErrorBox error={eff.error} onRetry={eff.reload} /> : !eff.data ? <Loading /> : <EffectiveView e={eff.data} />}
      <Card title={`${scope === "tenant" ? "Tenant" : "Workspace"} override${doc.data ? ` · version ${doc.data.version}` : ""}`}>
        {doc.error ? <ErrorBox error={doc.error} onRetry={doc.reload} /> : !doc.data ? <Loading /> : (
          <>
            <textarea className="code-input" rows={10} aria-label="Override document (JSON)"
              value={draft ?? JSON.stringify(doc.data.document ?? {}, null, 2)} onChange={(e) => setDraft(e.target.value)} />
            <p className="muted small">Keys: <code>allowlist</code>, <code>residency</code>, <code>budgets</code>
              {" "}(<code>per_run_usd</code>, <code>per_day_usd</code>, <code>per_user_day_usd</code>), <code>purposes</code>
              {" "}(<code>mode</code> off · auto · always, <code>ladder</code>, <code>profile</code>), <code>profile_models</code>.</p>
            <label className="field"><span>Note</span>
              <input type="text" value={note} onChange={(e) => setNote(e.target.value)} aria-label="Change note" /></label>
            <ErrorBox error={gate ? null : act.error} />
            <button type="button" className="btn btn-primary" disabled={act.busy || draft == null} onClick={() => void save()}>Save new version</button>
            {gate && <SwitchGatePanel gate={gate} busy={act.busy} onOverride={(r) => void save(r)} />}
            {doc.data.history.length > 0 && (
              <ul className="list compact" aria-label="Override versions">
                {doc.data.history.map((h) => (
                  <li key={h.version} className="list-item">
                    <span>v{h.version} · {fmtDate(h.created_at)} · {h.note || "—"}</span>
                    {h.version !== doc.data!.version && (
                      <button type="button" className="btn btn-sm btn-ghost" disabled={act.busy} onClick={() => void rollback(h.version)}>Roll back to v{h.version}</button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </Card>
    </div>
  );
}

function EffectiveView({ e }: { e: EffectiveModels }) {
  const blocked = new Set(e.blocked_families ?? []);
  return (
    <Card title={`Effective configuration${e.workspace_id ? ` · ${e.workspace_id}` : ` · tenant ${e.tenant_id}`}`}>
      {e.violations.length > 0 && (
        <Notice tone="warning">Ignored because they would widen the layer above: {e.violations.map((v) => `${v.layer} ${v.path}`).join("; ")}</Notice>
      )}
      <div className="grid-2">
        <div>
          <h4>Allowlist <LayerTag layer={e.sources.allowlist} /></h4>
          <div className="chip-row">{e.allowlist.map((m) => <span key={m} className="tag">{m}</span>)}</div>
          {Object.keys(e.excluded_models).length > 0 && (
            <ul className="list compact" aria-label="Excluded models">
              {Object.entries(e.excluded_models).map(([m, x]) => (
                <li key={m} className="list-item small"><span className={`tag ${blocked.has(family(m)) ? "tag-danger" : ""}`}>{m}</span>
                  <span className="muted">{blocked.has(family(m)) ? "blocked" : "excluded"} · {x.reason} <LayerTag layer={x.layer} /></span></li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <h4>Residency <LayerTag layer={e.sources.residency} /></h4>
          <p className="small">{e.residency ? (e.residency.length ? e.residency.join(", ") : "none (no region allowed)") : "no rule"}</p>
          <h4>Budgets</h4>
          <ul className="list compact" aria-label="Budgets">
            {(["per_run_usd", "per_day_usd", "per_user_day_usd"] as const).map((k) => (
              <li key={k} className="list-item small"><span>{k.replaceAll("_", " ")}</span>
                <span>{e.budgets[k] == null ? "no limit" : fmtUsd(e.budgets[k] as number)} <LayerTag layer={e.sources[`budgets.${k}`]} /></span></li>
            ))}
          </ul>
        </div>
      </div>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th>Purpose</th><th>Mode</th><th>Profile</th><th>Models</th></tr></thead>
          <tbody>
            {Object.entries(e.purposes).map(([p, v]) => (
              <tr key={p}>
                <td><code>{p}</code></td>
                <td className="small">{v.mode} <LayerTag layer={e.sources[`purposes.${p}.ladder`]} /></td>
                <td className="small">{v.profile} <LayerTag layer={e.sources[`purposes.${p}.profile`]} /></td>
                <td className="small">{v.models.join(" → ") || (v.model_unavailable
                  ? <StatusBadge status="failed" label={`no model (${v.model_unavailable.reason})${v.rules_fallback ? " · rules answer" : ""}`} />
                  : <span className="muted">rules only</span>)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <TechnicalDetails value={e.layers} label="Layers applied" />
    </Card>
  );
}

// ------------------------------------------------------------------------------------------ presets
function PresetsView() {
  const doc = useAsync(() => api.adminSettings(), []);
  const act = useAction();
  const [pending, setPending] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const gate = switchGateOf(act.failure);
  if (doc.error) return <ErrorBox error={doc.error} onRetry={doc.reload} />;
  if (!doc.data) return <Loading />;
  const apply = async (preset: string, override?: string) => {
    setPending(preset);
    const r = await act.run(() => api.applyPreset(preset, override));
    if (r) {
      setDone(preset);
      doc.reload();
    }
  };
  return (
    <Card title="Presets">
      <p className="muted small">A preset sets when models are used per purpose. <code>pilot_low_cost</code> (the dev and pilot default,
        ADR-016) keeps deterministic rungs first, enables only the low-cost open models and JEV, and sets a $2 daily cap.{" "}
        <code>air_gapped</code> keeps deterministic rungs first,
        turns JEV off and leaves a local model as the last rung; pair it with the deployment flag (see Air-gap). A preset that changes
        a purpose's primary model passes the evaluation gate first.</p>
      <div className="chip-row">
        {Object.keys(doc.data.presets).map((p) => (
          <button key={p} type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void apply(p)}>{p}</button>
        ))}
      </div>
      {done && <Notice tone="success">Preset {done} applied (settings v{doc.data.version}).</Notice>}
      <ErrorBox error={gate ? null : act.error} />
      {gate && pending && <SwitchGatePanel gate={gate} busy={act.busy} onOverride={(r) => void apply(pending, r)} />}
    </Card>
  );
}

// ------------------------------------------------------------------------------------------ spend
function SpendView() {
  const providers = useAsync(() => api.modelProviders(), []);
  const usage = useAsync(() => api.usage(), []);
  if (providers.error || usage.error) return <ErrorBox error={providers.error ?? usage.error} onRetry={() => { providers.reload(); usage.reload(); }} />;
  if (!providers.data || !usage.data) return <Loading />;
  const s = providers.data.spend_today;
  const byModel = new Map<string, { calls: number; cost: number; complete: boolean }>();
  for (const m of usage.data.models) {
    const cur = byModel.get(m.model) ?? { calls: 0, cost: 0, complete: true };
    byModel.set(m.model, { calls: cur.calls + m.calls, cost: cur.cost + m.cost_usd, complete: cur.complete && m.cost_complete !== false });
  }
  return (
    <div className="stack">
      <Card title="Today">
        <p>{fmtUsd(s.usd)} spent{s.cap_usd != null ? <> of the {fmtUsd(s.cap_usd)} daily cap ({Math.round((s.fraction ?? 0) * 100)}%)</> : " (no daily cap)"}
          <span className="muted small"> · resets {fmtDate(s.resets_at)}</span></p>
        <ul className="list compact" aria-label="Spend by provider">
          {providers.data.providers.map((p) => (
            <li key={p.provider} className="list-item small"><span>{p.provider}</span><span>{p.calls_today} calls · {fmtUsd(p.spend_today_usd)}</span></li>
          ))}
        </ul>
      </Card>
      <Card title="All time, by model">
        {byModel.size === 0 ? <EmptyState title="No model calls yet" /> : (
          <ul className="list compact" aria-label="Spend by model">
            {[...byModel.entries()].sort((a, b) => b[1].cost - a[1].cost).map(([m, v]) => (
              <li key={m} className="list-item small"><span>{m}</span><span>{v.calls} calls · {v.complete ? fmtUsd(v.cost) : "cost incomplete"}</span></li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------------------------------ evaluations
function EvaluationsView({ effective }: { effective: EffectiveModels | null | undefined }) {
  const list = useAsync(() => api.modelEvaluations(), []);
  const act = useAction();
  const [purpose, setPurpose] = useState("sql_generation");
  const [model, setModel] = useState("");
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  const run = async () => {
    const r = await act.run(() => api.runModelEvaluation(purpose, model || undefined));
    if (r) list.reload();
  };
  const purposes = effective ? Object.keys(effective.purposes) : [purpose];
  return (
    <div className="stack">
      <Card title="Golden suites">
        <p className="muted small">Suite version {list.data.suite_version} · tolerance {Math.round(list.data.tolerance * 100)} points ·
          minimum {Math.round(list.data.min_score * 100)}%. Suites: {Object.entries(list.data.suites).map(([n, c]) => `${n} (${c} cases)`).join(", ")}.</p>
        <div className="row">
          <label className="field"><span>Purpose</span>
            <select value={purpose} onChange={(e) => setPurpose(e.target.value)} aria-label="Purpose">
              {purposes.map((p) => <option key={p} value={p}>{p}</option>)}
            </select></label>
          <label className="field"><span>Model (empty = current primary)</span>
            <select value={model} onChange={(e) => setModel(e.target.value)} aria-label="Model">
              <option value="">current primary</option>
              {(effective?.allowlist ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
            </select></label>
          <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void run()}>Run suite (billable)</button>
        </div>
        <ErrorBox error={act.error} />
      </Card>
      <Card title={`Scores (${list.data.items.length})`}>
        {list.data.items.length === 0 ? <EmptyState title="No evaluations yet" /> : (
          <div className="table-wrap">
            <table className="table table-compact">
              <thead><tr><th>When</th><th>Purpose</th><th>Model</th><th>Suite</th><th>Score</th><th className="num">Cost</th></tr></thead>
              <tbody>
                {list.data.items.map((e) => (
                  <tr key={e.id}>
                    <td className="small">{e.created_at ? fmtDate(e.created_at) : "—"}</td>
                    <td><code>{e.purpose}</code></td>
                    <td className="small">{e.model}</td>
                    <td className="small">{e.suite} v{e.suite_version}</td>
                    <td className="small">{score(e)}</td>
                    <td className="num">{fmtUsd(e.cost_usd)}</td>
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

// ------------------------------------------------------------------------------------------ air-gap
function AirgapView() {
  const p = useAsync(() => api.modelsAirgap(), []);
  if (p.error) return <ErrorBox error={p.error} onRetry={p.reload} />;
  if (!p.data) return <Loading />;
  const local = Object.entries(p.data.purposes).filter(([, v]) => v.served_locally).map(([k]) => k);
  return (
    <Card title="Air-gapped posture" actions={<StatusBadge status={p.data.ok ? "ok" : "failed"} label={p.data.ok ? "ready" : "not ready"} />}>
      <p className="muted small">Read-only check of the deployment flag, the local provider, the air_gapped preset and which purposes a
        local model answers. Setup: docs/30-runbooks/08-local-models.md.</p>
      <ul className="list compact" aria-label="Air-gap checks">
        {p.data.checks.map((c) => (
          <li key={c.name} className="list-item small">
            <span><StatusBadge status={c.ok ? "ok" : c.severity === "warning" ? "warning" : "failed"} label={c.name} /></span>
            <span className="muted">{c.detail}</span>
          </li>
        ))}
      </ul>
      <p className="small">{local.length} purpose(s) answered by a local model{local.length ? `: ${local.join(", ")}` : ""}.</p>
    </Card>
  );
}
