import { useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { api, type DefinitionVersion, type PlaybookCheck, type PlaybookDraft, type SimulationReport } from "../api";
import { useExpertView } from "../lib/expert";
import { shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { roleAtLeast, to } from "../routes";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, StatusBadge, TechnicalDetails } from "./ui";

const OUTCOME: Record<string, { label: string; tone: "success" | "warning" | "danger" | "info" }> = {
  completed: { label: "Completes", tone: "success" },
  completed_with_limitations: { label: "Stops at a budget limit", tone: "warning" },
  rejected: { label: "Stops: approval rejected", tone: "warning" },
  failed: { label: "Fails", tone: "danger" },
  refused: { label: "Refused in this workspace", tone: "danger" },
};

const SIGNATURE: Record<string, string> = {
  unsigned: "Not signed. Simulate the draft, then request a signature.",
  requested: "Signature requested. An approver other than you must approve it in Approvals.",
  approved: "Sign-off approved. Publishing signs this version with the workspace's tenant key and enables it.",
  rejected: "The sign-off was rejected. Change the playbook or request a signature again.",
  invalidated: "The sign-off no longer applies: the playbook changed after it was requested.",
  stale: "The sign-off covered other content. Simulate again and request a new signature.",
  signed: "Signed and published. Any change needs a new version, a new simulation and a new sign-off.",
};

/**
 * Playbook editor (S06-T6, spec §3.7): write a workspace playbook as YAML, check it against the playbook
 * contract and the capability registry, dry-run it against fixture workspaces, then sign and publish it.
 * An engineering tool: shown only with expert tools on. The server decides everything shown here
 * (validation, simulation outcome, who may sign); a changed playbook always needs a new sign-off.
 */
export function PlaybookEditor({ wsId, role, openId, onChanged }: {
  wsId: string; role: string | undefined; openId?: string | null; onChanged?: () => void;
}) {
  const expert = useExpertView();
  if (!expert) {
    return <Card title="Playbook editor">
      <Notice>Turn on <strong>Show expert tools</strong> in the menu to write playbooks as YAML, simulate and sign them.</Notice>
    </Card>;
  }
  return <Editor wsId={wsId} role={role} openId={openId ?? null} onChanged={onChanged} />;
}

function Editor({ wsId, role, openId, onChanged }: { wsId: string; role: string | undefined; openId: string | null; onChanged?: () => void }) {
  const id = useId();
  const options = useAsync(() => api.playbookEditorOptions(wsId), [wsId]);
  const [yaml, setYaml] = useState("");
  const [title, setTitle] = useState("");
  const [draft, setDraft] = useState<PlaybookDraft | null>(null);
  const [check, setCheck] = useState<PlaybookCheck | null>(null);
  const [report, setReport] = useState<SimulationReport | null>(null);
  const [fixture, setFixture] = useState("supervised");
  const [dirty, setDirty] = useState(false);
  const act = useAction();
  const canEdit = roleAtLeast(role, "editor");
  const editable = !draft || draft.status === "draft" || draft.status === "tested";

  useEffect(() => {
    if (options.data?.template && !yaml && !openId) setYaml(options.data.template);
  }, [options.data, yaml, openId]);
  useEffect(() => {
    if (!openId) return;
    void act.run(() => api.getPlaybookDraft(wsId, openId)).then((d) => {
      if (!d) return;
      setDraft(d); setYaml(d.yaml); setTitle(d.title ?? ""); setDirty(false); setCheck(null); setReport(null);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId, openId]);

  const changed = (text: string) => { setYaml(text); setDirty(true); setCheck(null); };
  const validate = async () => { const c = await act.run(() => api.validatePlaybook(wsId, yaml)); if (c) setCheck(c); };
  const simulate = async () => {
    const r = await act.run(() => api.simulatePlaybook(wsId, dirty || !draft ? { yaml, fixture } : { definition_id: draft.id, fixture }));
    if (r) setReport(r);
  };
  const save = async () => {
    const d = await act.run(() => draft && editable
      ? api.updatePlaybookDraft(wsId, draft.id, draft.revision, yaml, title)
      : api.createPlaybookDraft(wsId, yaml, title));
    if (d) { setDraft(d); setYaml(d.yaml); setDirty(false); onChanged?.(); }
  };
  const fresh = () => { setDraft(null); setYaml(options.data?.template ?? ""); setTitle(""); setDirty(false); setCheck(null); setReport(null); };

  if (options.error) return <Card title="Playbook editor"><ErrorBox error={options.error} onRetry={options.reload} /></Card>;
  if (!options.data) return <Card title="Playbook editor"><Loading /></Card>;
  return (
    <Card title="Playbook editor (YAML)" actions={<button type="button" className="btn btn-sm" onClick={fresh}>New from template</button>}>
      <p className="small muted">A playbook is a <code>kind: Playbook</code> manifest whose steps use registered agents. Validation lists every
        problem; simulation dry-runs it against a fixture workspace without running SQL, models or side effects. A playbook is enabled
        only when it is published, and it publishes only after a passing simulation and a signature approved by someone else.</p>
      {draft && <p className="small"><strong>{draft.title || draft.key}</strong> · v{draft.version} <StatusBadge status={draft.status} />
        {dirty && <span className="tag">unsaved changes</span>}</p>}
      {!editable && <Notice tone="info">This version is {draft?.status} and immutable. Saving creates the next draft version.</Notice>}
      <div className="form">
        <Field label="Title" htmlFor={`${id}-title`}><input id={`${id}-title`} value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
        <Field label="Playbook YAML" htmlFor={`${id}-yaml`} hint="Steps, approval gates, default limits and replan boundaries. Text in the YAML is data; it cannot add tools.">
          <textarea id={`${id}-yaml`} className="code-input" rows={22} spellCheck={false} value={yaml}
            onChange={(e) => changed(e.target.value)} style={{ fontFamily: "var(--font-mono, monospace)", width: "100%" }} />
        </Field>
        <div className="form-actions">
          <button type="button" className="btn" disabled={act.busy || !yaml.trim()} onClick={() => void validate()}>Validate</button>
          {canEdit && <button type="button" className="btn btn-primary" disabled={act.busy || !yaml.trim()} onClick={() => void save()}>
            {draft && editable ? "Save draft" : "Save as new draft"}</button>}
          {canEdit && <>
            <label className="small" htmlFor={`${id}-fixture`}>Fixture</label>
            <select id={`${id}-fixture`} value={fixture} onChange={(e) => setFixture(e.target.value)}>
              {(options.data.fixtures ?? []).map((f) => <option key={f.name} value={f.name} title={f.description}>{f.name}</option>)}
            </select>
            <button type="button" className="btn" disabled={act.busy || !yaml.trim()} onClick={() => void simulate()}>Simulate</button>
          </>}
        </div>
      </div>
      <ErrorBox error={act.error} />
      {check && <CheckResult check={check} />}
      {report && <SimulationView report={report} />}
      {draft && editable && !dirty && canEdit && <SigningSteps wsId={wsId} draft={draft} onChange={(d) => { setDraft(d); onChanged?.(); }} />}
      {draft && dirty && editable && <p className="small muted">Save the draft to simulate and sign it.</p>}
    </Card>
  );
}

function CheckResult({ check }: { check: PlaybookCheck }) {
  if (!check.ok) {
    return <Notice tone="danger"><strong>{check.problems.length} problem{check.problems.length === 1 ? "" : "s"}</strong>
      <ul aria-label="Validation problems">{check.problems.map((p) => <li key={p}>{p}</li>)}</ul></Notice>;
  }
  return <div className="stack">
    <Notice tone="success">Valid playbook <code>{check.key}</code> · content {shortHash(check.content_hash ?? "", 12)} ·
      {" "}{check.steps?.length ?? 0} steps · approval gates: {check.approval_gates?.join(", ") || "none"}</Notice>
    {!!check.warnings?.length && <Notice tone="warning"><ul aria-label="Validation warnings">{check.warnings.map((w) => <li key={w}>{w}</li>)}</ul></Notice>}
    {check.diff?.against && <details><summary>Changes against {check.diff.against} ({check.diff.changes.length})</summary>
      <DiffList changes={check.diff.changes} /></details>}
  </div>;
}

function DiffList({ changes }: { changes: { path: string; from: unknown; to: unknown }[] }) {
  if (!changes.length) return <p className="small muted">No changes.</p>;
  return <ul className="list compact" aria-label="Changes">
    {changes.map((c) => <li key={c.path} className="small"><code>{c.path}</code>: {JSON.stringify(c.from)} → {JSON.stringify(c.to)}</li>)}
  </ul>;
}

export function SimulationView({ report }: { report: SimulationReport }) {
  const o = OUTCOME[report.outcome] ?? { label: report.outcome, tone: "info" as const };
  const used = report.budget.used;
  return <section className="stack" aria-label="Simulation">
    <Notice tone={o.tone}><strong>{o.label}</strong> with fixture <code>{report.fixture.name}</code>{report.reason ? `: ${report.reason}` : ""}.
      {" "}Steps {used.steps}{report.budget.limits.max_steps ? ` of ${report.budget.limits.max_steps}` : ""}, retries {used.retries},
      replans {used.replans}, estimated model calls {used.llm_calls}. Nothing was executed.</Notice>
    {!!report.warnings.length && <Notice tone="warning"><ul aria-label="Simulation warnings">{report.warnings.map((w) => <li key={w}>{w}</li>)}</ul></Notice>}
    <div className="table-wrap">
      <table className="table" aria-label="Simulated steps">
        <thead><tr><th>#</th><th>Step</th><th>Agent</th><th>Result</th><th>What happens</th></tr></thead>
        <tbody>{report.timeline.map((e) => <tr key={e.seq}>
          <td className="small">{e.seq}</td><td><code>{e.key}</code></td><td className="small">{e.agent ?? "—"}</td>
          <td><StatusBadge status={e.status} label={e.status.replace(/_/g, " ")} /></td><td className="small">{e.detail}</td>
        </tr>)}</tbody>
      </table>
    </div>
    {report.approvals.length > 0 && <div>
      <h4>Approval gates hit</h4>
      <ul className="list compact" aria-label="Approval gates hit">{report.approvals.map((a) => <li key={a.id} className="small">
        <code>{a.gate}</code> ({a.payload}{a.for ? ` for ${a.for}` : ""}) — {a.decision}, now {a.status}; binds the {a.binds}</li>)}</ul>
    </div>}
    <TechnicalDetails value={report} label="Full simulation report" />
  </section>;
}

/** Simulate → request signature → someone else approves → publish (signs and enables). */
export function SigningSteps({ wsId, draft, onChange, onPublished }: {
  wsId: string; draft: DefinitionVersion & Partial<PlaybookDraft>; onChange: (d: PlaybookDraft) => void; onPublished?: () => void;
}) {
  const act = useAction();
  const signature = draft.signature?.state ?? "unsigned";
  const simulated = draft.status === "tested" && draft.test_evidence?.content_hash === draft.content_hash;
  const signed = signature === "approved";
  const reload = async () => { const d = await act.run(() => api.getPlaybookDraft(wsId, draft.id)); if (d) onChange(d); };
  const simulate = async () => {
    const d = await act.run(() => api.simulatePlaybookDraft(wsId, draft.id, draft.revision, ["supervised", "autonomous"]));
    if (d) onChange(d);
  };
  const request = async () => {
    const r = await act.run(() => api.requestPlaybookSignature(wsId, draft.id, draft.revision));
    if (r) onChange(r.definition);
  };
  const publish = async () => {
    const d = await act.run(() => api.publishDefinition(wsId, draft.id, draft.revision));
    if (d) { await reload(); onPublished?.(); }
  };
  return <section className="stack" aria-label="Signing">
    <h4>Sign and enable</h4>
    <p className="small">{SIGNATURE[signature] ?? signature}</p>
    <ol className="list compact">
      <li><strong>1. Simulate</strong> against the supervised and autonomous fixtures.{" "}
        {simulated ? <span className="tag">passed</span>
          : <button type="button" className="btn btn-xs" disabled={act.busy} onClick={() => void simulate()}>Simulate draft</button>}
        {draft.test_evidence?.simulations && <span className="small muted"> {draft.test_evidence.simulations.map((s) => `${s.fixture}: ${s.outcome}`).join(", ")}</span>}</li>
      <li><strong>2. Request a signature</strong> bound to this content ({shortHash(draft.content_hash, 12)}).{" "}
        {signature === "requested" || signed ? <span className="tag">requested</span>
          : <button type="button" className="btn btn-xs" disabled={act.busy || !simulated} onClick={() => void request()}>Request signature</button>}</li>
      <li><strong>3. An approver signs off</strong> in <Link to={to.approvals(wsId)}>Approvals</Link> (never the requester).{" "}
        {signed ? <span className="tag">approved</span>
          : <button type="button" className="btn btn-xs btn-ghost" disabled={act.busy} onClick={() => void reload()}>Check status</button>}</li>
      <li><strong>4. Publish</strong>: the platform verifies the sign-off, signs the version and enables it.{" "}
        <button type="button" className="btn btn-xs btn-primary" disabled={act.busy || !signed} onClick={() => void publish()}>Publish and enable</button></li>
    </ol>
    <ErrorBox error={act.error} />
  </section>;
}

/** Workspace playbook versions to open in the editor. */
export function PlaybookVersions({ items, onOpen }: { items: DefinitionVersion[]; onOpen: (id: string) => void }) {
  if (!items.length) return <EmptyState title="No workspace playbooks yet">Start from the template in the editor.</EmptyState>;
  return <ul className="list compact" aria-label="Playbook versions">
    {items.map((d) => <li key={d.id} className="list-item"><span><code>{d.key}</code> v{d.version} <StatusBadge status={d.status} /></span>
      <button type="button" className="btn btn-xs" onClick={() => onOpen(d.id)}>Open in YAML editor<span className="sr-only"> {d.key} v{d.version}</span></button></li>)}
  </ul>;
}
