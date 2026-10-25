import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session, type Alert, type Approval, type CapabilitySummary, type TokenSavings } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { PayloadDiff } from "../components/ApprovalsPanel";
import { resolveRenderer, ResultView } from "../components/renderers";
import { SchemaForm } from "../components/SchemaForm";
import { buildBoard, columnOf, trustFacts } from "../lib/board";
import { groupByKind, sideEffectInfo } from "../lib/capabilities";
import { diffJson } from "../lib/diff";
import { initialDraft, normalize, objectFields, toValue, validate } from "../lib/jsonSchema";
import { explainTriage } from "../lib/monitors";
import { TokenSavingsView } from "../pages/AdminSettings";
import { SCREEN_BUDGET, SCREENS } from "../routes";
import { FUNNEL_RESULT, mockBackend, PLUGIN_METHOD, RUN, USER, WS } from "./mockBackend";

// Full-shell renders with axe are slow in jsdom when the whole suite runs in parallel.
vi.setConfig({ testTimeout: 20000 });

type Override =(method: string, path: string, body: string | null) => { status: number; body: unknown } | undefined;

/** The shared mock backend, with per-test overrides (a newly installed plugin, a refusal, …). */
function mockFetch(override?: Override) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? init.body : null;
    const o = override?.(method, String(input), body);
    if (o) return new Response(JSON.stringify(o.body), { status: o.status, headers: { "Content-Type": "application/json" } });
    const r = mockBackend(method, String(input), body);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function calls(fetchMock: ReturnType<typeof mockFetch>, method: string, re: RegExp): [string, RequestInit][] {
  return (fetchMock.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u)))
    .map(([u, i]) => [String(u), i ?? {}]);
}

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

async function axeClean(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
}

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

// ------------------------------------------------------------------------------------ P4-U07 JSON-Schema forms
describe("JSON Schema form model", () => {
  const schema = PLUGIN_METHOD.input_schema!;

  it("reads fields, required flags, nullable unions and defaults", () => {
    const fields = objectFields(schema);
    expect(fields.map((f) => [f.name, f.kind, f.required])).toEqual([
      ["asset", "string", true], ["steps", "array", true], ["segment", "string", false], ["alpha", "number", false],
      ["window_days", "integer", false], ["method", "enum", false], ["include_nulls", "boolean", false],
    ]);
    expect(fields.find((f) => f.name === "segment")?.nullable).toBe(true);
    expect(initialDraft(schema)).toEqual({ asset: "", steps: [], segment: "", alpha: "0.05", window_days: "30", method: "chi_square", include_nulls: false });
  });

  it("validates required fields, numbers, bounds, integers, enums and minItems", () => {
    const draft = { ...initialDraft(schema), alpha: "0.9", window_days: "2.5", steps: ["visit"], method: "bogus" };
    expect(validate(schema, draft)).toEqual({
      asset: "This field is required.", steps: "Add at least 2 values.", alpha: "Must be at most 0.5.",
      window_days: "Enter a whole number.", method: "Choose one of the listed options.",
    });
    expect(validate(schema, { ...draft, alpha: "abc" }).alpha).toBe("Enter a number.");
  });

  it("produces a typed value and leaves empty optional fields out", () => {
    const draft = { ...initialDraft(schema), asset: "events", steps: ["visit", " ", "buy"] };
    expect(validate(schema, draft)).toEqual({});
    expect(toValue(schema, draft)).toEqual({ asset: "events", steps: ["visit", "buy"], alpha: 0.05, window_days: 30, method: "chi_square", include_nulls: false });
  });

  it("follows local $refs and falls back to JSON for shapes it cannot draw", () => {
    const s = { type: "object", $defs: { Win: { type: "integer", minimum: 1 } }, properties: {
      win: { $ref: "#/$defs/Win" }, rows: { type: "array", items: { type: "object", properties: { a: { type: "string" } } } } } };
    const [win, rows] = objectFields(s);
    expect(win.kind).toBe("integer");
    expect(win.schema.minimum).toBe(1);
    expect(rows.kind).toBe("json");
    expect(validate(s, { win: "0", rows: "[{" })).toEqual({ win: "Must be at least 1.", rows: "Enter valid JSON." });
    expect(normalize({ type: ["number", "null"] })).toEqual({ schema: { type: "number" }, nullable: true });
  });
});

