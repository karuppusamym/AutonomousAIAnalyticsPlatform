import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session, streamAskTurn, type AskThread } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import {
  REFUSAL_VIEWS, canPromote, decisionLine, groupThreads, promotionText, provenancePills, refusalText, refusalView, stalenessPill,
  turnSuggestions,
} from "../lib/ask";
import { setExpertView } from "../lib/expert";
import { askTurn, mockBackend, rulesTurn, RUN, THREAD_NEW, THREAD_OLD, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const r = mockBackend(method, String(input), typeof init?.body === "string" ? init.body : null);
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

async function ask(question: string) {
  fireEvent.change(await screen.findByLabelText("Question"), { target: { value: question } });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
}

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

it("labels governed calculations and prevents promoting changed evidence", () => {
  const turn = askTurn("metric", "revenue", { answered_by: "semantic", provenance: { governance: "governed" },
    evidence_status: { state: "changed", reasons: ["Metric definition changed"] } });
  expect(provenancePills(turn).some((p) => p.label === "Approved metric calculation" && p.tone === "warning")).toBe(true);
  expect(canPromote(turn)).toBe(false);
});

it("refreshes saved SQL into a new answer and keeps the original", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input).endsWith("/rerun")) return new Response(JSON.stringify(askTurn("refreshed", "Revenue", { seq: 2 })),
      { headers: { "Content-Type": "application/json" } });
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
  renderAt(`/w/${WS}/ask`);
  await ask("How many P1 incidents per assignment group?");
  const first = await screen.findByRole("article", { name: "Question 1" });
  expect(within(first).getByText("Why these numbers?")).toBeTruthy();
  fireEvent.click(within(first).getByRole("button", { name: "Refresh saved calculation" }));
  await screen.findByRole("article", { name: "Question 2" });
  expect(screen.getByRole("article", { name: "Question 1" })).toBeTruthy();
  expect(calls(fetchMock, "POST", /\/rerun$/)).toHaveLength(1);
  expect(JSON.parse(String(calls(fetchMock, "POST", /\/rerun$/)[0][1].body))).toEqual({});
});

it("requests approval before activating a saved calculation schedule", async () => {
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    if (String(input).endsWith("/schedule")) {
      const body = JSON.parse(String(init?.body));
      return new Response(JSON.stringify(body.approval_id ? { status: "created", id: "sch" }
        : { status: "approval_required", approval_id: "approval", expires_at: "2026-09-29T12:00:00Z" }),
      { headers: { "Content-Type": "application/json" } });
    }
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
  renderAt(`/w/${WS}/ask`);
  await ask("How many P1 incidents per assignment group?");
  fireEvent.click(await screen.findByRole("button", { name: "Schedule this calculation" }));
  fireEvent.click(screen.getByRole("button", { name: "Request schedule approval" }));
  fireEvent.click(await screen.findByRole("button", { name: "Activate approved schedule" }));
  await screen.findByText(/Calculation scheduled/);
  const posts = calls(fetchMock, "POST", /\/schedule$/);
  expect(posts).toHaveLength(2);
  expect(JSON.parse(String(posts[1][1].body)).approval_id).toBe("approval");
});

