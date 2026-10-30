import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { session, type CatalogAsset, type Crawl, type PlatformSettings, type SourceKindInfo } from "../api";
import { AuthProvider } from "../auth";
import { CrawlPanel } from "../components/CrawlPanel";
import { AdminPage } from "../pages/Admin";
import { SettingsEditor, TokenSavingsView } from "../pages/AdminSettings";
import { AskPage } from "../pages/Ask";
import { CatalogPage } from "../pages/Catalog";
import { MonitorForm } from "../pages/Monitoring";
import { ScheduleForm } from "../pages/Schedules";
import { AddSource, SourceCard } from "../pages/Sources";
import { buildCrawlInput, crawlStatsSummary, driftCounts, emptyCrawlForm, stageProgress } from "../lib/crawls";
import { buildMonitorConfig, describeMonitorConfig, emptyMonitorForm, validateMonitorForm } from "../lib/monitors";
import { buildScheduleConfig, emptyScheduleForm, formFromSchedule, mergeScheduleConfig } from "../lib/schedules";

const PROMPT = (name: string, text: string, placeholders: string[] = []) =>
  ({ name, version: name.split(".").pop() ?? "v1", text, built_in: text, edited: false, placeholders, settings_version: 3 });
import {
  fieldSchema, pendingChanges, presetEffect, savedShare, settingsPatch, validateLimits, withPurposeMode,
} from "../lib/settings";
import { buildSourceConfig, defaultKind, groupKinds, suggestedSecretRef, validateSourceForm } from "../lib/sourceKinds";

// ------------------------------------------------------------------------------------ fixtures & fetch mock
function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Handler = (url: string, init: RequestInit) => unknown;
type RouteDef = [method: string, pattern: RegExp, handler: Handler | unknown];

/** Route fetch calls by method + URL regex; unmatched calls return []. A handler may return a Response. */
function mockApi(routes: RouteDef[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    for (const [m, re, h] of routes) {
      if (m === method && re.test(url)) {
        const out = typeof h === "function" ? (h as Handler)(url, init ?? {}) : h;
        return out instanceof Response ? out : jsonResponse(out);
      }
    }
    return jsonResponse([]);
  });
}

function calls(fetchMock: ReturnType<typeof vi.spyOn>, method: string, re: RegExp): [string, RequestInit][] {
  return (fetchMock.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u)))
    .map(([u, i]) => [String(u), i ?? {}]);
}

const bodyOf = (init: RequestInit) => JSON.parse(String(init.body));

const USER = { id: "u1", email: "a@b", name: "A", is_admin: false, active: true, attributes: {}, created_at: "" };
const ADMIN = { ...USER, id: "u0", is_admin: true };

const KIND = (over: Partial<SourceKindInfo>): SourceKindInfo => ({
  kind: "postgres", label: "PostgreSQL", category: "database", required: ["host", "database", "username"],
  optional: ["port", "schemas", "include", "exclude", "max_tables"], default_port: 5432, docs: "", secret_field: "password",
  execution_mode: "pushdown", dialect: "postgres", driver_installed: true, install_hint: null, enabled: true, ...over,
});

const KINDS: SourceKindInfo[] = [
  KIND({}),
  KIND({ kind: "oracle", label: "Oracle", driver_installed: false, install_hint: "pip install 'analystos[oracle]'", execution_mode: "staged", dialect: "postgres" }),
  KIND({ kind: "snowflake", label: "Snowflake", category: "warehouse", required: ["account", "database", "username"], enabled: false }),
  KIND({ kind: "csv", label: "CSV / Parquet / XLSX", category: "file", required: ["path"], optional: ["delimiter"], secret_field: null, default_port: null, execution_mode: "staged" }),
  KIND({ kind: "servicenow", label: "ServiceNow", category: "api", required: ["username"], optional: ["instance_url", "tables", "page_size"], default_port: null, execution_mode: "staged" }),
];

const CRAWL = (over: Partial<Crawl> = {}): Crawl => ({
  id: "crl_1", source_id: "src_1", workspace_id: "ws_1", mode: "full", trigger: "manual", status: "succeeded", stage: "done",
  options: { include: [], exclude: [] }, stats: { discovered: 5, new: 1, changed: 1, deprecated: 1 },
  changes: {
    new: ["sales.returns"],
    changed: [{ key: "sales.orders", added: ["channel"], removed: ["legacy_flag"], retyped: [{ name: "amount", previous_type: "INTEGER", current_type: "NUMERIC" }], attributes_changed: false }],
    missing: ["sales.tmp"], deprecated: ["sales.tmp"], rename_candidates: [{ previous_key: "sales.cust", current_key: "sales.customers", similarity: 0.92 }],
  },
  log: [{ at: "2026-09-25T10:00:00Z", stage: "discover", message: "5 assets discovered, 5 after include/exclude" }],
  error: null, started_by: "user:u1", started_at: "2026-09-25T10:00:00Z", finished_at: "2026-09-25T10:00:05Z", ...over,
});

const SOURCE = {
  id: "src_1", workspace_id: "ws_1", kind: "postgres", name: "Warehouse", config: {}, secret_ref: "env:PG", status: "discovered",
  execution_mode: "pushdown", staging_schema: null, last_discovered_at: null, last_error: null, created_at: "",
};

