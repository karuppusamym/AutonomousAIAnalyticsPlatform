import { useEffect, useId, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type DefinitionVersion, type Source } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { useExpertView } from "../lib/expert";
import { agentDefinitions, AgentsPanel } from "./AgentForms";
import { Drawer } from "./Drawer";
import { PlaybookEditor, SigningSteps } from "./PlaybookEditor";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge } from "./ui";

type AgentChoice = { id: string; summary: string; side_effect: string };
type StepDraft = { key: string; title: string; use: string; after: string; optional: boolean };
type Editor = { mode: "new" | "edit" | "copy"; id?: string };
const NEW_STEP: StepDraft = { key: "", title: "", use: "", after: "", optional: false };
const BUILDER_TAG = "workspace_workflow";
const KEY = /^playbook\.[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$/;
const STEP_KEY = /^[a-z_]+$/;

async function workflowDefinitions(wsId: string): Promise<DefinitionVersion[]> {
  const result: DefinitionVersion[] = [];
  let cursor: string | null = null;
  do {
    const page = await api.listDefinitions(wsId, { kind: "playbook", ...(cursor ? { cursor } : {}) });
    result.push(...page.items.filter((d) => d.kind === "playbook"));
    cursor = page.next_cursor;
  } while (cursor);
  return result;
}

const editable = (d: DefinitionVersion): boolean => Array.isArray(d.spec?.tags) && d.spec.tags.includes(BUILDER_TAG);

/** Workflows are definitions: one source of version, validation and schedule pinning. */
export function WorkflowPanel({ wsId, role }: { wsId: string; role: string | undefined }) {
  const defs = useAsync(() => workflowDefinitions(wsId), [wsId]);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [yamlId, setYamlId] = useState<string | null>(null);
  const expert = useExpertView();
  const canEdit = roleAtLeast(role, "editor");
  return <div className="stack">
  <AgentsPanel wsId={wsId} role={role} />
  <Card title="Workflows" actions={canEdit && <button type="button" className="btn btn-sm btn-primary" onClick={() => setEditor({ mode: "new" })}>Build workflow</button>}>
    <p className="small muted">Compose registered agents into steps. Drafts are checked before saving, simulated and signed off by an approver before they are published; published versions can be run manually and stay pinned for history.</p>
    <ErrorBox error={defs.error} onRetry={defs.reload} />
    {defs.loading && !defs.data && <Loading />}
    {defs.data?.length === 0 && <EmptyState title="No workspace workflows yet">Build a short workflow from registered agents.</EmptyState>}
    {!!defs.data?.length && <ul className="list" aria-label="Workflows">
      {defs.data.map((d) => <li key={d.id} className="list-item">
        <div><strong>{d.title || d.key}</strong> <span className="small muted">{d.key} · v{d.version}</span>
          <div><StatusBadge status={d.status} /></div></div>
        <div className="chip-row">
          {canEdit && (d.status === "draft" || d.status === "tested") && <button type="button" className="btn btn-xs" onClick={() => setEditor({ mode: "edit", id: d.id })}>Edit</button>}
          {expert && <button type="button" className="btn btn-xs btn-ghost" onClick={() => setYamlId(d.id)}>Open in YAML editor<span className="sr-only"> {d.key} v{d.version}</span></button>}
          {canEdit && d.status === "published" && <button type="button" className="btn btn-xs" onClick={() => setEditor({ mode: "copy", id: d.id })}>New version</button>}
          {d.status === "published" && <button type="button" className="btn btn-xs btn-primary" onClick={() => setRunId(d.id)}>Run</button>}
        </div>
      </li>)}
    </ul>}
    {editor && <WorkflowEditor wsId={wsId} editor={editor} onClose={() => setEditor(null)}
      onSaved={() => { setEditor(null); void defs.reload(); }} />}
    {runId && <RunWorkflow wsId={wsId} definitionId={runId} onClose={() => setRunId(null)} />}
  </Card>
  <PlaybookEditor wsId={wsId} role={role} openId={yamlId} onChanged={() => void defs.reload()} />
  </div>;
}

function WorkflowEditor({ wsId, editor, onClose, onSaved }: { wsId: string; editor: Editor; onClose: () => void; onSaved: () => void }) {
  const id = useId();
  const agents = useAsync(() => api.listCapabilities({ kind: "Agent", workspace_id: wsId }), [wsId]);
  const wsAgents = useAsync(() => agentDefinitions(wsId, "published"), [wsId]);
  const playbooks = useAsync(() => api.listCapabilities({ kind: "Playbook" }), []);
  const original = useAsync(() => editor.id ? api.getDefinition(wsId, editor.id) : Promise.resolve(null), [wsId, editor.id]);
  const act = useAction();
  const [title, setTitle] = useState("");
  const [slug, setSlug] = useState("");
  const [summary, setSummary] = useState("");
  const [steps, setSteps] = useState<StepDraft[]>([{ ...NEW_STEP }]);
  const [saved, setSaved] = useState<DefinitionVersion | null>(null);
  const source = original.data;
  useEffect(() => {
    if (!source) return;
    setTitle(source.title || "");
    setSlug(source.key.replace(/^playbook\./, ""));
    setSummary(String(source.spec?.summary || ""));
    const raw = source.spec?.spec && typeof source.spec.spec === "object" ? source.spec.spec as Record<string, unknown> : {};
    const rows = Array.isArray(raw.steps) ? raw.steps as Record<string, unknown>[] : [];
    setSteps(rows.map((s) => ({ key: String(s.key || ""), title: String(s.title || ""), use: String(s.use || ""),
      after: Array.isArray(s.after) ? String(s.after[0] || "") : "", optional: Boolean(s.optional) })));
  }, [source]);
  const installed: AgentChoice[] = (agents.data?.capabilities ?? []).filter((a) => a.entry && a.available !== false && a.enabled !== false
    && a.side_effect !== "write_external" && a.certification.status !== "deprecated");
  // The workspace's own published agents (P7-19); a run binds their newest published version.
  const own: AgentChoice[] = [...new Map((wsAgents.data ?? []).filter((d) => !installed.some((a) => a.id === d.key))
    .map((d) => [d.key, { id: d.key, summary: `${d.title || d.key} (workspace agent)`, side_effect: "write_internal" }])).values()];
  const choices = [...installed, ...own];
  const key = `playbook.${slug.trim()}`;
  const duplicate = !!playbooks.data?.capabilities.some((p) => p.id === key) && editor.mode === "new";
  const stepKeys = steps.map((s) => s.key);
  const problem = !KEY.test(key) ? "Use a short lowercase workflow key, such as weekly_sla."
    : duplicate ? "That key belongs to an installed playbook. Choose another."
      : title.trim().length < 3 ? "Give this workflow a title."
        : summary.trim().length < 10 ? "Describe what the workflow accomplishes in at least 10 characters."
          : steps.length === 0 ? "Add at least one step."
            : steps.some((s) => !STEP_KEY.test(s.key) || !s.title.trim() || !choices.some((a) => a.id === s.use))
              ? "Each step needs a lowercase key, a title and an available agent."
              : new Set(stepKeys).size !== stepKeys.length ? "Step keys must be unique."
                : steps.some((s, i) => s.after && !stepKeys.slice(0, i).includes(s.after)) ? "A step can depend only on an earlier step."
                  : null;
  const setStep = (index: number, patch: Partial<StepDraft>) => setSteps((prev) => prev.map((s, i) => i === index ? { ...s, ...patch } : s));
  const removeStep = (index: number) => setSteps((prev) => {
    const removed = prev[index].key;
    return prev.filter((_, i) => i !== index).map((s) => s.after === removed ? { ...s, after: "" } : s);
  });
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (problem) return;
    const used = choices.filter((a) => steps.some((s) => s.use === a.id));
    const effects = ["none", "read_source", "write_internal"];
    const effect = used.reduce((max, a) => Math.max(max, effects.indexOf(a.side_effect)), 0);
    const spec = { apiVersion: "analystos/v1", kind: "Playbook", id: key,
      version: editor.mode === "copy" ? `${(source?.version ?? 1) + 1}.0.0`
        : editor.mode === "edit" ? String(source?.spec?.version || "1.0.0") : "1.0.0",
      summary: summary.trim(), determinism: "model", side_effect: effects[effect], cost_class: "llm_large",
      certification: { status: "draft" }, tags: [BUILDER_TAG], spec: { framing: false,
        steps: steps.map((s) => ({ key: s.key, title: s.title.trim(), use: s.use,
          after: s.after ? [s.after] : [], optional: s.optional })) } };
    const row = await act.run(() => editor.mode === "edit" && source
      ? api.updateDefinition(wsId, source.id, source.revision, { title: title.trim(), spec })
      : api.createDefinition(wsId, { kind: "playbook", key, title: title.trim(), spec }));
    if (row) setSaved(row);
  };
  if (source && !editable(source)) return <Drawer title="Edit workflow" onClose={onClose}>
    <Notice tone="warning">This workflow has advanced manifest fields. Its version is preserved; edit it through the manifest API.</Notice>
  </Drawer>;
  return <Drawer title={editor.mode === "new" ? "Build workflow" : editor.mode === "copy" ? "New workflow version" : "Edit workflow"} onClose={onClose} className="drawer-wide">
    <ErrorBox error={agents.error ?? wsAgents.error ?? playbooks.error ?? original.error} />
    {(!agents.data || !playbooks.data || (editor.id && !original.data)) && <Loading />}
    {agents.data && !choices.length && <Notice tone="warning">No available agents with a default action are enabled here. Ask an owner to enable agents in Capabilities.</Notice>}
    {!saved && <form className="form" onSubmit={save} aria-label="Workflow builder">
      <Field label="Title" htmlFor={`${id}-title`}><input id={`${id}-title`} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      <Field label="Key" htmlFor={`${id}-key`} hint="A stable identifier; published versions keep this key."><div className="form-inline">playbook.<input id={`${id}-key`} value={slug} disabled={editor.mode !== "new"} onChange={(e) => setSlug(e.target.value)} /></div></Field>
      <Field label="Purpose" htmlFor={`${id}-summary`}><textarea id={`${id}-summary`} rows={2} value={summary} onChange={(e) => setSummary(e.target.value)} /></Field>
      <h3>Steps</h3><p className="small muted">Choose an available agent for each step. A dependency waits for the earlier step; no dependency lets steps run together. External writes require a separately governed playbook.</p>
      {steps.map((s, index) => <fieldset key={index} className="card"><legend>Step {index + 1}</legend>
        <div className="form-grid">
          <Field label="Step key" htmlFor={`${id}-step-${index}-key`}><input id={`${id}-step-${index}-key`} value={s.key}
            onChange={(e) => setStep(index, { key: e.target.value })} /></Field>
          <Field label="Title" htmlFor={`${id}-step-${index}-title`}><input id={`${id}-step-${index}-title`} value={s.title}
            onChange={(e) => setStep(index, { title: e.target.value })} /></Field>
          <Field label="Agent" htmlFor={`${id}-step-${index}-agent`}><select id={`${id}-step-${index}-agent`} value={s.use}
            onChange={(e) => setStep(index, { use: e.target.value })}><option value="">Choose an agent…</option>
            {choices.map((a) => <option key={a.id} value={a.id}>{a.id} — {a.summary}</option>)}</select></Field>
          <Field label="Wait for" htmlFor={`${id}-step-${index}-after`}><select id={`${id}-step-${index}-after`} value={s.after}
            onChange={(e) => setStep(index, { after: e.target.value })}><option value="">No dependency</option>
            {steps.slice(0, index).filter((prev) => STEP_KEY.test(prev.key)).map((prev) => <option key={prev.key} value={prev.key}>{prev.title || prev.key}</option>)}</select></Field>
        </div>
        <label><input type="checkbox" checked={s.optional} onChange={(e) => setStep(index, { optional: e.target.checked })} /> Optional step</label>
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => removeStep(index)}>Remove step</button>
      </fieldset>)}
      <button type="button" className="btn btn-sm" onClick={() => setSteps((prev) => [...prev, { ...NEW_STEP }])}>Add step</button>
      {problem && <p className="small warn-text" role="status">{problem}</p>}
      <ErrorBox error={act.error} />
      <div className="form-actions"><button type="submit" className="btn btn-primary" disabled={!!problem || act.busy || !agents.data}>Save draft</button></div>
    </form>}
    {saved && <div className="stack"><Notice tone="success">Draft v{saved.version} saved and validated.</Notice>
      <p className="small">Publishing freezes this version and enables it here. It needs a passing simulation and a signature approved by
        someone else first; any later change needs a new version and a new sign-off.</p>
      <SigningSteps wsId={wsId} draft={saved} onChange={setSaved} onPublished={onSaved} />
      <div className="form-actions"><button type="button" className="btn" onClick={onSaved}>Finish later</button></div>
      <Link to={to.data(wsId, "definitions")} onClick={onClose}>See all definitions</Link>
    </div>}
  </Drawer>;
}

