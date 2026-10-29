import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, type DefinitionVersion, type ReadinessAssessment, type Source, type WorkspaceDetail } from "../api";
import { analysisContexts, contextSpec } from "../lib/analysisContexts";
import { jobKindsFromAvailability, type JobKind, type JobKindId } from "../lib/jobKinds";
import { useAction, useAsync } from "../lib/hooks";
import { autonomyInWords } from "../lib/status";
import { to } from "../routes";
import { ReadinessResult } from "./Brief";
import { Drawer } from "./Drawer";
import { ReviewedContext } from "./ReviewedContext";
import { optionLabel } from "../lib/format";
import { valueApi } from "../lib/investigationValue";
import { ErrorBox, Field, KeyValue, Loading, Notice, TechnicalDetails, Value } from "./ui";

/** The job kinds for a workspace, decided by the server for this caller (P4-04): executor, capabilities, role, data. */
export function useJobKinds(wsId: string) {
  const res = useAsync(() => api.jobKinds(wsId), [wsId]);
  const kinds = res.data ? jobKindsFromAvailability(res.data.job_kinds) : undefined;
  return { kinds, digest: res.data?.digest, error: res.error, reload: res.reload };
}

/** The readiness of a job kind over the caller's scope, assessed once when the kind is chosen (P4-04). */
function useReadiness(wsId: string, kind: string) {
  return useAsync<ReadinessAssessment>(() => api.assessReadiness(wsId, { job_kind: kind, assets: [], measures: [] }), [wsId, kind]);
}

interface Draft { kind?: JobKindId; objective?: string; sourceId?: string; contextId?: string; templateId?: string; period?: string; comparison?: string; focus?: string; decision?: string }
const draftKey = (ws: string) => `analystos.startWork.${ws}`;

/** The unsent brief survives a reload or a failed start (workbench-ux §2); storage may be unavailable. */
function loadDraft(ws: string): Draft {
  try {
    return JSON.parse(window.localStorage.getItem(draftKey(ws)) ?? "{}") as Draft;
  } catch {
    return {};
  }
}
function saveDraft(ws: string, d: Draft | null) {
  try {
    if (d) window.localStorage.setItem(draftKey(ws), JSON.stringify(d));
    else window.localStorage.removeItem(draftKey(ws));
  } catch {
    /* storage blocked: the draft lives for this session only */
  }
}