beforeEach(() => session.set("tok123", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

// ------------------------------------------------------------------------------------ source kinds
describe("source-kind helpers", () => {
  it("groups kinds by category in a stable order and picks an enabled default", () => {
    const groups = groupKinds(KINDS);
    expect(groups.map((g) => g.category)).toEqual(["database", "warehouse", "file", "api"]);
    expect(groups[0].kinds.map((k) => k.kind)).toEqual(["oracle", "postgres"]);
    expect(defaultKind(KINDS)).toBe("servicenow");
    expect(defaultKind(KINDS.filter((k) => k.kind !== "servicenow"))).toBe("postgres");
    expect(suggestedSecretRef(KINDS[0])).toBe("env:POSTGRES_PASSWORD");
    expect(suggestedSecretRef(KINDS[3])).toBe("");
  });

  it("builds typed config (lists, numbers) and omits empty optional fields", () => {
    const cfg = buildSourceConfig(KINDS[0], { host: " db ", database: "dw", username: "ro", port: "5433", schemas: "public, sales", include: "" });
    expect(cfg).toEqual({ host: "db", database: "dw", username: "ro", port: 5433, schemas: ["public", "sales"] });
    expect(buildSourceConfig(KINDS[0], { host: "h", database: "d", username: "u" }, "staged").execution_mode).toBe("staged");
    expect(buildSourceConfig(KINDS[0], { host: "h", database: "d", username: "u" }, "pushdown").execution_mode).toBeUndefined();
  });

  it("validates required fields, numbers and the secret reference shape", () => {
    const errs = validateSourceForm(KINDS[0], "", { host: "h", port: "abc" }, "hunter2");
    expect(Object.keys(errs).sort()).toEqual(["database", "name", "port", "secret_ref", "username"]);
    expect(validateSourceForm(KINDS[0], "x", { host: "h", database: "d", username: "u" }, "env:PG_PASS")).toEqual({});
    expect(validateSourceForm(KINDS[0], "x", { host: "h", database: "d", username: "u" }, "file:/run/secrets/pg")).toEqual({});
  });
});

describe("AddSource", () => {
  it("renders the catalog grouped, disables admin-disabled kinds, warns about missing drivers and posts the typed body", async () => {
    const fetchMock = mockApi([
      ["GET", /\/api\/source-kinds$/, KINDS],
      ["POST", /\/api\/workspaces\/ws_1\/sources$/, SOURCE],
    ]);
    const onAdded = vi.fn();
    render(<MemoryRouter><AddSource wsId="ws_1" onAdded={onAdded} /></MemoryRouter>);
    const kindSel = (await screen.findByLabelText("Kind")) as HTMLSelectElement;
    expect(kindSel.value).toBe("servicenow");
    expect(kindSel.querySelectorAll("optgroup").length).toBe(4);
    const snow = within(kindSel).getByRole("option", { name: /Snowflake/ }) as HTMLOptionElement;
    expect(snow.disabled).toBe(true);
    expect(snow.textContent).toMatch(/disabled by admin/);

    fireEvent.change(kindSel, { target: { value: "oracle" } });
    expect(screen.getByText(/pip install 'analystos\[oracle\]'/)).toBeTruthy();
    expect(screen.getByText(/Staged: selected tables are copied/)).toBeTruthy();

    fireEvent.change(kindSel, { target: { value: "postgres" } });
    expect((screen.getByLabelText("Port") as HTMLInputElement).value).toBe("5432");
    expect((screen.getByLabelText("Secret reference (password)") as HTMLInputElement).value).toBe("env:POSTGRES_PASSWORD");
    expect(screen.getByText(/Secrets never go in config/)).toBeTruthy();

    // client-side validation first
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(await screen.findByText("Host is required.")).toBeTruthy();
    expect(calls(fetchMock, "POST", /\/sources$/)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Name *"), { target: { value: "Warehouse" } });
    fireEvent.change(screen.getByLabelText("Host *"), { target: { value: "db.internal" } });
    fireEvent.change(screen.getByLabelText("Database *"), { target: { value: "dw" } });
    fireEvent.change(screen.getByLabelText("Username *"), { target: { value: "reader" } });
    fireEvent.change(screen.getByLabelText("Schemas"), { target: { value: "public, sales" } });
    fireEvent.change(screen.getByLabelText("Execution mode"), { target: { value: "staged" } });
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
    const [, init] = calls(fetchMock, "POST", /\/sources$/)[0];
    expect(bodyOf(init)).toEqual({
      kind: "postgres", name: "Warehouse", secret_ref: "env:POSTGRES_PASSWORD",
      config: { host: "db.internal", database: "dw", username: "reader", port: 5432, schemas: ["public", "sales"], execution_mode: "staged" },
    });
  });

  it("shows the server's validation error verbatim", async () => {
    mockApi([
      ["GET", /\/api\/source-kinds$/, KINDS],
      ["POST", /\/sources$/, jsonResponse({ error: { code: "invalid_input", message: "source kind 'postgres' is disabled by the administrator" } }, 422)],
    ]);
    render(<MemoryRouter><AddSource wsId="ws_1" onAdded={vi.fn()} /></MemoryRouter>);
    const kindSel = await screen.findByLabelText("Kind");
    fireEvent.change(kindSel, { target: { value: "postgres" } });
    for (const [label, v] of [["Name *", "W"], ["Host *", "h"], ["Database *", "d"], ["Username *", "u"]]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value: v } });
    }
    fireEvent.click(screen.getByRole("button", { name: "Add source" }));
    expect(await screen.findByText(/disabled by the administrator/)).toBeTruthy();
  });
});