function RunWorkflow({ wsId, definitionId, onClose }: { wsId: string; definitionId: string; onClose: () => void }) {
  const id = useId();
  const nav = useNavigate();
  const definition = useAsync(() => api.getDefinition(wsId, definitionId), [wsId, definitionId]);
  const sources = useAsync(() => api.listSources(wsId), [wsId]);
  const act = useAction();
  const [objective, setObjective] = useState("");
  const [source, setSource] = useState("");
  const ready = (sources.data ?? []).filter((s: Source) => s.status === "ready");
  const selected = source || (ready.length === 1 ? ready[0].id : "");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!selected || objective.trim().length < 10) return;
    const request: Parameters<typeof api.startRun>[1] = { objective: objective.trim(), source_ids: [selected], definition: definitionId };
    if ((definition.data?.spec?.certification as { status?: string } | undefined)?.status !== "certified") request.autonomy_level = 2;
    const run = await act.run(() => api.startRun(wsId, request));
    if (run) { onClose(); nav(to.run(wsId, run.id)); }
  };
  return <Drawer title="Run workflow" onClose={onClose}>
    <ErrorBox error={definition.error ?? sources.error} />
    {(!definition.data || !sources.data) && <Loading />}
    {definition.data && <form className="form" onSubmit={submit} aria-label="Run workflow">
      <p><strong>{definition.data.title || definition.data.key}</strong> · v{definition.data.version}</p>
      <p className="small muted">This run uses the exact published version. Workflows without certification run with reduced autonomy; approval gates in the plan still apply.</p>
      <Field label="Goal" htmlFor={`${id}-goal`}><textarea id={`${id}-goal`} rows={3} value={objective} onChange={(e) => setObjective(e.target.value)} /></Field>
      <Field label="Data source" htmlFor={`${id}-source`}><select id={`${id}-source`} value={selected} onChange={(e) => setSource(e.target.value)}>
        <option value="">Choose a ready source…</option>{ready.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></Field>
      {!ready.length && <Notice tone="warning">Connect and select a source in Data before running this workflow.</Notice>}
      <ErrorBox error={act.error} />
      <button type="submit" className="btn btn-primary" disabled={!selected || objective.trim().length < 10 || act.busy}>Start workflow</button>
    </form>}
  </Drawer>;
}
