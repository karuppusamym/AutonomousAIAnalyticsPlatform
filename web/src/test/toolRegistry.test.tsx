/** S10 registry UI: data tools (list, publish, signatures, usage, try it), external MCP admission and quarantine
 * review, MCP clients and grants, and the catalogue's grant-coverage rule (mirrors mcp/grants.py). */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, type McpClientRow, type RegistryTool, type RegistryToolDetail } from "../api";
import { AuthProvider } from "../auth";
import { DataTools, McpClients, McpServers } from "../components/ToolRegistry";
import { AdminPage } from "../pages/Admin";
import { globMatch, grantCovers, mcpCatalogue, parseList, signatureInfo } from "../lib/toolRegistry";

vi.setConfig({ testTimeout: 20000 });

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
type Route = [string, RegExp, unknown | ((url: string, init: RequestInit) => unknown)];

function mockApi(routes: Route[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    for (const [m, re, h] of routes) {
      if (m === method && re.test(url)) {
        const out = typeof h === "function" ? (h as (u: string, i: RequestInit) => unknown)(url, init ?? {}) : h;
        return out instanceof Response ? out : json(out);
      }
    }
    return json([]);
  });
}

const calls = (f: ReturnType<typeof mockApi>, method: string, re: RegExp) =>
  (f.mock.calls as [string, RequestInit | undefined][]).filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u)));
const body = (f: ReturnType<typeof mockApi>, method: string, re: RegExp) => JSON.parse(String(calls(f, method, re)[0][1]?.body));

const ADMIN = { id: "u0", email: "a@b", name: "A", is_admin: true, active: true, attributes: {}, created_at: "" };
const WS = [{ id: "ws1", name: "Payments", role: "owner" }];

const METRIC: RegistryTool = {
  tool_id: "metric.payment_failure_rate", version: "4.0.0", kind: "data_product", owner: "payments-analytics", status: "certified",
  side_effect: "none", title: "Payment failure rate", description: "Share of failed payments.", exposure: { rest: true, mcp: true },
};
const QUARANTINED: RegistryTool = {
  tool_id: "mcp.bi.count_rows", version: "1.0.0", kind: "external_mcp", owner: "mcp:bi", status: "quarantined",
  side_effect: "write_external", title: "count_rows", description: "Count the rows of a dataset.", exposure: { rest: true, mcp: false },
};
const REFUSED: RegistryTool = { ...QUARANTINED, tool_id: "mcp.bi.poisoned", status: "refused", title: "poisoned",
  description: "List dashboards." };

function detail(t: RegistryTool, over: Partial<RegistryToolDetail> = {}): RegistryToolDetail {
  return {
    id: "tool_1", tool_id: t.tool_id, version: t.version, kind: t.kind, owner: t.owner, status: t.status, status_reason: null,
    side_effect: t.side_effect, manifest_hash: "abcdef0123456789", source: { type: "metric", name: "payment_failure_rate", version: 4 },
    screening: {}, created_by: "user:u0", created_at: "2026-10-27T10:00:00Z",
    manifest: {
      apiVersion: "analystos/tool.v2", tool_id: t.tool_id, version: t.version, kind: t.kind, owner: t.owner, title: t.title,
      description: t.description, side_effect: t.side_effect, permissions: ["dataset.read"], data_classification_max: "internal_aggregate",
      input_schema: { type: "object", additionalProperties: false, properties: { limit: { type: "integer", minimum: 1, maximum: 100 } } },
      output_schema: {}, execution: { via: "semantic_compiler", ref: {} }, limits: { timeout_s: 30, max_rows: 5000, rate_per_min: 60 },
      exposure: { rest: true, mcp: !!t.exposure.mcp, grants_required: [t.tool_id] },
      signature: { alg: "ed25519", key_id: "tenant-signing-1", value: "c2lnbmF0dXJlLXZhbHVlLXZhbHVl" },
    },
    health: { status: t.status, signature: "valid", source_current: true, calls_24h: 3, error_rate_24h: 0, last_ok_at: null, invocable: true },
    versions: [{ version: t.version, status: t.status, status_reason: null, created_at: "2026-10-27T10:00:00Z" }],
    ...over,
  };
}

