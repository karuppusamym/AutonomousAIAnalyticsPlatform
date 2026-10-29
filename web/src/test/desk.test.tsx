import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session, type DeskRequest, type User, type ValueReport } from "../api";
import { slaText } from "../pages/Desk";
import { fmtDuration } from "../pages/Value";
import { SCREEN_BUDGET, SCREENS, to } from "../routes";
import { bodyOf, calls, mockFetch, renderAt, type Handler } from "./harness";
import { resetMockState, USER, WORKSPACE, WS } from "./mockBackend";

const T = "2026-10-27T09:00:00Z";
const VIEWER: User = { ...USER, id: "usr_viewer", email: "viewer@x", name: "Vera Viewer", is_admin: false };
const MEMBERS = [...WORKSPACE.members, { user_id: VIEWER.id, role: "viewer", email: VIEWER.email, name: VIEWER.name },
  { user_id: "usr_ana", role: "analyst", email: "ana@x", name: "Ana Analyst" }];

function req(over: Partial<DeskRequest> = {}): DeskRequest {
  return {
    id: "rq_1", workspace_id: WS, requester_id: VIEWER.id, channel: "ui", text: "How many incidents were opened last week?",
    normalized_intent: {}, class: "R2", state: "auto_answered", priority: "normal", sla_due_at: null, sla_breached_at: null,
    sla_breached: false, assignee_id: null, conversation_id: "ask_1", run_ids: [],
    triage: { class: "R2", confidence: 0.85, reason: "asks for a count, ranking or aggregate (ad hoc)", answered_by: "rules", flags: [], missing: [] },
    answer: { via: "auto", kind: "ask_turn", turn_id: "askt_1", thread_id: "ask_1", answered_by: "model", governance: "ad_hoc", explanation: "412 incidents." },
    resolution: null, human_minutes: 0, cost_usd: 0.0012, duplicate_of: null, followers: [], revision: 3, created_at: T, updated_at: T,
    first_answer_at: T, closed_at: null,
    timeline: [{ seq: 1, type: "created", from_state: null, to_state: "new", actor: `user:${VIEWER.id}`, payload: {}, created_at: T },
      { seq: 2, type: "triaged", from_state: "new", to_state: "triaged", actor: "desk:rules", payload: {}, created_at: T }],
    ...over,
  };
}

/** The desk endpoints over a tiny in-memory store; everything else falls through to the shared mock backend. */
function desk(role: string, store: Record<string, DeskRequest>, me: User = USER): Handler {
  return (method, path, body, url) => {
    if (method === "GET" && path === "/api/auth/me") return { status: 200, body: me };
    if (method === "GET" && path === `/api/workspaces/${WS}`) return { status: 200, body: { ...WORKSPACE, role, members: MEMBERS } };
    if (method === "GET" && path === "/api/requests") {
      const view = url.searchParams.get("view");
      const all = Object.values(store);
      const rows = view === "mine" ? all.filter((r) => r.requester_id === VIEWER.id)
        : view === "queue" ? all.filter((r) => ["escalated", "auto_with_review", "reopened"].includes(r.state)) : all;
      return { status: 200, body: rows };
    }
    if (method === "POST" && path === "/api/requests") {
      const b = JSON.parse(body ?? "{}") as { text: string; priority: DeskRequest["priority"] };
      store.rq_new = req({ id: "rq_new", text: b.text, priority: b.priority, state: "auto_answered" });
      return { status: 201, body: { request: store.rq_new, conversation_id: "ask_1" } };
    }
    const one = /^\/api\/requests\/([^/]+)(\/actions)?$/.exec(path);
    if (one && store[one[1]]) {
      const r = store[one[1]];
      if (method === "GET" && !one[2]) return { status: 200, body: r };
      if (method === "POST" && one[2]) {
        const a = JSON.parse(body ?? "{}") as { type: string; payload: Record<string, unknown> };
        const next = a.type === "accept" ? { state: "accepted" as const, resolution: "auto" }
          : a.type === "answer" ? { state: "answered" as const, answer: { via: "human" as const, text: String(a.payload.text ?? "") } }
            : a.type === "assign" ? { assignee_id: String(a.payload.assignee_id) } : {};
        store[r.id] = { ...r, ...next };
        return { status: 200, body: store[r.id] };
      }
    }
    return null;
  };
}