/** The primary action: opens Start work. */
export function StartWorkButton({ wsId, className = "btn btn-primary", label = "Start work", initialObjective }: { wsId: string; className?: string; label?: string; initialObjective?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLButtonElement>(null);
  return (
    <>
      <button type="button" ref={ref} className={className} aria-haspopup="dialog" data-tour="start-work" onClick={() => setOpen(true)}>{label}</button>
      {open && <StartWorkDialog wsId={wsId} initialObjective={initialObjective} initialKind={initialObjective ? "explain" : undefined} onClose={() => { setOpen(false); ref.current?.focus(); }} />}
    </>
  );
}

/**
 * Start work (spec v4 §15): the job kinds the server says can start here (P4-04). A kind that
 * cannot run is shown with every reason and its remediation and has no start button, so nothing
 * sends a predictably failing request. Predict and Forecast train a published ML plan (or open the spec form in Work),
 * Prepare data its panel, Monitor a new monitor; the others an investigation with a preflight.
 */
export function StartWorkDialog({ wsId, onClose, initialKind, initialObjective }: { wsId: string; onClose: () => void; initialKind?: JobKindId; initialObjective?: string }) {
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const { kinds, digest, error } = useJobKinds(wsId);
  const [draft, setDraft] = useState<Draft>(() => ({ ...loadDraft(wsId), ...(initialKind ? { kind: initialKind } : {}), ...(initialObjective ? {objective: initialObjective, templateId: undefined, contextId: undefined, sourceId: undefined} : {}) }));
  const nav = useNavigate();
  const chosen = kinds?.find((k) => k.id === draft.kind && ((k.enabled && k.action === "investigate") || (k.action === "ml" && (k.enabled || k.specFirst))));

  const update = (patch: Draft) => setDraft((d) => {
    const next = { ...d, ...patch };
    saveDraft(wsId, next);
    return next;
  });

  const pick = (k: JobKind) => {
    if (!k.enabled && !k.specFirst) return;
    if (k.action === "prepare") {
      onClose();
      nav(to.work(wsId, "prepare"));
      return;
    }
    if (k.action === "monitor") {
      onClose();
      nav(`${to.monitoring(wsId, { tab: "monitors" })}&new=1`);
      return;
    }
    update({ kind: k.id, objective: draft.objective || k.objectivePrefix || ws.data?.objective || "" });
  };

  return (
    <Drawer title={chosen ? `Start work: ${chosen.label}` : "Start work"} onClose={onClose} className="drawer-wide">
      <ErrorBox error={ws.error ?? sources.error ?? error} />
      {!kinds && !error && <Loading />}
      {kinds && !chosen && (
        <>
          <p className="small muted">What do you want to do? Choices that cannot run here say what they need.</p>
          <ul className="job-kinds" aria-label="Job kinds">
            {kinds.map((k) => (
              <li key={k.id} className={`job-kind ${k.enabled || k.specFirst ? "" : "job-kind-disabled"}`}>
                {k.enabled || k.specFirst ? (
                  <button type="button" className="job-kind-button" onClick={() => pick(k)}>
                    <strong>{k.label}</strong>
                    <span className="small muted block">{k.description}</span>
                    {k.specFirst && <span className="small block">Needs a published model spec first: write one here.</span>}
                  </button>
                ) : (
                  <div className="job-kind-button" aria-disabled="true">
                    <strong>{k.label}</strong> <span className="tag tag-neutral">not available</span>
                    <span className="small muted block">{k.description}</span>
                    <ul className="small job-kind-reasons" aria-label={`Why ${k.label} cannot start`}>
                      {k.reasons.map((r, i) => (
                        <li key={`${r.code}-${i}`} className="job-kind-reason">{r.message}{r.remediation && <span className="muted block">{r.remediation}</span>}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </li>
            ))}
          </ul>
          <TechnicalDetails value={{ digest, kinds: kinds.map((k) => ({ kind: k.id, enabled: k.enabled, uses: k.uses,
            reasons: k.reasons.map((r) => r.code), readiness_checks: k.readinessChecks })) }} label="Technical details" />
        </>
      )}
      {chosen?.action === "ml" && <MLStartForm kind={chosen} wsId={wsId}
        onBack={() => update({ kind: undefined })}
        onNewSpec={() => { saveDraft(wsId, null); onClose(); nav(to.work(wsId, "experiments", { new: chosen.id })); }}
        onStarted={(id) => { saveDraft(wsId, null); onClose(); nav(to.work(wsId, "experiments", { experiment: id })); }} />}
      {chosen && chosen.action === "investigate" && ws.data && sources.data && (
        <InvestigationForm kind={chosen} ws={ws.data} sources={sources.data} draft={draft} update={update}
          onBack={() => update({ kind: undefined })}
          onChooseKind={(id) => { const k = kinds?.find((x) => x.id === id); if (k?.enabled) pick(k); }}
          onStarted={(runId) => { saveDraft(wsId, null); onClose(); nav(to.run(wsId, runId)); }} />
      )}
    </Drawer>
  );
}

function mlTaskMatches(kind: JobKindId, definition: DefinitionVersion): boolean {
  const task = definition.spec?.task;
  return kind === "forecast" ? task === "forecast" : task === "classify" || task === "regress";
}

async function publishedMLPlans(wsId: string): Promise<DefinitionVersion[]> {
  const result: DefinitionVersion[] = [];
  let cursor: string | null = null;
  do {
    const page = await api.listDefinitions(wsId, { kind: "ml_spec", status: "published", ...(cursor ? { cursor } : {}) });
    result.push(...await Promise.all(page.items.map((d) => api.getDefinition(wsId, d.id))));
    cursor = page.next_cursor;
  } while (cursor);
  return result;
}

/** A published spec pins the dataset, target, split and budget; this form never invents a spec. */
function MLStartForm({ kind, wsId, onBack, onNewSpec, onStarted }: {
  kind: JobKind; wsId: string; onBack: () => void; onNewSpec: () => void; onStarted: (id: string) => void;
}) {
  const id = useId();
  const defs = useAsync(() => publishedMLPlans(wsId), [wsId]);
  const act = useAction();
  const [selected, setSelected] = useState("");
  const options = (defs.data ?? []).filter((d) => mlTaskMatches(kind.id, d));
  const chosen = options.find((d) => d.id === selected);
  const spec = chosen?.spec ?? {};
  const dataset = spec.dataset && typeof spec.dataset === "object" ? spec.dataset as Record<string, unknown> : {};
  const search = spec.search && typeof spec.search === "object" ? spec.search as Record<string, unknown> : {};
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!chosen) return;
    const result = await act.run(() => api.startExperiment(wsId, chosen.id));
    if (result) onStarted(result.id);
  };
  return <form className="form" onSubmit={submit} aria-label={`Start ${kind.label.toLowerCase()}`}>
    <p className="small muted">Choose a published model plan. Training reads the approved data, checks readiness and leakage, and evaluates on held-out rows.</p>
    <ErrorBox error={defs.error} onRetry={defs.reload} />
    {defs.loading && !defs.data && <Loading />}
    {defs.data && !options.length && <Notice tone="info">No published {kind.id === "forecast" ? "forecast" : "classification or regression"} plan is available yet. Write a new spec: it is proposed from your data, checked, and published before it trains.</Notice>}
    {!!options.length && <Field label="Published model plan" htmlFor={`${id}-spec`} hint="The exact version selected here will be trained.">
      <select id={`${id}-spec`} value={selected} onChange={(e) => setSelected(e.target.value)}>
        <option value="">Choose a plan…</option>
        {options.map((d) => <option key={d.id} value={d.id}>{optionLabel(d.title || d.key, 60)} · v{d.version}</option>)}
      </select>
    </Field>}
    {chosen && <section className="preflight" aria-labelledby={`${id}-pf`}>
      <h3 id={`${id}-pf`}>Before it starts</h3>
      <KeyValue items={[
        ["Task", String(spec.task || "unknown")],
        ["Reads", String(dataset.asset || "no dataset")],
        ["Target", String(spec.target || "not set")],
        ["Maximum trials", String(search.max_trials ?? "platform limit")],
        ["Maximum seconds", String(search.max_seconds ?? "platform limit")],
        ["Version", `${chosen.key} v${chosen.version}`],
      ]} />
    </section>}
    <ErrorBox error={act.error} />
    <div className="form-actions">
      <button type="button" className="btn btn-ghost" onClick={onBack}>Back</button>
      <button type="button" className="btn" onClick={onNewSpec}>Write a new spec</button>
      <button type="submit" className="btn btn-primary" disabled={!chosen || act.busy}>{act.busy ? "Training and evaluating…" : "Start experiment"}</button>
    </div>
  </form>;
}

/** The brief and a preflight: what will be read, the limits it runs under, what needs a person, and readiness. */
function InvestigationForm({ kind, ws, sources, draft, update, onBack, onChooseKind, onStarted }: {
  kind: JobKind; ws: WorkspaceDetail; sources: Source[]; draft: Draft; update: (d: Draft) => void; onBack: () => void;
  onChooseKind: (id: string) => void; onStarted: (runId: string) => void;
}) {
  const id = useId();
  const act = useAction();
  const ready = sources.filter((s) => s.status === "ready");
  const templates = useAsync(() => valueApi.templates(ws.id), [ws.id]);
  const template = templates.data?.find((t) => t.id === draft.templateId);
  const templateIncomplete = !!draft.templateId && (!template || !draft.period?.trim() || !draft.comparison?.trim() || (draft.decision?.trim().length ?? 0) < 5);
  const contexts = useAsync(() => analysisContexts(ws.id), [ws.id]);
  const publishedContexts = (contexts.data ?? []).filter((c, i, all) => c.status === "published" &&
    !all.slice(0, i).some((earlier) => earlier.key === c.key && earlier.status === "published"));
  const context = publishedContexts.find((c) => c.id === draft.contextId);
  const contextSources = context ? contextSpec(context).source_ids : [];
  const objective = draft.objective ?? "";
  const tooShort = objective.trim().length < 10;
  const needsSource = !context && ready.length > 1 && !draft.sourceId;
  const readiness = useReadiness(ws.id, kind.id);
  const refused = readiness.data?.status === "blocked" || readiness.data?.status === "unsupported";
  useEffect(() => {
    if (ready.length === 1 && draft.sourceId !== ready[0].id) update({ sourceId: ready[0].id });
  }, [ready.length]); // eslint-disable-line react-hooks/exhaustive-deps
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (tooShort || needsSource || refused || templateIncomplete) return;
    const run = await act.run(() => template ? valueApi.start(ws.id, { template_id: template.id, period: draft.period!.trim(),
      comparison: draft.comparison!.trim(), focus: draft.focus?.trim() || "All records in the selected source", decision: draft.decision!.trim(),
      source_ids: draft.sourceId ? [draft.sourceId] : undefined }) : api.startRun(ws.id, { objective: objective.trim(),
      source_ids: context ? contextSources : draft.sourceId ? [draft.sourceId] : undefined,
      analysis_context: context?.id }));
    if (run) onStarted(run.id);
  };
  const p = ws.policy ?? {};
  const reading = context ? sources.filter((s) => contextSources.includes(s.id)) : draft.sourceId ? ready.filter((s) => s.id === draft.sourceId) : ready;
  return (
    <form className="form" onSubmit={submit} aria-label={`Start ${kind.label.toLowerCase()}`}>
      <p className="small muted">{kind.description}</p>
      <Field label="Guided investigation" htmlFor={`${id}-template`} hint="A template structures the question; source permissions and approval checks still apply.">
        <select id={`${id}-template`} value={draft.templateId ?? ""} onChange={(e) => {
          const selected = templates.data?.find((t) => t.id === e.target.value);
          update({ templateId: selected?.id, contextId: undefined, ...(selected ? { objective: selected.question } : {}) });
        }}><option value="">Custom question</option>{templates.data?.map((t) => <option key={t.id} value={t.id} title={t.title}>{optionLabel(t.title)}</option>)}</select>
      </Field>
      <ErrorBox error={templates.error} onRetry={templates.reload} />
      {template && <>
        <p className="small muted">{template.description} · template v{template.version}</p>
        <Field label="Period to investigate" htmlFor={`${id}-period`}><input id={`${id}-period`} required maxLength={200} value={draft.period ?? ""} onChange={(e) => update({ period: e.target.value })} placeholder="For example, Q3 2026" /></Field>
        <Field label="Comparison baseline" htmlFor={`${id}-comparison`}><input id={`${id}-comparison`} required maxLength={200} value={draft.comparison ?? ""} onChange={(e) => update({ comparison: e.target.value })} placeholder="For example, Q2 2026 or peer teams" /></Field>
        <Field label="Focus or exclusions" htmlFor={`${id}-focus`}><input id={`${id}-focus`} maxLength={500} value={draft.focus ?? ""} onChange={(e) => update({ focus: e.target.value })} placeholder="All records in the selected source" /></Field>
        <Field label="Decision this should support" htmlFor={`${id}-decision`}><input id={`${id}-decision`} required maxLength={500} value={draft.decision ?? ""} onChange={(e) => update({ decision: e.target.value })} placeholder="Which team needs operational attention?" /></Field>
        <ReviewedContext wsId={ws.id} compact />
      </>}
      <Field label="What should the investigation find out?" htmlFor={`${id}-obj`} hint="A question or goal, at least 10 characters.">
        <textarea id={`${id}-obj`} rows={3} value={objective} readOnly={!!template} onChange={(e) => update({ objective: e.target.value })} aria-invalid={tooShort} />
      </Field>
      {!template && <Field label="Business context" htmlFor={`${id}-context`} hint="Optional. A published context pins a business purpose and sources; the question remains editable for this run.">
        <select id={`${id}-context`} value={context?.id ?? ""} disabled={!!template} onChange={(e) => {
          const picked = publishedContexts.find((c) => c.id === e.target.value);
          update({ contextId: picked?.id, sourceId: undefined, ...(picked ? { objective: contextSpec(picked).question_template } : {}) });
        }}>
          <option value="">One-off investigation</option>
          {publishedContexts.map((c) => <option key={c.id} value={c.id}>{optionLabel(c.title ?? c.key, 60)} · v{c.version}</option>)}
        </select>
      </Field>}
      {context && <p className="small muted">Purpose: {contextSpec(context).purpose}</p>}
      {contexts.error && <p className="small warn-text">Analysis contexts could not be loaded: {contexts.error}</p>}
      {!context && ready.length > 1 && (
        <Field label="Data" htmlFor={`${id}-src`} hint="One source per investigation in this release.">
          <select id={`${id}-src`} value={draft.sourceId ?? ""} onChange={(e) => update({ sourceId: e.target.value })}>
            <option value="">Choose a source…</option>
            {ready.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </Field>
      )}
      <section className="preflight" aria-labelledby={`${id}-pf`}>
        <h3 id={`${id}-pf`}>Before it starts</h3>
        <KeyValue items={[
          ["Reads", reading.length ? reading.map((s) => s.name).join(", ") : "no source chosen yet"],
          ["Spends at most", <span key="b"><Value value={p.run_cost_budget_usd} format="usd" /> per investigation</span>],
          ["Checks", "Every finding is re-tested before it is reported; unverified results are labelled."],
          ["Needs a person for", p.publish_requires_approval === false ? "nothing outside the platform by default" : "anything published or sent outside the platform"],
          ["How much it does alone", autonomyInWords(ws.autonomy_level)],
        ]} />
        <h4 className="small">Is the data ready?</h4>
        {readiness.loading && !readiness.data && <Loading label="Checking readiness…" />}
        {readiness.error && <p className="small warn-text">Readiness could not be checked: {readiness.error}. The server checks again when work starts.</p>}
        {readiness.data && <ReadinessResult assessment={readiness.data} onChooseAlternative={onChooseKind} />}
      </section>
      {tooShort && objective.length > 0 && <p className="warn-text small" role="status">Say a little more: at least 10 characters.</p>}
      {needsSource && <Notice tone="info">Choose which source to read.</Notice>}
      {refused && <Notice tone="warning">Readiness {readiness.data?.status === "blocked" ? "blocks" : "does not support"} this here: fix the failing
        checks above, or choose a supported kind explicitly.</Notice>}
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onBack}>Back</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy || tooShort || needsSource || refused || templateIncomplete}>
          {act.busy ? "Starting…" : "Start investigation"}</button>
      </div>
    </form>
  );
}