describe("SchemaForm", () => {
  it("renders labelled, described inputs, shows messages on submit and submits typed values", async () => {
    const onSubmit = vi.fn();
    const { container } = render(<SchemaForm schema={PLUGIN_METHOD.input_schema!} onSubmit={onSubmit} />);
    expect(screen.getByLabelText(/^Asset/).getAttribute("aria-describedby")).toBeTruthy();
    expect(screen.getByText("Table holding one row per event")).toBeTruthy();
    expect(screen.getByRole("group", { name: /Steps/ })).toBeTruthy();
    expect((screen.getByLabelText(/^Method/) as HTMLSelectElement).value).toBe("0");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText("This field is required.")).toBeTruthy();
    expect(screen.getByLabelText(/^Asset/).getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toMatch(/2 fields need attention/);

    fireEvent.change(screen.getByLabelText(/^Asset/), { target: { value: "events" } });
    fireEvent.click(screen.getByRole("button", { name: "Add steps" }));
    fireEvent.click(screen.getByRole("button", { name: "Add steps" }));
    fireEvent.change(screen.getByLabelText("Steps 1"), { target: { value: "visit" } });
    fireEvent.change(screen.getByLabelText("Steps 2"), { target: { value: "buy" } });
    fireEvent.change(screen.getByLabelText(/^Window days/), { target: { value: "14" } });
    fireEvent.click(screen.getByLabelText("Include nulls"));
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(onSubmit).toHaveBeenCalledWith({ asset: "events", steps: ["visit", "buy"], alpha: 0.05, window_days: 14, method: "chi_square", include_nulls: true });
    expect(await axeClean(container)).toEqual([]);
  });
});

