/**
 * CONV-01..03 in the web client: a draft dashboard from a request (failed tiles listed), a chart added by asking
 * (and a clarifying reply with questions to try), and an Ask answer added to a new or existing draft.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session } from "../api";
import { describeOp } from "../components/DashboardBuilder";
import { answeredByText } from "../components/DashboardDraft";
import { promotionText } from "../lib/ask";
import { setExpertView } from "../lib/expert";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("wording", () => {
  it("says in plain words how a chart was answered and what a question op does", () => {
    expect(answeredByText({ answered_by: "semantic" })).toBe("from an approved metric");
    expect(answeredByText({ answered_by: "model" })).toBe("a new calculation, checked before it ran");
    expect(describeOp({ op: "chart.from_question", question: "resolution time by priority" })).toBe("add a chart: “resolution time by priority”");
    expect(describeOp({ op: "create", origin: { kind: "prompt", prompt: "incident operations" } })).toBe("drafted from “incident operations”");
    expect(promotionText({ target: "draft", id: "a", status: "created", dashboard: "P1 view" })).toMatch(/Started the draft dashboard "P1 view"/);
    expect(promotionText({ target: "draft", id: "a", status: "added", dashboard: "Ops", version: 3 })).toMatch(/now version 3/);
  });
});

async function buildDraft(prompt: string) {
  const form = await screen.findByRole("form", { name: "Build a dashboard from a request" });
  fireEvent.change(within(form).getByLabelText("What should the dashboard show?"), { target: { value: prompt } });
  fireEvent.click(within(form).getByRole("button", { name: "Build a draft" }));
}

describe("draft from a request", () => {
  it("builds a draft, lists the tile it could not build and opens it in the builder", async () => {
    setExpertView(false);
    const f = mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    fireEvent.click(await screen.findByText("Build a dashboard"));
    await buildDraft("Build an incident operations dashboard");
    const result = await screen.findByLabelText("Draft result");
    expect(within(result).getByText(/5 of 6 tiles built/)).toBeTruthy();
    expect(within(result).getByText(/SLA breach rate by priority — metric sla_breach_rate reads data you may not see/)).toBeTruthy();
    expect(within(result).queryByText(/Planned from/)).toBeNull();  // business view: no planner detail
    expect(bodyOf(calls(f, "POST", /\/dashboards\/draft$/)[0][1])).toEqual({ prompt: "Build an incident operations dashboard" });
    expect((await screen.findAllByRole("heading", { name: /Incident operations dashboard/ })).length).toBeGreaterThan(0);
    expect(await screen.findByText("priority: any")).toBeTruthy();  // a filter control, not a fixed value
  });

  it("answers a request it cannot ground with a clarifying reply and creates nothing", async () => {
    mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    fireEvent.click(await screen.findByText("Build a dashboard"));
    await buildDraft("a weather dashboard");
    expect(await screen.findByText(/could not match that to data this workspace has reviewed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Build a incident count dashboard" })).toBeTruthy();
  });

  it("shows how tiles were planned in expert view", async () => {
    setExpertView(true);
    mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    fireEvent.click(await screen.findByText("Build a dashboard"));
    await buildDraft("Build an incident operations dashboard");
    expect(await screen.findByText(/Planned from approved metrics; order by rules/)).toBeTruthy();
  });
});

describe("refining a draft by asking", () => {
  it("adds a chart from a question, and a question it cannot ground gets questions to try", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    fireEvent.click(await screen.findByText("Build a dashboard"));
    await buildDraft("Build an incident operations dashboard");
    const say = await screen.findByLabelText("Describe a change");
    fireEvent.change(say, { target: { value: "add weather by city" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    const clarify = await screen.findByRole("status", { name: "Clarifying question" });
    expect(within(clarify).getByText(/no table in your scope has weather data/)).toBeTruthy();
    fireEvent.click(within(clarify).getByRole("button", { name: "resolution time by priority" }));
    expect(await screen.findByText(/Added a bar for “resolution time by priority”, from an approved metric/)).toBeTruthy();
    const sent = calls(f, "POST", /\/messages$/).map(([, init]) => bodyOf(init).text);
    expect(sent).toEqual(["add weather by city", "add resolution time by priority"]);
    await waitFor(() => expect(screen.getByText(/Applied: now version 2/)).toBeTruthy());
  });
});

describe("Ask answer into a draft", () => {
  async function openAddToDashboard() {
    fireEvent.change(await screen.findByLabelText("Question"), { target: { value: "How many P1 incidents per assignment group?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    const promote = await screen.findByRole("region", { name: "Promote this answer" });
    fireEvent.click(within(promote).getByRole("button", { name: "Add to dashboard" }));
    return { promote, form: within(promote).getByRole("form", { name: "Add to dashboard" }) };
  }

  it("starts a new draft with the recommended chart", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/ask`);
    const { promote, form } = await openAddToDashboard();
    fireEvent.change(within(form).getByLabelText("Draft title"), { target: { value: "P1 view" } });
    fireEvent.click(within(form).getByRole("button", { name: "Add the chart" }));
    expect(await within(promote).findByText(/Started the draft dashboard "P1 view" with this chart/)).toBeTruthy();
    expect(within(promote).getByRole("link", { name: "Open the draft" }).getAttribute("href")).toContain("dashboard=art_draft_ask");
    expect(bodyOf(calls(f, "POST", /\/promote$/)[0][1])).toEqual({ target: "draft", title: "P1 view" });
  });

  it("adds it to an existing draft chosen from the list", async () => {
    const f = mockFetch((method, path) => method === "GET" && path === `/api/v2/workspaces/${WS}/dashboards`
      ? { status: 200, body: [{ id: "art_ops", workspace_id: WS, name: "dashboard:ops", title: "Ops", version: 4, status: "draft",
        platform: null, external_url: null, content_hash: "h", updated_at: null }] } : null);
    renderAt(`/w/${WS}/ask`);
    const { promote, form } = await openAddToDashboard();
    fireEvent.click(within(form).getByLabelText("An existing draft"));
    expect(await within(form).findByRole("option", { name: "Ops (v4, draft)" })).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Add the chart" }));
    expect(await within(promote).findByText(/Added the chart to the draft dashboard/)).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /\/promote$/)[0][1])).toEqual({ target: "draft", dashboard_id: "art_ops" });
  });
});
