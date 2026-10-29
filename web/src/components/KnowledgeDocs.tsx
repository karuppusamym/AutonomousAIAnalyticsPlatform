import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { ApiError, api, type KnowledgeDocument, type KnowledgePackInfo } from "../api";
import { useAuth } from "../auth";
import {
  DOC_STATUSES, draftFrom, draftProblems, emptyDraft, frontmatterOf, groupByFolder, matchesDoc, trustTier, type DocDraft,
} from "../lib/knowledge";
import { fmtDate, optionLabel, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { Markdown } from "./Markdown";
import { Card, CodeBlock, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, Tag } from "./ui";

type SetParams = (patch: Record<string, string | null>) => void;

const PACK_KIND_LABEL: Record<string, string> = { workspace: "workspace", imported: "imported (read-only)", platform: "platform (read-only)" };

export function TrustTag({ tier }: { tier: string | null | undefined }) {
  const t = trustTier(tier);
  return <span className={`tag tag-${t.tone}`} title={t.title}>{t.label}</span>;
}

/**
 * Knowledge → Documents (P4-U04): browse every pack the workspace sees, read a document with its
 * trust fields, and — in the workspace pack, as an editor — edit it. A save is one pack revision;
 * the server keeps `verified` honest (you can only add your own review) and turns a review-queue
 * draft into owner content once a person edits it.
 */
export function KnowledgeDocs({ wsId, params, set }: { wsId: string; params: URLSearchParams; set: SetParams }) {
  const packs = useAsync(() => api.knowledgePacks(wsId), [wsId]);
  const docRef = params.get("doc");
  const locateAct = useAction();
  useEffect(() => {
    if (!docRef) return;
    void locateAct.run(async () => {
      const loc = await api.locateKnowledge(wsId, docRef);
      set({ doc: null, pack: loc.pack_id, path: loc.path, rev: null });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId, docRef]);

  const pack = useMemo(() => {
    const list = packs.data ?? [];
    return list.find((p) => p.id === params.get("pack")) ?? list[0];
  }, [packs.data, params]);

  if (packs.error) return <ErrorBox error={packs.error} onRetry={packs.reload} />;
  if (!packs.data || !pack) return <Loading label="Loading knowledge packs…" />;
  return (
    <>
      <ErrorBox error={locateAct.error} />
      <PackDocs key={pack.id} wsId={wsId} pack={pack} packs={packs.data} params={params} set={set} onChanged={() => void packs.reload()} />
    </>
  );
}

function PackDocs({ wsId, pack, packs, params, set, onChanged }: {
  wsId: string; pack: KnowledgePackInfo; packs: KnowledgePackInfo[]; params: URLSearchParams; set: SetParams; onChanged: () => void;
}) {
  const id = useId();
  const docs = useAsync(() => api.knowledgeDocuments(wsId, pack.id), [wsId, pack.id, pack.head_revision]);
  const [q, setQ] = useState("");
  const path = params.get("path");
  const creating = params.get("new") === "1";
  const rev = params.get("rev") ? Number(params.get("rev")) : undefined;
  const groups = useMemo(() => groupByFolder((docs.data?.documents ?? []).filter((d) => matchesDoc(d, q))), [docs.data, q]);
  const saved = (p: string) => {
    onChanged();
    void docs.reload();
    set({ path: p, new: null, rev: null });
  };

  return (
    <div className="split knowledge-docs">
      <div className="split-list">
        <label className="inline-field small" htmlFor={`${id}-pack`}>Pack</label>
        <select id={`${id}-pack`} value={pack.id} onChange={(e) => set({ pack: e.target.value, path: null, rev: null, new: null })}>
          {packs.map((p) => <option key={p.id} value={p.id}>{optionLabel(p.title || p.slug, 60)} · {PACK_KIND_LABEL[p.kind] ?? p.kind}</option>)}
        </select>
        <p className="muted small">
          {pack.head_revision ? <>Revision {pack.head_revision} · {pack.files} files</> : "No revision yet."}
        </p>
        {!pack.writable && (
          <p className="small" role="note">
            {pack.kind === "workspace" ? "Editors and owners can change this pack." : `The ${pack.kind} pack is read-only here${pack.kind === "imported"
              ? ": it changes only by re-import" : ": it is built from the installed domain packs"}.`}
          </p>
        )}
        {pack.writable && <button type="button" className="btn btn-sm" onClick={() => set({ new: "1", path: null, rev: null })}>New document</button>}
        <label className="sr-only" htmlFor={`${id}-q`}>Filter documents</label>
        <input id={`${id}-q`} type="search" placeholder="Filter by path, title, type…" value={q} onChange={(e) => setQ(e.target.value)} />
        <ErrorBox error={docs.error} onRetry={docs.reload} />
        {docs.loading && !docs.data && <Loading />}
        {docs.data && !docs.data.documents.length && <EmptyState title="No documents">Approve suggestions or create a document.</EmptyState>}
        {groups.map(([folder, list]) => (
          <section key={folder || "/"} className="artifact-group">
            <h2 className="group-title">{folder || "(root)"}</h2>
            <ul className="list selectable" aria-label={`Documents in ${folder || "the root"}`}>
              {list.map((d) => (
                <li key={d.path}>
                  <button type="button" className={`list-button ${d.path === path ? "active" : ""}`} aria-current={d.path === path ? "true" : undefined}
                    onClick={() => set({ path: d.path, rev: null, new: null })}>
                    <span className="list-button-head"><span className="clamp-1">{d.title}</span>{d.type && <span className="muted small">{d.type}</span>}</span>
                    <span className="chip-row small">
                      {d.markdown && !d.reserved && d.type ? <TrustTag tier={d.trust_tier} /> : <Tag>{d.reserved ? "reserved" : "file"}</Tag>}
                      {d.status && d.status !== "stable" && <Tag tone="warning">{d.status}</Tag>}
                      {d.stale && <Tag tone="danger">stale</Tag>}
                      {d.authorship === "review_queue" && <Tag tone="jev">from review queue</Tag>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      <div className="split-detail">
        {creating && pack.writable ? (
          <Card title="New document">
            <DocEditor wsId={wsId} pack={pack} doc={null} onSaved={saved} onCancel={() => set({ new: null })} />
          </Card>
        ) : path ? (
          <DocDetail key={`${path}@${rev ?? "head"}`} wsId={wsId} pack={pack} path={path} rev={rev} set={set} onSaved={saved} />
        ) : <EmptyState title="Select a document">Its trust fields, text, links and revision history.</EmptyState>}
      </div>
    </div>
  );
}

function DocDetail({ wsId, pack, path, rev, set, onSaved }: {
  wsId: string; pack: KnowledgePackInfo; path: string; rev: number | undefined; set: SetParams; onSaved: (path: string) => void;
}) {
  const doc = useAsync(() => api.knowledgeDocument(wsId, pack.id, path, rev), [wsId, pack.id, path, rev]);
  const history = useAsync(() => api.knowledgeRevisions(wsId, pack.id, path), [wsId, pack.id, path, pack.head_revision]);
  const [editing, setEditing] = useState(false);
  if (doc.error) return <ErrorBox error={doc.error} onRetry={doc.reload} />;
  if (!doc.data) return <Loading />;
  const d = doc.data;
  const canEdit = pack.writable && rev === undefined && d.markdown && !d.reserved;
  return (
    <div className="stack">
      {rev !== undefined && (
        <Notice tone="info">
          Viewing revision {rev}.{" "}
          <button type="button" className="btn btn-xs" onClick={() => set({ rev: null })}>Back to the current revision</button>
        </Notice>
      )}
      <Card title={d.title} actions={canEdit && !editing ? <button type="button" className="btn btn-sm" onClick={() => setEditing(true)}>Edit</button> : null}>
        {editing ? (
          <DocEditor wsId={wsId} pack={pack} doc={d} onSaved={(p) => { setEditing(false); void doc.reload(); onSaved(p); }}
            onCancel={() => setEditing(false)} />
        ) : <DocView doc={d} />}
      </Card>
      <Card title="Revision history">
        <ErrorBox error={history.error} onRetry={history.reload} />
        {history.data && !history.data.length && <p className="muted small">No revision touched this path.</p>}
        {!!history.data?.length && (
          <ol className="list compact revision-list" aria-label="Revisions of this document">
            {history.data.map((h) => (
              <li key={h.number} className="list-item small">
                <span>
                  <strong>r{h.number}</strong> {h.reason} <span className="muted">· {h.author} · {fmtDate(h.created_at)}</span>{" "}
                  <Tag tone={h.origin === "studio" ? "success" : h.origin === "review" ? "jev" : "neutral"}>{h.origin}</Tag>
                  {h.removed.includes(path) && <Tag tone="danger">deleted</Tag>}
                </span>
                {h.sha256 && (rev === h.number
                  ? <span className="muted">viewing</span>
                  : <button type="button" className="btn btn-xs btn-ghost" onClick={() => set({ rev: String(h.number) })}
                    aria-label={`View revision ${h.number}`}>View</button>)}
              </li>
            ))}
          </ol>
        )}
      </Card>
    </div>
  );
}

function DocView({ doc: d }: { doc: KnowledgeDocument }) {
  const t = d.trust;
  return (
    <div className="stack">
      {t ? (
        <KeyValue items={[
          ["Type", d.type ?? "—"],
          ["Trust", <TrustTag key="t" tier={t.tier} />],
          ["Verified by", t.verified.length
            ? <ul key="v" className="plain-list" aria-label="Verified by">{t.verified.map((v, i) => <li key={i}><code>{v.by}</code>{v.at ? <span className="muted"> · {fmtDate(String(v.at))}</span> : null}</li>)}</ul>
            : <span key="v" className="muted">nobody</span>],
          ["Status", <span key="s">{t.status}{t.trusted === false ? <Tag tone="warning">untrusted</Tag> : null}</span>],
          ["Stale after", t.stale_after ? <span key="st">{fmtDate(t.stale_after)} {t.stale && <Tag tone="danger">stale</Tag>}</span> : "—"],
          ["Written by", d.authorship === "review_queue"
            ? <span key="a">the review queue <span className="muted">(a later approved draft may replace it until a person edits it)</span></span>
            : <span key="a">a person or the platform <span className="muted">(drafts never replace it)</span></span>],
          ["Path", <code key="p">{d.path}</code>],
          ["Revision", <span key="r">r{d.revision} · <code title={d.sha256}>{shortHash(d.sha256, 12)}</code></span>],
        ]} />
      ) : <Notice tone="warning">This file has no readable header fields{d.problem ? ` (${d.problem})` : ""}; it is shown as text.</Notice>}
      {d.body !== null ? <div className="doc-body"><Markdown text={d.body} /></div> : <CodeBlock code={d.text} label={d.path} />}
      {d.links.length > 0 && (
        <div>
          <h3 className="h-sm">Links</h3>
          <ul className="list compact" aria-label="Links">
            {d.links.map((l, i) => (
              <li key={i} className="list-item small">
                <code>{l.raw}</code>
                {l.kind === "internal" ? (l.exists ? <Tag tone="success">resolves</Tag> : <Tag tone="danger">dangling</Tag>) : <Tag>{l.kind}</Tag>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function DocEditor({ wsId, pack, doc, onSaved, onCancel }: {
  wsId: string; pack: KnowledgePackInfo; doc: KnowledgeDocument | null; onSaved: (path: string) => void; onCancel: () => void;
}) {
  const id = useId();
  const { user } = useAuth();
  const [draft, setDraft] = useState<DocDraft>(() => (doc ? draftFrom(doc) : emptyDraft("notes/new-note.md")));
  const [touched, setTouched] = useState(false);
  const [conflict, setConflict] = useState(false);
  const act = useAction();
  const problems = touched ? draftProblems(draft) : {};
  const statuses = DOC_STATUSES.includes(draft.status) ? DOC_STATUSES : [draft.status, ...DOC_STATUSES];
  const upd = (patch: Partial<DocDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (Object.keys(draftProblems(draft)).length) return;
    const r = await act.run(async () => {
      try {
        return await api.saveKnowledgeDocument(wsId, pack.id, {
          path: draft.path, frontmatter: frontmatterOf(draft), body: draft.body, base_sha256: doc?.sha256 ?? null,
          mark_reviewed: draft.markReviewed, reason: draft.reason.trim() || null,
        });
      } catch (err) {
        setConflict(err instanceof ApiError && err.status === 409);
        throw err;
      }
    });
    if (r) onSaved(r.document.path);
  };
  const err = (k: keyof typeof problems) => problems[k] ? <span id={`${id}-${k}-err`} className="field-error" role="alert">{problems[k]}</span> : null;
  const inv = (k: keyof typeof problems) => ({ "aria-invalid": problems[k] ? true : undefined, "aria-describedby": problems[k] ? `${id}-${k}-err` : undefined });

  return (
    <form className="form doc-editor" onSubmit={submit} aria-label={doc ? `Edit ${doc.path}` : "New document"} noValidate>
      {doc?.authorship === "review_queue" && (
        <Notice tone="info">This document was written by the review queue. Once you save an edit it becomes your content: no later draft replaces it.</Notice>
      )}
      <div className="form-row">
        <Field label="Path" htmlFor={`${id}-path`} hint={doc ? "Fixed for an existing document" : "folder/name.md in the workspace pack"}>
          <input id={`${id}-path`} className="mono" value={draft.path} readOnly={!!doc} onChange={(e) => upd({ path: e.target.value })} {...inv("path")} />
          {err("path")}
        </Field>
        <Field label="Type" htmlFor={`${id}-type`} hint="e.g. Glossary Term, Metric, Business Rule, Note">
          <input id={`${id}-type`} value={draft.type} onChange={(e) => upd({ type: e.target.value })} {...inv("type")} />
          {err("type")}
        </Field>
        <Field label="Title" htmlFor={`${id}-title`}>
          <input id={`${id}-title`} value={draft.title} onChange={(e) => upd({ title: e.target.value })} />
        </Field>
      </div>
      <fieldset className="trust-fields">
        <legend>Trust fields</legend>
        <div className="form-row">
          <Field label="Status" htmlFor={`${id}-status`} hint="Draft, reviewed or deprecated">
            <select id={`${id}-status`} value={draft.status} onChange={(e) => upd({ status: e.target.value })}>
              {statuses.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </Field>
          <Field label="Stale after" htmlFor={`${id}-stale`} hint="After this date prompts mark it stale (§5.5)">
            <input id={`${id}-stale`} type="date" value={draft.staleAfter} onChange={(e) => upd({ staleAfter: e.target.value })} />
          </Field>
          <Field label="Tags" htmlFor={`${id}-tags`} hint="Comma-separated">
            <input id={`${id}-tags`} value={draft.tags} onChange={(e) => upd({ tags: e.target.value })} />
          </Field>
        </div>
        <div className="verified-edit">
          <span className="field-label">Verified by</span>
          {draft.verified.length === 0 && <p className="muted small">Nobody has verified this document.</p>}
          {draft.verified.map((v, i) => (
            <label key={i} className="toggle small">
              <input type="checkbox" checked={v.keep}
                onChange={(e) => upd({ verified: draft.verified.map((x, j) => (j === i ? { ...x, keep: e.target.checked } : x)) })} />
              Keep <code>{v.entry.by}</code>{v.entry.at ? <span className="muted"> · {fmtDate(String(v.entry.at))}</span> : null}
            </label>
          ))}
          <label className="toggle small">
            <input type="checkbox" checked={draft.markReviewed} onChange={(e) => upd({ markReviewed: e.target.checked })} />
            Mark as reviewed by me{user ? <> (<code>human:{user.id}</code>)</> : null}
          </label>
          <p className="muted small">You can remove verifications or add your own; the server stamps the time and refuses anyone else&apos;s.</p>
        </div>
      </fieldset>
      <Field label="Description" htmlFor={`${id}-desc`}>
        <input id={`${id}-desc`} value={draft.description} onChange={(e) => upd({ description: e.target.value })} />
      </Field>
      <Field label="Body (Markdown)" htmlFor={`${id}-body`} hint="Top-level # headings become the sections prompts cite">
        <textarea id={`${id}-body`} className="mono" rows={10} value={draft.body} onChange={(e) => upd({ body: e.target.value })} />
      </Field>
      <details className="technical">
        <summary>Other frontmatter (extensions, as JSON)</summary>
        <Field label="Other frontmatter" htmlFor={`${id}-other`}>
          <textarea id={`${id}-other`} className="mono" rows={6} value={draft.other} onChange={(e) => upd({ other: e.target.value })} {...inv("other")} />
          {err("other")}
        </Field>
      </details>
      <Field label="Reason for this revision" htmlFor={`${id}-reason`}>
        <input id={`${id}-reason`} value={draft.reason} maxLength={500} onChange={(e) => upd({ reason: e.target.value })} placeholder="What changed and why" />
      </Field>
      <ErrorBox error={act.error} />
      {conflict && <p className="small" role="note">Someone saved this document after you opened it. Cancel and reopen it to edit the current revision.</p>}
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Saving…" : "Save revision"}</button>
      </div>
    </form>
  );
}