// ------------------------------------------------------------------------------------ helpers
describe("Ask helpers", () => {
  const thread = (id: string, updated: string): AskThread => ({ id, workspace_id: WS, user_id: "u", title: id, archived: false, created_at: updated, updated_at: updated });

  it("groups threads by recency, newest first, dropping empty groups", () => {
    const now = new Date("2026-09-25T12:00:00Z");
    const g = groupThreads([thread("old", "2026-08-01T00:00:00Z"), thread("today", "2026-09-25T10:00:00Z"),
      thread("week", "2026-09-22T10:00:00Z"), thread("today2", "2026-09-25T11:00:00Z")], now);
    expect(g.map((x) => [x.label, x.items.map((t) => t.id)])).toEqual([
      ["Today", ["today2", "today"]], ["Previous 7 days", ["week"]], ["Earlier", ["old"]]]);
    expect(groupThreads([], now)).toEqual([]);
  });

  it("names who answered, the gateway and the tables; staleness has its own tone", () => {
    const pills = provenancePills(askTurn("t", "q"));
    expect(pills.map((p) => p.label)).toEqual(["Ad hoc analysis", "Generated SQL · openrouter/auto", "Validated by the query gateway", "Incidents (stg_sn.incident)"]);
    const reg = provenancePills(askTurn("t", "q", { answered_by: "registry", model: null, provenance: { verified_query: { id: "vq", name: "p1_by_group" } } }));
    expect(reg[1]).toMatchObject({ tone: "success", label: "Verified query: p1_by_group" });
    expect(stalenessPill({ state: "stale", label: "Data as of 9 days ago", data_as_of: null }).tone).toBe("danger");
    expect(stalenessPill(undefined).label).toBe("Data freshness unknown");
  });

  it("has one refusal state per kind, each with a remedy action; unknown kinds read as failures", () => {
    for (const kind of ["needs_input", "clarify", "sql_rejected", "policy_denied", "budget_exceeded", "no_model", "no_scope", "timeout", "unavailable", "failed",
      "mode_off", "no_api_key", "provider_cooldown", "policy_blocked", "residency_blocked", "approval_required", "model_budget", "cap_reached",
      "context_over_budget", "invalid_output"]) {
      expect(REFUSAL_VIEWS[kind]?.actionLabel).toBeTruthy();
    }
    expect(refusalView({ kind: "residency_blocked", title: "", message: "", remedy: "", details: {} }).action).toBe("access");
    expect(refusalView({ kind: "policy_denied", title: "", message: "", remedy: "", details: {} }).state).toBe("not-entitled");
    expect(refusalView({ kind: "teleport", title: "", message: "", remedy: "", details: {} })).toBe(REFUSAL_VIEWS.failed);
  });

  it("marks a rules answer as built from the catalog and offers its follow-ups", () => {
    const turn = rulesTurn("distribution of incident");
    expect(provenancePills(turn)[1]).toMatchObject({ tone: "success", label: "Built from the catalog · no model" });
    expect(turnSuggestions(turn)).toEqual(["distribution of incident by contact channel", "distribution of incident by priority"]);
    const clarify = askTurn("c", "count of incidents by group", { status: "clarify", result: null, provenance: {},
      refusal: { kind: "clarify", title: "", message: "", remedy: "", details: { suggestions: ["count of incident by assignment group"] } } });
    expect(turnSuggestions(clarify)).toEqual(["count of incident by assignment group"]);
    expect(turnSuggestions(askTurn("t", "q"))).toEqual([]);
  });

  it("describes promotions and decisions in words", () => {
    expect(promotionText({ target: "dashboard", id: "a", status: "approval_required" })).toMatch(/Waiting for approval/);
    expect(promotionText({ target: "metric", id: "m", status: "proposed", name: "p1" })).toMatch(/an approver approves it/);
    expect(canPromote(askTurn("t", "q", { status: "clarify", result: null }))).toBe(false);
    expect(decisionLine({ id: "d", purpose: "ask_route", authority: "route", backend: "rules", model: null, answer: "verified_query",
      proposal: null, probabilities: {}, confidence: null, fallback_reason: null, attempts: [], enforced: [], subject: null, latency_ms: 0,
      created_at: "" })).toBe("ask_route: verified_query — decided by rule");
  });

  it("streams stages then the persisted turn over a POST", async () => {
    mockFetch();
    const stages: string[] = [];
    const turn = await streamAskTurn(THREAD_NEW, "How many P1 incidents per assignment group?", undefined, { onStage: (s) => stages.push(s.key) });
    expect(stages).toEqual(["scope", "registry", "route", "context", "generate", "execute", "done"]);
    expect(turn.status).toBe("answered");
  });
});