beforeEach(() => { resetMockState(); window.localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

describe("route manifest", () => {
  it("adds the Desk under Work and Value under Overview within the screen budget", () => {
    expect(SCREENS.length).toBeLessThanOrEqual(SCREEN_BUDGET);
    expect(SCREENS.find((s) => s.id === "desk")?.area).toBe("work");
    expect(SCREENS.find((s) => s.id === "value")?.area).toBe("overview");
    expect(to.desk("ws_1", "rq_9", { view: "queue" })).toBe("/w/ws_1/work/desk/rq_9?view=queue");
    expect(to.value("ws_1")).toBe("/w/ws_1/value");
  });
});

describe("Request desk (S11-T1)", () => {
  it("lets a requester send a request, see who answered it and accept it", async () => {
    session.set("mock-token", VIEWER);
    const store: Record<string, DeskRequest> = {};
    const f = mockFetch(desk("viewer", store, VIEWER));
    renderAt(`/w/${WS}/work/desk`);
    expect(await screen.findByRole("heading", { name: "Requests" })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Analyst queue" })).toBeNull();  // the queue is for analysts
    fireEvent.change(screen.getByLabelText(/What do you need/), { target: { value: "How many incidents were opened last week?" } });
    fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "high" } });
    fireEvent.click(screen.getByRole("button", { name: "Send to the desk" }));
    await waitFor(() => expect(calls(f, "POST", /\/requests$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "POST", /\/requests$/)[0][1])).toEqual({ workspace_id: WS, text: "How many incidents were opened last week?", priority: "high", channel: "ui" });
    const answer = await screen.findByRole("region", { name: "Answer" });
    expect(within(answer).getByText("Answered automatically")).toBeTruthy();
    expect(within(answer).getByText("ad hoc")).toBeTruthy();
    expect(within(answer).getByRole("link", { name: /Open the answer in Ask/ }).getAttribute("href")).toBe(`/w/${WS}/work/ask?thread=ask_1`);
    expect(screen.queryByRole("region", { name: "Analyst actions" })).toBeNull();
    fireEvent.click(within(screen.getByRole("region", { name: "Your decision" })).getByRole("button", { name: "Accept the answer" }));
    await waitFor(() => expect(calls(f, "POST", /\/requests\/rq_new\/actions$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "POST", /\/requests\/rq_new\/actions$/)[0][1])).toEqual({ type: "accept", payload: {} });
  });

  it("shows an analyst the queue with SLA, the prepared data pack and the draft schedule, and records minutes on an answer", async () => {
    session.set("mock-token", USER);
    const late = new Date(Date.now() - 3 * 3_600_000).toISOString();
    const store: Record<string, DeskRequest> = {
      rq_esc: req({
        id: "rq_esc", text: "Send me the weekly ops pack every Monday", class: "R4", state: "escalated", priority: "high", answer: null,
        sla_due_at: late, assignee_id: "usr_ana",
        data_pack: {
          reason: "Recurring report — a draft is prepared; an analyst completes it and it goes through approval",
          triage: { class: "R4" }, intent: {}, missing: [], refusal: null,
          context_receipts: [{ kind: "asset", id: "a1", asset: "itsm.incident", business_name: "Incidents", excerpt: "One row per incident" }],
          candidate_sql: [{ sql: "SELECT COUNT(*) FROM itsm.incident", from: "ask_turn:askt_9", status: "refused", error: "unknown column" }],
          evidence: [{ kind: "ask_turn", id: "askt_9", thread_id: "ask_9", status: "refused" }],
          proposal: { kind: "schedule", endpoint: "POST /api/workspaces/{workspace_id}/schedules",
            body: { name: "weekly ops pack", kind: "report", cron: "0 9 * * 1" }, summary: "A report every Monday at 09:00 UTC.",
            needs: ["the report's content"], approval: "Creating it needs the analyst role." },
        },
      }),
    };
    const f = mockFetch(desk("owner", store));
    renderAt(`/w/${WS}/work/desk`);
    const queue = await screen.findByRole("complementary", { name: "Requests" });
    expect(await within(queue).findByText(/SLA missed by 3 h/)).toBeTruthy();
    fireEvent.click(within(queue).getByRole("button", { name: /weekly ops pack/ }));
    const pack = await screen.findByRole("region", { name: "Data pack" });
    expect(within(pack).getByText(/Draft schedule/)).toBeTruthy();
    expect(within(pack).getByText(/The desk did not create it/)).toBeTruthy();
    expect(within(pack).getByRole("link", { name: /Open Schedules/ }).getAttribute("href")).toBe(`/w/${WS}/operate/autonomy?view=schedules`);  // S11-T5: schedules live in the Autonomy Center
    expect(within(pack).getByText("itsm.incident")).toBeTruthy();
    expect(within(pack).getByText(/unknown column/)).toBeTruthy();

    const actions = screen.getByRole("region", { name: "Analyst actions" });
    fireEvent.click(within(actions).getByRole("button", { name: "Assign to me" }));
    await waitFor(() => expect(calls(f, "POST", /\/actions$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "POST", /\/actions$/)[0][1])).toEqual({ type: "assign", payload: { assignee_id: USER.id } });
    const send = within(actions).getByRole("button", { name: "Send the answer" }) as HTMLButtonElement;
    fireEvent.change(within(actions).getByLabelText("Your answer"), { target: { value: "Scheduled from next Monday." } });
    expect(send.disabled).toBe(true);  // minutes are required: they are the human cost the value screen weighs
    fireEvent.change(within(actions).getByLabelText("Minutes you spent"), { target: { value: "12" } });
    fireEvent.click(send);
    await waitFor(() => expect(calls(f, "POST", /\/actions$/)).toHaveLength(2));
    expect(bodyOf(calls(f, "POST", /\/actions$/)[1][1])).toEqual({ type: "answer", payload: { text: "Scheduled from next Monday.", human_minutes: 12 } });
    expect(await within(screen.getByRole("region", { name: "Answer" })).findByText("Answered by an analyst")).toBeTruthy();
  });

  it("states the SLA as time left or how late, and nothing once answered", () => {
    const now = Date.parse(T);
    expect(slaText({ sla_due_at: "2026-10-27T12:00:00Z", sla_breached: false, state: "escalated" }, now)).toEqual({ text: "SLA in 3 h", tone: "neutral" });
    expect(slaText({ sla_due_at: "2026-10-27T09:20:00Z", sla_breached: false, state: "escalated" }, now)?.tone).toBe("warning");
    expect(slaText({ sla_due_at: "2026-10-26T09:00:00Z", sla_breached: true, state: "reopened" }, now)?.text).toBe("SLA missed by 1 d");
    expect(slaText({ sla_due_at: "2026-10-26T09:00:00Z", sla_breached: true, state: "answered" }, now)).toBeNull();
  });
});

