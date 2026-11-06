import { useId, useState, type FormEvent } from "react";
import { api, type ContextWebhook, type ContextWebhookDelivery } from "../api";
import { Card, ErrorBox, Field, Notice, StatusBadge, Tag } from "./ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";

const KINDS = ["glossary", "metric", "relationship", "verified_query", "finding", "decision", "dataset", "document_section"];
const CHANGES = [["create", "created"], ["update", "updated"], ["deprecate", "deprecated"]] as const;
const TAGS = ["pii", "sensitive", "restricted"];

/** P1-6: push context changes out. A subscription sends ledger metadata only (ids, kind, change, version) and nothing
 *  until an approver authorizes the exact filters and destination; changing either asks again. */
export function ContextWebhooks({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const list = useAsync(() => api.listContextWebhooks(wsId), [wsId]);
  const log = useAsync(() => api.listContextWebhookDeliveries(wsId), [wsId]);
  const [adding, setAdding] = useState(false);
  const act = useAction();
  const names = new Map((list.data ?? []).map((h) => [h.id, h.name]));
  const revoke = async (h: ContextWebhook) => {
    if (!window.confirm(`Revoke “${h.name}”? Queued deliveries to it are refused.`)) return;
    if (await act.run(() => api.revokeContextWebhook(h.id))) { void list.reload(); void log.reload(); }
  };
  const reauthorize = async (h: ContextWebhook) => {
    if (await act.run(() => api.reauthorizeContextWebhook(h.id))) void list.reload();
  };
  const redrive = async (d: ContextWebhookDelivery) => {
    if (await act.run(() => api.redriveContextWebhookDelivery(d.id))) void log.reload();
  };
  return (
    <Card title="Context change push" label="Context change push subscriptions"
      actions={canEdit && !adding && <button type="button" className="btn btn-sm" onClick={() => setAdding(true)}>Add webhook</button>}>
      <p className="muted small">Tell another system when shared context changes, for example when a dataset's schema changes or gains a PII
        tag. Deliveries are signed and carry ids, kind, change and version only, never the content.</p>
      {adding && <WebhookForm wsId={wsId} onCancel={() => setAdding(false)} onSaved={() => { setAdding(false); void list.reload(); }} />}
      <ErrorBox error={list.error ?? act.error} onRetry={list.reload} />
      {list.data?.length === 0 && !adding && <p className="muted small">No subscriptions yet.</p>}
      {!!list.data?.length && (
        <div className="table-wrap">
          <table className="table table-compact" aria-label="Context change subscriptions">
            <thead><tr><th>Name</th><th>What</th><th>Status</th><th /></tr></thead>
            <tbody>
              {list.data.map((h) => (
                <tr key={h.id}>
                  <td>{h.name} <Tag tone="info">{h.kind}</Tag></td>
                  <td className="small">{h.summary}</td>
                  <td>
                    <StatusBadge status={h.authorized ? "authorized" : h.status} />
                    {h.status === "pending" && <span className="muted small"> awaiting approval</span>}
                    {h.status === "lapsed" && <span className="muted small"> authorization lapsed: request it again</span>}
                    {h.authorized && h.kind === "webhook" && h.authorized_until && <span className="muted small"> until {fmtDate(h.authorized_until)}</span>}
                  </td>
                  <td>
                    {canEdit && h.kind === "webhook" && (h.status === "lapsed" || h.status === "rejected") &&
                      <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void reauthorize(h)}>Request again</button>}
                    {canEdit && h.kind === "webhook" && h.status !== "revoked" &&
                      <button type="button" className="btn btn-sm btn-danger" disabled={act.busy} onClick={() => void revoke(h)}>Revoke</button>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {!!log.data?.length && (
        <details className="recent-runs">
          <summary className="small">Delivery log ({log.data.length})</summary>
          <div className="table-wrap">
            <table className="table table-compact" aria-label="Context change deliveries">
              <thead><tr><th>Status</th><th>Change</th><th>To</th><th>Attempts</th><th>When</th><th>Error</th><th /></tr></thead>
              <tbody>
                {log.data.map((d) => (
                  <tr key={d.id}>
                    <td><StatusBadge status={d.status} /></td>
                    <td className="small mono">#{d.seq} {d.entry_id}</td>
                    <td className="small">{names.get(d.webhook_id) ?? d.webhook_id}</td>
                    <td className="small">{d.attempts}/{d.max_attempts}</td>
                    <td className="small">{fmtDate(d.delivered_at ?? d.next_attempt_at ?? d.created_at)}</td>
                    <td className="small warn-text clamp-2">{d.last_error ?? ""}</td>
                    <td>{canEdit && (d.status === "dead_letter" || d.status === "refused") &&
                      <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void redrive(d)}>Retry</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </Card>
  );
}

function Toggles({ legend, options, value, onChange }: {
  legend: string; options: readonly (readonly [string, string])[]; value: string[]; onChange: (v: string[]) => void;
}) {
  return (
    <fieldset className="autonomy">
      <legend>{legend}</legend>
      <div className="toggle-group">
        {options.map(([k, label]) => (
          <label key={k} className="toggle small">
            <input type="checkbox" checked={value.includes(k)}
              onChange={(e) => onChange(e.target.checked ? [...value, k] : value.filter((x) => x !== k))} /> {label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function WebhookForm({ wsId, onSaved, onCancel }: { wsId: string; onSaved: () => void; onCancel: () => void }) {
  const id = useId();
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secretRef, setSecretRef] = useState("env:ANALYSTOS_WEBHOOK_");
  const [kinds, setKinds] = useState<string[]>([]);
  const [changes, setChanges] = useState<string[]>([]);
  const [tags, setTags] = useState<string[]>([]);
  const [schema, setSchema] = useState(false);
  const [saved, setSaved] = useState<ContextWebhook | null>(null);
  const act = useAction();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.createContextWebhook({
      workspace: wsId, name: name.trim(), url: url.trim(), secret_ref: secretRef.trim(),
      filters: { kinds, changes, tag_added: tags, schema_changed: schema },
    }));
    if (r) setSaved(r);
  };
  if (saved) {
    return (
      <Notice tone="success">
        “{saved.name}” was registered and an authorization was requested (approval {saved.approval_id}). It sends nothing until an approver
        decides. <button type="button" className="btn btn-sm btn-ghost" onClick={onSaved}>Done</button>
      </Notice>
    );
  }
  return (
    <form className="form" onSubmit={submit} aria-label="New context change webhook">
      <Field label="Name" htmlFor={`${id}-name`}>
        <input id={`${id}-name`} value={name} onChange={(e) => setName(e.target.value)} required />
      </Field>
      <div className="form-row">
        <Field label="Webhook URL" htmlFor={`${id}-url`} hint="https only; internal addresses are refused unless an operator allows them.">
          <input id={`${id}-url`} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://hooks.example.com/context" required />
        </Field>
        <Field label="Signing secret reference" htmlFor={`${id}-sec`} hint="env:ANALYSTOS_WEBHOOK_<NAME>, never the secret itself.">
          <input id={`${id}-sec`} className="mono" value={secretRef} onChange={(e) => setSecretRef(e.target.value)} required />
        </Field>
      </div>
      <Toggles legend="Kinds (none selected: all)" options={KINDS.map((k) => [k, k] as const)} value={kinds} onChange={setKinds} />
      <Toggles legend="Changes (none selected: all)" options={CHANGES} value={changes} onChange={setChanges} />
      <Toggles legend="Dataset gained a tag (with schema change, either one qualifies)" options={TAGS.map((k) => [k, k] as const)}
        value={tags} onChange={setTags} />
      <label className="toggle small">
        <input type="checkbox" checked={schema} onChange={(e) => setSchema(e.target.checked)} /> A dataset's schema changed
      </label>
      <ErrorBox error={act.error} />
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy}>Request authorization</button>
      </div>
    </form>
  );
}
