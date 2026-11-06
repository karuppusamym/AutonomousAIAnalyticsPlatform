/**
 * Settings → Capability registry, the S10 tabs: data tools (the signed v2 registry: list, publish-as-tool,
 * signatures, health, usage, versions, deprecate, try it), external MCP servers (register, allow, admit,
 * quarantine review and classification) and MCP clients (clients, grants, and the MCP catalogue: what a
 * workspace offers over MCP and which client reaches each tool).
 *
 * Tool descriptions come from sources and third-party servers: they are shown as quoted data, never as
 * instructions (CLAUDE.md rule 7). Every decision here is made again by the server.
 */
import { useId, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api, type McpClientRow, type McpGrantRow, type McpServerRow, type RegistryTool, type RegistryToolDetail,
  type ToolInvocationResult,
} from "../api";
import { sideEffectInfo } from "../lib/capabilities";
import { fmtDate, fmtMs, fmtPct, fmtUsd, shortHash } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import type { Schema } from "../lib/jsonSchema";
import {
  CLASSIFICATIONS, GRANTABLE_BUILTINS, SOURCE_TYPES, grantCovers, grantExpired, kindLabel, mcpCatalogue, parseList,
  signatureInfo, statusTone, type SourceType,
} from "../lib/toolRegistry";
import { to } from "../routes";
import { SchemaForm } from "./SchemaForm";
import {
  Card, DataTable, EmptyState, EnabledToggle, ErrorBox, Field, JsonView, KeyValue, Loading, Notice, StateView, Tag,
  TechnicalDetails,
} from "./ui";

// ------------------------------------------------------------------------------------ shared
function useWorkspaceParam(): [string, (ws: string) => void] {
  const [params, setParams] = useSearchParams();
  const set = (ws: string) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (ws) next.set("ws", ws);
    else next.delete("ws");
    next.delete("tool");
    return next;
  }, { replace: true });
  return [params.get("ws") || "", set];
}