const REPORT: ValueReport = {
  workspace_id: WS, from: T, to: T, truncated: false, n: 12, duplicates: 1, closed: 10, accepted: 9, auto_resolved: 6,
  auto_resolution_rate: 0.6, accepted_rate: 0.9, tta_p50_seconds: 42, tta_p95_seconds: 5400, human_minutes: 55,
  est_minutes_saved: 140.4, unbaselined: 2, cost_usd: 0.031,
  by_class: {
    R1: { n: 5, duplicates: 0, closed: 5, accepted: 5, auto_resolved: 5, auto_resolution_rate: 1, accepted_rate: 1, tta_p50_seconds: 12,
      tta_p95_seconds: 30, human_minutes: 0, est_minutes_saved: 75, unbaselined: 0, cost_usd: 0.01, baseline_minutes: 15 },
    R9: { n: 2, duplicates: 0, closed: 2, accepted: 2, auto_resolved: 0, auto_resolution_rate: 0, accepted_rate: 1, tta_p50_seconds: 7200,
      tta_p95_seconds: 9000, human_minutes: 40, est_minutes_saved: 0, unbaselined: 2, cost_usd: 0, baseline_minutes: null },
  },
  baselines: { R1: 15 },
  daily: [{ day: "2026-10-26", class: "all", n: 7, duplicates: 0, closed: 6, accepted: 5, auto_resolved: 4, auto_resolution_rate: 0.6667,
    accepted_rate: 0.83, tta_p50_seconds: 40, tta_p95_seconds: 600, human_minutes: 20, est_minutes_saved: 60, unbaselined: 1, cost_usd: 0.02,
    computed_at: T }],
  rollup_computed_at: T,
};