describe("renderer registry", () => {
  it("uses the manifest's renderer, infers one from the shape, and falls back to JSON", () => {
    expect(resolveRenderer("renderer.stat_result", FUNNEL_RESULT).id).toBe("renderer.stat_result");
    expect(resolveRenderer("renderer.sankey", { columns: ["a"], rows: [[1]] }).id).toBe("renderer.table");
    expect(resolveRenderer(null, FUNNEL_RESULT).id).toBe("renderer.stat_result");
    expect(resolveRenderer(null, { note: "x", nested: { a: 1 } }).id).toBe("renderer.json");
  });

  it("keeps raw JSON under a closed Technical details and says when a renderer is missing", () => {
    const { container } = render(<ResultView rendererId="renderer.sankey" value={{ status: "done", rows_written: 12, nested: { a: 1 } }} />);
    expect(screen.getByText(/renderer\.sankey/)).toBeTruthy();
    expect(screen.getByText("done")).toBeTruthy();
    const details = container.querySelector("details[data-technical]") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(details.querySelector(".json")).toBeTruthy();
  });

  it("renders a stat result with n, q-value and effect size", () => {
    render(<ResultView rendererId="renderer.stat_result" value={FUNNEL_RESULT} />);
    expect(screen.getByText("1,830")).toBeTruthy();
    expect(screen.getByText("0.0042")).toBeTruthy();
    expect(screen.getByText("Cramér's V effect")).toBeTruthy(); // plain words (UI review P2-17); the code stays in the tooltip
    expect(screen.getByText("supported")).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ P4-U06 registry page
describe("capability registry page", () => {
  /** A plugin of a kind this UI has never heard of, with unknown certification and side-effect values. */
  const ALIEN: CapabilitySummary = {
    id: "forecaster.prophet_lite", kind: "Forecaster", version: "2.0.0", ref: "forecaster.prophet_lite@2.0.0", summary: "Seasonal forecasts",
    source: "pack:acme", entry: "python:acme.forecast:Prophet", determinism: "seeded", side_effect: "network", cost_class: "compute",
    certification: { status: "experimental" }, autonomous_ok: false, needs_approval: false, tags: [], enabled: null,
  };
  const withAlien: Override = (method, path) => {
    if (method === "GET" && /\/api\/capabilities(\?|$)/.test(path)) {
      const base = JSON.parse(mockBackend(method, path).body) as { digest: string; capabilities: CapabilitySummary[] };
      return { status: 200, body: { ...base, capabilities: [...base.capabilities, ALIEN] } };
    }
    return undefined;
  };

  it("lists every installed capability grouped by kind, including a new plugin and an unknown kind, with no UI change", async () => {
    mockFetch(withAlien);
    const { container } = renderAt("/operate/registry");
    const methods = await screen.findByRole("table", { name: "Analysis methods" });
    expect(within(methods).getByText(PLUGIN_METHOD.id)).toBeTruthy();
    expect(within(methods).getByText("entrypoint:acme-methods")).toBeTruthy();
    expect(within(methods).getAllByText("tested").length).toBeGreaterThan(0);
    const alien = screen.getByRole("table", { name: "Forecaster" });
    expect(within(alien).getByText("forecaster.prophet_lite")).toBeTruthy();
    expect(within(alien).getByText("experimental")).toBeTruthy();
    expect(within(alien).getByText("network (unclassified)").className).toContain("tag-danger");
    expect(screen.getByRole("heading", { name: "Forecaster (1)" })).toBeTruthy();
    const publishers = screen.getByRole("table", { name: "Publishers" });
    expect(within(publishers).getByText("writes externally")).toBeTruthy();
    expect(within(publishers).getByText("needs approval")).toBeTruthy();
    // The screen budget did not move: the plugin needed no route.
    expect(SCREENS.length).toBeLessThanOrEqual(SCREEN_BUDGET);
    expect(SCREENS.some((s) => /acme|forecast|funnel/.test(s.path))).toBe(false);
    expect(await axeClean(container)).toEqual([]);
  });

  it("toggles per-workspace enablement for owners and reloads the registry for admins", async () => {
    const fetchMock = mockFetch();
    renderAt(`/operate/registry?ws=${WS}`);
    const toggle = await screen.findByRole("switch", { name: `Enable ${PLUGIN_METHOD.id} in this workspace` });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    expect(calls(fetchMock, "GET", new RegExp(`/api/capabilities\\?workspace_id=${WS}$`)).length).toBeGreaterThan(0);
    fireEvent.click(toggle);
    await waitFor(() => expect(calls(fetchMock, "PUT", /\/capabilities\/method\.acme_funnel$/)).toHaveLength(1));
    expect(JSON.parse(String(calls(fetchMock, "PUT", /acme_funnel$/)[0][1].body))).toEqual({ enabled: true });
    await waitFor(() => expect((screen.getByRole("switch", { name: `Enable ${PLUGIN_METHOD.id} in this workspace` }) as HTMLInputElement).checked).toBe(true));
    // connectors are governed by source registration, not the toggle
    expect(within(screen.getByRole("table", { name: "Connectors" })).getByText("governed by source")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Reload registry" }));
    expect(await screen.findByText(/Registry reloaded: 7 capabilities/)).toBeTruthy();
    expect(calls(fetchMock, "POST", /\/admin\/capabilities\/reload$/)).toHaveLength(1);
  });

  it("gives a new method a generated form and a result view from its ui.renderer (no new screen)", async () => {
    const fetchMock = mockFetch((method, path) =>
      (method === "GET" && /\/api\/capabilities\?workspace_id=/.test(path)
        ? { status: 200, body: { digest: "d", capabilities: (JSON.parse(mockBackend(method, path).body) as { capabilities: CapabilitySummary[] })
          .capabilities.map((c) => (c.id === PLUGIN_METHOD.id ? { ...c, enabled: true } : c)) } }
        : undefined));
    const { container } = renderAt(`/operate/registry?ws=${WS}&cap=${PLUGIN_METHOD.id}`);
    const run = await screen.findByRole("region", { name: "Run this capability" });
    expect(screen.getByText("acme_methods/tests/test_funnel.py")).toBeTruthy();
    expect(screen.getByText("renderer.stat_result")).toBeTruthy();
    fireEvent.change(within(run).getByLabelText(/^Asset/), { target: { value: "events" } });
    fireEvent.click(within(run).getByRole("button", { name: "Add steps" }));
    fireEvent.click(within(run).getByRole("button", { name: "Add steps" }));
    fireEvent.change(within(run).getByLabelText("Steps 1"), { target: { value: "visit" } });
    fireEvent.change(within(run).getByLabelText("Steps 2"), { target: { value: "buy" } });
    fireEvent.click(within(run).getByRole("button", { name: "Run" }));
    await waitFor(() => expect(calls(fetchMock, "POST", /\/capabilities\/method\.acme_funnel\/invoke$/)).toHaveLength(1));
    expect(JSON.parse(String(calls(fetchMock, "POST", /invoke$/)[0][1].body))).toEqual({
      arguments: { asset: "events", steps: ["visit", "buy"], alpha: 0.05, window_days: 30, method: "chi_square", include_nulls: false } });
    const result = await screen.findByText("Result");
    const view = result.parentElement!.querySelector("[data-renderer]")!;
    expect(view.getAttribute("data-renderer")).toBe("renderer.stat_result");
    expect(within(view as HTMLElement).getByText("0.0042")).toBeTruthy();
    expect(await axeClean(container)).toEqual([]);
  });

  it("degrades to an explicit state when the server has no invoke route for a capability", async () => {
    mockFetch();
    renderAt(`/operate/registry?ws=${WS}&cap=method.trend`);
    const run = await screen.findByRole("region", { name: "Run this capability" });
    fireEvent.click(within(run).getByRole("button", { name: "Run" }));
    expect(await within(run).findByText("Not runnable from here yet")).toBeTruthy();
  });

  it("groups known kinds in order and treats unknown side effects as external writes", () => {
    const g = groupByKind([{ kind: "Zeta" }, { kind: "Tool" }, { kind: "Playbook" }, { kind: "Alpha" }].map((x, i) => ({ ...x, id: `x.${i}` }) as CapabilitySummary));
    expect(g.map((x) => x.kind)).toEqual(["Playbook", "Tool", "Alpha", "Zeta"]);
    expect(sideEffectInfo("teleport").tone).toBe("danger");
    expect(sideEffectInfo("none").tone).toBe("neutral");
  });
});

// ------------------------------------------------------------------------------------ P4-U03 investigation board
describe("investigation board", () => {
  it("puts hypotheses in status columns and hides superseded ones", () => {
    expect(["proposed", "approved", "testing", "supported", "rejected", "inconclusive", "weird"].map(columnOf))
      .toEqual(["proposed", "proposed", "testing", "supported", "rejected", "inconclusive", "proposed"]);
    expect(columnOf("superseded")).toBeNull();
    const b = buildBoard([], []);
    expect(b.columns.map((c) => c.label)).toEqual(["Proposed", "Testing", "Supported", "Rejected", "Inconclusive"]);
  });

  it("shows no raw JSON on the default path", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}/investigate/${RUN}`);
    await screen.findByRole("article", { name: "Hypothesis H1" });
    await screen.findByText(/Tokens avoided/);
    const blocks = [...container.querySelectorAll(".json")];
    expect(blocks.length).toBeGreaterThan(0); // the compiled constraints exist …
    for (const b of blocks) {
      const d = b.closest("details[data-technical]") as HTMLDetailsElement | null;
      expect(d, "raw JSON outside Technical details").not.toBeNull();
      expect(d!.open).toBe(false); // … but only behind a closed disclosure
    }
    expect(container.querySelector("details.json")).toBeNull();
  });

  it("lays out the hypotheses, the redirect chat and the cost meter", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}/investigate/${RUN}`);
    // Board and tree are one view: status counts that filter, and one row per hypothesis carrying its findings.
    const filters = await screen.findByRole("radiogroup", { name: "Show hypotheses" });
    expect(within(filters).getAllByRole("radio").map((b) => b.textContent?.replace(/^\W+/, "").trim()))
      .toEqual(["All (5)", "Proposed (1)", "Testing (1)", "Supported (1)", "Rejected (1)", "Inconclusive (1)"]);
    const h1 = screen.getByRole("article", { name: "Hypothesis H1" });
    expect(within(h1).getByText("Network resolves P1s slower")).toBeTruthy();
    expect(within(h1).getByRole("article", { name: "Finding F1" })).toBeTruthy();
    expect((h1.querySelector("details") as HTMLDetailsElement).open).toBe(true);
    // a rejected hypothesis reads from its row but starts closed
    expect((screen.getByRole("article", { name: "Hypothesis H2" }).querySelector("details") as HTMLDetailsElement).open).toBe(false);
    expect(within(screen.getByRole("article", { name: "Hypothesis H5" })).getByRole("article", { name: "Finding F2" })).toBeTruthy();
    fireEvent.click(within(filters).getByRole("radio", { name: /Rejected \(1\)/ }));
    expect(screen.getByRole("article", { name: "Hypothesis H2" })).toBeTruthy();
    expect(screen.queryByRole("article", { name: "Hypothesis H1" })).toBeNull();
    fireEvent.click(within(filters).getByRole("radio", { name: /All/ }));
    expect(screen.queryByText("An earlier framing replaced by a replan")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Show superseded hypotheses \(1\)/ }));
    expect(screen.getByText("An earlier framing replaced by a replan")).toBeTruthy();

    expect(screen.getByRole("list", { name: "Instructions sent to this investigation" }).textContent).toMatch(/Exclude auto-closed tickets/);
    expect(await screen.findByText("9,100")).toBeTruthy(); // tokens avoided
    // the model-tier breakdown is technical detail: inside a closed disclosure
    expect(screen.getByText("Tier breakdown not reported by this server.").closest("details[data-technical]:not([open])")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "Tokens used of run budget" }).getAttribute("aria-valuenow")).toBe("9"); // 18,250 / 200,000
    expect(await axeClean(container)).toEqual([]);
  });

  it("explains why to trust a finding, check by check, and returns focus on Escape", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}/investigate/${RUN}`);
    const f1 = await screen.findByRole("article", { name: "Finding F1" });
    const trigger = within(f1).getByRole("button", { name: "Why trust this" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Why trust F1?" });
    expect(within(dialog).getByText(/identical result hash on re-run/)).toBeTruthy();
    expect(await within(dialog).findByText(/result hash 9f86d081884c/)).toBeTruthy();
    expect(within(dialog).getByText(/welch_t: p 0.0007/)).toBeTruthy();
    expect(within(dialog).getByText(/q = 0\.0010/)).toBeTruthy();
    expect(within(dialog).getByText(/rank_biserial = 0\.41/)).toBeTruthy();
    expect(within(dialog).getByText(/n = 4,210/)).toBeTruthy();
    expect(within(dialog).getByText(/representative \(time_window\)/)).toBeTruthy();
    expect(within(dialog).getByText(/2.1% of resolved_at values are null/)).toBeTruthy();
    expect(within(dialog).getByText(/p\(supports claim\) 91%/)).toBeTruthy();
    expect((within(dialog).getByText("Technical details").closest("details") as HTMLDetailsElement).open).toBe(false);
    expect(document.activeElement?.textContent).toBe("Close");
    expect(await axeClean(container)).toEqual([]);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("shows unrecorded checks as unknown, never as passed", async () => {
    mockFetch();
    renderAt(`/w/${WS}/investigate/${RUN}`);
    const f2 = await screen.findByRole("article", { name: "Finding F2" });
    fireEvent.click(within(f2).getByRole("button", { name: "Why trust this" }));
    const dialog = screen.getByRole("dialog", { name: "Why trust F2?" });
    expect(within(dialog).getByText(/Not verified/)).toBeTruthy();
    expect(within(dialog).getAllByLabelText("not reported").length).toBeGreaterThanOrEqual(6);
    expect(within(dialog).queryByLabelText("passed")).toBeNull();
  });

  it("captures accept (finding outcome) and reject (feedback) signals", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/investigate/${RUN}`);
    const f1 = await screen.findByRole("article", { name: "Finding F1" });
    fireEvent.click(within(f1).getByRole("button", { name: "Accept" }));
    expect(await within(f1).findByText(/Accepted — signal recorded for calibration/)).toBeTruthy();
    const [, acceptInit] = calls(fetchMock, "POST", /\/insights\/ins_demo\/outcome$/)[0];
    expect(JSON.parse(String(acceptInit.body))).toEqual({ signal: "accept" });

    fireEvent.click(within(f1).getByRole("button", { name: "Reject" }));
    fireEvent.change(within(f1).getByLabelText(/Why is F1 wrong/), { target: { value: "Network was reorganised in August." } });
    fireEvent.click(within(f1).getByRole("button", { name: "Reject finding" }));
    expect(await within(f1).findByText(/Replanned to plan v2/)).toBeTruthy();
    const [, rejectInit] = calls(fetchMock, "POST", /\/feedback$/)[0];
    expect(JSON.parse(String(rejectInit.body))).toEqual({
      text: "Network was reorganised in August.", kind: "reject_finding", target_type: "insight", target_id: "ins_demo" });
  });

  it("redirects by chat and shows how the supervisor read the message", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/investigate/${RUN}`);
    fireEvent.change(await screen.findByLabelText("Message"), { target: { value: "Focus on the Network group" } });
    fireEvent.change(screen.getByLabelText("What would you like to do?"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/Focus on the Network group\./)).toBeTruthy();
    const [, init] = calls(fetchMock, "POST", /\/feedback$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ text: "Focus on the Network group", kind: null, target_type: null, target_id: null });
    // who classified it (the decision model) is a tooltip, not the default text
    expect(screen.getByText("redirect", { selector: "strong" }).getAttribute("title")).toMatch(/jev:typesafe\/jev-1/);
  });

  it("renders the rung meter when the server reports rungs", async () => {
    mockFetch((method, path) => (method === "GET" && path.endsWith(`/analysis/${RUN}/console`)
      ? { status: 200, body: { messages: [], tool_calls: [], model_calls: [], queries: [], cost: { usd: 0.04, tokens: 18250, model_calls: 3, jev_calls: 1,
        failed_calls: 0, by_rung: { L0: { calls: 4, cost_usd: 0, tokens_saved: 6000 }, L5: { calls: 2, cost_usd: 0.04 } } } } }
      : undefined));
    renderAt(`/w/${WS}/investigate/${RUN}`);
    const rungs = await screen.findByRole("list", { name: "Spend by rung" });
    expect(rungs.textContent).toMatch(/L0.*4 calls.*6,000 avoided/);
    expect(rungs.textContent).toMatch(/L5.*2 calls.*\$0\.0400/);
  });

  it("uses trust facts from the REV record", () => {
    const facts = trustFacts({ verification: { evaluate: [{ check: "second_method", passed: false, detail: "failed: timeout" }] }, caveats: [],
      population_size: 0 } as never, null, null);
    expect(facts.find((f) => f.id === "second_method")).toMatchObject({ state: "fail", value: "does not agree" });
    expect(facts.find((f) => f.id === "reproducible_rerun")).toMatchObject({ state: "unknown", value: "not reported" });
  });
});

// ------------------------------------------------------------------------------------ P4-U06 operate
describe("approval inbox", () => {
  it("diffs the payload against the last approved proposal and flags a moved policy", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}/operate/approvals`);
    const diff = await screen.findByRole("table", { name: "Payload changes" });
    const text = diff.textContent ?? "";
    expect(text).toMatch(/dashboards\[key=p1_resolution\]\.title/);
    expect(text).toMatch(/P1 resolution \(weekly\)/);
    expect(text).toMatch(/charts\[key=breaches\].*added/);
    expect(screen.getByText(/Compared with the last approved proposal/)).toBeTruthy();
    expect(screen.getByText("(current v2)")).toBeTruthy();
    expect(screen.getByText(/escalates risk only; never grants/)).toBeTruthy();
    expect(await axeClean(container)).toEqual([]);
  });

  it("lists a first proposal's contents when there is nothing to compare", () => {
    const a = { payload: { dashboards: [{ key: "d" }], destination: "superset" } } as unknown as Approval;
    render(<PayloadDiff approval={a} previous={null} />);
    expect(screen.getByText(/Nothing was approved before/)).toBeTruthy();
    expect(screen.getByText("[1 items]")).toBeTruthy();
  });

  it("diffs keyed arrays by key and plain values by position", () => {
    expect(diffJson({ a: [{ key: "x", v: 1 }, { key: "y" }] }, { a: [{ key: "x", v: 2 }], b: true })).toEqual([
      { path: "a[key=x].v", kind: "changed", before: 1, after: 2 },
      { path: "a[key=y]", kind: "removed", before: { key: "y" } },
      { path: "b", kind: "added", after: true },
    ]);
    expect(diffJson([1, 2], [1, 3])).toEqual([{ path: "[1]", kind: "changed", before: 2, after: 3 }]);
  });
});