describe("EditSource", () => {
  const CONFIGURED = { ...SOURCE, config: { host: "db.internal", database: "dw", username: "reader" } };

  it("pre-fills from the source's own config and sends only the changed field", async () => {
    const fetchMock = mockApi([
      ["GET", /\/api\/source-kinds$/, KINDS],
      ["PATCH", /\/api\/workspaces\/ws_1\/sources\/src_1$/, { ...CONFIGURED, name: "Warehouse (primary)" }],
    ]);
    const onChanged = vi.fn();
    render(<MemoryRouter><SourceCard wsId="ws_1" source={CONFIGURED} assets={[]} onChanged={onChanged} onOpenAsset={vi.fn()}
      activeAsset={null} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(((await screen.findByLabelText("Host *")) as HTMLInputElement).value).toBe("db.internal");
    expect((screen.getByLabelText("Database *") as HTMLInputElement).value).toBe("dw");
    expect((screen.getByLabelText("Secret reference (password)") as HTMLInputElement).value).toBe("env:PG");

    fireEvent.change(screen.getByLabelText("Name *"), { target: { value: "Warehouse (primary)" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const [, init] = calls(fetchMock, "PATCH", /\/sources\/src_1$/)[0];
    expect(bodyOf(init)).toEqual({ name: "Warehouse (primary)" }); // config and secret untouched: not sent
  });

  it("refuses to save nothing, and Cancel closes without a request", async () => {
    const fetchMock = mockApi([["GET", /\/api\/source-kinds$/, KINDS]]);
    render(<MemoryRouter><SourceCard wsId="ws_1" source={CONFIGURED} assets={[]} onChanged={vi.fn()} onOpenAsset={vi.fn()}
      activeAsset={null} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    await screen.findByLabelText("Host *");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Nothing changed yet.")).toBeTruthy();
    expect(calls(fetchMock, "PATCH", /\/sources\//)).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Host *")).toBeNull();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("sends clear_secret_ref when the secret reference is emptied", async () => {
    const fetchMock = mockApi([
      ["GET", /\/api\/source-kinds$/, KINDS],
      ["PATCH", /\/api\/workspaces\/ws_1\/sources\/src_1$/, { ...CONFIGURED, secret_ref: null }],
    ]);
    render(<MemoryRouter><SourceCard wsId="ws_1" source={CONFIGURED} assets={[]} onChanged={vi.fn()} onOpenAsset={vi.fn()}
      activeAsset={null} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    fireEvent.change(await screen.findByLabelText("Secret reference (password)"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls(fetchMock, "PATCH", /\/sources\/src_1$/)).toHaveLength(1));
    const [, init] = calls(fetchMock, "PATCH", /\/sources\/src_1$/)[0];
    expect(bodyOf(init)).toEqual({ clear_secret_ref: true });
  });
});

// ------------------------------------------------------------------------------------ crawls
describe("crawl helpers", () => {
  it("sends only what was set", () => {
    expect(buildCrawlInput(emptyCrawlForm())).toEqual({});
    expect(buildCrawlInput({ mode: "full", include: "sales.*, incident", exclude: " ", profile: false, enrich: true }))
      .toEqual({ mode: "full", include: ["sales.*", "incident"], profile: false, enrich: true });
  });

  it("summarises stats, drift and stage progress", () => {
    expect(crawlStatsSummary({ discovered: 12, new: 2, changed: 0, deprecated: 1, tokens_saved: 300 }))
      .toBe("12 discovered · 2 new · 1 deprecated · 300 tokens saved");
    expect(crawlStatsSummary({})).toBe("");
    expect(driftCounts(CRAWL().changes)).toEqual({ new: 1, changed: 1, missing: 1, deprecated: 1, renames: 1 });
    expect(stageProgress("semantics")).toBe("stage 4 of 10: semantics");
    expect(stageProgress("done")).toBe("done");
  });
});

describe("CrawlPanel", () => {
  it("starts a crawl with mode and patterns, polls it until it finishes, and shows drift and the stage log", async () => {
    let polls = 0;
    const running = CRAWL({ id: "crl_2", status: "running", stage: "semantics", stats: {}, changes: {}, log: [], finished_at: null });
    const fetchMock = mockApi([
      ["GET", /\/crawls\?source_id=src_1$/, [CRAWL()]],
      ["POST", /\/sources\/src_1\/crawl$/, running],
      ["GET", /\/api\/crawls\/crl_2$/, () => (++polls < 2 ? running : CRAWL({ id: "crl_2" }))],
    ]);
    const onFinished = vi.fn();
    render(<CrawlPanel wsId="ws_1" source={SOURCE} onFinished={onFinished} pollMs={15} />);
    expect(await screen.findByText(/5 discovered · 1 new · 1 changed · 1 deprecated/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Crawl" }));
    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "incremental" } });
    fireEvent.change(screen.getByLabelText("Include patterns"), { target: { value: "sales.*" } });
    fireEvent.change(screen.getByLabelText("Exclude patterns"), { target: { value: "*_tmp" } });
    fireEvent.click(screen.getByRole("button", { name: "Start crawl" }));
    await waitFor(() => expect(calls(fetchMock, "POST", /\/crawl$/)).toHaveLength(1));
    expect(bodyOf(calls(fetchMock, "POST", /\/crawl$/)[0][1])).toEqual({ mode: "incremental", include: ["sales.*"], exclude: ["*_tmp"] });
    expect((await screen.findAllByText(/stage 4 of 10: semantics/)).length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "Crawl running…" }) as HTMLButtonElement).disabled).toBe(true);

    await waitFor(() => expect(onFinished).toHaveBeenCalledTimes(1), { timeout: 2000 });
    expect(polls).toBeGreaterThanOrEqual(2);
    // the finished crawl replaced the running row; the detail (opened on start) shows the drift
    const detail = await screen.findByRole("region", { name: "Crawl crl_2" });
    expect(within(detail).getByText("+channel")).toBeTruthy();
    expect(within(detail).getByText("−legacy_flag")).toBeTruthy();
    expect(within(detail).getByText(/INTEGER → NUMERIC/)).toBeTruthy();
    expect(within(detail).getByText("sales.customers")).toBeTruthy();
    expect(within(detail).getByText("92% similar")).toBeTruthy();
    expect(within(detail).getByText(/5 assets discovered/)).toBeTruthy();
    // polling stops once nothing is running
    const after = polls;
    await new Promise((r) => setTimeout(r, 60));
    expect(polls).toBe(after);
  });

  it("shows a failed crawl's error", async () => {
    mockApi([["GET", /\/crawls\?source_id=src_1$/, [CRAWL({ status: "failed", error: "invalid_input: connection failed: timeout", changes: {} })]]]);
    render(<CrawlPanel wsId="ws_1" source={SOURCE} pollMs={15} />);
    expect(await screen.findByText(/connection failed: timeout/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(within(screen.getByRole("region", { name: "Crawl crl_1" })).getByRole("alert").textContent).toMatch(/connection failed/);
  });
});

// ------------------------------------------------------------------------------------ catalog
const ASSET = (over: Partial<CatalogAsset> = {}): CatalogAsset => ({
  id: "ast_1", fq: "sales.orders", source_id: "src_1", name: "orders", business_name: "Orders", description: "One row per order.",
  description_origin: "rule", reviewed: false, selected: true, lifecycle: "active", row_count: 1200, role: "fact", domain: "sales",
  grain: "order", confidence: 0.8, last_crawled_at: "2026-09-25T10:00:00Z",
  columns: [
    { name: "customer_email", data_type: "TEXT", business_name: "Customer email", description: null, tags: ["pii"], tags_origin: "crawler",
      role: "attribute", unit: null, pii: { category: "email", sensitivity: "confidential", confidence: 0.95, reasons: ["name"] }, glossary: null },
    { name: "amount", data_type: "NUMERIC", business_name: "Amount", description: "Order total", tags: [], tags_origin: "crawler",
      role: "measure", unit: "currency", pii: null, glossary: { term_id: "ctx_1", term: "Revenue", score: 0.8, reason: "token overlap" } },
  ],
  ...over,
});

function renderCatalog() {
  return render(
    <MemoryRouter initialEntries={["/w/ws_1/catalog"]}>
      <Routes><Route path="/w/:wsId/catalog" element={<CatalogPage />} /></Routes>
    </MemoryRouter>,
  );
}

describe("CatalogPage", () => {
  it("shows measured profiles and explains when values are withheld", async () => {
    const a = ASSET();
    mockApi([
      ["GET", /\/api\/workspaces\/ws_1$/, { id: "ws_1", name: "W", role: "viewer", counts: {}, policy: {}, members: [] }],
      ["GET", /\/catalog/, [{ ...a, profile_meta: { profiled_at: "2026-09-25T10:00:00Z", rows_profiled: 1200,
        source: "snapshot", truncated: true }, columns: [
        { ...a.columns[0], profile: { non_null: 1100, null_rate: 0.0833, distinct: 600 } },
        { ...a.columns[1], profile: { non_null: 1200, null_rate: 0, distinct: 50, min: 2, max: 90,
          mean: 24, histogram: [{ bin: 0, low: 2, high: 46, count: 500 }, { bin: 1, low: 46, high: 90, count: 700 }] } },
      ] }]],
    ]);
    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Columns (2)" }));
    expect(screen.getByText(/staged copy is truncated/)).toBeTruthy();
    const buttons = screen.getAllByRole("button", { name: "View profile" });
    fireEvent.click(buttons[0]);
    expect(screen.getByText(/withheld for this sensitive column/)).toBeTruthy();
    fireEvent.click(buttons[1]);
    expect(screen.getByText("Range")).toBeTruthy();
    expect(screen.getByText("Distribution")).toBeTruthy();
  });

  it("lists assets with origin badges, filters server-side, expands columns and lets an editor curate", async () => {
    const fetchMock = mockApi([
      ["GET", /\/api\/workspaces\/ws_1$/, { id: "ws_1", name: "W", role: "editor", counts: {}, policy: {}, members: [] }],
      ["GET", /\/catalog/, (url: string) => (url.includes("domain=hr") ? [] : [ASSET(), ASSET({ id: "ast_2", fq: "hr.people", domain: "hr", role: "dimension", description_origin: "model" })])],
      ["PATCH", /\/api\/assets\/ast_1\/metadata$/, (_u: string, init: RequestInit) => ({
        id: "ast_1", business_name: "Customer orders", description: bodyOf(init).description ?? "One row per order.",
        description_origin: bodyOf(init).description ? "user" : "rule", reviewed: bodyOf(init).reviewed ?? false,
      })],
    ]);
    renderCatalog();
    expect(await screen.findByText("sales.orders")).toBeTruthy();
    expect(screen.getByText("rule").getAttribute("title")).toMatch(/deterministic catalog rules/);
    expect(screen.getByText("model")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Search tables, columns and descriptions"), { target: { value: "orders" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(calls(fetchMock, "GET", /\/catalog\?q=orders$/)).toHaveLength(1));
    await waitFor(() => expect((screen.getByLabelText("Domain") as HTMLSelectElement).options.length).toBe(3));
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "hr" } });
    expect(await screen.findByText("No tables match")).toBeTruthy();
    // the option list keeps both domains even though the filtered result is empty
    expect((screen.getByLabelText("Domain") as HTMLSelectElement).options.length).toBe(3);
    fireEvent.change(screen.getByLabelText("Domain"), { target: { value: "" } });
    fireEvent.click(screen.getByLabelText(/Include deprecated/));
    await waitFor(() => expect(calls(fetchMock, "GET", /include_deprecated=true/).length).toBeGreaterThan(0));

    const expand = await screen.findAllByRole("button", { name: "Columns (2)" });
    fireEvent.click(expand[0]);
    expect(expand[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("customer_email")).toBeTruthy();
    expect(screen.getByText("email")).toBeTruthy();
    expect(screen.getByText("Revenue").getAttribute("title")).toBe("token overlap");
    expect(screen.getByText("currency")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    expect(screen.getByText(/curation always wins/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Business name"), { target: { value: "Customer orders" } });
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "Every confirmed order." } });
    fireEvent.click(screen.getByLabelText("Reviewed"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls(fetchMock, "PATCH", /metadata$/)).toHaveLength(1));
    expect(bodyOf(calls(fetchMock, "PATCH", /metadata$/)[0][1])).toEqual({
      business_name: "Customer orders", description: "Every confirmed order.", reviewed: true,
    });
    expect((await screen.findAllByText("Every confirmed order.")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("user").length).toBeGreaterThan(0);
    expect(screen.getAllByText("reviewed").length).toBeGreaterThan(0);
  });

  it("does not offer curation to viewers", async () => {
    mockApi([
      ["GET", /\/api\/workspaces\/ws_1$/, { id: "ws_1", name: "W", role: "viewer", counts: {}, policy: {}, members: [] }],
      ["GET", /\/catalog/, [ASSET()]],
    ]);
    renderCatalog();
    fireEvent.click(await screen.findByRole("button", { name: "Columns (2)" }));
    expect(screen.queryByRole("button", { name: "Edit description" })).toBeNull();
    expect(screen.getByText(/Editors and owners can curate/)).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ SQL console explain
describe("SQL console Explain", () => {
  function renderAsk() {
    return render(<MemoryRouter initialEntries={["/w/ws_1/ask"]}><Routes><Route path="/w/:wsId/ask" element={<AskPage />} /></Routes></MemoryRouter>);
  }

  it("explains an accepted query without executing it", async () => {
    const fetchMock = mockApi([["POST", /\/query\/explain$/, {
      statement: "SELECT", read_only: true, summary: "Returns priority and COUNT(*) from src.incident, grouped by priority.",
      tables: ["src.incident"], joins: [{ kind: "left", table: "src.problem", on: "incident.problem_id = problem.id" }],
      filter: "state = 'open'", group_by: ["priority"], aggregations: ["COUNT(*)"], order_by: ["2 descending"], limit: "10",
      gateway: { accepted: true },
    }]]);
    renderAsk();
    fireEvent.change(screen.getByLabelText("SQL (read-only)"), { target: { value: "SELECT priority, COUNT(*) FROM src.incident GROUP BY 1" } });
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/The gateway would accept this query/)).toBeTruthy();
    expect(screen.getByText(/grouped by priority\./)).toBeTruthy();
    expect(screen.getByText("src.problem")).toBeTruthy();
    expect(screen.getByText("state = 'open'")).toBeTruthy();
    const [, init] = calls(fetchMock, "POST", /\/query\/explain$/)[0];
    expect(bodyOf(init)).toEqual({ sql: "SELECT priority, COUNT(*) FROM src.incident GROUP BY 1", max_rows: 500 });
    expect(calls(fetchMock, "POST", /\/query$/)).toHaveLength(0);
  });

  it("shows the gateway's rejection reason", async () => {
    mockApi([["POST", /\/query\/explain$/, {
      statement: "SELECT", summary: "Returns all columns from src.users.", tables: ["src.users"],
      gateway: { accepted: false, code: "sql_rejected", reason: "column src.users.email is restricted" },
    }]]);
    renderAsk();
    fireEvent.change(screen.getByLabelText("SQL (read-only)"), { target: { value: "SELECT * FROM src.users" } });
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    const alert = await screen.findByText(/would reject this query/);
    expect(alert.closest("[role=alert]")?.textContent).toMatch(/sql_rejected.*column src\.users\.email is restricted/);
  });
});

// ------------------------------------------------------------------------------------ admin settings
const SETTINGS = (): PlatformSettings => ({
  llm: {
    purpose_modes: { planning: "auto", summarization: "off" }, routing_overrides: {}, profile_models: {}, disabled_models: [],
    cache_enabled: true, cache_ttl_hours: 168, cacheable_purposes: ["planning"], max_prompt_tokens: 16000,
    downgrade_below_budget_fraction: 0.25, compact_prompts: true, catalog_max_columns_per_table: 30, catalog_max_tables: 12,
  },
  analysis: { max_round1_hypotheses: 8, min_sample_size: 100 },
  crawl: { default_mode: "incremental", max_tables: 2000, llm_enrichment: false },
  monitors: { default_z_threshold: 3.0 },
  sources: { enabled_kinds: [], allow_pushdown: true, staged_max_rows: 1000000 },
  features: { jev_decisions: true, reports: true },
});

const SCHEMA = {
  properties: { llm: { $ref: "#/$defs/LLMSettings" }, crawl: { $ref: "#/$defs/CrawlSettings" }, analysis: { $ref: "#/$defs/AnalysisSettings" } },
  $defs: {
    LLMSettings: { properties: { max_prompt_tokens: { type: "integer", minimum: 500, maximum: 400000 } } },
    CrawlSettings: { properties: { default_mode: { enum: ["full", "incremental"], type: "string" }, max_tables: { type: "integer", minimum: 1, maximum: 100000 } } },
    AnalysisSettings: { properties: { min_sample_size: { type: "integer", minimum: 30, maximum: 1000000 } } },
  },
};

describe("settings helpers", () => {
  it("sends replaced maps whole and deep-merged sections as partial patches", () => {
    const saved = SETTINGS();
    const draft = SETTINGS();
    draft.llm.purpose_modes = withPurposeMode(saved.llm.purpose_modes, draft.llm.purpose_modes, "summarization", "auto");
    draft.crawl.max_tables = 500;
    draft.features.reports = false;
    expect(settingsPatch(saved, draft)).toEqual({
      llm: { purpose_modes: { planning: "auto", summarization: "auto" } },
      crawl: { max_tables: 500 },
      features: { reports: false },
    });
    expect(settingsPatch(saved, SETTINGS())).toEqual({});
    expect(pendingChanges(saved, draft).map((c) => c.path)).toEqual(["crawl.max_tables", "features.reports", "llm.purpose_modes.summarization"]);
  });

  it("removes a key when 'always' is chosen for a purpose the saved map lacks", () => {
    const saved = { planning: "auto" as const };
    const cur = withPurposeMode(saved, saved, "chart_selection", "off");
    expect(cur).toEqual({ planning: "auto", chart_selection: "off" });
    expect(withPurposeMode(saved, cur, "chart_selection", "always")).toEqual({ planning: "auto" });
    expect(withPurposeMode(saved, cur, "planning", "always")).toEqual({ planning: "always", chart_selection: "off" });
  });

  it("computes a preset's effect, schema ranges and limit errors, and saved share", () => {
    expect(presetEffect({ planning: "auto" }, { planning: "off", sql_generation: "off" }, ["planning", "sql_generation", "verification"]))
      .toEqual([{ purpose: "planning", from: "auto", to: "off" }, { purpose: "sql_generation", from: "always", to: "off" }]);
    expect(fieldSchema(SCHEMA, "crawl", "default_mode").enum).toEqual(["full", "incremental"]);
    expect(fieldSchema(SCHEMA, "llm", "max_prompt_tokens")).toMatchObject({ type: "integer", minimum: 500, maximum: 400000 });
    expect(fieldSchema(SCHEMA, "nope", "x")).toEqual({});
    const bad = SETTINGS();
    bad.analysis.min_sample_size = 10;
    bad.crawl.max_tables = 1.5;
    bad.llm.max_prompt_tokens = Number.NaN;
    expect(validateLimits(bad, SCHEMA)).toEqual({
      "analysis.min_sample_size": "Must be ≥ 30.", "crawl.max_tables": "Must be a whole number.", "llm.max_prompt_tokens": "Must be a number.",
    });
    expect(savedShare({ tokens_used: 300, tokens_saved: 100 })).toBe(0.25);
    expect(savedShare({ tokens_used: 0, tokens_saved: 0 })).toBe(0);
  });
});

describe("SettingsEditor", () => {
  const MODELS = {
    allowlist: ["openai/gpt-x", "typesafe/jev"], profiles: {}, routing: { planning: "reasoning", summarization: "chat", sql_generation: "sql" },
    providers: {}, available: {},
    effective: {
      planning: { profile: "reasoning", models: [], mode: "auto", available: true, deterministic_path: true, decision_model: false },
      summarization: { profile: "chat", models: [], mode: "off", available: true, deterministic_path: true, decision_model: false },
      sql_generation: { profile: "sql", models: [], mode: "always", available: false, deterministic_path: false, decision_model: false },
    },
  };

  function mockSettings(extra: RouteDef[] = []) {
    return mockApi([
      ...extra,
      ["GET", /\/api\/admin\/settings$/, { version: 3, settings: SETTINGS(), defaults: SETTINGS(), schema: SCHEMA,
        presets: { balanced: { planning: "always" }, offline: { planning: "off", summarization: "off", sql_generation: "off" } } }],
      ["GET", /\/api\/admin\/models$/, MODELS],
      ["GET", /\/api\/source-kinds$/, KINDS],
      ["GET", /\/api\/admin\/settings\/history$/, [
        { version: 3, note: "cheaper", created_by: "u0", created_at: "2026-09-24T10:00:00Z" },
        { version: 2, note: "initial", created_by: "u0", created_at: "2026-09-20T10:00:00Z" },
      ]],
      ["PUT", /\/api\/admin\/settings$/, { version: 4, changes: [{ path: "llm.purpose_modes.sql_generation", from: null, to: "auto" }] }],
      ["POST", /\/api\/admin\/settings\/preset$/, { version: 4, changes: [] }],
      ["POST", /\/api\/admin\/settings\/rollback$/, { version: 4, rolled_back_to: 2 }],
    ]);
  }

  it("edits a purpose mode and limits, requires a note, and PUTs the full purpose_modes map", async () => {
    session.set("tok", ADMIN);
    const fetchMock = mockSettings();
    render(<SettingsEditor />);
    const sel = (await screen.findByLabelText("Mode for sql_generation")) as HTMLSelectElement;
    expect(sel.value).toBe("always");
    expect(screen.getAllByText("rule path").length).toBe(2);
    expect(screen.getByText("model only")).toBeTruthy();
    fireEvent.change(sel, { target: { value: "off" } });
    expect(screen.getByText(/this capability stops working/)).toBeTruthy();
    fireEvent.change(sel, { target: { value: "auto" } });
    fireEvent.change(screen.getByLabelText("Max tables"), { target: { value: "500" } });
    fireEvent.click(screen.getByLabelText("Feature Reports"));
    expect(screen.getByRole("list", { name: "Pending changes" }).textContent).toMatch(/llm\.purpose_modes\.sql_generation/);

    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(await screen.findByText(/A note is required/)).toBeTruthy();
    expect(calls(fetchMock, "PUT", /\/admin\/settings$/)).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Note (required)"), { target: { value: "rules first for SQL" } });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));
    await waitFor(() => expect(calls(fetchMock, "PUT", /\/admin\/settings$/)).toHaveLength(1));
    expect(bodyOf(calls(fetchMock, "PUT", /\/admin\/settings$/)[0][1])).toEqual({
      patch: {
        llm: { purpose_modes: { planning: "auto", summarization: "off", sql_generation: "auto" } },
        crawl: { max_tables: 500 },
        features: { reports: false },
      },
      note: "rules first for SQL",
    });
    expect(await screen.findByText(/Saved as version 4/)).toBeTruthy();
  });

  it("blocks out-of-range limits", async () => {
    session.set("tok", ADMIN);
    mockSettings();
    render(<SettingsEditor />);
    fireEvent.change(await screen.findByLabelText("Min sample size"), { target: { value: "5" } });
    expect(screen.getByText("Must be ≥ 30.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Note (required)"), { target: { value: "x" } });
    expect((screen.getByRole("button", { name: "Save settings" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("confirms before applying a preset and rolling back", async () => {
    session.set("tok", ADMIN);
    const fetchMock = mockSettings();
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
    render(<SettingsEditor />);
    fireEvent.click(await screen.findByRole("button", { name: "Offline" }));
    expect(confirm.mock.calls[0][0]).toMatch(/2 purpose\(s\) change:\n  planning: auto → off/);
    expect(calls(fetchMock, "POST", /preset$/)).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Offline" }));
    await waitFor(() => expect(calls(fetchMock, "POST", /preset$/)).toHaveLength(1));
    expect(bodyOf(calls(fetchMock, "POST", /preset$/)[0][1])).toEqual({ preset: "offline" });

    expect(await screen.findByText("current")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Roll back to v2" }));
    await waitFor(() => expect(calls(fetchMock, "POST", /rollback$/)).toHaveLength(1));
    expect(bodyOf(calls(fetchMock, "POST", /rollback$/)[0][1])).toEqual({ version: 2 });
    expect(await screen.findByText(/Rolled back to version 2/)).toBeTruthy();
  });
});

describe("TokenSavingsView", () => {
  it("shows totals and per-purpose rows and refetches for another period", async () => {
    session.set("tok", ADMIN);
    const fetchMock = mockApi([["GET", /\/api\/admin\/token-savings/, (url: string) => ({
      days: Number(/days=(\d+)/.exec(url)?.[1] ?? 30),
      totals: { calls: 10, tokens_used: 3000, tokens_saved: 1000, cost_usd: 0.12, cache_hits: 4, deterministic_skips: 6, refused: 1, saved_share: 0.25 },
      by_purpose: {
        planning: { calls: 4, tokens_used: 2000, tokens_saved: 800, cost_usd: 0.1, by_status: { ok: 4, cache_hit: 3, skipped: 2 } },
        summarization: { calls: 6, tokens_used: 1000, tokens_saved: 200, cost_usd: 0.02, by_status: { ok: 6, refused: 1 } },
      },
    })]]);
    render(<TokenSavingsView />);
    expect(await screen.findByText("25.0% of actual plus estimated tokens")).toBeTruthy();
    const rows = screen.getAllByRole("row");
    expect(rows[1].textContent).toMatch(/planning/); // sorted by tokens saved
    expect(within(rows[1]).getByText("28.6%")).toBeTruthy(); // 800 / 2800
    expect(within(rows[2]).getAllByText("1").length).toBeGreaterThan(0); // refused
    fireEvent.change(screen.getByLabelText("Period in days"), { target: { value: "7" } });
    await waitFor(() => expect(calls(fetchMock, "GET", /token-savings\?days=7$/)).toHaveLength(1));
  });
});

describe("Admin page gating", () => {
  it("loads a linked Skills tab when a seeded skill omits tools", async () => {
    session.set("tok", ADMIN);
    mockApi([["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/skills$/, [{ id: "anova", category: "statistical", description: "One-way ANOVA",
      function: "analystos.skills.stats.one_way_anova", runtime: "in_process", deterministic: true, enabled: true }]]]);
    render(<AuthProvider><MemoryRouter initialEntries={["/operate/registry?tab=skills"]}><AdminPage section="registry" /></MemoryRouter></AuthProvider>);
    expect(screen.getByRole("tab", { name: "Skills", selected: true })).toBeTruthy();
    expect(await screen.findByText("One-way ANOVA")).toBeTruthy();
    expect(screen.getByText("analystos.skills.stats.one_way_anova")).toBeTruthy();
  });

  it("shows admin-only tabs as a notice to non-admins without calling their endpoints", async () => {
    const fetchMock = mockApi([["GET", /\/api\/auth\/me$/, USER], ["GET", /\/api\/agents$/, []]]);
    const settings = render(<AuthProvider><MemoryRouter><AdminPage section="settings" /></MemoryRouter></AuthProvider>);
    expect(screen.getByText(/platform administrators only/)).toBeTruthy();
    settings.unmount();
    render(<AuthProvider><MemoryRouter><AdminPage section="usage" /></MemoryRouter></AuthProvider>);
    expect(screen.getByRole("tab", { name: "Token savings", selected: true })).toBeTruthy();
    expect(screen.getByText(/platform administrators only/)).toBeTruthy();
    expect(calls(fetchMock, "GET", /\/admin\/(settings|token-savings|prompts)/)).toHaveLength(0);
  });

  it("lets an admin read the prompt templates", async () => {
    session.set("tok", ADMIN);
    mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/agents$/, []],
      ["GET", /\/api\/admin\/prompts$/, [PROMPT("planning.v2", "You plan analyses."), PROMPT("sql.v1", "Write SQL.")]],
    ]);
    render(<AuthProvider><MemoryRouter><AdminPage /></MemoryRouter></AuthProvider>);
    fireEvent.click(screen.getByRole("tab", { name: "Prompts" }));
    expect(await screen.findByText("Prompt templates (2)")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Filter prompts"), { target: { value: "sql" } });
    expect(screen.getByText("Prompt templates (1)")).toBeTruthy();
    expect(screen.getByText("Write SQL.")).toBeTruthy();
  });

  it("lets an admin edit a prompt, warns about a dropped placeholder and resets an edit", async () => {
    session.set("tok", ADMIN);
    const fetchMock = mockApi([
      ["GET", /\/api\/auth\/me$/, ADMIN], ["GET", /\/api\/agents$/, []],
      ["GET", /\/api\/admin\/prompts$/, [PROMPT("sql_generation.v1", "Write {dialect} SQL.", ["dialect"]),
        { ...PROMPT("planning.v1", "Plan briefly."), built_in: "Plan.", edited: true }]],
      ["PUT", /\/api\/admin\/prompts\/sql_generation\.v1$/, { version: 7, changes: [{ path: "prompts.overrides.sql_generation.v1" }] }],
      ["DELETE", /\/api\/admin\/prompts\/planning\.v1$/, { version: 8, changes: [] }],
    ]);
    render(<AuthProvider><MemoryRouter><AdminPage /></MemoryRouter></AuthProvider>);
    fireEvent.click(screen.getByRole("tab", { name: "Prompts" }));
    expect(await screen.findByText("1 edited.")).toBeTruthy();
    fireEvent.click(screen.getAllByRole("button", { name: "Edit" })[0]);  // rows keep the served order: sql_generation first
    const box = screen.getByLabelText("Prompt text");
    fireEvent.change(box, { target: { value: "Write SQL." } });
    expect(screen.getByText(/Saving will be refused/)).toBeTruthy();
    fireEvent.change(box, { target: { value: "Write careful {dialect} SQL." } });
    expect(screen.queryByText(/Saving will be refused/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved as platform settings version 7.")).toBeTruthy();
    const put = calls(fetchMock, "PUT", /\/api\/admin\/prompts\/sql_generation\.v1$/)[0];
    expect(JSON.parse(String(put[1]?.body))).toMatchObject({ text: "Write careful {dialect} SQL." });
    fireEvent.click(screen.getByRole("button", { name: "Reset to built-in" }));
    await waitFor(() => expect(calls(fetchMock, "DELETE", /\/api\/admin\/prompts\/planning\.v1$/)).toHaveLength(1));
  });
});

// ------------------------------------------------------------------------------------ monitors & schedules
describe("forecast_deviation monitors", () => {
  it("validates and builds the config", () => {
    const f = { ...emptyMonitorForm(), name: "Forecast", kind: "forecast_deviation" as const, metric: "mttr", grain: "day" as const };
    expect(validateMonitorForm(f)).toEqual({});
    expect(buildMonitorConfig(f)).toEqual({ metric: "mttr", grain: "day", z: 2.5, history: 60 });
    expect(buildMonitorConfig({ ...f, seasonalPeriods: "7" }).seasonal_periods).toBe(7);
    expect(Object.keys(validateMonitorForm({ ...f, forecastZ: "0", history: "2", seasonalPeriods: "1" })).sort())
      .toEqual(["forecastZ", "history", "seasonalPeriods"]);
    expect(describeMonitorConfig({ kind: "forecast_deviation", config: { metric: "mttr", grain: "day", z: 2.5, history: 60, seasonal_periods: 7 } } as never))
      .toBe("mttr per day · outside the ±2.5σ forecast from 60 periods · season 7");
  });

  it("creates a forecast_deviation monitor from the form", async () => {
    const fetchMock = mockApi([
      ["GET", /\/artifacts\?type=metric$/, [{ id: "a1", name: "mttr", content: { display_name: "MTTR" } }]],
      ["POST", /\/monitors$/, { id: "mon_1", name: "Forecast MTTR", kind: "forecast_deviation", config: {} }],
    ]);
    const onSaved = vi.fn();
    render(<MemoryRouter><MonitorForm wsId="ws_1" onSaved={onSaved} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Forecast MTTR" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "forecast_deviation" } });
    await screen.findByRole("option", { name: /MTTR/ });
    fireEvent.change(screen.getByLabelText("Metric"), { target: { value: "mttr" } });
    fireEvent.change(screen.getByLabelText("Interval z"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Season length (optional)"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: /Create monitor|Save|Create/ }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(bodyOf(calls(fetchMock, "POST", /\/monitors$/)[0][1])).toEqual({
      name: "Forecast MTTR", kind: "forecast_deviation", auto_investigate: false,
      config: { metric: "mttr", grain: "week", z: 3, history: 60, seasonal_periods: 4 },
    });
  });
});

describe("crawl schedules", () => {
  it("builds, round-trips and merges crawl config", () => {
    const f = { ...emptyScheduleForm("UTC"), name: "Nightly crawl", kind: "crawl" as const };
    expect(buildScheduleConfig(f)).toEqual({});
    const full = { ...f, sourceIds: ["src_1"], crawlMode: "full" as const, crawlInclude: "sales.*, hr.*", crawlExclude: "*_tmp" };
    expect(buildScheduleConfig(full)).toEqual({ source_ids: ["src_1"], mode: "full", include: ["sales.*", "hr.*"], exclude: ["*_tmp"] });
    const back = formFromSchedule({ id: "s", workspace_id: "ws", name: "n", kind: "crawl", cron: "0 6 * * *", timezone: "UTC",
      config: buildScheduleConfig(full), enabled: true, owner_id: "u", next_run_at: null, last_run_at: null, created_at: "" });
    expect(back.kind).toBe("crawl");
    expect(back.crawlInclude).toBe("sales.*, hr.*");
    expect(buildScheduleConfig(back)).toEqual(buildScheduleConfig(full));
    expect(mergeScheduleConfig({ mode: "full", include: ["x"], extra: 1 }, { mode: "incremental" })).toEqual({ extra: 1, mode: "incremental" });
  });

  it("creates a crawl schedule from the form", async () => {
    const fetchMock = mockApi([
      ["GET", /\/sources$/, [SOURCE]],
      ["POST", /\/schedules$/, { id: "sch_1" }],
    ]);
    const onSaved = vi.fn();
    render(<MemoryRouter><ScheduleForm wsId="ws_1" onSaved={onSaved} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Nightly crawl" } });
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "crawl" } });
    fireEvent.click(await screen.findByLabelText(/Warehouse/));
    fireEvent.change(screen.getByLabelText("Crawl mode"), { target: { value: "full" } });
    fireEvent.change(screen.getByLabelText("Exclude patterns"), { target: { value: "*_tmp" } });
    fireEvent.change(screen.getByLabelText("Time zone"), { target: { value: "UTC" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const body = bodyOf(calls(fetchMock, "POST", /\/schedules$/)[0][1]);
    expect(body.kind).toBe("crawl");
    expect(body.config).toEqual({ source_ids: ["src_1"], mode: "full", exclude: ["*_tmp"] });
  });
});
