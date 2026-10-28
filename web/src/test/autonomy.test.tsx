/**
 * S11-T3 (run view budget meters and plan versions) and S11-T5 (Autonomy Center) over the Goal API v2.
 * The /api/v2 routes are answered here; everything else by the shared mock backend.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { session, type AutonomySummary, type BudgetSnapshot, type GoalRunView, type GoalSummary, type PlanVersionView } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { BudgetMeters, diffPlans, formatBudget, latestBudget } from "../components/Autonomy";
import { mockBackend, RUN, USER, WS } from "./mockBackend";

const BUDGET: BudgetSnapshot = {
  limits: { max_steps: 10, max_cost_usd: 0.5, max_duration_s: 1800, max_parallel: 4, source_slots: { src_1: 2 } },
  used: { steps: 10, cost_usd: 0.1, duration_s: 95 },
  remaining: { max_steps: 0, max_cost_usd: 0.4, max_duration_s: 1705 },
  exhausted: ["max_steps (10 of 10)"],
};
const RUN_VIEW: GoalRunView = {
  run_id: RUN, goal_id: "goal_1", workspace_id: WS, state: "COMPLETED_WITH_LIMITATIONS", control: "run", objective: "Review data quality",
  autonomy_level: 2, plan_version: 2, plan_hash: "abcdef1234567890", playbook: "playbook.dq_review", error: null,
  complexity: { tier: "medium", floor: "medium", rules_tier: "low", requested_tier: null, playbook: "playbook.dq_review",
    playbook_reason: "intent matched playbook.dq_review", limits: {}, reasons: ["masked or sensitive column(s) in scope -> medium"],
    features: { table_count: 1, join_depth: 0, estimated_scan_rows: 20000, ambiguity_flags: [], policy_tier: "medium" },
    clarify: { value: "proceed", backend: "rules", missing_inputs: [] } },
  budget: BUDGET,
  limitations: { outcome: "COMPLETED_WITH_LIMITATIONS", reason: "max_steps (10 of 10)", limits: {}, used: {},
    completed_steps: ["metadata"], not_run: ["finalize"], verified_findings: ["ins_1"], unverified_findings: 2 },
  steps: [], events_url: `/v2/runs/${RUN}/events`, created_at: null, finished_at: null,
};
const PLAN_V1: PlanVersionView = {
  run_id: RUN, version: 1, current: false, plan: {}, plan_hash: "1111111111111111",
  superseded_by: { reason_code: "quality_failure", detail: "duplicate keys", actor: "engine", at: "2026-10-27T10:00:00Z" },
  tasks: [{ key: "profile", status: "COMPLETED", attempts: 1, output_keys: ["profiles"] }, { key: "quality", status: "FAILED", attempts: 1 }],
};
const PLAN_V2: PlanVersionView = {
  run_id: RUN, version: 2, current: true, plan: {}, plan_hash: "2222222222222222",
  tasks: [{ key: "profile", status: "COMPLETED", attempts: 1 }, { key: "quality", status: "COMPLETED", attempts: 2 }, { key: "finalize", status: "SKIPPED", attempts: 0 }],
};
const SUMMARY: AutonomySummary = {
  workspace_id: WS, window_days: 30, runs: { by_state: { COMPLETED: 3, FAILED: 1 }, total: 4 },
  queues: { NEW: 1, PLANNING: 0, READY: 0, RUNNING: 2, WAITING_USER: 1, PAUSED: 0 },
  exceptions: { count: 2, waiting_for_people: 1, runs: [{ run_id: "run_bad", objective: "Explain the variance", state: "FAILED", origin: "goal", reason: "source down", finished_at: null }] },
  budgets: { month_spend_usd: 4.5, month_budget_usd: 50, goal_spend_usd: 1.25, run_cost_budget_usd: 2, max_parallel_steps: 4 },
  schedules: { total: 3, enabled: 2, next_run_at: "2026-10-28T06:00:00Z" }, monitors: { total: 2, enabled: 2 },
  alerts: { raised: 10, by_status: { resolved: 6, open: 4 }, resolved_without_investigation: 5, noise_rate: 0.5 },
};
const GOALS: GoalSummary[] = [{
  goal_id: "goal_1", run_id: RUN, question: "Review data quality of the incident table", mode: "goal_supervised", state: "COMPLETED_WITH_LIMITATIONS",
  tier: "medium", playbook: "playbook.dq_review", plan_version: 2, limits: { max_steps: 10, max_cost_usd: 0.5 },
  used: { steps: 10, cost_usd: 0.1, duration_s: 95, replans: 1 }, limitation: "max_steps (10 of 10)", error: null,
  created_at: "2026-10-27T09:00:00Z", finished_at: null,
}];

let posted: { body: unknown; key: string | null }[] = [];

function install() {
  posted = [];
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
    const p = url.pathname;
    if (p === "/api/v2/autonomy") return reply(SUMMARY);
    if (p === "/api/v2/goals" && (init?.method ?? "GET") === "GET") return reply(GOALS);
    if (p === "/api/v2/goals") {
      posted.push({ body: JSON.parse(String(init?.body)), key: new Headers(init?.headers).get("Idempotency-Key") });
      return reply({ goal_id: "goal_2", run_id: "run_new", state: "NEW", events_url: "/v2/runs/run_new/events", tier: "low", playbook: "playbook.investigate", limits: {} }, 202);
    }
    if (p === `/api/v2/runs/${RUN}`) return reply(RUN_VIEW);
    if (p === `/api/v2/runs/${RUN}/plan`) return reply(url.searchParams.get("version") === "1" ? PLAN_V1 : PLAN_V2);
    const headers: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => { headers[k] = v; });
    const r = mockBackend(init?.method ?? "GET", String(input), (init?.body as string | undefined) ?? null, headers);
    if (p === `/api/workspaces/${WS}/analysis/${RUN}` && r.status === 200) {  // the run was replanned once
      return reply({ ...JSON.parse(r.body), plan_version: 2 });
    }
    return new Response(r.status === 204 ? null : r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function Where() {
  const l = useLocation();
  return <span data-testid="location">{l.pathname}{l.search}</span>;
}

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /><Where /></MemoryRouter></AuthProvider>);
}

beforeEach(() => {
  session.set("mock-token", USER);
  install();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("budget meters (S11-T3)", () => {
  it("formats each counter in its unit and marks what ran out", () => {
    expect(formatBudget(95, "seconds")).toBe("1 min 35 s");
    expect(formatBudget(3 * 1024 ** 3, "bytes")).toBe("3.0 GB");
    expect(formatBudget(0.1234, "usd")).toBe("$0.1234");
    expect(formatBudget(undefined, "int")).toBe("unknown");
    render(<BudgetMeters budget={BUDGET} />);
    const steps = screen.getByTestId("budget-steps");
    expect(within(steps).getByText("used up")).toBeTruthy();
    expect(within(steps).getByRole("meter").getAttribute("aria-valuenow")).toBe("100");
    expect(within(screen.getByTestId("budget-cost_usd")).getByRole("meter").getAttribute("aria-valuenow")).toBe("20");
    expect(screen.queryByTestId("budget-scan_bytes")).toBeNull(); // no limit, no invented meter
    expect(screen.getByText(/Up to 4 steps at once/)).toBeTruthy();
  });

  it("takes the newest run.budget event and diffs plan versions", () => {
    const ev = (id: number, type: string, payload: object) => ({ id, type, payload, actor: null, created_at: null }) as never;
    expect(latestBudget([ev(1, "run.budget", { exhausted: ["a"] }), ev(2, "agent.completed", {}), ev(3, "run.budget", { exhausted: [] })]))
      .toEqual({ exhausted: [] });
    expect(latestBudget([ev(1, "agent.completed", {})])).toBeNull();
    expect(diffPlans(PLAN_V1.tasks, PLAN_V2.tasks)).toEqual({ added: ["finalize"], removed: [], changed: [{ key: "quality", from: "FAILED", to: "COMPLETED" }] });
  });

  it("shows the goal's budget, tier and limitation on the run view, and every plan version", async () => {
    renderAt(`/w/${WS}/work/investigations/${RUN}`);
    const panel = (await screen.findByRole("heading", { name: "Goal budget" })).closest("section")!;
    await within(panel as HTMLElement).findByTestId("budget-steps");
    expect(within(panel as HTMLElement).getByText(/Stopped by a budget:/)).toBeTruthy();
    expect(within(panel as HTMLElement).getByText("medium")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Plan versions \(2\)/ }));
    expect(await screen.findByText("The plan the run follows now.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "v1" }));
    expect(await screen.findByText(/was replaced because a data-quality check failed/)).toBeTruthy();
    expect((await screen.findByTestId("plan-diff")).textContent).toMatch(/1 step changed, 1 added \(finalize\)/);
    expect(screen.getByText("kept: profiles")).toBeTruthy();
  });
});

describe("Autonomy Center (S11-T5)", () => {
  it("is one screen for queues, exceptions, budgets, schedules and alert noise", async () => {
    renderAt(`/w/${WS}/operate/autonomy`);
    expect(await screen.findByRole("heading", { name: "Automations", level: 1 })).toBeTruthy();
    const queues = (await screen.findByRole("heading", { name: "Queues" })).closest("section")!;
    expect(within(queues as HTMLElement).getByText("Waiting for a person")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Explain the variance" }).getAttribute("href")).toBe(`/w/${WS}/work/investigations/run_bad`);
    expect(screen.getByRole("meter", { name: /Model spend this month/ }).getAttribute("aria-valuenow")).toBe("9");
    expect(screen.getByText("5 (50%)")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Schedules" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/operate/autonomy?view=schedules`));
    expect(await screen.findByRole("heading", { name: "Schedules", level: 2 })).toBeTruthy();
  });

  it("keeps the old Schedules and Monitors URLs working", async () => {
    renderAt(`/w/${WS}/operate/monitoring?tab=alerts`);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/operate/autonomy?view=monitors&tab=alerts`));
    expect(await screen.findByRole("heading", { name: "Monitors & alerts", level: 2 })).toBeTruthy();
  });

  it("lists goals and starts one with an Idempotency-Key, then opens its run", async () => {
    renderAt(`/w/${WS}/operate/autonomy?view=goals`);
    const link = await screen.findByRole("link", { name: "Review data quality of the incident table" });
    expect(link.getAttribute("href")).toBe(`/w/${WS}/work/investigations/${RUN}`);
    expect(screen.getByText("Stopped: max_steps (10 of 10)")).toBeTruthy();
    const start = screen.getByRole("button", { name: "Start goal" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true); // a goal needs a real question
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: "Explain the month-end variance in volume" } });
    fireEvent.change(screen.getByLabelText("Max steps (optional)"), { target: { value: "12" } });
    fireEvent.click(start);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work/investigations/run_new`));
    expect(posted).toHaveLength(1);
    expect(posted[0].key).toMatch(/.{8,}/);
    expect(posted[0].body).toMatchObject({ workspace_id: WS, question: "Explain the month-end variance in volume", mode: "goal_supervised",
      requested_outputs: ["investigation"], limits: { max_steps: 12 } });
  });
});