const USAGE = { tool_id: METRIC.tool_id, days: 7, calls: 2, by_status: { ok: 1, denied: 1 }, by_channel: { mcp: 2 },
  top_callers: [["mcp_client:mcpc_1", 2]], latency_ms_p50: 40, cost_usd: 0,
  recent: [{ at: "2026-10-27T10:00:00Z", version: "4.0.0", caller: "mcp_client:mcpc_1", channel: "mcp", status: "denied",
    reason: "quota_exceeded", latency_ms: 3, row_count: null, query_id: null }] };

function renderAt(node: React.ReactNode, url = "/settings/registry?ws=ws1") {
  return render(<AuthProvider><MemoryRouter initialEntries={[url]}>{node}</MemoryRouter></AuthProvider>);
}

beforeEach(() => session.set("tok", ADMIN));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("grant coverage mirrors the server", () => {
  const g = (over: Partial<McpClientRow["grants"][number]> = {}) => ({ tools: [], tool_patterns: [], expires_at: null, ...over });
  it("exact ids, families, patterns, context.read, never patterns over built-ins, never after expiry", () => {
    expect(grantCovers(g({ tools: ["metric.a"] }), "metric.a")).toBe(true);
    expect(grantCovers(g({ tools: ["metric.*"] }), "metric.b")).toBe(true);
    expect(grantCovers(g({ tool_patterns: ["metric.pay_*"] }), "metric.pay_rate")).toBe(true);
    expect(grantCovers(g({ tool_patterns: ["metric.pay_*"] }), "metric.refunds")).toBe(false);
    expect(grantCovers(g({ tool_patterns: ["*"] }), "ask")).toBe(false);
    expect(grantCovers(g({ tools: ["context.read"] }), "context.search")).toBe(true);
    expect(grantCovers(g({ tools: ["metric.a"], expires_at: "2000-01-01T00:00:00Z" }), "metric.a")).toBe(false);
    expect(globMatch("mcp.bi.x", "mcp.bi.?")).toBe(true);
    expect(globMatch("metric.a1", "metric.a[0-9]")).toBe(true);
    expect(parseList("a, b  c,,a")).toEqual(["a", "b", "c"]);
    expect(signatureInfo("tampered").tone).toBe("danger");
  });

  it("the catalogue lists built-ins and certified registry tools offered over MCP only", () => {
    const names = mcpCatalogue([METRIC, QUARANTINED, { ...METRIC, tool_id: "dataset.rest_only", exposure: { rest: true, mcp: false } }])
      .map((t) => t.name);
    expect(names).toContain("ask");
    expect(names).toContain("metric.payment_failure_rate");
    expect(names).not.toContain("mcp.bi.count_rows");
    expect(names).not.toContain("dataset.rest_only");
  });
});

