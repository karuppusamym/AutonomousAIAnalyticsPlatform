import { useMemo, useState } from "react";
import type { Artifact } from "../api";
import { outputDisplayName } from "../lib/outputNames";
import { fmtDate } from "../lib/format";

function browserUrl(value: string | null): string | null {
  try {
    const url = new URL(value ?? "");
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}

/** The workspace's published collection spans investigations; repeated publications share one BI object. */
export function PublishedOutputs({ wsId, artifacts, onSelect }: {
  wsId: string; artifacts: Artifact[]; onSelect: (artifact: Artifact) => void;
}) {
  const [search, setSearch] = useState("");
  const published = useMemo(() => {
    const seen = new Set<string>();
    return artifacts.filter((a) => a.workspace_id === wsId && a.status === "published" &&
      ["dashboard", "report"].includes(a.type))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .filter((a) => {
        const key = a.external_id && a.platform ? `${a.platform}:${a.type}:${a.external_id}` : a.id;
        if (seen.has(key)) return false;
        seen.add(key); return true;
      });
  }, [wsId, artifacts]);
  if (!published.length) return null;
  const shown = published.filter((a) => `${outputDisplayName(a)} ${a.platform ?? ""}`.toLowerCase().includes(search.trim().toLowerCase()));
  return (
    <details className="card generate-report" open>
      <summary>Published dashboards &amp; reports ({published.length})</summary>
      <div className="card-body stack">
        <p className="small muted">Published items from all investigations in this workspace. The BI tool may ask you to sign in separately; after signing in, reopen the link in the same browser.</p>
        <label className="field"><span>Find a published dashboard or report</span>
          <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by title or BI tool…" />
        </label>
        <ul className="list" aria-label="Published dashboards and reports">
          {shown.map((a) => {
            const url = browserUrl(a.external_url);
            return <li className="list-item" key={a.id}>
              <div className="stack">
                <strong>{outputDisplayName(a)}</strong>
                <span className="small muted">{a.type} · {a.platform ?? "BI"} · Published record updated {fmtDate(a.updated_at)}</span>
                {a.banner && <span className="alert alert-warning" role="alert">Needs re-verification: {a.banner.reason}</span>}
                <span className="chip-row">
                  {url ? <a className="btn btn-sm" href={url} target="_blank" rel="noreferrer noopener" aria-label={`Open ${outputDisplayName(a)} in ${a.platform ?? "BI"}`}>Open in {a.platform ?? "BI"} ↗</a>
                    : <span className="small muted">Published link unavailable. Open details to inspect the publication.</span>}
                  <button type="button" className="btn btn-sm" onClick={() => onSelect(a)} aria-label={`View details for ${outputDisplayName(a)}`}>Details &amp; evidence</button>
                </span>
              </div>
            </li>;
          })}
        </ul>
        {!shown.length && <p className="small muted">No published items match your search.</p>}
        <p className="small muted">Publication does not rerun the analysis. Check Details &amp; evidence for the investigation and its data freshness before using the numbers.</p>
      </div>
    </details>
  );
}
