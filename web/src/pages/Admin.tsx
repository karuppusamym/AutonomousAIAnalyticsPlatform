import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, spendCountersDown, type AgentSpec, type ModelHealth, type ToolSpec } from "../api";
import { useAuth } from "../auth";
import { Card, EmptyState, EnabledToggle, ErrorBox, Loading, Notice, PageHeader, StatusBadge, Tabs, Tag, TechnicalDetails, Value } from "../components/ui";
import { fmtDate, fmtMs, fmtUsd } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import { PromptsView, SettingsEditor, TokenSavingsView } from "./AdminSettings";
import { CapabilityRegistry } from "./Registry";
import { DataTools, McpClients, McpServers } from "../components/ToolRegistry";

type Tab = "capabilities" | "agents" | "tools" | "skills" | "models" | "settings" | "savings" | "prompts" | "usage"
  | "data-tools" | "mcp-servers" | "mcp-clients";

export type AdminSection = "registry" | "settings" | "usage";

/**
 * The gear's platform screens (spec v4 §15), for platform administrators only (App.tsx renders a
 * not-entitled state for everyone else). Model configuration has one home: Platform settings.
 */
const SECTIONS: Record<AdminSection, { title: string; subtitle: string; tabs: { id: Tab; label: string }[] }> = {
  registry: {
    title: "Capability registry",
    subtitle: "Every installed capability (playbooks, agents, methods, tools, connectors, plugins and MCP tools) with certification, side effects and per-workspace enablement; signed data tools published for other agents; external MCP servers and MCP clients.",
    tabs: [{ id: "capabilities", label: "Capabilities" }, { id: "agents", label: "Agents" }, { id: "tools", label: "Tools" }, { id: "skills", label: "Skills" },
      { id: "prompts", label: "Prompts" }, { id: "data-tools", label: "Data tools" }, { id: "mcp-servers", label: "MCP servers" },
      { id: "mcp-clients", label: "MCP clients" }],
  },
  settings: {
    title: "Platform settings",
    subtitle: "Versioned runtime settings and model configuration: when models are used, presets, feature flags, limits, enabled source kinds, routing and provider health.",
    tabs: [{ id: "settings", label: "Settings" }, { id: "models", label: "Models & routing" }],
  },
  usage: {
    title: "Usage & cost",
    subtitle: "Model calls avoided, spend by purpose and query gateway activity. The audit log is under Members & policy.",
    tabs: [{ id: "savings", label: "Token savings" }, { id: "usage", label: "Usage" }],
  },
};

export function AdminPage({ section = "registry" }: { section?: AdminSection }) {
  const { user } = useAuth();
  const spec = SECTIONS[section];
  const [params, setParams] = useSearchParams();
  const requestedTab = params.get("tab");
  const tab = spec.tabs.find((item) => item.id === requestedTab)?.id ?? spec.tabs[0].id;
  const setTab = (next: Tab) => setParams((current) => {
    const updated = new URLSearchParams(current);
    updated.set("tab", next);
    return updated;
  });
  const tabbed = spec.tabs.length > 1;
  const isAdmin = !!user?.is_admin;
  return (
    <div className="page">
      <PageHeader title={spec.title} subtitle={spec.subtitle} />
      {tabbed && <Tabs value={tab} onChange={setTab} tabs={spec.tabs} />}
      <div className="tab-panel" role={tabbed ? "tabpanel" : undefined}>
        {!isAdmin ? (
          <Notice tone="warning">This section is available to platform administrators only.</Notice>
        ) : (
          <>
            {tab === "capabilities" && <CapabilityRegistry isAdmin={isAdmin} />}
            {tab === "agents" && <Agents canEdit={isAdmin} />}
            {tab === "tools" && <Tools canEdit={isAdmin} />}
            {tab === "skills" && <Skills />}
            {tab === "models" && <Models isAdmin={isAdmin} />}
            {tab === "settings" && <SettingsEditor />}
            {tab === "savings" && <TokenSavingsView />}
            {tab === "prompts" && <PromptsView />}
            {tab === "usage" && <UsageView />}
            {tab === "data-tools" && <DataTools />}
            {tab === "mcp-servers" && <McpServers />}
            {tab === "mcp-clients" && <McpClients />}
          </>
        )}
      </div>
    </div>
  );
}