describe("Data tools", () => {
  it("lists tools, opens health/usage/versions, and runs a tool as the viewer", async () => {
    const f = mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS],
      ["GET", /\/v2\/tools\?workspace_id=ws1/, [METRIC]],
      ["GET", /\/v2\/tools\/metric\.payment_failure_rate\/usage/, USAGE],
      ["GET", /\/v2\/tools\/metric\.payment_failure_rate\?/, detail(METRIC)],
      ["GET", /\/v2\/tool-keys/, [{ key_id: "tenant-signing-1", alg: "ed25519", public_key: "pk", status: "active", created_at: null, retired_at: null }]],
      ["POST", /:invoke$/, { tool_id: METRIC.tool_id, version: "4.0.0", kind: "data_product", status: "ok",
        result: { columns: ["region", "rate"], rows: [["EU", 0.02]], governed: true, metric_version: 4 } }],
    ]);
    renderAt(<DataTools />);
    expect(await screen.findByText("metric.payment_failure_rate")).toBeTruthy();
    expect(screen.getByText("tenant-signing-1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open metric.payment_failure_rate" }));
    expect(await screen.findByText("signature valid")).toBeTruthy();
    expect(screen.getByText("source current")).toBeTruthy();
    expect(await screen.findByText("quota_exceeded")).toBeTruthy();
    const form = screen.getByRole("region", { name: "Try this tool" });
    fireEvent.click(within(form).getByRole("button", { name: "Run" }));
    expect(await screen.findByText("EU")).toBeTruthy();
    expect(screen.getByText("governed")).toBeTruthy();
    expect(calls(f, "POST", /\/v2\/tools\/metric\.payment_failure_rate%404\.0\.0:invoke$/)).toHaveLength(1);
  });

  it("publishes a certified source with its options", async () => {
    const f = mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS], ["GET", /\/v2\/tools\?/, []],
      ["POST", /\/v2\/tools:publish$/, { ...detail(METRIC), created: true }],
      ["GET", /\/usage/, USAGE],
      ["GET", /\/v2\/tools\/metric\.payment_failure_rate\?/, detail(METRIC)],
    ]);
    renderAt(<DataTools />);
    fireEvent.click(await screen.findByRole("button", { name: "Publish as tool…" }));
    fireEvent.change(screen.getByLabelText("Source reference"), { target: { value: "payment_failure_rate" } });
    fireEvent.change(screen.getByLabelText("Row cap (optional)"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByText(/^Published/)).toBeTruthy();
    expect(body(f, "POST", /tools:publish/)).toEqual({ workspace_id: "ws1", source_type: "metric", ref: "payment_failure_rate",
      options: { exposure: { rest: true, mcp: true }, limits: { max_rows: 100 } } });
  });

  it("shows why a tampered manifest is refused and offers no run", async () => {
    mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS], ["GET", /\/v2\/tools\?/, [METRIC]],
      ["GET", /\/v2\/tools\/metric\.payment_failure_rate\?/, detail(METRIC, {
        health: { status: "certified", signature: "tampered", source_current: true, calls_24h: 0, error_rate_24h: null, last_ok_at: null,
          invocable: false } })],
      ["GET", /\/usage/, USAGE],
    ]);
    renderAt(<DataTools />, "/settings/registry?ws=ws1&tool=metric.payment_failure_rate");
    expect(await screen.findByText("changed after signing: refused")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Try this tool" })).toBeNull();
  });
});

describe("MCP servers", () => {
  it("admits a server's tools and classifies a quarantined one; a refused one cannot be classified", async () => {
    const server = { id: "mcps_1", workspace_id: "ws1", name: "bi", url: "https://bi.example.org/mcp", transport: "streamable_http",
      status: "ready", allowed: true, has_secret: true, config: {}, tools: [], last_refreshed_at: null, last_error: null };
    const f = mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS],
      ["GET", /\/mcp\/servers$/, [server]],
      ["GET", /\/v2\/tools\?workspace_id=ws1&kind=external_mcp/, [QUARANTINED, REFUSED]],
      ["POST", /\/v2\/mcp-servers\/mcps_1:admit$/, { server: "bi", server_id: "mcps_1", quarantined: ["mcp.bi.count_rows"], rescreened: [],
        refused: ["mcp.bi.poisoned"], retired: [], unchanged: [] }],
      ["GET", /\/v2\/tools\/mcp\.bi\.count_rows\/usage/, { ...USAGE, calls: 0, recent: [] }],
      ["GET", /\/v2\/tools\/mcp\.bi\.count_rows\?/, detail(QUARANTINED, { screening: { flags: [], screened_at: "2026-10-27T10:00:00Z" },
        health: { status: "quarantined", signature: "valid", source_current: null, calls_24h: 0, error_rate_24h: null, last_ok_at: null,
          invocable: false } })],
      ["POST", /mcp\.bi\.count_rows:classify$/, detail({ ...QUARANTINED, status: "certified", side_effect: "none" })],
    ]);
    renderAt(<McpServers />);
    fireEvent.click(await screen.findByRole("button", { name: "Admit tools" }));
    expect(await screen.findByText("1 quarantined · 1 refused")).toBeTruthy();
    expect(screen.getByText("Review queue (2)")).toBeTruthy();
    expect(screen.getByText("Count the rows of a dataset.").tagName).toBe("BLOCKQUOTE"); // server text is quoted data
    fireEvent.click(screen.getByRole("button", { name: "Review mcp.bi.count_rows" }));
    expect(await screen.findByText(/Quarantined: treated as writing externally/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Classify and certify" }));
    await waitFor(() => expect(calls(f, "POST", /:classify$/)).toHaveLength(1));
    expect(body(f, "POST", /:classify$/)).toEqual({ workspace_id: "ws1", side_effect: "none" });
  });
});

