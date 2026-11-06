import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, saveBlob, type Approval, type KnowledgeImportReport, type KnowledgePackInfo } from "../api";
import { useAction, useAsync } from "../lib/hooks";
import { to } from "../routes";
import { ContextExport } from "./ContextExport";
import { KnowledgeSources } from "./KnowledgeSources";
import { Card, ErrorBox, Field, KeyValue, Loading, Notice, Tag } from "./ui";

const SLUG = /^[a-z0-9][a-z0-9._-]{0,63}$/;

/**
 * Knowledge → Import & export (P4-U04 over K02/K01): upload an OKF or Atlas bundle into a
 * read-only imported pack; download any pack as a deterministic OKF zip (a download to your own
 * browser); push the workspace pack to its git remote, which writes outside the platform and so
 * needs a hash-bound approval decided in the approvals inbox first. First on the tab: the whole workspace
 * context (or one source's) as a download, and what the agents see (ContextExport).
 */
export function KnowledgeTransfer({ wsId, canEdit, onBrowse }: { wsId: string; canEdit: boolean; onBrowse: (packId: string) => void }) {
  const packs = useAsync(() => api.knowledgePacks(wsId), [wsId]);
  return (
    <div className="stack">
      <ContextExport wsId={wsId} canEdit={canEdit} />
      {canEdit ? <ImportCard wsId={wsId} onImported={() => void packs.reload()} onBrowse={onBrowse} />
        : <p className="small" role="note">Editors and owners can import bundles.</p>}
      <KnowledgeSources wsId={wsId} canEdit={canEdit} />
      <ErrorBox error={packs.error} onRetry={packs.reload} />
      {!packs.data && !packs.error && <Loading />}
      {packs.data && <ExportCard wsId={wsId} packs={packs.data} />}
      {packs.data && canEdit && packs.data.filter((p) => p.kind === "workspace").map((p) => <PushCard key={p.id} wsId={wsId} pack={p} />)}
    </div>
  );
}

function ImportCard({ wsId, onImported, onBrowse }: { wsId: string; onImported: () => void; onBrowse: (packId: string) => void }) {
  const id = useId();
  const [file, setFile] = useState<File | null>(null);
  const [slug, setSlug] = useState("");
  const [touched, setTouched] = useState(false);
  const [report, setReport] = useState<KnowledgeImportReport | null>(null);
  const act = useAction();
  const slugProblem = touched && !SLUG.test(slug) ? "Lowercase letters, digits, dots and dashes (e.g. atlas-revenue)." : null;
  const fileProblem = touched && !file ? "Choose a .zip bundle." : null;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!file || !SLUG.test(slug)) return;
    const r = await act.run(() => api.importKnowledge(wsId, file, slug));
    if (r) {
      setReport(r);
      onImported();
    }
  };
  return (
    <Card title="Import a bundle">
      <form className="form" onSubmit={submit} aria-label="Import a knowledge bundle" noValidate>
        <p className="small muted">An OKF v0.2 or Atlas bundle (.zip). It becomes a read-only imported pack, stored byte for byte; re-importing
          the same bytes changes nothing. Unsafe archives (traversal, symlinks, bombs) are refused before anything is stored.</p>
        <div className="form-row">
          <Field label="Bundle (.zip)" htmlFor={`${id}-file`}>
            <input id={`${id}-file`} type="file" accept=".zip,application/zip" onChange={(e) => {
              const f = e.target.files?.[0] ?? null;
              setFile(f);
              if (f && !slug) setSlug(f.name.replace(/\.(okf\.)?zip$/i, "").toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[^a-z0-9]+/, "").slice(0, 64));
            }} aria-invalid={fileProblem ? true : undefined} aria-describedby={fileProblem ? `${id}-file-err` : undefined} />
            {fileProblem && <span id={`${id}-file-err`} className="field-error" role="alert">{fileProblem}</span>}
          </Field>
          <Field label="Pack name (slug)" htmlFor={`${id}-slug`} hint="Re-import under the same slug to update it">
            <input id={`${id}-slug`} value={slug} onChange={(e) => setSlug(e.target.value)} aria-invalid={slugProblem ? true : undefined}
              aria-describedby={slugProblem ? `${id}-slug-err` : undefined} />
            {slugProblem && <span id={`${id}-slug-err`} className="field-error" role="alert">{slugProblem}</span>}
          </Field>
        </div>
        <ErrorBox error={act.error} />
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Importing…" : "Import"}</button>
        </div>
      </form>
      {report && (
        <div className="stack import-report" aria-label="Import report">
          <Notice tone={report.warnings.length || report.conformance.length ? "warning" : "success"}>
            {report.changed ? <>Imported <code>{report.slug}</code> as revision {report.revision}.</> : <>No change: <code>{report.slug}</code> already holds these bytes.</>}
          </Notice>
          <KeyValue items={[
            ["Format", `${report.format}${report.okf_root ? ` (root ${report.okf_root}/)` : ""}`],
            ["Files / documents", `${report.files} / ${report.documents}`],
            ["Trust claims", <span key="c">{report.verified_claims} <span className="muted">counted as claims, not AnalystOS approvals</span></span>],
            ["Attested computations", <span key="a">{report.attested_computations} <span className="muted">counted, never executed</span></span>],
            ["Dangling links", report.dangling_links],
            ["Conformance problems", report.conformance.length],
          ]} />
          {report.warnings.length > 0 && <ul className="small warn-list" aria-label="Warnings">{report.warnings.map((w) => <li key={w}>{w}</li>)}</ul>}
          <div className="form-actions">
            <button type="button" className="btn btn-sm" onClick={() => onBrowse(report.pack_id)}>Browse the imported pack</button>
          </div>
        </div>
      )}
    </Card>
  );
}