describe("alert triage explanation", () => {
  const base: Alert = { id: "a", workspace_id: WS, monitor_id: null, severity: "critical", title: "t", message: "MTTR 14h vs median 10h",
    data: { severity: "warning", triage: { p_material: 0.87, model: "typesafe" } }, dedupe_key: "k", status: "open", investigation_run_id: null,
    acknowledged_by: null, created_at: "", resolved_at: null };

  it("states the rule, the decision model's probability and that it only escalates", () => {
    const e = explainTriage(base);
    expect(e.escalated).toBe(true);
    expect(e.lines).toEqual([
      "Rule: MTTR 14h vs median 10h (rule severity: warning).",
      "The decision model judged it material (87% likely) and raised the severity from warning to critical.",
      "The decision model can only raise severity; it never lowers it or closes an alert.",
    ]);
    expect(explainTriage({ ...base, severity: "warning", data: {} }).lines[1]).toBe("The decision model did not run; the severity is the rule's.");
  });
});

describe("spend by rung and model", () => {
  const savings = (extra: Partial<TokenSavings>): TokenSavings => ({ days: 30, totals: { calls: 1, tokens_used: 10, tokens_saved: 5, cost_usd: 0.01,
    cache_hits: 0, deterministic_skips: 0, refused: 0, saved_share: 0.33 }, by_purpose: {}, ...extra });

  it("says the rung breakdown is unknown until the server reports it, then renders it", async () => {
    session.set("tok", USER);
    mockFetch((m, p) => (m === "GET" && p.includes("token-savings") ? { status: 200, body: savings({}) } : undefined));
    const first = render(<TokenSavingsView />);
    expect(await screen.findByText("Spend by rung not reported")).toBeTruthy();
    first.unmount();
    vi.restoreAllMocks();
    mockFetch((m, p) => (m === "GET" && p.includes("token-savings")
      ? { status: 200, body: savings({ by_rung: { L2: { calls: 0, tokens_saved: 4200, cost_usd: 0 } }, by_model: { "openrouter/auto": { calls: 3, cost_usd: 0.01 } } }) }
      : undefined));
    render(<TokenSavingsView />);
    expect(await screen.findByText("L2")).toBeTruthy();
    expect(screen.getByText("4,200")).toBeTruthy();
    expect(screen.getByText("openrouter/auto")).toBeTruthy();
  });

  it("shows prompt-cache tokens and reused context when the server reports them", async () => {
    session.set("tok", USER);
    const base = savings({});
    mockFetch((m, p) => (m === "GET" && p.includes("token-savings") ? { status: 200, body: savings({
      totals: { ...base.totals, input_tokens: 1000, cached_input_tokens: 800, cached_input_share: 0.8, cached_input_saved_usd: 2.16 },
      context_cache: { shared: true, by_kind: { compiled: { sql_generation: { hits: 2, misses: 1, chars_reused: 17974 } },
        retrieval: { follow_up_generation: { hits: 1, misses: 1, chars_reused: 2 } } } },
    }) } : undefined));
    render(<TokenSavingsView />);
    expect(await screen.findByText("Prompt-cache tokens")).toBeTruthy();
    expect(screen.getByText("800")).toBeTruthy();
    expect(screen.getByText(/80.0% of input served from the provider's cache, saving \$2.16/)).toBeTruthy();
    expect(screen.getByText("Context reused")).toBeTruthy();
    expect(screen.getByText(/shared across workers/)).toBeTruthy();
  });
});