// ------------------------------------------------------------------------------------ page
describe("Ask page (P4-U02)", () => {
  it("asks in a new thread: streamed stages, answer card with chart, table and pills, inspector tabs", async () => {
    const fetchMock = mockFetch();
    const { container } = renderAt(`/w/${WS}/ask`);
    await ask("How many P1 incidents per assignment group?");
    const answer = await screen.findByRole("article", { name: "Question 1" });
    expect(within(answer).getByText("P1 incidents by assignment group.")).toBeTruthy();
    const pills = within(answer).getByRole("list", { name: "Provenance" });
    expect(within(pills).getByText("Validated by the query gateway")).toBeTruthy();
    expect(within(pills).getByText("Data as of 3 hours ago")).toBeTruthy();
    expect(within(answer).getAllByRole("table").length).toBeGreaterThan(0);
    const [, init] = calls(fetchMock, "POST", /\/ask\/threads\/ask_new\/turns$/)[0];
    expect((init.headers as Record<string, string>).Accept).toBe("text/event-stream");

    const inspector = screen.getByRole("complementary", { name: "Answer inspector" });
    fireEvent.click(within(inspector).getByRole("tab", { name: "Decision" }));
    expect(await within(inspector).findByText(/ask_route: generate — decided by rule/)).toBeTruthy();
    expect(within(inspector).getByText(/clarify_needed: answer — decided by jev \(typesafe\/jev-1\)/)).toBeTruthy();
    fireEvent.click(within(inspector).getByRole("tab", { name: "Evidence" }));
    expect(within(inspector).getByText("P1 = priority 1 (critical)")).toBeTruthy();
    fireEvent.click(within(inspector).getByRole("tab", { name: "SQL" }));
    expect(within(inspector).getByText(/LIMIT 5001/)).toBeTruthy();
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });

  it("shows the clarify refusal with its remedy, and nothing ran", async () => {
    mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("what about it?");
    const turn = await screen.findByRole("article", { name: "Question 1" });
    expect(within(turn).getByText("The question needs more detail")).toBeTruthy();
    expect(within(turn).getByText(/Say what to measure/)).toBeTruthy();
    fireEvent.click(within(turn).getByRole("button", { name: "Rephrase the question" }));
    expect((screen.getByLabelText("Question") as HTMLTextAreaElement).value).toBe("what about it?");
    expect(within(turn).queryByRole("region", { name: "Promote this answer" })).toBeNull();
  });

  it("answers a distribution by the rules and asks a follow-up grouping with one click", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("distribution of incident");
    const answer = await screen.findByRole("article", { name: "Question 1" });
    expect(within(answer).getByText("Built from the catalog · no model")).toBeTruthy();
    expect(within(answer).getByText(/SQL · built from the catalog/)).toBeTruthy();
    const follow = within(answer).getByRole("region", { name: "Follow-up questions" });
    fireEvent.click(within(follow).getByRole("button", { name: "distribution of incident by contact channel" }));
    await waitFor(() => expect(screen.getAllByRole("article", { name: /^Question / })).toHaveLength(2));
    const second = screen.getAllByRole("article", { name: /^Question / })[1];
    expect(within(second).getAllByText(/by contact channel/).length).toBeGreaterThan(0);
    const posts = calls(fetchMock, "POST", /\/ask\/threads\/ask_new\/turns$/);
    expect(JSON.parse(String(posts[1][1].body)).question).toBe("distribution of incident by contact channel");
  });

  it("says which key to set and to restart when the API has no provider key", async () => {
    mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("Which configuration items had incidents in two consecutive weeks?");
    const turn = await screen.findByRole("article", { name: "Question 1" });
    expect(turn.querySelector("[data-refusal=no_api_key]")).toBeTruthy();
    expect(within(turn).getByText("No model provider key is set for the API")).toBeTruthy();
    expect(within(turn).getByText(/Set OPENROUTER_API_KEY for the api and worker containers/)).toBeTruthy();
    expect(within(turn).getByRole("button", { name: "Try again after the restart" })).toBeTruthy();
  });

  it("tells an analyst in the business view to ask the administrator, without deployment steps (rehearsal friction)", async () => {
    const analyst = { ...USER, id: "usr_analyst", email: "analyst@analystos.local", name: "Ann Analyst", is_admin: false };
    session.set("mock-token", analyst);
    setExpertView(false);
    const fetchMock = mockFetch();
    fetchMock.mockImplementation(async (input, init) => {
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && /\/auth\/me$/.test(String(input))) return new Response(JSON.stringify(analyst), { status: 200, headers: { "Content-Type": "application/json" } });
      const r = mockBackend(method, String(input), typeof init?.body === "string" ? init.body : null);
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    renderAt(`/w/${WS}/ask`);
    await ask("Which configuration items had incidents in two consecutive weeks?");
    const turn = await screen.findByRole("article", { name: "Question 1" });
    expect(turn.querySelector("[data-refusal=no_api_key]")).toBeTruthy();
    expect(within(turn).getByText("This question needs an AI model")).toBeTruthy();
    expect(within(turn).getByText("This question needs an AI model, which isn't turned on for this workspace. Ask your administrator.")).toBeTruthy();
    expect(turn.textContent).not.toMatch(/OPENROUTER_API_KEY|docker compose|environment variable/);
    expect(within(turn).queryByRole("button", { name: "Try again after the restart" })).toBeNull();
    // The expert view keeps the technical detail for whoever runs the deployment.
    act(() => setExpertView(true));
    expect(await within(turn).findByText("No model provider key is set for the API")).toBeTruthy();
    expect(within(turn).getByText(/Set OPENROUTER_API_KEY for the api and worker containers/)).toBeTruthy();
    setExpertView(false);
  });

  it("refusalText keeps the technical words for administrators and the expert view", () => {
    const r = { kind: "no_api_key", title: "No model provider key is set for the API", message: "no API key (set OPENROUTER_API_KEY)",
      remedy: "Set OPENROUTER_API_KEY …", details: {} };
    expect(refusalText(r, false)).toMatchObject({ title: "This question needs an AI model", business: true });
    expect(refusalText(r, true)).toMatchObject({ title: "No model provider key is set for the API", business: false });
    expect(refusalText({ ...r, kind: "clarify" }, false)).toMatchObject({ title: r.title, business: false });
  });

  it("shows an answer's notes, such as an incomplete quarter", async () => {
    mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("What share of incidents met SLA for each assignment group in Q3 2026?");
    const answer = await screen.findByRole("article", { name: "Question 1" });
    const notes = within(answer).getByRole("list", { name: "Notes on this answer" });
    expect(within(notes).getByText(/Q3 2026 is incomplete in this data: .* none in September 2026/)).toBeTruthy();
  });

  it("promotes to a monitor through the form, then investigates why", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("How many P1 incidents per assignment group?");
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    fireEvent.click(within(promote).getByRole("button", { name: "Monitor this" }));
    const form = within(promote).getByRole("form", { name: "New monitor" });
    fireEvent.change(within(form).getByLabelText("Watch for"), { target: { value: "metric_threshold" } });
    fireEvent.change(within(form).getByLabelText("Threshold"), { target: { value: "150" } });
    fireEvent.click(within(form).getByRole("button", { name: "Create monitor" }));
    expect(await within(promote).findByText(/Monitor "P1 incidents per assignment group" created/)).toBeTruthy();
    const [, init] = calls(fetchMock, "POST", /\/ask\/turns\/askt_1\/promote$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ target: "monitor", kind: "metric_threshold", op: ">", value: 150, grain: "week" });

    fireEvent.click(within(promote).getByRole("button", { name: "Add to dashboard" }));
    const where = within(promote).getByRole("form", { name: "Add to dashboard" });
    fireEvent.click(within(where).getByLabelText("Straight to the BI tool (needs approval)"));
    fireEvent.click(within(where).getByRole("button", { name: "Request approval" }));
    expect(await within(promote).findByText(/Waiting for approval/)).toBeTruthy();
    fireEvent.click(within(promote).getByRole("button", { name: "Investigate why" }));
    const investigation = within(promote).getByRole("form", { name: "Plan an investigation" });
    expect(calls(fetchMock, "POST", /\/ask\/turns\/askt_1\/promote$/).map(([, init]) => JSON.parse(String(init.body)).target)).not.toContain("investigate");
    expect(within(investigation).getByText(/not limited to those rows/)).toBeTruthy();
    fireEvent.change(within(investigation).getByLabelText("What should the investigation explain?"), { target: { value: "Why did P1 incidents rise last month?" } });
    fireEvent.click(within(investigation).getByRole("button", { name: "Start investigation" }));
    expect(await screen.findByRole("heading", { level: 1, name: /Why are P1 resolution times rising/ })).toBeTruthy();
    const last = calls(fetchMock, "POST", /\/ask\/turns\/askt_1\/promote$/).at(-1)!;
    expect(JSON.parse(String(last[1].body))).toEqual({ target: "investigate", objective: "Why did P1 incidents rise last month?" });
    await waitFor(() => expect(calls(fetchMock, "GET", new RegExp(`/analysis/${RUN}$`)).length).toBeGreaterThan(0));
  });

  it("lets the user cancel or refine an investigation without starting work", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/ask`);
    await ask("How many P1 incidents per assignment group?");
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    fireEvent.click(within(promote).getByRole("button", { name: "Investigate why" }));
    const form = within(promote).getByRole("form", { name: "Plan an investigation" });
    const objective = within(form).getByLabelText("What should the investigation explain?") as HTMLTextAreaElement;
    expect(objective.value).toBe("How many P1 incidents per assignment group?");
    fireEvent.change(objective, { target: { value: "    " } });
    expect((within(form).getByRole("button", { name: "Start investigation" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(objective, { target: { value: "Why did P1 incidents rise in September?" } });
    fireEvent.click(within(form).getByRole("button", { name: "Cancel" }));
    expect(within(promote).queryByRole("form", { name: "Plan an investigation" })).toBeNull();
    expect(calls(fetchMock, "POST", /\/ask\/turns\/askt_1\/promote$/)).toHaveLength(0);
    fireEvent.click(within(promote).getByRole("button", { name: "Investigate why" }));
    expect((within(promote).getByLabelText("What should the investigation explain?") as HTMLTextAreaElement).value).toBe("Why did P1 incidents rise in September?");
  });

  it("lists, searches and reopens threads", async () => {
    const fetchMock = mockFetch();
    renderAt(`/w/${WS}/ask`);
    const threads = await screen.findByRole("navigation", { name: "Threads" });
    fireEvent.click(await within(threads).findByRole("button", { name: /Weekly P1 volume/ }));
    expect((await screen.findAllByText("How many P1 incidents per week?")).length).toBeGreaterThan(0);
    expect(calls(fetchMock, "GET", new RegExp(`/ask/threads/${THREAD_OLD}$`))).toHaveLength(1);
    fireEvent.change(within(threads).getByLabelText("Search threads"), { target: { value: "volume" } });
    await waitFor(() => expect(calls(fetchMock, "GET", /\/ask\/threads\?q=volume$/)).toHaveLength(1));
  });

  it("explains pasted SQL with the source plan and runs nothing", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => new Response(JSON.stringify(String(input).endsWith("/query/explain") ? {
      statement: "SELECT", summary: "Counts incidents by priority.", tables: ["stg.incident"], gateway: { accepted: true },
      plan: { available: true, node: "Aggregate", estimated_rows: 5, total_cost: 12.5, nodes: ["Aggregate", "Seq Scan"], relations: ["stg.incident"] },
    } : []), { status: 200, headers: { "Content-Type": "application/json" } }));
    renderAt(`/w/${WS}/ask`);
    fireEvent.change(await screen.findByLabelText("SQL (read-only)"), { target: { value: "SELECT priority, COUNT(*) FROM stg.incident GROUP BY 1" } });
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(/Source plan/)).toBeTruthy();
    expect(screen.getByText(/Aggregate → Seq Scan/)).toBeTruthy();
  });
});