function Agents({ canEdit }: { canEdit: boolean }) {
  const list = useAsync(() => api.agents(), []);
  const act = useAction();
  const toggle = async (a: AgentSpec, enabled: boolean) => {
    const r = await act.run(() => api.patchAgent(a.id, enabled));
    if (r) list.setData((prev) => prev?.map((x) => (x.id === a.id ? { ...x, enabled: r.enabled } : x)));
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <Card>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Agent</th><th>Phase</th><th>Model profile</th><th>Tools / skills</th><th>Knowledge / outputs</th><th>Enabled</th></tr></thead>
          <tbody>
            {list.data.map((a) => (
              <tr key={a.id}>
                <td><strong>{a.name}</strong> <code className="small">{a.id}</code> <span className="muted small">v{a.version}</span><div className="muted small">{a.description}</div></td>
                <td><Tag tone={a.phase === "mvp" ? "info" : "neutral"}>{a.phase}</Tag></td>
                <td><code className="small">{a.model_profile}</code></td>
                <td className="small">{a.tools.length} tools · {a.skills.length} skills</td>
                <td className="small">{a.knowledge?.sections.length ?? 0} knowledge sections · {(a.output_contract ?? []).map((o) => o.type).join(", ") || "no outputs"}</td>
                <td><EnabledToggle enabled={a.enabled} disabled={!canEdit || act.busy} onChange={(v) => toggle(a, v)} label={`Enable agent ${a.id}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Tools({ canEdit }: { canEdit: boolean }) {
  const list = useAsync(() => api.tools(), []);
  const act = useAction();
  const toggle = async (t: ToolSpec, enabled: boolean) => {
    const r = await act.run(() => api.patchTool(t.tool_id, enabled));
    if (r) list.setData((prev) => prev?.map((x) => (x.tool_id === t.tool_id ? { ...x, enabled: r.enabled } : x)));
  };
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  return (
    <Card>
      <ErrorBox error={act.error} />
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Tool</th><th>Category</th><th>Risk</th><th>Approval policy</th><th>Side effects</th><th>Min role</th><th>Enabled</th></tr></thead>
          <tbody>
            {list.data.map((t) => (
              <tr key={t.tool_id}>
                <td><code>{t.tool_id}</code><div className="muted small">{t.description}</div></td>
                <td>{t.category}</td>
                <td><Tag tone={t.risk === "high" ? "danger" : t.risk === "medium" ? "warning" : "neutral"}>{t.risk}</Tag></td>
                <td>{t.approval_policy === "none" ? <span className="muted">none</span> : <Tag tone="warning">{t.approval_policy}</Tag>}</td>
                <td className="small">{t.side_effects}</td>
                <td className="small">{t.min_role}</td>
                <td><EnabledToggle enabled={t.enabled} disabled={!canEdit || act.busy} onChange={(v) => toggle(t, v)} label={`Enable tool ${t.tool_id}`} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Skills() {
  const list = useAsync(() => api.skills(), []);
  if (list.error) return <ErrorBox error={list.error} onRetry={list.reload} />;
  if (!list.data) return <Loading />;
  if (!list.data.length) return <EmptyState title="No skills registered" />;
  return (
    <Card>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Skill</th><th>Category</th><th>Implementation</th><th>Deterministic</th><th>Status</th></tr></thead>
          <tbody>
            {list.data.map((s) => (
              <tr key={s.id}>
                <td><code>{s.id}</code><div className="muted small">{s.description}</div></td>
                <td>{s.category}</td>
                <td className="small"><code>{s.function ?? ((s.tools ?? []).join(", ") || "—")}</code>{s.runtime && <div className="muted">{s.runtime}</div>}</td>
                <td>{s.deterministic ? "yes" : "no"}</td>
                <td><StatusBadge status={s.enabled ? "ok" : "skipped"} label={s.enabled ? "enabled" : "disabled"} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/** Provider health: is a key set in the server process, last success, credit cooldown, today's spend vs the daily cap. */
export function ModelHealthPanel() {
  const h = useAsync(() => api.modelHealth(), []);
  const probe = useAction();
  const [probed, setProbed] = useState<ModelHealth | null>(null);
  const d = probed ?? h.data;
  if (h.error) return <ErrorBox error={h.error} onRetry={h.reload} />;
  if (!d) return <Loading />;
  const s = d.spend_today;
  const over = s.fraction != null && s.fraction >= s.alert_fraction;
  return (
    <Card title="Model health" actions={
      <button className="btn btn-sm" disabled={probe.busy} title="Sends one tiny billable request per provider"
        onClick={async () => { const r = await probe.run(() => api.modelHealth(true)); if (r) setProbed(r); }}>
        {probe.busy ? "Probing…" : "Probe credits"}
      </button>}>
      <ErrorBox error={probe.error} />
      {Array.from(new Set(d.providers.map((p) => p.message).filter((m): m is string => !!m)))
        .map((m) => <Notice key={m} tone="danger">{m}</Notice>)}
      {spendCountersDown(d) && <Notice tone="warning">
        Spend counters ({d.spend_counters?.store ?? "Redis"}) are unavailable: billable model calls are paused (fail closed) until
        they are back.</Notice>}
      <p className={`small ${over ? "" : "muted"}`}>
        Spend today (UTC): <strong>{fmtUsd(s.usd)}</strong>{s.cap_usd != null ? <> of the {fmtUsd(s.cap_usd)} daily cap
          ({Math.round((s.fraction ?? 0) * 100)}%)</> : " (no daily cap)"}; resets {fmtDate(s.resets_at)}.
        {over && <> <Tag tone="warning">above the {Math.round(s.alert_fraction * 100)}% alert</Tag></>}
      </p>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th>Provider</th><th>API key</th><th>Last success</th><th>Cooldown</th><th className="num">Calls today</th><th className="num">Spend today</th>{probed && <th>Probe</th>}</tr></thead>
          <tbody>
            {d.providers.map((p) => (
              <tr key={p.provider}>
                <td><strong>{p.provider}</strong> <span className="muted small">{p.kind}</span></td>
                <td>{!p.key_required ? <span className="muted small">not needed</span>
                  : <StatusBadge status={p.key_present ? "ok" : "failed"} label={p.key_present ? `${p.api_key_env} set` : `${p.api_key_env} missing`} />}</td>
                <td className="small">{p.last_success_at ? fmtDate(p.last_success_at) : "never (30 days)"}
                  {p.last_error && <div className="muted small" title={p.last_error.error}>last error {fmtDate(p.last_error.at)}: {p.last_error.error.slice(0, 80)}</div>}</td>
                <td className="small">{p.cooldown ? <span title={p.cooldown.reason}><Tag tone="warning">{p.cooldown.remaining_seconds}s left</Tag> {p.cooldown.reason.slice(0, 60)}</span> : "—"}</td>
                <td className="num">{p.calls_today}</td>
                <td className="num">{fmtUsd(p.spend_today_usd)}</td>
                {probed && <td className="small">{!p.probe ? "—" : p.probe.ok ? <StatusBadge status="ok" label={`ok (${p.probe.model})`} />
                  : p.probe.probed ? <span title={p.probe.error}><StatusBadge status="failed" label={p.probe.code ?? "failed"} /> {p.probe.remedy ?? p.probe.error?.slice(0, 80)}</span>
                    : <span className="muted">{p.probe.detail}</span>}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Models({ isAdmin }: { isAdmin: boolean }) {
  const m = useAsync(() => api.models(), []);
  if (m.error) return <ErrorBox error={m.error} onRetry={m.reload} />;
  if (!m.data) return <Loading />;
  const d = m.data;
  const decisionProfiles = new Set(Object.entries(d.profiles).filter(([, p]) => d.providers[p.provider]?.kind === "decision").map(([k]) => k));
  return (
    <div className="stack">
      {isAdmin && <ModelHealthPanel />}
      <Card title="Routing: purpose → profile → models">
        <p className="muted small">Agents request a purpose; the router picks the first allowed, available model of its profile (fail closed).
          Cheap first: the large tier answers only when the small answer fails a deterministic check (escalation, recorded per call).
          <span className="tag tag-jev">JEV decision</span> purposes use the TypeSafe Jev decision model: typed choices with probabilities.</p>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>Purpose</th><th>Profile</th><th>Models (fallback order)</th><th>Escalation</th><th>Mode</th><th>Available</th></tr></thead>
            <tbody>
              {Object.entries(d.routing).map(([purpose, profile]) => {
                const p = d.profiles[profile];
                const jev = decisionProfiles.has(profile);
                return (
                  <tr key={purpose} className={jev ? "row-jev" : undefined}>
                    <td><code>{purpose}</code>{jev && <span className="tag tag-jev">JEV decision</span>}</td>
                    <td><code className="small">{profile}</code>{p?.exclude_families?.length ? <div className="muted small">excludes: {p.exclude_families.join(", ")}</div> : null}</td>
                    <td className="small">{(d.effective?.[purpose]?.models ?? p?.models)?.join(" → ") ?? "—"}</td>
                    <td className="small">{d.effective?.[purpose]?.escalation_models?.length
                      ? <>{d.effective[purpose].escalation_models!.join(" → ")}<div className="muted small">{d.effective[purpose].escalation}</div></>
                      : <span className="muted">{d.effective?.[purpose]?.escalation === "always_large" ? "large first" : "—"}</span>}</td>
                    <td className="small">{d.effective?.[purpose]?.mode ?? "always"}{d.effective?.[purpose]?.deterministic_path && <div className="muted small">rule path</div>}</td>
                    <td><StatusBadge status={d.available[purpose] ? "ok" : "failed"} label={d.available[purpose] ? "available" : "unavailable"} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="grid-2">
        <Card title="Providers">
          <ul className="list compact">
            {Object.entries(d.providers).map(([k, p]) => (
              <li key={k} className="list-item"><span><strong>{k}</strong> <Tag tone={p.kind === "decision" ? "info" : "neutral"}>{p.kind}</Tag></span><code className="small">{p.base_url}</code></li>
            ))}
          </ul>
        </Card>
        <Card title="Allowlist">
          <div className="chip-row">{d.allowlist.map((a) => <span key={a} className={`tag ${a.startsWith("typesafe/") ? "tag-jev" : ""}`}>{a}</span>)}</div>
        </Card>
      </div>
      <Card title="Profiles"><TechnicalDetails value={d.profiles} label="Profile settings" /></Card>
    </div>
  );
}

function UsageView() {
  const u = useAsync(() => api.usage(), []);
  if (u.error) return <ErrorBox error={u.error} onRetry={u.reload} />;
  if (!u.data) return <Loading />;
  const total = u.data.models.reduce((s, m) => s + m.cost_usd, 0);
  const calls = u.data.models.reduce((s, m) => s + m.calls, 0);
  const complete = u.data.models.every((m) => m.cost_complete !== false);
  return (
    <div className="stack">
      <Card title={`Model usage — ${calls.toLocaleString()} calls, ${complete ? fmtUsd(total) : "cost incomplete"}`}>
        {!complete && <Notice tone="warning">Some calls have no known price. Their costs are unknown; the recorded amounts are only a subtotal.</Notice>}
        {u.data.models.length === 0 ? <EmptyState title="No model calls yet" /> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Purpose</th><th>Provider / model</th><th className="num">Calls</th><th className="num">Failed</th><th className="num">Avg latency</th><th className="num">Cost</th></tr></thead>
              <tbody>
                {u.data.models.map((m, i) => (
                  <tr key={i} className={m.provider === "typesafe" ? "row-jev" : undefined}>
                    <td><code>{m.purpose}</code>{m.provider === "typesafe" && <span className="tag tag-jev">JEV</span>}</td>
                    <td className="small">{m.provider} / {m.model}</td>
                    <td className="num"><Value value={m.calls} format="int" /></td>
                    <td className="num"><Value value={m.failed} format="int" /></td>
                    <td className="num"><Value value={m.avg_latency_ms} format="ms" /></td>
                    <td className="num">{m.cost_complete === false ? "Incomplete" : <Value value={m.cost_usd} format="usd" />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <Card title="Query gateway">
        {u.data.queries.length === 0 ? <EmptyState title="No queries yet" /> : (
          <div className="chip-row">
            {u.data.queries.map((q) => <span key={q.status} className="stat-inline"><StatusBadge status={q.status} /> {q.count.toLocaleString()} · avg {fmtMs(q.avg_ms)}</span>)}
          </div>
        )}
      </Card>
    </div>
  );
}

export function AuditTable({ rows, filter, onFilter }: { rows: import("../api").AuditEvent[]; filter: string; onFilter: (s: string) => void }) {
  return (
    <Card title={`Audit events (${rows.length})`} actions={
      <input type="search" placeholder="Filter actor, action, target…" aria-label="Filter audit events" value={filter} onChange={(e) => onFilter(e.target.value)} />}>
      {rows.length === 0 ? <EmptyState title="No audit events" /> : (
        <div className="table-wrap">
          <table className="table table-compact">
            <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Decision</th><th>Details</th></tr></thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td className="small">{fmtDate(e.created_at)}</td>
                  <td className="small"><code>{e.actor}</code></td>
                  <td><code>{e.action}</code></td>
                  <td className="small">{e.target ?? "—"}</td>
                  <td>{e.decision ? <StatusBadge status={e.decision} /> : "—"}{e.reasons?.length ? <div className="muted small">{e.reasons.join(", ")}</div> : null}</td>
                  <td>{e.details && Object.keys(e.details).length ? <TechnicalDetails value={e.details} label="details" /> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