function ExportCard({ wsId, packs }: { wsId: string; packs: KnowledgePackInfo[] }) {
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  const download = async (p: KnowledgePackInfo) => {
    const f = await act.run(() => api.exportKnowledge(wsId, p.id, p.slug));
    if (f) {
      saveBlob(f);
      setDone(f.filename);
    }
  };
  return (
    <Card title="Export">
      <p className="small muted">A zip of the current revision, downloaded to your browser. The publish policy applies:
        no dangling links, safe paths, size caps.</p>
      <ul className="list" aria-label="Packs to export">
        {packs.map((p) => (
          <li key={p.id} className="list-item">
            <span>{p.title || p.slug} <Tag>{p.kind}</Tag> <span className="muted small">{p.head_revision ? `r${p.head_revision} · ${p.files} files` : "empty"}</span></span>
            <button type="button" className="btn btn-sm" disabled={!p.head_revision || act.busy} onClick={() => void download(p)}
              aria-label={`Download ${p.title || p.slug} as a zip`}>Download .zip</button>
          </li>
        ))}
      </ul>
      {done && <p className="small" role="status">Downloaded {done}.</p>}
      <ErrorBox error={act.error} />
    </Card>
  );
}

function PushCard({ wsId, pack }: { wsId: string; pack: KnowledgePackInfo }) {
  const id = useId();
  const [approval, setApproval] = useState<Approval | null>(null);
  const [approvalId, setApprovalId] = useState("");
  const [pushed, setPushed] = useState<{ commit: string; revision: number } | null>(null);
  const act = useAction();
  if (!pack.git_remote) {
    return (
      <Card title="Push to a git remote">
        <p className="small muted">No remote is configured for the workspace pack. An administrator sets one with{" "}
          <code>analystos knowledge remote</code>; every push then needs an approval.</p>
      </Card>
    );
  }
  const request = async () => {
    const a = await act.run(() => api.requestKnowledgePush(wsId, pack.id));
    if (a) {
      setApproval(a);
      setApprovalId(a.id);
    }
  };
  const push = async (e: FormEvent) => {
    e.preventDefault();
    const r = await act.run(() => api.pushKnowledge(wsId, pack.id, approvalId.trim()));
    if (r) setPushed({ commit: r.commit, revision: r.revision });
  };
  return (
    <Card title="Push to a git remote">
      <KeyValue items={[["Remote", <code key="r">{pack.git_remote}</code>], ["Branch", <code key="b">{pack.git_branch}</code>],
        ["Revision", pack.head_revision ? `r${pack.head_revision}` : "—"]]} />
      <p className="small muted">A push writes outside the platform, so it is an approval bound to this exact revision and content digest.
        Request it, have an approver decide it in the inbox, then push with its id; the approval is used once.</p>
      <div className="form-actions">
        <button type="button" className="btn btn-sm" disabled={act.busy || !pack.head_revision} onClick={() => void request()}>Request push approval</button>
      </div>
      {approval && (
        <Notice tone="info">Approval <code>{approval.id}</code> is {approval.status}. Decide it in the <Link to={to.approvals(wsId)}>approvals inbox</Link>.</Notice>
      )}
      <form className="form-inline" onSubmit={push} aria-label="Push with an approval">
        <label className="inline-field small" htmlFor={`${id}-apr`}>Approval id</label>
        <input id={`${id}-apr`} className="mono" value={approvalId} onChange={(e) => setApprovalId(e.target.value)} />
        <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || !approvalId.trim()}>Push</button>
      </form>
      {pushed && <Notice tone="success">Pushed revision {pushed.revision} as commit <code>{pushed.commit.slice(0, 12)}</code>.</Notice>}
      <ErrorBox error={act.error} />
    </Card>
  );
}