describe("MCP clients", () => {
  const CLIENT: McpClientRow = { client_id: "mcpc_1", name: "partner-agent", status: "active", service_user_id: "usr_s", created_at: null,
    last_used_at: null, grants: [{ workspace_id: "ws1", role: "analyst", tools: ["ask"], quotas: { ask: 10 }, tool_patterns: ["metric.*"],
      rate_per_min: 30, expires_at: null }] };

  it("shows a new token once, edits a grant keeping its quotas, and the catalogue shows who reaches what", async () => {
    const f = mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS], ["GET", /\/api\/admin\/mcp\/clients$/, [CLIENT]],
      ["POST", /\/api\/admin\/mcp\/clients$/, { client_id: "mcpc_2", name: "new", token: "mcpc_2.secret-value", note: "" }],
      ["PUT", /\/grants\/ws1$/, { ...CLIENT.grants[0], client_id: "mcpc_1" }],
      ["GET", /\/v2\/tools\?workspace_id=ws1$/, [METRIC]],
    ]);
    renderAt(<McpClients />, "/settings/registry");
    expect(await screen.findByText("partner-agent")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Client name"), { target: { value: "new" } });
    fireEvent.click(screen.getByRole("button", { name: "Create client" }));
    expect((await screen.findByLabelText("Client token")).textContent).toBe("mcpc_2.secret-value");

    fireEvent.click(screen.getByRole("button", { name: "Grants of partner-agent" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Tool patterns"), { target: { value: "metric.*, dataset.paid_*" } });
    fireEvent.click(screen.getByRole("button", { name: "Save grant" }));
    await waitFor(() => expect(calls(f, "PUT", /\/grants\/ws1$/)).toHaveLength(1));
    expect(body(f, "PUT", /\/grants\/ws1$/)).toMatchObject({ role: "analyst", tools: ["ask"], quotas: { ask: 10 },
      tool_patterns: ["metric.*", "dataset.paid_*"], rate_per_min: 30, expires_at: null });

    fireEvent.change(screen.getByLabelText("Catalogue workspace"), { target: { value: "ws1" } });
    const table = await screen.findByRole("table", { name: "MCP catalogue" });
    const metricRow = within(table).getByText("metric.payment_failure_rate").closest("tr") as HTMLElement;
    expect(within(metricRow).getByText("partner-agent")).toBeTruthy();
    const investigateRow = within(table).getByText("investigate").closest("tr") as HTMLElement;
    expect(within(investigateRow).getByText("none")).toBeTruthy();
  });
});

describe("Registry screen tabs", () => {
  it("adds Data tools, MCP servers and MCP clients to the admin registry", async () => {
    mockApi([["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/workspaces$/, WS], ["GET", /\/api\/admin\/mcp\/clients$/, []]]);
    renderAt(<AdminPage section="registry" />, "/settings/registry?tab=mcp-clients");
    expect(screen.getByRole("tab", { name: "MCP clients", selected: true })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Data tools" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "MCP servers" })).toBeTruthy();
    expect(await screen.findByText("No MCP clients yet")).toBeTruthy();
  });
});
