import { useId, useState, type FormEvent } from "react";
import { api, type Owners, type PilotCheck } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { Card, ErrorBox, Field, Loading, Notice, Tag } from "./ui";

const TONE: Record<PilotCheck["status"], string> = { pass: "success", missing: "warning", fail: "danger" };
const CHECK_LABEL: Record<string, string> = {
  "owner.business": "Business owner", "owner.technical": "Technical owner", "connector.certified": "Connector certified live",
  "connector.health": "Connection", "identity.sso": "Single sign-on", "recovery.drill": "Recovery drill",
  "conversation.definitions": "Reviewed definitions for Ask and dashboards", "pilot.rehearsal": "Task cards rehearsed",
};

/** Form values for the two named owners; empty name and email clear a role. */
export function ownersBody(f: Record<string, string>): { body: { business?: { name: string; email: string } | null; technical?: { name: string; email: string } | null }; problems: string[] } {
  const problems: string[] = [];
  const one = (role: "business" | "technical") => {
    const name = (f[`${role}Name`] ?? "").trim();
    const email = (f[`${role}Email`] ?? "").trim();
    if (!name && !email) return null;
    if (!name) problems.push(`Name the ${role} owner.`);
    if (!/^[^@\s]+@[^@\s]+$/.test(email)) problems.push(`The ${role} owner needs an email address.`);
    return { name, email };
  };
  return { body: { business: one("business"), technical: one("technical") }, problems };
}

function OwnersForm({ label, owners, onSave }: { label: string; owners: Owners | undefined; onSave: (body: ReturnType<typeof ownersBody>["body"]) => Promise<unknown> }) {
  const id = useId();
  const [f, setF] = useState<Record<string, string>>({
    businessName: owners?.business?.name ?? "", businessEmail: owners?.business?.email ?? "",
    technicalName: owners?.technical?.name ?? "", technicalEmail: owners?.technical?.email ?? "",
  });
  const [tried, setTried] = useState(false);
  const act = useAction();
  const { body, problems } = ownersBody(f);
  const set = (k: string, v: string) => setF((x) => ({ ...x, [k]: v }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTried(true);
    if (!problems.length) await act.run(() => onSave(body));
  };
  return (
    <form className="form" onSubmit={submit} aria-label={`Owners of ${label}`} noValidate>
      <div className="form-row">
        <Field label="Business owner" htmlFor={`${id}-bn`}><input id={`${id}-bn`} value={f.businessName} onChange={(e) => set("businessName", e.target.value)} /></Field>
        <Field label="Business owner email" htmlFor={`${id}-be`}><input id={`${id}-be`} type="email" value={f.businessEmail} onChange={(e) => set("businessEmail", e.target.value)} /></Field>
      </div>
      <div className="form-row">
        <Field label="Technical owner" htmlFor={`${id}-tn`}><input id={`${id}-tn`} value={f.technicalName} onChange={(e) => set("technicalName", e.target.value)} /></Field>
        <Field label="Technical owner email" htmlFor={`${id}-te`}><input id={`${id}-te`} type="email" value={f.technicalEmail} onChange={(e) => set("technicalEmail", e.target.value)} /></Field>
      </div>
      {tried && problems.length > 0 && <Notice tone="warning">{problems.join(" ")}</Notice>}
      <ErrorBox error={act.error} />
      <button type="submit" className="btn btn-sm" disabled={act.busy}>Save owners of {label}</button>
    </form>
  );
}

/**
 * Pilot readiness (P4-09): what a controlled pilot of this workspace still lacks, check by check, where it is
 * missing — named owners of the workspace and each source, live connector certification, the last connection,
 * single sign-on and the recovery drill — with owners editable in place (workspace owners only).
 */
export function PilotReadiness({ wsId, owners, canEdit }: { wsId: string; owners: Owners | undefined; canEdit: boolean }) {
  const r = useAsync(() => api.pilotReadiness(wsId), [wsId]);
  const sources = useAsync(() => (canEdit ? api.listSources(wsId) : Promise.resolve([])), [wsId, canEdit]);
  const [wsOwners, setWsOwners] = useState<Owners | undefined>(owners);
  const reload = () => { void r.reload(); void sources.reload(); };
  if (r.error) return <Card title="Pilot readiness"><ErrorBox error={r.error} onRetry={r.reload} /></Card>;
  if (!r.data || !Array.isArray(r.data.checks)) return <Card title="Pilot readiness"><Loading /></Card>;
  const gaps = r.data.checks.filter((c) => c.status !== "pass");
  return (
    <Card title="Pilot readiness" label="Pilot readiness"
      actions={<Tag tone={r.data.verdict === "ready" ? "success" : "warning"}>{r.data.verdict === "ready" ? "Ready" : `${gaps.length} to resolve`}</Tag>}>
      <table className="table" aria-label="Pilot readiness checks">
        <thead><tr><th>Check</th><th>Where</th><th>Status</th><th>Why</th></tr></thead>
        <tbody>
          {r.data.checks.map((c, i) => (
            <tr key={`${c.check}-${c.subject}-${i}`}>
              <td>{CHECK_LABEL[c.check] ?? c.check}</td>
              <td className="small">{c.subject}</td>
              <td><Tag tone={TONE[c.status]}>{c.status}</Tag></td>
              <td className="small">{c.reason}{c.remediation && c.status !== "pass" ? <div className="muted">{c.remediation}</div> : null}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {canEdit && (
        <details>
          <summary>Name the owners</summary>
          <OwnersForm label="this workspace" owners={wsOwners}
            onSave={async (body) => { setWsOwners((await api.putWorkspaceOwners(wsId, body)).owners); reload(); }} />
          {(sources.data ?? []).filter((s) => s.kind !== "recipe").map((s) => (
            <OwnersForm key={s.id} label={s.name} owners={s.owners}
              onSave={async (body) => { await api.putSourceOwners(wsId, s.id, body); reload(); }} />
          ))}
        </details>
      )}
    </Card>
  );
}
