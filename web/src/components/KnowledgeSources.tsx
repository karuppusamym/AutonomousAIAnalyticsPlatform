import { useId, useState, type FormEvent } from "react";
import { request } from "../api";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Card, EmptyState, ErrorBox, Field, Loading, Notice, Tag } from "./ui";

interface SyncReport {
  fetched?: number; written?: number; skipped_unchanged?: number; truncated?: boolean; revision?: number | null;
  failed?: { path: string; error: string }[]; skipped_too_large?: string[]; removed_remote?: string[]; error?: string;
}
interface KnowledgeSource {
  id: string; kind: "github" | "confluence"; name: string; config: Record<string, string>; secret_ref: string | null;
  enabled: boolean; tracked_files: number; last_sync_at: string | null; last_status: string | null; last_report: SyncReport;
  /** The second-person approval of (host, target, secret_ref); a source syncs only once it is approved. */
  binding_approval_id?: string | null; binding_status?: string;
}

const BINDING: Record<string, { label: string; tone: string }> = {
  approved: { label: "approved", tone: "success" }, pending: { label: "awaiting approval", tone: "warning" },
  rejected: { label: "approval rejected", tone: "danger" }, invalidated: { label: "approval out of date", tone: "danger" },
  expired: { label: "approval expired", tone: "danger" }, none: { label: "no approval yet", tone: "warning" },
};

const base = (ws: string) => `/api/workspaces/${encodeURIComponent(ws)}/knowledge/sources`;

/**
 * Knowledge → Import & export → Remote sources (P2-10): read-only GitHub paths and Confluence spaces. A sync reads
 * the files through the platform's SSRF-safe client with the credential named by `secret_ref` (never typed here),
 * screens each one and adds it to the review queue as an untrusted draft. Nothing becomes knowledge until a person
 * approves it.
 */
export function KnowledgeSources({ wsId, canEdit }: { wsId: string; canEdit: boolean }) {
  const list = useAsync(() => request<KnowledgeSource[]>("GET", base(wsId)), [wsId]);
  const act = useAction();
  const [scheduled, setScheduled] = useState<string | null>(null);
  const call = async (method: string, path: string, body?: unknown) => {
    const r = await act.run(() => request(method, path, body));
    void list.reload();
    return r;
  };
  const scheduleDaily = async () => {
    const ok = await act.run(() => request("POST", `/api/workspaces/${encodeURIComponent(wsId)}/schedules`,
      { name: "Sync knowledge sources", kind: "knowledge_sync", cron: "0 3 * * *", timezone: "UTC", config: {} }));
    if (ok) setScheduled("Scheduled daily at 03:00 UTC (see Schedules).");
  };
  return (
    <Card title="Remote sources (GitHub, Confluence)">
      <p className="small muted">Read-only. Files are fetched with the credential named by the secret reference, screened like an upload
        (credentials and e-mail addresses redacted, instruction-like lines removed, front matter dropped) and added to the review queue as
        drafts. Unchanged files are skipped on the next sync.</p>
      <ErrorBox error={list.error ?? act.error} onRetry={list.reload} />
      {scheduled && <Notice tone="success">{scheduled}</Notice>}
      {list.loading && !list.data && <Loading />}
      {list.data && !list.data.length && <EmptyState title="No remote sources">Add a GitHub repository path or a Confluence space.</EmptyState>}
      <ul className="list">
        {(list.data ?? []).map((s) => (
          <li key={s.id}>
            <div className="list-button-head">
              <strong>{s.name}</strong>
              <span className="chip-row small">
                <Tag>{s.kind}</Tag>
                {!s.enabled && <Tag tone="warning">paused</Tag>}
                {(() => { const b = BINDING[s.binding_status ?? "none"] ?? { label: s.binding_status ?? "", tone: "warning" };
                  return <Tag tone={b.tone}>{b.label}</Tag>; })()}
                {s.last_status && <Tag tone={s.last_status === "ok" ? "success" : s.last_status === "partial" ? "warning" : "danger"}>{s.last_status}</Tag>}
              </span>
            </div>
            <p className="small muted">
              {s.kind === "github" ? `${s.config.repo} @ ${s.config.branch}${s.config.path ? ` / ${s.config.path}` : ""}` : `${s.config.base_url} · space ${s.config.space}`}
              {" · "}credential {s.secret_ref ?? "none (public)"} · {s.tracked_files} files tracked
              {s.last_sync_at ? ` · last sync ${fmtDate(s.last_sync_at)}` : " · never synced"}
            </p>
            {s.binding_status !== "approved" && (
              <p className="small" role="note">A second person must approve this source's host, target and credential before it can
                sync{s.binding_status === "pending" ? <> (<a href={`/w/${encodeURIComponent(wsId)}/operate/approvals`}>approvals inbox</a>)</> : ""}.</p>
            )}
            {s.last_report?.error && <p className="small" role="alert">{s.last_report.error}</p>}
            {s.last_status && !s.last_report?.error && (
              <p className="small muted">
                {s.last_report.fetched ?? 0} fetched · {s.last_report.written ?? 0} drafts written · {s.last_report.skipped_unchanged ?? 0} unchanged
                {s.last_report.truncated ? " · cut at the size/count cap" : ""}
                {s.last_report.failed?.length ? ` · ${s.last_report.failed.length} could not be read` : ""}
                {s.last_report.skipped_too_large?.length ? ` · ${s.last_report.skipped_too_large.length} too large` : ""}
              </p>
            )}
            {canEdit && (
              <div className="btn-row">
                <button type="button" className="btn btn-sm" disabled={act.busy || !s.enabled || s.binding_status !== "approved"}
                  title={s.binding_status !== "approved" ? "Waiting for the approval of this source" : undefined}
                  onClick={() => void call("POST", `${base(wsId)}/${s.id}/sync`)}>Sync now</button>
                <button type="button" className="btn btn-sm" disabled={act.busy}
                  onClick={() => void call("PATCH", `${base(wsId)}/${s.id}`, { enabled: !s.enabled })}>{s.enabled ? "Pause" : "Resume"}</button>
                <button type="button" className="btn btn-sm" disabled={act.busy}
                  onClick={() => void call("DELETE", `${base(wsId)}/${s.id}`)}>Remove</button>
              </div>
            )}
          </li>
        ))}
      </ul>
      {canEdit && <AddSource wsId={wsId} onAdded={() => void list.reload()} />}
      {canEdit && !!list.data?.length && (
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void scheduleDaily()}>Sync daily (schedule)</button>
      )}
    </Card>
  );
}