function WorkspacePicker({ value, onChange, label = "Workspace" }: { value: string; onChange: (ws: string) => void; label?: string }) {
  const workspaces = useAsync(() => api.listWorkspaces(), []);
  return (
    <label className="inline-field small">{label}
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        <option value="">Choose a workspace…</option>
        {(workspaces.data ?? []).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
      </select>
    </label>
  );
}

/** Text from a source or a third-party server: quoted, screened, and labelled as data. */
function UntrustedText({ text }: { text: string }) {
  if (!text) return <span className="muted small">no description</span>;
  return <blockquote className="muted small clamp-2" title="Screened text from the source; shown as data, never followed">{text}</blockquote>;
}

function SideEffect({ value }: { value: string }) {
  const se = sideEffectInfo(value);
  return <span title={se.hint}><Tag tone={se.tone}>{se.label}</Tag></span>;
}

// ------------------------------------------------------------------------------------ data tools
export function DataTools() {
  const [ws, setWs] = useWorkspaceParam();
  const [params, setParams] = useSearchParams();
  const selected = params.get("tool");
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("");
  const [showHeld, setShowHeld] = useState(false);
  const list = useAsync(() => (ws ? api.listRegistryTools(ws, { include_quarantined: showHeld }) : Promise.resolve([])), [ws, showHeld]);
  const select = (id: string | null) => setParams((cur) => {
    const next = new URLSearchParams(cur);
    if (id) next.set("tool", id);
    else next.delete("tool");
    return next;
  }, { replace: true });
  const tools = list.data ?? [];
  const kinds = useMemo(() => [...new Set(tools.map((t) => t.kind))].sort(), [tools]);
  const shown = tools.filter((t) => (!kind || t.kind === kind) && (!q || `${t.tool_id} ${t.title} ${t.description}`.toLowerCase()
    .includes(q.toLowerCase())));

  return (
    <div className="stack">
      <Card>
        <div className="toolbar">
          <div className="chip-row">
            <WorkspacePicker value={ws} onChange={setWs} />
            <input type="search" placeholder="Search tool id, title…" aria-label="Search tools" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <label className="inline-field small">
            <input type="checkbox" checked={showHeld} onChange={(e) => setShowHeld(e.target.checked)} /> Show quarantined and refused
          </label>
        </div>
        {kinds.length > 1 && (
          <div className="chip-row" role="group" aria-label="Filter by kind">
            <button type="button" className={`chip ${kind === "" ? "active" : ""}`} aria-pressed={kind === ""} onClick={() => setKind("")}>All</button>
            {kinds.map((k) => (
              <button key={k} type="button" className={`chip ${kind === k ? "active" : ""}`} aria-pressed={kind === k}
                onClick={() => setKind(kind === k ? "" : k)}>{kindLabel(k)}</button>
            ))}
          </div>
        )}
        <p className="muted small">
          Certified metrics, verified queries, datasets and playbooks published as signed, typed tools. Callers pass names and
          values, never SQL; every call runs through the compiler or the query gateway as the caller.
        </p>
      </Card>
      {!ws ? (
        <StateView kind="empty" title="Choose a workspace">Tools are published and governed per workspace.</StateView>
      ) : (
        <>
          <PublishTool ws={ws} onPublished={(id) => { void list.reload(); select(id); }} />
          <ErrorBox error={list.error} onRetry={list.reload} />
          {list.loading && !list.data && <Loading label="Loading tools…" />}
          <div className={`registry-grid ${selected ? "has-detail" : ""}`}>
            <Card title={`Tools (${shown.length})`}>
              {list.data && shown.length === 0 ? (
                <EmptyState title={tools.length ? "No tool matches" : "No tools published yet"}>
                  Publish a certified metric, a verified query, a published dataset or a playbook above.
                </EmptyState>
              ) : (
                <div className="table-wrap">
                  <table className="table table-compact" aria-label="Registry tools">
                    <thead><tr><th>Tool</th><th>Kind</th><th>Status</th><th>Side effect</th><th>Exposed</th><th><span className="sr-only">Actions</span></th></tr></thead>
                    <tbody>
                      {shown.map((t) => (
                        <tr key={t.tool_id} className={t.tool_id === selected ? "row-active" : undefined}>
                          <td><code>{t.tool_id}</code> <span className="muted small">v{t.version}</span><div className="small">{t.title}</div></td>
                          <td className="small">{kindLabel(t.kind)}</td>
                          <td><Tag tone={statusTone(t.status)}>{t.status}</Tag></td>
                          <td><SideEffect value={t.side_effect} /></td>
                          <td className="small">{[t.exposure?.rest !== false && "REST", t.exposure?.mcp && "MCP"].filter(Boolean).join(" · ") || "—"}</td>
                          <td>
                            <button type="button" className="btn btn-xs btn-ghost" aria-label={`Open ${t.tool_id}`} aria-pressed={t.tool_id === selected}
                              onClick={() => select(t.tool_id === selected ? null : t.tool_id)}>{t.tool_id === selected ? "Close" : "Open"}</button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </Card>
            {selected && <ToolDetail key={`${ws}:${selected}`} ws={ws} toolId={selected} onClose={() => select(null)}
              onChanged={() => void list.reload()} />}
          </div>
          <SigningKeys ws={ws} />
        </>
      )}
    </div>
  );
}

function PublishTool({ ws, onPublished }: { ws: string; onPublished: (toolId: string) => void }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [sourceType, setSourceType] = useState<SourceType>("metric");
  const [ref, setRef] = useState("");
  const [name, setName] = useState("");
  const [mcp, setMcp] = useState(true);
  const [maxRows, setMaxRows] = useState("");
  const act = useAction();
  const [done, setDone] = useState<{ tool_id: string; version: string; created: boolean } | null>(null);
  const hint = SOURCE_TYPES.find((s) => s.id === sourceType)?.refHint;
  const submit = async () => {
    setDone(null);
    const options: Record<string, unknown> = { exposure: { rest: true, mcp } };
    if (name.trim()) options.name = name.trim();
    if (maxRows) options.limits = { max_rows: Number(maxRows) };
    const r = await act.run(() => api.publishTool({ workspace_id: ws, source_type: sourceType, ref: ref.trim(), options }));
    if (r) {
      setDone({ tool_id: r.tool_id, version: r.version, created: r.created });
      onPublished(r.tool_id);
    }
  };
  if (!open) {
    return <div><button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>Publish as tool…</button></div>;
  }
  return (
    <Card title="Publish as tool" actions={<button type="button" className="btn btn-xs btn-ghost" onClick={() => setOpen(false)}>Close</button>}>
      <p className="muted small">
        Only a certified source can be published: an approved metric, an active verified query, a published dataset or a published
        playbook. The input schema is derived from what the source allows; the manifest is signed with the tenant key.
      </p>
      <form className="form-row" aria-label="Publish as tool" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Field label="Source" htmlFor={`${id}-type`}>
          <select id={`${id}-type`} value={sourceType} onChange={(e) => setSourceType(e.target.value as SourceType)}>
            {SOURCE_TYPES.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}
          </select>
        </Field>
        <Field label="Source reference" htmlFor={`${id}-ref`} hint={hint}>
          <input id={`${id}-ref`} required value={ref} onChange={(e) => setRef(e.target.value)} />
        </Field>
        <Field label="Tool name (optional)" htmlFor={`${id}-name`} hint="lower_snake_case; the id becomes <family>.<name>">
          <input id={`${id}-name`} pattern="[a-z][a-z0-9_]{0,79}" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Row cap (optional)" htmlFor={`${id}-rows`}>
          <input id={`${id}-rows`} type="number" min={1} max={50000} value={maxRows} onChange={(e) => setMaxRows(e.target.value)} />
        </Field>
        <label className="inline-field small"><input type="checkbox" checked={mcp} onChange={(e) => setMcp(e.target.checked)} /> Offer over MCP</label>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || !ref.trim()}>{act.busy ? "Publishing…" : "Publish"}</button>
        </div>
      </form>
      <ErrorBox error={act.error} />
      {done && (
        <Notice tone="success">
          {done.created ? "Published" : "Already published, unchanged:"} <code>{done.tool_id}</code> v{done.version}.
        </Notice>
      )}
    </Card>
  );
}

function ToolDetail({ ws, toolId, onClose, onChanged }: { ws: string; toolId: string; onClose: () => void; onChanged: () => void }) {
  const d = useAsync(() => api.getRegistryTool(ws, toolId), [ws, toolId]);
  return (
    <Card className="registry-detail" title={<><code>{toolId}</code>{d.data && <span className="muted small"> v{d.data.version}</span>}</>}
      actions={<button type="button" className="btn btn-xs btn-ghost" onClick={onClose}>Close</button>}>
      <ErrorBox error={d.error} onRetry={d.reload} />
      {!d.data && !d.error && <Loading label="Loading manifest…" />}
      {d.data && <ToolBody ws={ws} tool={d.data} onChanged={() => { void d.reload(); onChanged(); }} />}
    </Card>
  );
}

function ToolBody({ ws, tool, onChanged }: { ws: string; tool: RegistryToolDetail; onChanged: () => void }) {
  const m = tool.manifest;
  const h = tool.health;
  const sig = signatureInfo(h.signature);
  return (
    <div className="stack">
      <div className="small"><strong>{m.title || tool.tool_id}</strong></div>
      <UntrustedText text={m.description} />
      <div className="chip-row" aria-label="Health">
        <Tag tone={statusTone(tool.status)}>{tool.status}</Tag>
        <Tag tone={sig.tone}>{sig.label}</Tag>
        {h.source_current === false && <Tag tone="danger">source changed: republish</Tag>}
        {h.source_current === true && <Tag tone="success">source current</Tag>}
        <SideEffect value={tool.side_effect} />
        {h.invocable ? <Tag tone="success">invocable</Tag> : <Tag tone="warning">not invocable</Tag>}
      </div>
      {tool.status_reason && <p className="muted small">{tool.status_reason}</p>}
      <KeyValue items={[
        ["Kind", kindLabel(tool.kind)],
        ["Owner", tool.owner],
        ["Runs via", <code key="v">{m.execution.via}</code>],
        ["Source", tool.source?.type ? `${String(tool.source.type)} ${String(tool.source.name ?? tool.source.id ?? "")}`
          + (tool.source.version !== undefined ? ` v${String(tool.source.version)}` : "") : "—"],
        ["Permissions", m.permissions.join(", ") || "—"],
        ["Data classification", m.data_classification_max],
        ["Limits", `${m.limits.max_rows} rows · ${m.limits.timeout_s}s · ${m.limits.rate_per_min}/min`],
        ["Exposure", [m.exposure.rest && "REST", m.exposure.mcp && "MCP"].filter(Boolean).join(" · ") || "none"],
        ["Grants that cover it", (m.exposure.grants_required ?? []).join(", ") || "—"],
        ["Signature", m.signature ? <span key="s">{m.signature.alg} · key <code>{m.signature.key_id}</code> · {shortHash(m.signature.value)}</span>
          : "unsigned"],
        ["Manifest hash", <code key="h">{shortHash(tool.manifest_hash)}</code>],
        ["Calls (24 h)", `${h.calls_24h}${h.error_rate_24h !== null ? ` · ${fmtPct(h.error_rate_24h)} errors` : ""}`],
        ["Last success", h.last_ok_at ? fmtDate(h.last_ok_at) : "—"],
      ]} />
      {tool.kind === "external_mcp" && <ScreeningReview ws={ws} tool={tool} onClassified={onChanged} />}
      <Versions tool={tool} />
      <Usage ws={ws} toolId={tool.tool_id} />
      {h.invocable && m.exposure.rest && <TryTool ws={ws} tool={tool} />}
      <Lifecycle ws={ws} tool={tool} onChanged={onChanged} />
      <TechnicalDetails value={m} label="Technical details (signed manifest)" />
    </div>
  );
}

function Versions({ tool }: { tool: RegistryToolDetail }) {
  return (
    <section aria-label="Versions">
      <h3>Versions</h3>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th>Version</th><th>Status</th><th>Why</th><th>Published</th></tr></thead>
          <tbody>{[...tool.versions].reverse().map((v) => (
            <tr key={v.version}>
              <td><code>{v.version}</code></td>
              <td><Tag tone={statusTone(v.status)}>{v.status}</Tag></td>
              <td className="small">{v.status_reason ?? ""}</td>
              <td className="small">{v.created_at ? fmtDate(v.created_at) : ""}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <p className="muted small">Callers may pin a major version (<code>{tool.tool_id}@{tool.version.split(".")[0]}</code>); unpinned calls get the
        latest certified version, and a deprecated one only when pinned.</p>
    </section>
  );
}

function Usage({ ws, toolId }: { ws: string; toolId: string }) {
  const [days, setDays] = useState(7);
  const u = useAsync(() => api.registryToolUsage(ws, toolId, days), [ws, toolId, days]);
  return (
    <section aria-label="Usage">
      <div className="toolbar">
        <h3>Usage</h3>
        <label className="inline-field small">Period
          <select value={days} onChange={(e) => setDays(Number(e.target.value))} aria-label="Usage period in days">
            {[1, 7, 30, 90].map((d) => <option key={d} value={d}>{d} days</option>)}
          </select>
        </label>
      </div>
      <ErrorBox error={u.error} onRetry={u.reload} />
      {u.data && (!u.data.calls ? <p className="muted small">No calls in this period.</p> : (
        <>
          <KeyValue items={[
            ["Calls", String(u.data.calls)],
            ["By outcome", Object.entries(u.data.by_status ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")],
            ["By channel", Object.entries(u.data.by_channel ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")],
            ["Median latency", u.data.latency_ms_p50 !== null ? fmtMs(u.data.latency_ms_p50) : "—"],
            ["Cost", fmtUsd(u.data.cost_usd)],
            ["Top callers", (u.data.top_callers ?? []).map(([c, n]) => `${c} (${n})`).join(", ")],
          ]} />
          <div className="table-wrap">
            <table className="table table-compact" aria-label="Recent calls">
              <thead><tr><th>When</th><th>Caller</th><th>Channel</th><th>Version</th><th>Outcome</th><th>Rows</th><th>Latency</th></tr></thead>
              <tbody>{(u.data.recent ?? []).map((r, i) => (
                <tr key={i}>
                  <td className="small">{r.at ? fmtDate(r.at) : ""}</td><td className="small"><code>{r.caller}</code></td><td className="small">{r.channel}</td>
                  <td className="small">{r.version ?? ""}</td>
                  <td><Tag tone={r.status === "ok" ? "success" : r.status === "approval_required" ? "warning" : "danger"}>{r.status}</Tag>
                    {r.reason && <span className="muted small"> {r.reason}</span>}</td>
                  <td className="small">{r.row_count ?? ""}</td><td className="small">{fmtMs(r.latency_ms)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </>
      ))}
    </section>
  );
}

function TryTool({ ws, tool }: { ws: string; tool: RegistryToolDetail }) {
  const act = useAction();
  const [out, setOut] = useState<ToolInvocationResult | null>(null);
  const run = async (args: Record<string, unknown>) => {
    setOut(null);
    const r = await act.run(() => api.invokeRegistryTool(ws, `${tool.tool_id}@${tool.version}`, args));
    if (r) setOut(r);
  };
  const result = (out?.result ?? null) as { columns?: string[]; rows?: unknown[][]; governed?: boolean; metric_version?: number } | null;
  return (
    <section aria-label="Try this tool">
      <h3>Try it</h3>
      <p className="muted small">Runs as you, under your scope, exactly as an API or MCP caller would (and is recorded in usage).</p>
      <SchemaForm schema={tool.manifest.input_schema as Schema} onSubmit={(v) => void run(v)} busy={act.busy}
        submitLabel={tool.side_effect === "none" ? "Run" : "Request run"} />
      <ErrorBox error={act.error} />
      {out?.status === "approval_required" && (
        <StateView kind="refused" title="Waiting for approval" action={<Link to={to.approvals(ws)}>Open the approval inbox</Link>}>
          This tool has a side effect; the call is held for an approval bound to these exact inputs
          {out.approval_id ? <> (<code>{out.approval_id}</code>)</> : null}.
        </StateView>
      )}
      {out?.status === "ok" && result && Array.isArray(result.columns) && Array.isArray(result.rows) && (
        <div aria-live="polite">
          <div className="chip-row">
            {result.governed && <Tag tone="success">governed</Tag>}
            {result.metric_version !== undefined && <Tag>metric v{result.metric_version}</Tag>}
          </div>
          <DataTable columns={result.columns} rows={result.rows} caption={`${tool.tool_id} result`} />
        </div>
      )}
      {out?.status === "ok" && !(result && Array.isArray(result.columns)) && <JsonView value={out.result} label="Result" />}
    </section>
  );
}

function Lifecycle({ ws, tool, onChanged }: { ws: string; tool: RegistryToolDetail; onChanged: () => void }) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const [done, setDone] = useState<string | null>(null);
  if (!["certified", "deprecated"].includes(tool.status)) return null;
  const change = async (retire: boolean) => {
    const r = await act.run(() => api.deprecateRegistryTool(ws, tool.tool_id, { version: tool.version, reason: reason || undefined, retire }));
    if (r) {
      setDone(`${r.status} ${r.versions.join(", ") || "(no change)"}`);
      onChanged();
    }
  };
  return (
    <section aria-label="Lifecycle">
      <h3>Deprecate or retire</h3>
      <p className="muted small">Deprecated versions keep working for callers that pin them; retired versions never run.</p>
      <div className="chip-row">
        <input aria-label="Reason" placeholder="Reason (shown to callers)" value={reason} onChange={(e) => setReason(e.target.value)} />
        {tool.status === "certified" && (
          <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void change(false)}>Deprecate v{tool.version}</button>
        )}
        <button type="button" className="btn btn-sm btn-danger" disabled={act.busy} onClick={() => void change(true)}>Retire v{tool.version}</button>
      </div>
      <ErrorBox error={act.error} />
      {done && <Notice tone="success">{done}</Notice>}
    </section>
  );
}

function SigningKeys({ ws }: { ws: string }) {
  const keys = useAsync(() => api.toolSigningKeys(ws), [ws]);
  const act = useAction();
  const [revoke, setRevoke] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const rotate = async () => {
    const r = await act.run(() => api.rotateToolSigningKey(ws, revoke));
    if (r) {
      setDone(`New key ${r.key_id}; ${r.resigned} manifests re-signed.`);
      void keys.reload();
    }
  };
  return (
    <Card title="Manifest signing keys">
      <p className="muted small">
        Every manifest is signed with this tenant's ed25519 key; the runtime refuses unsigned, tampered or revoked-key manifests.
        Only public keys are stored: the private key is derived from the platform signing secret when needed.
      </p>
      <ErrorBox error={keys.error} onRetry={keys.reload} />
      {keys.data && (keys.data.length === 0 ? <p className="muted small">No key yet: one is created with the first published tool.</p> : (
        <div className="table-wrap">
          <table className="table table-compact" aria-label="Signing keys">
            <thead><tr><th>Key</th><th>Status</th><th>Public key</th><th>Created</th></tr></thead>
            <tbody>{keys.data.map((k) => (
              <tr key={k.key_id}>
                <td><code>{k.key_id}</code> <span className="muted small">{k.alg}</span></td>
                <td><Tag tone={k.status === "active" ? "success" : k.status === "revoked" ? "danger" : "neutral"}>{k.status}</Tag></td>
                <td><code className="small" title={k.public_key}>{shortHash(k.public_key, 20)}</code></td>
                <td className="small">{k.created_at ? fmtDate(k.created_at) : ""}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ))}
      <div className="chip-row">
        <label className="inline-field small"><input type="checkbox" checked={revoke} onChange={(e) => setRevoke(e.target.checked)} /> Revoke the current key</label>
        <button type="button" className="btn btn-sm" disabled={act.busy} onClick={() => void rotate()}>{act.busy ? "Rotating…" : "Rotate and re-sign"}</button>
      </div>
      <ErrorBox error={act.error} />
      {done && <Notice tone="success">{done}</Notice>}
    </Card>
  );
}

// ------------------------------------------------------------------------------------ external MCP servers
function ScreeningReview({ ws, tool, onClassified }: { ws: string; tool: RegistryToolDetail; onClassified: () => void }) {
  const [side, setSide] = useState("none");
  const act = useAction();
  const flags = tool.screening?.flags ?? [];
  const classify = async () => {
    const r = await act.run(() => api.classifyRegistryTool(ws, tool.tool_id, side));
    if (r) onClassified();
  };
  return (
    <section aria-label="Screening and classification">
      <h3>Screening</h3>
      <KeyValue items={[
        ["Screened", tool.screening?.screened_at ? fmtDate(String(tool.screening.screened_at)) : "—"],
        ["Flags", flags.length ? flags.join(", ") : "none"],
        ["Server hint", tool.screening?.read_only_hint === true ? "says read-only (advisory only)" : tool.screening?.read_only_hint === false
          ? "not read-only" : "none"],
        ["Re-screen", tool.screening?.rescreen ? "the server changed this tool's definition" : "first admission"],
        ["Classified by", tool.screening?.classified_by ? String(tool.screening.classified_by) : "not yet"],
      ]} />
      {tool.status === "refused" && (
        <Notice tone="danger">Refused at screening: its text reads like instructions to a model. It cannot be classified or invoked.</Notice>
      )}
      {tool.status === "quarantined" && (
        <>
          <Notice tone="warning">Quarantined: treated as writing externally and not invocable until an owner classifies it.</Notice>
          <div className="chip-row">
            <label className="inline-field small">Side effect
              <select value={side} onChange={(e) => setSide(e.target.value)} aria-label="Classify side effect">
                {CLASSIFICATIONS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
              </select>
            </label>
            <button type="button" className="btn btn-sm btn-primary" disabled={act.busy} onClick={() => void classify()}>Classify and certify</button>
          </div>
          <ErrorBox error={act.error} />
        </>
      )}
    </section>
  );
}

export function McpServers() {
  const [ws, setWs] = useWorkspaceParam();
  const servers = useAsync(() => (ws ? api.listMcpServers(ws) : Promise.resolve([] as McpServerRow[])), [ws]);
  const queue = useAsync(() => (ws ? api.listRegistryTools(ws, { kind: "external_mcp", include_quarantined: true })
    : Promise.resolve([] as RegistryTool[])), [ws]);
  const reloadAll = () => { void servers.reload(); void queue.reload(); };
  const held = (queue.data ?? []).filter((t) => t.status !== "certified");
  const admitted = (queue.data ?? []).filter((t) => t.status === "certified");
  return (
    <div className="stack">
      <Card>
        <div className="toolbar"><WorkspacePicker value={ws} onChange={setWs} /></div>
        <p className="muted small">
          External MCP servers on the platform host allowlist. Admission lists and screens their tools; each one enters the registry
          quarantined as writing externally until a workspace owner classifies it. A changed tool definition is re-screened and
          quarantined again. Calls go only to the admitted host.
        </p>
      </Card>
      {!ws ? <StateView kind="empty" title="Choose a workspace">MCP servers are registered per workspace.</StateView> : (
        <>
          <RegisterServer ws={ws} onDone={reloadAll} />
          <ErrorBox error={servers.error} onRetry={servers.reload} />
          {servers.loading && !servers.data && <Loading label="Loading servers…" />}
          {servers.data && (servers.data.length === 0 ? <EmptyState title="No MCP server registered" /> : (
            <Card title={`Servers (${servers.data.length})`}>
              <div className="table-wrap">
                <table className="table table-compact" aria-label="MCP servers">
                  <thead><tr><th>Server</th><th>Status</th><th>Allowed</th><th>Tools</th><th>Last admission</th><th><span className="sr-only">Actions</span></th></tr></thead>
                  <tbody>{servers.data.map((s) => <ServerRow key={s.id} ws={ws} server={s} onChanged={reloadAll} />)}</tbody>
                </table>
              </div>
            </Card>
          ))}
          <ErrorBox error={queue.error} onRetry={queue.reload} />
          <Card title={`Review queue (${held.length})`}>
            {held.length === 0 ? <p className="muted small">Nothing awaits classification.</p> : <ReviewTable ws={ws} tools={held} onChanged={reloadAll} />}
          </Card>
          {admitted.length > 0 && (
            <Card title={`Admitted and classified (${admitted.length})`}>
              <ReviewTable ws={ws} tools={admitted} onChanged={reloadAll} />
            </Card>
          )}
        </>
      )}
    </div>
  );
}

function RegisterServer({ ws, onDone }: { ws: string; onDone: () => void }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const act = useAction();
  if (!open) return <div><button type="button" className="btn btn-sm" onClick={() => setOpen(true)}>Register a server…</button></div>;
  const submit = async () => {
    const r = await act.run(() => api.registerMcpServer(ws, { name: name.trim(), url: url.trim(), secret_ref: secret.trim() || null }));
    if (r) {
      setOpen(false);
      setName(""); setUrl(""); setSecret("");
      onDone();
    }
  };
  return (
    <Card title="Register an MCP server" actions={<button type="button" className="btn btn-xs btn-ghost" onClick={() => setOpen(false)}>Close</button>}>
      <form className="form-row" aria-label="Register an MCP server" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Field label="Name" htmlFor={`${id}-name`} hint="lower_snake_case">
          <input id={`${id}-name`} required pattern="[a-z][a-z0-9_]{0,39}" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="URL" htmlFor={`${id}-url`} hint="Its host must be on the platform MCP host allowlist">
          <input id={`${id}-url`} required type="url" value={url} onChange={(e) => setUrl(e.target.value)} />
        </Field>
        <Field label="Credential reference (optional)" htmlFor={`${id}-secret`} hint="env:NAME or file:/path, never the value">
          <input id={`${id}-secret`} pattern="(env:[A-Za-z_][A-Za-z0-9_]*|file:/\S+)" value={secret} onChange={(e) => setSecret(e.target.value)} />
        </Field>
        <div className="form-actions"><button type="submit" className="btn btn-primary btn-sm" disabled={act.busy}>Register</button></div>
      </form>
      <ErrorBox error={act.error} />
    </Card>
  );
}

function ServerRow({ ws, server: s, onChanged }: { ws: string; server: McpServerRow; onChanged: () => void }) {
  const act = useAction();
  const [admission, setAdmission] = useState<string | null>(null);
  const refused = s.tools.filter((t) => t.refused).length;
  const allow = async (v: boolean) => {
    if (await act.run(() => api.allowMcpServer(ws, s.id, v)) !== undefined) onChanged();
  };
  const admit = async () => {
    const r = await act.run(() => api.admitMcpServer(ws, s.id));
    if (r) {
      const parts = [["quarantined", r.quarantined], ["re-screened", r.rescreened], ["refused", r.refused], ["retired", r.retired],
        ["unchanged", r.unchanged]].filter(([, v]) => (v as string[]).length).map(([k, v]) => `${(v as string[]).length} ${k}`);
      setAdmission(parts.join(" · ") || "no tools offered");
      onChanged();
    }
  };
  return (
    <tr>
      <td><code>{s.name}</code><div className="muted small">{s.url}</div>{s.last_error && <div className="small">{s.last_error}</div>}</td>
      <td><Tag tone={s.status === "ready" ? "success" : s.status === "error" ? "danger" : "neutral"}>{s.status}</Tag></td>
      <td><EnabledToggle enabled={s.allowed} disabled={act.busy} onChange={(v) => void allow(v)} label={`Allow ${s.name} in this workspace`} /></td>
      <td className="small">{s.tools.length}{refused ? ` (${refused} refused)` : ""}</td>
      <td className="small">{s.last_refreshed_at ? fmtDate(s.last_refreshed_at) : "never"}{admission && <div role="status">{admission}</div>}</td>
      <td>
        <button type="button" className="btn btn-xs" disabled={act.busy || !s.allowed} onClick={() => void admit()}
          title={s.allowed ? "List and screen the server's tools into the registry" : "Allow the server first"}>Admit tools</button>
        <ErrorBox error={act.error} />
      </td>
    </tr>
  );
}

function ReviewTable({ ws, tools, onChanged }: { ws: string; tools: RegistryTool[]; onChanged: () => void }) {
  const [open, setOpen] = useState<string | null>(null);
  return (
    <div className="stack">
      <div className="table-wrap">
        <table className="table table-compact" aria-label="External MCP tools">
          <thead><tr><th>Tool</th><th>Status</th><th>Side effect</th><th>Description from the server</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{tools.map((t) => (
            <tr key={t.tool_id} className={t.tool_id === open ? "row-active" : undefined}>
              <td><code>{t.tool_id}</code> <span className="muted small">v{t.version}</span></td>
              <td><Tag tone={statusTone(t.status)}>{t.status}</Tag></td>
              <td><SideEffect value={t.side_effect} /></td>
              <td><UntrustedText text={t.description} /></td>
              <td><button type="button" className="btn btn-xs btn-ghost" aria-label={`Review ${t.tool_id}`}
                onClick={() => setOpen(open === t.tool_id ? null : t.tool_id)}>{open === t.tool_id ? "Close" : "Review"}</button></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      {open && <ToolDetail key={open} ws={ws} toolId={open} onClose={() => setOpen(null)} onChanged={onChanged} />}
    </div>
  );
}

// ------------------------------------------------------------------------------------ MCP clients and catalogue
export function McpClients() {
  const clients = useAsync(() => api.listMcpClients(), []);
  // A deployment built without the `mcp` extra serves no /mcp: say so before anyone hands out a token for it.
  const health = useAsync(() => api.health(), []);
  const mcpOff = health.data?.checks?.mcp?.enabled === false;
  const act = useAction();
  const [name, setName] = useState("");
  const [token, setToken] = useState<{ client_id: string; token: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const create = async () => {
    const r = await act.run(() => api.createMcpClient(name.trim()));
    if (r) {
      setToken({ client_id: r.client_id, token: r.token });
      setName("");
      setOpen(r.client_id);
      void clients.reload();
    }
  };
  const revoke = async (c: McpClientRow) => {
    if (await act.run(() => api.revokeMcpClient(c.client_id)) !== undefined) void clients.reload();
  };
  const current = (clients.data ?? []).find((c) => c.client_id === open) ?? null;
  return (
    <div className="stack">
      <Card title="MCP and API clients">
        <p className="muted small">
          Other agents reach the platform at <code>/mcp</code> (and <code>/v2/tools/…:invoke</code>) with a client token. Each client acts
          as its own service identity within its grants: a role capped at analyst, tools by name, family or pattern, a per-minute rate,
          daily quotas and an expiry.
        </p>
        {mcpOff && <Notice tone="warning"><p>The MCP endpoint is turned off on this installation (the image was built without the
          <code>mcp</code> extra), so MCP clients cannot connect yet. Tokens still work for <code>/v2/tools/…:invoke</code>. Rebuild the
          API image with <code>EXTRAS=…,mcp</code> to turn it on.</p></Notice>}
        <form className="chip-row" aria-label="New client" onSubmit={(e) => { e.preventDefault(); void create(); }}>
          <input aria-label="Client name" placeholder="Client name, e.g. claude-desktop" required value={name} onChange={(e) => setName(e.target.value)} />
          <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || !name.trim()}>Create client</button>
        </form>
        <ErrorBox error={act.error} />
        {token && (
          <Notice tone="warning">
            Token for <code>{token.client_id}</code>, shown once: store it in the client's secret store now.
            <div><code className="small" aria-label="Client token">{token.token}</code></div>
            <button type="button" className="btn btn-xs btn-ghost" onClick={() => setToken(null)}>I stored it</button>
          </Notice>
        )}
      </Card>
      <ErrorBox error={clients.error} onRetry={clients.reload} />
      {clients.loading && !clients.data && <Loading label="Loading clients…" />}
      {clients.data && (clients.data.length === 0 ? <EmptyState title="No MCP clients yet" /> : (
        <Card title={`Clients (${clients.data.length})`}>
          <div className="table-wrap">
            <table className="table table-compact" aria-label="MCP clients">
              <thead><tr><th>Client</th><th>Status</th><th>Grants</th><th>Last used</th><th><span className="sr-only">Actions</span></th></tr></thead>
              <tbody>{clients.data.map((c) => (
                <tr key={c.client_id} className={c.client_id === open ? "row-active" : undefined}>
                  <td>{c.name}<div className="muted small"><code>{c.client_id}</code></div></td>
                  <td><Tag tone={c.status === "active" ? "success" : "neutral"}>{c.status}</Tag></td>
                  <td className="small">{c.grants.length}{c.grants.some((g) => grantExpired(g)) ? " (some expired)" : ""}</td>
                  <td className="small">{c.last_used_at ? fmtDate(c.last_used_at) : "never"}</td>
                  <td>
                    <button type="button" className="btn btn-xs btn-ghost" aria-label={`Grants of ${c.name}`}
                      onClick={() => setOpen(open === c.client_id ? null : c.client_id)}>{open === c.client_id ? "Close" : "Grants"}</button>
                    {c.status === "active" && (
                      <button type="button" className="btn btn-xs btn-danger" disabled={act.busy} onClick={() => void revoke(c)}>Revoke</button>
                    )}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </Card>
      ))}
      {current && <ClientGrants key={current.client_id} client={current} onChanged={() => void clients.reload()} />}
      <McpCatalogue clients={clients.data ?? []} />
    </div>
  );
}

function ClientGrants({ client, onChanged }: { client: McpClientRow; onChanged: () => void }) {
  const [editing, setEditing] = useState<string | null>(client.grants.length ? null : "");
  const act = useAction();
  const workspaces = useAsync(() => api.listWorkspaces(), []);
  const names = new Map((workspaces.data ?? []).map((w) => [w.id, w.name]));
  const remove = async (g: McpGrantRow) => {
    if (await act.run(() => api.deleteMcpGrant(client.client_id, g.workspace_id)) !== undefined) onChanged();
  };
  const grant = editing === null ? null : client.grants.find((g) => g.workspace_id === editing) ?? null;
  return (
    <Card title={<>Grants of {client.name}</>}
      actions={client.status === "active" ? <button type="button" className="btn btn-xs" onClick={() => setEditing("")}>Add grant</button> : null}>
      {client.grants.length === 0 && <p className="muted small">No grant: the client can authenticate but reach nothing.</p>}
      {client.grants.length > 0 && (
        <div className="table-wrap">
          <table className="table table-compact" aria-label="Grants">
            <thead><tr><th>Workspace</th><th>Role</th><th>Tools</th><th>Patterns</th><th>Rate</th><th>Slice</th><th>Expires</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>{client.grants.map((g) => (
              <tr key={g.workspace_id}>
                <td>{names.get(g.workspace_id) ?? <code>{g.workspace_id}</code>}</td>
                <td>{g.role}</td>
                <td className="small">{g.tools.join(", ") || "—"}</td>
                <td className="small">{(g.tool_patterns ?? []).join(", ") || "—"}</td>
                <td className="small">{g.rate_per_min ? `${g.rate_per_min}/min` : "—"}</td>
                <td className="small">{[...(g.domains ?? []), ...(g.assets ?? [])].join(", ") || "whole workspace"}</td>
                <td className="small">{g.expires_at ? <Tag tone={grantExpired(g) ? "danger" : "neutral"}>{fmtDate(g.expires_at)}</Tag> : "never"}</td>
                <td>
                  <button type="button" className="btn btn-xs btn-ghost" onClick={() => setEditing(g.workspace_id)}>Edit</button>
                  <button type="button" className="btn btn-xs btn-danger" disabled={act.busy} onClick={() => void remove(g)}>Remove</button>
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <ErrorBox error={act.error} />
      {editing !== null && client.status === "active" && (
        <GrantEditor key={editing || "new"} client={client} grant={grant} onDone={() => { setEditing(null); onChanged(); }}
          onCancel={() => setEditing(null)} />
      )}
    </Card>
  );
}

function toLocalInput(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function GrantEditor({ client, grant, onDone, onCancel }: { client: McpClientRow; grant: McpGrantRow | null; onDone: () => void; onCancel: () => void }) {
  const id = useId();
  const [ws, setWs] = useState(grant?.workspace_id ?? "");
  const [role, setRole] = useState(grant?.role ?? "analyst");
  const [builtins, setBuiltins] = useState<string[]>((grant?.tools ?? []).filter((t) => GRANTABLE_BUILTINS.includes(t)));
  const [registry, setRegistry] = useState((grant?.tools ?? []).filter((t) => !GRANTABLE_BUILTINS.includes(t)).join(", "));
  const [patterns, setPatterns] = useState((grant?.tool_patterns ?? []).join(", "));
  const [rate, setRate] = useState(grant?.rate_per_min ? String(grant.rate_per_min) : "");
  const [domains, setDomains] = useState((grant?.domains ?? []).join(", "));
  const [assets, setAssets] = useState((grant?.assets ?? []).join(", "));
  const [expires, setExpires] = useState(toLocalInput(grant?.expires_at ?? null));
  const act = useAction();
  const toggle = (t: string, on: boolean) => setBuiltins((cur) => on ? [...cur, t] : cur.filter((x) => x !== t));
  const submit = async () => {
    const body = {
      role, tools: [...builtins, ...parseList(registry)], tool_patterns: parseList(patterns), quotas: grant?.quotas ?? {},
      rate_per_min: rate ? Number(rate) : null, expires_at: expires ? new Date(expires).toISOString() : null,
      domains: parseList(domains), assets: parseList(assets),
    };
    if (await act.run(() => api.setMcpGrant(client.client_id, ws, body)) !== undefined) onDone();
  };
  return (
    <form className="form" aria-label="Grant" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <div className="form-row">
        {grant ? <Field label="Workspace"><code>{grant.workspace_id}</code></Field> : (
          <Field label="Workspace"><WorkspacePicker value={ws} onChange={setWs} label="Grant workspace" /></Field>
        )}
        <Field label="Role" htmlFor={`${id}-role`} hint="An MCP client is capped at analyst: it can never publish, approve or administer">
          <select id={`${id}-role`} value={role} onChange={(e) => setRole(e.target.value)}>
            <option value="viewer">viewer</option><option value="analyst">analyst</option>
          </select>
        </Field>
        <Field label="Rate per minute (optional)" htmlFor={`${id}-rate`}>
          <input id={`${id}-rate`} type="number" min={1} max={100000} value={rate} onChange={(e) => setRate(e.target.value)} />
        </Field>
        <Field label="Expires (optional)" htmlFor={`${id}-exp`}>
          <input id={`${id}-exp`} type="datetime-local" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
      </div>
      <fieldset className="chip-row" aria-label="Built-in tools">
        <legend className="small">Built-in tools</legend>
        {GRANTABLE_BUILTINS.map((t) => (
          <label key={t} className="inline-field small">
            <input type="checkbox" checked={builtins.includes(t)} onChange={(e) => toggle(t, e.target.checked)} /> <code>{t}</code>
          </label>
        ))}
      </fieldset>
      <div className="form-row">
        <Field label="Registry and workspace tools" htmlFor={`${id}-reg`} hint="Exact ids or a family, e.g. metric.payment_failure_rate, query.*">
          <input id={`${id}-reg`} value={registry} onChange={(e) => setRegistry(e.target.value)} />
        </Field>
        <Field label="Tool patterns" htmlFor={`${id}-pat`} hint="Globs within a family, e.g. metric.payments_*; never cover built-in tools">
          <input id={`${id}-pat`} value={patterns} onChange={(e) => setPatterns(e.target.value)} />
        </Field>
      </div>
      <div className="form-row">
        <Field label="Domains (optional)" htmlFor={`${id}-dom`} hint="Limit the client to these catalog domains, e.g. itsm. Only context tools can be granted with a slice">
          <input id={`${id}-dom`} value={domains} onChange={(e) => setDomains(e.target.value)} />
        </Field>
        <Field label="Assets (optional)" htmlFor={`${id}-assets`} hint="Or to these tables as schema.table; combined with domains as a union">
          <input id={`${id}-assets`} value={assets} onChange={(e) => setAssets(e.target.value)} />
        </Field>
      </div>
      <div className="form-actions">
        <button type="button" className="btn btn-sm btn-ghost" onClick={onCancel}>Cancel</button>
        <button type="submit" className="btn btn-sm btn-primary" disabled={act.busy || !ws}>Save grant</button>
      </div>
      <ErrorBox error={act.error} />
    </form>
  );
}

function McpCatalogue({ clients }: { clients: McpClientRow[] }) {
  const [ws, setWs] = useState("");
  const tools = useAsync(() => (ws ? api.listRegistryTools(ws) : Promise.resolve([] as RegistryTool[])), [ws]);
  const catalogue = mcpCatalogue(tools.data ?? []);
  const reach = (tool: string) => clients.filter((c) => c.status === "active")
    .filter((c) => c.grants.some((g) => g.workspace_id === ws && grantCovers(g, tool))).map((c) => c.name);
  return (
    <Card title="MCP catalogue">
      <div className="toolbar"><WorkspacePicker value={ws} onChange={setWs} label="Catalogue workspace" /></div>
      <p className="muted small">What this workspace offers over MCP: the built-in tools and every certified registry tool exposed over MCP, with the
        clients whose grants reach each one. Workspace query and HTTP tools (<code>query.*</code>, <code>http.*</code>) are listed by the MCP endpoint itself.</p>
      <ErrorBox error={tools.error} onRetry={tools.reload} />
      {ws && tools.data && (
        <div className="table-wrap">
          <table className="table table-compact" aria-label="MCP catalogue">
            <thead><tr><th>Tool</th><th>Kind</th><th>Access</th><th>Clients with access</th></tr></thead>
            <tbody>{catalogue.map((t) => {
              const who = reach(t.name);
              return (
                <tr key={t.name}>
                  <td><code>{t.name}</code>{t.description && <UntrustedText text={t.description} />}</td>
                  <td className="small">{t.kind}</td>
                  <td>{t.readOnly ? <Tag>read only</Tag> : <Tag tone="warning">acts</Tag>}</td>
                  <td className="small">{who.length ? who.join(", ") : <span className="muted">none</span>}</td>
                </tr>
              );
            })}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