describe("Value dashboard (S11-T7)", () => {
  it("shows the FR-053 numbers, says what is not estimated, exports and lets an owner set a baseline", async () => {
    session.set("mock-token", USER);
    const f = mockFetch((method, path) => {
      if (method === "GET" && path === "/api/value") return { status: 200, body: REPORT };
      if (method === "GET" && path === "/api/value/baselines")
        return { status: 200, body: [{ id: "cb_1", workspace_id: WS, class: "R1", baseline_minutes: 15, source: "practitioner_protocol", measured_at: T, created_at: T }] };
      if (method === "PUT" && path === "/api/value/baselines") return { status: 200, body: { id: "cb_2" } };
      if (method === "POST" && path === "/api/value/rollup") return { status: 200, body: { workspace_id: WS, from: T, to: T, rows: 3 } };
      return null;
    });
    renderAt(`/w/${WS}/value`);
    const head = await screen.findByRole("region", { name: "Headline" });
    expect(within(head).getByText("60%")).toBeTruthy();
    expect(within(head).getByText("6 of 10 closed")).toBeTruthy();
    expect(within(head).getByText("42 s")).toBeTruthy();
    expect(within(head).getByText(/2 accepted requests in classes without a baseline not estimated/)).toBeTruthy();
    const byClass = screen.getByRole("region", { name: "By request class" });
    expect(within(byClass).getByText("R9 · Novel or strategic")).toBeTruthy();
    expect(within(byClass).getAllByText("not estimated").length).toBeGreaterThan(0);
    expect(within(screen.getByRole("region", { name: "Trend" })).getByText(/^rollup /)).toBeTruthy();

    const objectUrl = vi.fn(() => "blob:x");
    Object.assign(URL, { createObjectURL: objectUrl, revokeObjectURL: vi.fn() });
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await waitFor(() => expect(objectUrl).toHaveBeenCalled());
    expect(calls(f, "GET", /\/value\/export\?workspace=/)).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Recompute the trend now" }));
    await waitFor(() => expect(calls(f, "POST", /\/value\/rollup\?workspace=/)).toHaveLength(1));

    const base = screen.getByRole("region", { name: "Class baselines" });
    fireEvent.change(within(base).getByLabelText("Class"), { target: { value: "R9" } });
    fireEvent.change(within(base).getByLabelText("Baseline minutes"), { target: { value: "240" } });
    fireEvent.click(within(base).getByRole("button", { name: "Save baseline" }));
    await waitFor(() => expect(calls(f, "PUT", /\/value\/baselines$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "PUT", /\/value\/baselines$/)[0][1])).toEqual({ workspace_id: WS, class: "R9", baseline_minutes: 240, source: "practitioner_protocol" });
  });

  it("formats durations without turning unknown into zero", () => {
    expect(fmtDuration(null)).toBe("unknown");
    expect(fmtDuration(42)).toBe("42 s");
    expect(fmtDuration(600)).toBe("10 min");
    expect(fmtDuration(9000)).toBe("2.5 h");
    expect(fmtDuration(259_200)).toBe("3.0 d");
  });
});