const EMPTY = { name: "", repo: "", branch: "main", path: "", base_url: "", space: "", secret_ref: "" };

function AddSource({ wsId, onAdded }: { wsId: string; onAdded: () => void }) {
  const id = useId();
  const [kind, setKind] = useState<"github" | "confluence">("github");
  const [f, setF] = useState(EMPTY);
  const act = useAction();
  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const config = kind === "github" ? { repo: f.repo, branch: f.branch, path: f.path } : { base_url: f.base_url, space: f.space };
    const r = await act.run(() => request("POST", base(wsId), { kind, name: f.name, config, secret_ref: f.secret_ref || null }));
    if (r) {
      setF(EMPTY);
      onAdded();
    }
  };
  return (
    <form className="form" onSubmit={submit} aria-label="Add a remote knowledge source">
      <h3>Add a source</h3>
      <ErrorBox error={act.error} />
      <div className="form-row">
        <Field label="Kind" htmlFor={`${id}-kind`}>
          <select id={`${id}-kind`} value={kind} onChange={(e) => setKind(e.target.value as "github" | "confluence")}>
            <option value="github">GitHub repository path</option>
            <option value="confluence">Confluence space</option>
          </select>
        </Field>
        <Field label="Name" htmlFor={`${id}-name`}><input id={`${id}-name`} required value={f.name} onChange={set("name")} /></Field>
      </div>
      {kind === "github" ? (
        <div className="form-row">
          <Field label="Repository (owner/name)" htmlFor={`${id}-repo`}><input id={`${id}-repo`} required value={f.repo} onChange={set("repo")} placeholder="acme/analytics" /></Field>
          <Field label="Branch" htmlFor={`${id}-branch`}><input id={`${id}-branch`} value={f.branch} onChange={set("branch")} /></Field>
          <Field label="Path filter" htmlFor={`${id}-path`} hint="README, docs Markdown, dbt schema.yml and .sql files below it">
            <input id={`${id}-path`} value={f.path} onChange={set("path")} placeholder="docs" />
          </Field>
        </div>
      ) : (
        <div className="form-row">
          <Field label="Confluence URL" htmlFor={`${id}-url`} hint="e.g. https://acme.atlassian.net/wiki"><input id={`${id}-url`} required value={f.base_url} onChange={set("base_url")} /></Field>
          <Field label="Space key" htmlFor={`${id}-space`}><input id={`${id}-space`} required value={f.space} onChange={set("space")} placeholder="DATA" /></Field>
        </div>
      )}
      <Field label="Secret reference" htmlFor={`${id}-ref`}
        hint={kind === "github" ? "e.g. env:ANALYSTOS_SOURCE_SECRET_GITHUB (a token); leave empty for a public repository" : "e.g. env:ANALYSTOS_SOURCE_SECRET_WIKI (user@example.com:api_token)"}>
        <input id={`${id}-ref`} value={f.secret_ref} onChange={set("secret_ref")} required={kind === "confluence"} autoComplete="off" />
      </Field>
      <p className="small muted">Enter a reference, never the token itself. Hosts other than api.github.com and *.atlassian.net must be allowed by your administrator.</p>
      <button type="submit" className="btn" disabled={act.busy}>Add source</button>
    </form>
  );
}
