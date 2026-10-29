/**
 * The light information architecture (spec v4 §15, P7-18, P4-07): one home per concept, the gear's
 * admin screens hidden from analysts and viewers, Start work from the capability registry, the
 * two-state Overview, plain language on the default path, and the policy form.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import axe from "axe-core";
import { session, type Source, type User, type WorkspaceDetail } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { visibleNav } from "../components/Layout";
import { jobKindsFromAvailability } from "../lib/jobKinds";
import { fieldText, fieldValue, setField } from "../lib/policy";
import { autonomyInWords } from "../lib/status";
import { firstRunSteps, nextStep } from "../pages/WorkspaceHome";
import { canSee, CONCEPTS, SCREENS } from "../routes";
import { headersOf, INSIGHT, mockBackend, resetMockState, RUN, USER, WORKSPACE, WS } from "./mockBackend";
import { JOB_KINDS } from "./mockWave2";
import { INSIGHT_VOID_ID } from "./mockWave1";

type Handler = (method: string, path: string, body: string | null) => { status: number; body: unknown } | null;

function mockFetch(override?: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? init.body : null;
    const o = override?.(method, new URL(String(input), "http://x").pathname, body);
    if (o) return new Response(JSON.stringify(o.body), { status: o.status, headers: { "Content-Type": "application/json" } });
    const r = mockBackend(method, String(input), body, headersOf(init));
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function LocationProbe() {
  const l = useLocation();
  return <div data-testid="location">{l.pathname + l.search}</div>;
}

function renderAt(path: string) {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={[path]}>
        <AppRoutes />
        <Routes><Route path="*" element={<LocationProbe />} /></Routes>
      </MemoryRouter>
    </AuthProvider>,
  );
}

const calls = (f: ReturnType<typeof vi.spyOn>, method: string, re: RegExp) =>
  (f.mock.calls as [string, RequestInit | undefined][]).filter(([u, i]) => (i?.method ?? "GET") === method && re.test(String(u)));

const ANALYST: User = { ...USER, id: "usr_analyst", email: "analyst@analystos.local", name: "Ana Analyst", is_admin: false };
const VIEWER: User = { ...USER, id: "usr_viewer", email: "viewer@analystos.local", name: "Vic Viewer", is_admin: false };

/** The workspace as seen by a non-admin member with `role`. */
const asRole = (user: User, role: string): Handler => (method, path) => {
  if (method !== "GET") return null;
  if (path === "/api/auth/me") return { status: 200, body: user };
  if (path === `/api/workspaces/${WS}`) return { status: 200, body: { ...WORKSPACE, role } satisfies WorkspaceDetail };
  if (path === "/api/workspaces") return { status: 200, body: [{ ...WORKSPACE, role }] };
  return null;
};

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  try { window.localStorage.clear(); } catch { /* ignore */ }
});

// ------------------------------------------------------------------------------------ one home per concept
/** Every page and component's source, for the static "editable in one place" checks. */
const SOURCES = import.meta.glob("../{pages,components}/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const filesUsing = (pattern: RegExp) => Object.entries(SOURCES).filter(([, src]) => pattern.test(src)).map(([f]) => f.replace("../", "")).sort();

describe("one home per concept (route manifest)", () => {
  it("gives every concept exactly one home screen", () => {
    const homes = new Map<string, string[]>();
    for (const s of SCREENS) for (const c of s.owns) homes.set(c, [...(homes.get(c) ?? []), s.id]);
    for (const c of CONCEPTS) expect(homes.get(c), `concept ${c}`).toHaveLength(1);
    expect([...homes.keys()].sort()).toEqual([...CONCEPTS].sort());
  });

  it("renders each concept's editor on its home screen only", () => {
    // [the editor, where it is used, the screen that owns the concept]
    const editors: [RegExp, string[], string][] = [
      [/<KpiEditor\b/, ["pages/Catalog.tsx"], "catalog"], // metrics: Data only
      [/<AutonomyPicker\b/, ["pages/Governance.tsx"], "policy"], // autonomy: one place, under Advanced
      [/<AuditTable\b/, ["pages/Governance.tsx"], "policy"], // one audit view
      [/api\.putPolicy\(/, ["pages/Governance.tsx"], "policy"],
      [/<PublishCard\b/, ["pages/Studio.tsx"], "outputs"], // dashboards: an Outputs filter
      [/<GenerateReportForm\b/, ["pages/Studio.tsx"], "outputs"],
      [/<(SettingsEditor|Models|ModelHealthPanel)\b/, ["pages/Admin.tsx"], "platform"], // model configuration: Settings only
      [/<DefinitionsPanel\b/, ["pages/Catalog.tsx"], "catalog"],
      [/<(PreparePanel|BuildPanel)\b/, ["pages/Runs.tsx"], "work"],
      [/<SchedulePins\b/, ["pages/Schedules.tsx"], "autonomy"], // a section of the Autonomy Center (S11-T5)
      [/autonomy_level:/, ["pages/Governance.tsx"], "policy"],
    ];
    for (const [re, files, screenId] of editors) {
      expect(filesUsing(re), String(re)).toEqual(files);
      expect(SCREENS.some((s) => s.id === screenId)).toBe(true);
    }
  });

  it("keeps model configuration in one Settings tab and no dashboards list outside Outputs", () => {
    const admin = SOURCES["../pages/Admin.tsx"];
    expect(admin.match(/\{ id: "models"/g)).toHaveLength(1);
    expect(admin).toMatch(/settings: \{[\s\S]*?\{ id: "models", label: "Models & routing" \}/);
    expect(filesUsing(/aria-label="Dashboards"/)).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------ roles
describe("the gear: admin screens are hidden from analysts and viewers", () => {
  const ADMIN_TITLES = ["Capability registry", "Platform settings", "Usage & cost"];

  it("filters the manifest by role", () => {
    for (const role of ["analyst", "viewer", "editor", "approver"]) {
      const titles = visibleNav({ isAdmin: false, role }, WS).flatMap((g) => g.screens.map((s) => s.title));
      for (const t of [...ADMIN_TITLES, "Members & policy"]) expect(titles, `${role}: ${t}`).not.toContain(t);
    }
    const owner = visibleNav({ isAdmin: false, role: "owner" }, WS).flatMap((g) => g.screens.map((s) => s.title));
    expect(owner).toContain("Members & policy");
    for (const t of ADMIN_TITLES) expect(owner).not.toContain(t);
    const admin = visibleNav({ isAdmin: true, role: "analyst" }, WS).flatMap((g) => g.screens.map((s) => s.title));
    for (const t of [...ADMIN_TITLES, "Members & policy"]) expect(admin).toContain(t);
    expect(SCREENS.filter((s) => s.audience === "admin").every((s) => !canSee(s, { isAdmin: false, role: "owner" }))).toBe(true);
  });

  it.each([["analyst", ANALYST], ["viewer", VIEWER]] as const)("%s: no admin screen in the nav, and a direct URL is not entitled", async (role, user) => {
    session.set("mock-token", user);
    mockFetch(asRole(user, role));
    const { unmount } = renderAt(`/w/${WS}`);
    const nav = screen.getByRole("navigation", { name: "Main" });
    await within(nav).findByRole("link", { name: "Dashboards & reports" });
    for (const t of [...ADMIN_TITLES, "Members & policy"]) expect(within(nav).queryByRole("link", { name: t })).toBeNull();
    expect(within(nav).queryByRole("group", { name: /Settings/ })).toBeNull();
    unmount();
    for (const path of ["/settings/registry", "/settings/platform", "/settings/usage"]) {
      const r = renderAt(path);
      expect(await screen.findByText(/is for platform administrators/)).toBeTruthy();
      expect(document.querySelector("[data-state=not-entitled]")).toBeTruthy();
      r.unmount();
    }
    renderAt(`/w/${WS}/settings/policy`);
    expect(await screen.findByText("Workspace owners manage members and policy")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save policy" })).toBeNull();
  });
});

// ------------------------------------------------------------------------------------ Start work
it("lets an owner review and save combined workspace work modes", async () => {
  resetMockState();
  const f = mockFetch();
  renderAt("/");
  fireEvent.click(await screen.findByRole("button", { name: "Work modes" }));
  const choice = await screen.findByRole("checkbox", { name: /ML and experiments/ });
  fireEvent.click(choice);
  fireEvent.click(screen.getByRole("button", { name: "Review changes" }));
  fireEvent.click(await screen.findByRole("button", { name: "Save work modes" }));
  await waitFor(() => expect(calls(f, "PUT", /\/work-modes$/)).toHaveLength(1));
  const [, init] = calls(f, "PUT", /\/work-modes$/)[0];
  expect(JSON.parse(String(init?.body))).toEqual({ modes: ["analysis", "engineering"] });
  resetMockState();
});

describe("Start work job kinds (from the P4-04 endpoint)", () => {
  it("maps the server's six kinds to actions, keeping every reason and never re-deriving availability", () => {
    const kinds = jobKindsFromAvailability(JOB_KINDS);
    expect(kinds.map((k) => k.label)).toEqual(["Explain", "Compare", "Forecast", "Predict", "Prepare data", "Monitor"]);
    const by = Object.fromEntries(kinds.map((k) => [k.id, k]));
    expect([by.explain.action, by.compare.action, by.forecast.action, by.predict.action, by.prepare.action, by.monitor.action])
      .toEqual(["investigate", "investigate", "ml", "ml", "prepare", "monitor"]);
    expect(by.forecast.enabled).toBe(false);
    expect(by.forecast.reasons).toEqual([expect.objectContaining({ code: "capability_unusable", remediation: expect.stringMatching(/workspace owner enables it/) })]);
    expect(by.explain.uses).toEqual(["playbook.investigate"]);
    expect(by.predict.readinessChecks).toContain("label_availability");
  });

  it("keeps role, no-data and no-executor reasons from the server, each with its remediation", () => {
    const blocked = JOB_KINDS.map((k) => (k.key === "prepare" ? { ...k, available: false, reasons: [
      { code: "role", message: "Prepare data needs the editor role here; you are analyst", remediation: "Ask a workspace owner for the role." },
      { code: "no_data", message: "no selected, ready table is in your scope", remediation: "Add a source, discover it and select its tables (Data > Sources)." },
    ] } : k));
    const prepare = jobKindsFromAvailability(blocked)[4];
    expect(prepare.enabled).toBe(false);
    expect(prepare.reasons.map((r) => r.code)).toEqual(["role", "no_data"]);
    expect(prepare.reason).toMatch(/needs the editor role/);
    // a disabled kind the server gives no reason for still says so rather than looking available
    const silent = jobKindsFromAvailability([{ ...JOB_KINDS[0], available: false, reasons: [] }])[0];
    expect(silent.enabled).toBe(false);
    expect(silent.reason).toMatch(/did not say why/);
  });

  it("shows disabled kinds with their reason and never starts a pretend run; an enabled kind shows the preflight", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    const dialog = await screen.findByRole("dialog", { name: "Start work" });
    const kinds = await within(dialog).findByRole("list", { name: "Job kinds" });
    // the mock server says Forecast's method is turned off here
    const item = within(kinds).getByText("Forecast").closest("li")!;
    expect(within(item).queryByRole("button")).toBeNull();
    expect(item.getAttribute("class")).toMatch(/job-kind-disabled/);
    const why = within(item).getByRole("list", { name: "Why Forecast cannot start" });
    expect(why.textContent).toMatch(/method\.ml\.forecast is turned off in this workspace/);
    expect(why.textContent).toMatch(/A workspace owner enables it/);
    fireEvent.click(item);
    expect(within(dialog).queryByRole("form")).toBeNull();
    expect(calls(f, "POST", /\/analysis$/)).toEqual([]);
    expect(calls(f, "GET", /\/api\/workspaces\/ws_demo\/capabilities$/)).toHaveLength(1);
    fireEvent.click(within(kinds).getByRole("button", { name: /Explain/ }));
    const form = await within(dialog).findByRole("form", { name: "Start explain" });
    expect(within(form).getByRole("heading", { name: "Before it starts" })).toBeTruthy();
    expect(within(form).getByText("ServiceNow")).toBeTruthy();
    expect(within(form).queryByText(/L3\b/)).toBeNull(); // autonomy in words, not codes
    expect(within(form).getByText(/Executes; publish needs approval/)).toBeTruthy();
    // readiness: every check with its own result, no averaged score
    const readiness = await within(form).findByRole("group", { name: "Readiness for explain" });
    expect(within(readiness).getByText("Ready")).toBeTruthy();
    expect(within(readiness).getByText("Advisory checks")).toBeTruthy();
    expect(within(readiness).getByText(/staged 30 hours ago/)).toBeTruthy();
    expect(readiness.textContent).not.toMatch(/score|%/i);
    fireEvent.click(within(form).getByRole("button", { name: "Start investigation" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work/investigations/${RUN}`));
    const [, init] = calls(f, "POST", /\/analysis$/)[0];
    expect(JSON.parse(String(init!.body))).toEqual({ objective: "Why are P1 resolution times rising?", source_ids: ["src_sn"] });
  });

  it("Predict offers published plans, and a new spec opens the ML spec form in Work (no new screen)", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    const kinds = await screen.findByRole("list", { name: "Job kinds" });
    fireEvent.click(within(kinds).getByRole("button", { name: /Predict/ }));
    const form = await screen.findByRole("form", { name: "Start predict" });
    fireEvent.click(within(form).getByRole("button", { name: "Write a new spec" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work?tab=experiments&new=predict`));
    expect(SCREENS.length).toBeLessThanOrEqual(20);
  });

  it("Predict without a published spec still opens its form so the spec can be written (live regression)", async () => {
    const noSpec = { code: "no_ml_spec", message: "Predict needs a published ml_spec definition for this task, and this workspace has none yet",
      remediation: "Publish an ml_spec definition (a reviewed MLSpec)." };
    const kindsNoSpec = JOB_KINDS.map((k) => (k.key === "predict" ? { ...k, available: false, reasons: [noSpec] } : k));
    const mapped = Object.fromEntries(jobKindsFromAvailability(kindsNoSpec).map((k) => [k.id, k]));
    expect(mapped.predict.enabled).toBe(false);
    expect(mapped.predict.specFirst).toBe(true);
    // any other reason (a role, a turned-off mode) keeps it closed
    const withRole = kindsNoSpec.map((k) => (k.key === "predict" ? { ...k, reasons: [noSpec, { code: "role", message: "role", remediation: "" }] } : k));
    expect(jobKindsFromAvailability(withRole)[3].specFirst).toBe(false);

    mockFetch((method, path) => (method === "GET" && path === `/api/workspaces/${WS}/capabilities`
      ? { status: 200, body: { digest: "d", job_kinds: kindsNoSpec } } : null));
    renderAt(`/w/${WS}/work`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    const kinds = await screen.findByRole("list", { name: "Job kinds" });
    fireEvent.click(within(kinds).getByRole("button", { name: /Predict.*Needs a published model spec first/ }));
    const form = await screen.findByRole("form", { name: "Start predict" });
    expect(within(form).getByRole("button", { name: "Start experiment" })).toHaveProperty("disabled", true);
    fireEvent.click(within(form).getByRole("button", { name: "Write a new spec" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work?tab=experiments&new=predict`));
  });

  it("Prepare data opens its panel in Work when the server says it can run (no new screen)", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    const kinds = await screen.findByRole("list", { name: "Job kinds" });
    fireEvent.click(within(kinds).getByRole("button", { name: /Prepare data/ }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work?tab=prepare`));
    expect(await screen.findByRole("form", { name: "Load a file" })).toBeTruthy();
    expect(SCREENS.length).toBeLessThanOrEqual(20);
  });

  it("starts Forecast from an exact published ML plan and shows it in Work → Experiments", async () => {
    const plan = { id: "defn_forecast", workspace_id: WS, kind: "ml_spec", key: "weekly_volume", version: 2,
      status: "published", title: "Weekly volume", spec: { task: "forecast", target: "orders",
        dataset: { asset: "sales.orders" }, search: { max_trials: 4, max_seconds: 120 } } };
    const experiment = { id: "mlx_forecast", workspace_id: WS, definition_id: plan.id, definition_key: plan.key,
      definition_version: 2, task: "forecast", status: "succeeded", verdict: "verified",
      dataset_asset: "sales.orders", summary: { metric: "mae", candidate: 4.2 }, readiness: {}, artifacts: {},
      error: null, created_at: "2026-09-26T12:00:00Z", finished_at: "2026-09-26T12:01:00Z" };
    const f = mockFetch((method, path) => {
      if (method === "GET" && path === `/api/workspaces/${WS}/capabilities`) return { status: 200, body: {
        workspace_id: WS, digest: "d", job_kinds: [{ key: "forecast", label: "Forecast", mode: "ml", work_order_kind: "forecast",
          available: true, reasons: [], capabilities: [], entry: { type: "work_order", payload_type: "ml" },
          readiness_checks: [], min_role: "analyst" }],
      } };
      if (method === "GET" && path === `/api/workspaces/${WS}/definitions`) return { status: 200, body: { items: [plan], next_cursor: null } };
      if (method === "GET" && path === `/api/workspaces/${WS}/definitions/${plan.id}`) return { status: 200, body: plan };
      if (method === "POST" && path === `/api/workspaces/${WS}/ml/experiments`) return { status: 201, body: experiment };
      if (method === "GET" && path === `/api/workspaces/${WS}/ml/experiments`) return { status: 200, body: [experiment] };
      if (method === "GET" && path === `/api/workspaces/${WS}/ml/experiments/${experiment.id}`) return { status: 200, body: { ...experiment, verification: { status: "passed" } } };
      return null;
    });
    renderAt(`/w/${WS}/work`);
    fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
    fireEvent.click(await screen.findByRole("button", { name: /Forecast/ }));
    const form = await screen.findByRole("form", { name: "Start forecast" });
    fireEvent.change(await within(form).findByLabelText("Published model plan"), { target: { value: plan.id } });
    expect(within(form).getByText("sales.orders")).toBeTruthy();
    fireEvent.click(within(form).getByRole("button", { name: "Start experiment" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work?tab=experiments&experiment=${experiment.id}`));
    expect(await screen.findByRole("region", { name: `Experiment ${experiment.id}` })).toBeTruthy();
    expect(calls(f, "POST", /\/analysis$/)).toHaveLength(0);
    const [, init] = calls(f, "POST", /\/ml\/experiments$/)[0];
    expect(JSON.parse(String(init?.body))).toEqual({ definition: plan.id });
  });
});

it("builds, publishes and runs a workspace workflow using the exact definition version", async () => {
  let definition: Record<string, unknown> | null = null;
  const f = mockFetch((method, path, body) => {
    if (method === "GET" && path === `/api/workspaces/${WS}/definitions`) return { status: 200, body: { items: definition ? [definition] : [], next_cursor: null } };
    if (method === "GET" && path === "/api/capabilities") return { status: 200, body: { digest: "d", capabilities: [
      { id: "agent.metadata", kind: "Agent", summary: "Discover metadata", entry: "python:analystos.agents.metadata:run",
        enabled: true, available: true, side_effect: "read_source", certification: { status: "certified" } },
    ] } };
    if (method === "POST" && path === `/api/workspaces/${WS}/definitions`) {
      const input = JSON.parse(body || "{}") as Record<string, unknown>;
      definition = { ...input, id: "defn_workflow", workspace_id: WS, version: 1, status: "draft", revision: 1 };
      return { status: 201, body: definition };
    }
    if (method === "POST" && path === `/api/workspaces/${WS}/definitions/defn_workflow/publish`) {
      definition = { ...definition, status: "published", revision: 2 };
      return { status: 200, body: definition };
    }
    if (method === "GET" && path === `/api/workspaces/${WS}/definitions/defn_workflow`) return { status: 200, body: definition };
    if (method === "POST" && path === `/api/workspaces/${WS}/analysis`) return { status: 200, body: { id: RUN } };
    return null;
  });
  renderAt(`/w/${WS}/work?tab=workflows`);
  fireEvent.click(await screen.findByRole("button", { name: "Build workflow" }));
  const form = await screen.findByRole("form", { name: "Workflow builder" });
  fireEvent.change(within(form).getAllByLabelText("Title")[0], { target: { value: "Metadata review" } });
  fireEvent.change(within(form).getByLabelText("Key"), { target: { value: "metadata_review" } });
  fireEvent.change(within(form).getByLabelText("Purpose"), { target: { value: "Review selected table metadata" } });
  fireEvent.change(within(form).getByLabelText("Step key"), { target: { value: "metadata" } });
  fireEvent.change(within(form).getAllByLabelText("Title")[1], { target: { value: "Discover metadata" } });
  fireEvent.change(within(form).getByLabelText("Agent"), { target: { value: "agent.metadata" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save draft" }));
  await waitFor(() => expect(calls(f, "POST", /\/definitions$/)).toHaveLength(1));
  const created = JSON.parse(String(calls(f, "POST", /\/definitions$/)[0][1]?.body));
  expect(created.kind).toBe("playbook");
  expect(created.spec.certification.status).toBe("draft");
  expect(created.spec.spec.steps).toEqual([{ key: "metadata", title: "Discover metadata", use: "agent.metadata", after: [], optional: false }]);
  fireEvent.click(await screen.findByRole("button", { name: "Publish version" }));
  const runButton = await screen.findByRole("button", { name: "Run" });
  fireEvent.click(runButton);
  const runForm = await screen.findByRole("form", { name: "Run workflow" });
  fireEvent.change(within(runForm).getByLabelText("Goal"), { target: { value: "Review the latest table metadata" } });
  fireEvent.click(within(runForm).getByRole("button", { name: "Start workflow" }));
  await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/work/investigations/${RUN}`));
  const started = JSON.parse(String(calls(f, "POST", /\/analysis$/)[0][1]?.body));
  expect(started).toMatchObject({ definition: "defn_workflow", autonomy_level: 2, source_ids: ["src_sn"] });
});

// ------------------------------------------------------------------------------------ Overview
const src = (over: Partial<Source>): Source => ({ id: "s", workspace_id: WS, kind: "postgres", name: "S", config: {}, secret_ref: null, status: "registered",
  execution_mode: "staged", staging_schema: null, last_discovered_at: null, last_error: null, created_at: "", ...over });

describe("Overview in two states", () => {
  it("derives the first-run checklist from real state and resumes at the first unfinished step", () => {
    const empty = firstRunSteps({ sources: [], catalog: [], objective: "", runs: [] });
    expect(empty.map((s) => s.id)).toEqual(["connect", "select", "brief", "goal", "start"]);
    expect(nextStep(empty)?.id).toBe("connect");
    expect(nextStep(firstRunSteps({ sources: [src({})], catalog: [], objective: "", runs: [] }))?.id).toBe("select");
    expect(nextStep(firstRunSteps({ sources: [src({ status: "ready" })], catalog: [{} as never], objective: "short", runs: [] }))?.id).toBe("goal");
    expect(nextStep(firstRunSteps({ sources: [src({ status: "ready" })], catalog: [{} as never], objective: "Why are P1 times rising?", runs: [] }))?.id)
      .toBe("start");
  });

  it("first run: only the next step has a button", async () => {
    const FRESH = { ...WORKSPACE, objective: "" };
    mockFetch((method, path) => {
      if (method !== "GET") return null;
      if (path === `/api/workspaces/${WS}`) return { status: 200, body: FRESH };
      if (path === `/api/workspaces/${WS}/analysis`) return { status: 200, body: [] };
      return null;
    });
    renderAt(`/w/${WS}`);
    const list = await screen.findByRole("list", { name: "Getting started" });
    const steps = within(list).getAllByRole("listitem");
    expect(steps).toHaveLength(5);
    const current = steps.filter((s) => s.getAttribute("aria-current") === "step");
    expect(current).toHaveLength(1);
    expect(within(current[0]).getByText("Describe what you want to know")).toBeTruthy();
    for (const s of steps) {
      const controls = within(s).queryAllByRole("button").length + within(s).queryAllByRole("link").length;
      expect(controls > 0, s.textContent ?? "").toBe(s === current[0]);
    }
    expect(within(steps[0]).getByText("(done)")).toBeTruthy();
    // no counters, cost or Start work button before the first piece of work
    expect(screen.queryByText("At a glance")).toBeNull();
    expect(screen.queryByRole("button", { name: "Start work" })).toBeNull();
  });

  it("after the first run: what needs you (including void findings), one Start work, counters secondary", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}`);
    const card = (await screen.findByRole("heading", { name: "What needs you" })).closest("section")!;
    await waitFor(() => expect(within(card).getByText("Findings to re-check").previousElementSibling?.textContent).toBe("1"));
    expect(within(card).getByText("Pending approvals").closest("a")?.getAttribute("href")).toBe(`/w/${WS}/operate/approvals`);
    expect(within(card).getByText("Open data questions")).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Start work" })).toHaveLength(1);
    const glance = screen.getByText("At a glance").closest("details")!;
    expect(glance.hasAttribute("open")).toBe(false);
    expect(container.textContent).not.toMatch(/Autonomy L\d/);
  });
});

// ------------------------------------------------------------------------------------ plain language
/** Text on the default path: everything except closed disclosures, tooltips and screen-reader-only text. */
function visibleText(root: HTMLElement): string {
  const clone = root.cloneNode(true) as HTMLElement;
  clone.querySelectorAll("details:not([open])").forEach((d) => {
    Array.from(d.children).forEach((c) => { if (c.tagName !== "SUMMARY") c.remove(); });
  });
  return clone.textContent ?? "";
}

const JARGON = /\b(JEV|REV|OKF|Ossie|rungs?|hash(es)?|L[0-4] ·)\b/;

describe("plain language first: jargon only under Technical details", () => {
  it.each([
    ["Overview", `/w/${WS}`, /What needs you/],
    ["investigation", `/w/${WS}/work/investigations/${RUN}`, /Network resolves P1s slower/],
    ["finding", `/w/${WS}/outputs/findings/${INSIGHT}`, /How it was checked/],
    ["approval inbox", `/w/${WS}/operate/approvals`, /Publish dashboards/],
    ["alerts", `/w/${WS}/operate/monitoring?tab=alerts`, /can only raise severity/],
    ["outputs", `/w/${WS}/outputs?artifact=art_dash`, /Request publication/],
    ["members & policy", `/w/${WS}/settings/policy`, /Effective policy/],
  ])("%s", async (_n, path, ready) => {
    mockFetch();
    const { container } = renderAt(path);
    await waitFor(() => expect(container.textContent).toMatch(ready), { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 50));
    const main = container.querySelector("main")!;
    expect(visibleText(main).match(JARGON)?.[0] ?? null).toBeNull();
  });
});

// ------------------------------------------------------------------------------------ policy form
describe("policy form (raw JSON under Advanced)", () => {
  it("converts form values to policy values and back, keeping unknown fields", () => {
    expect(fieldValue("int", "5000")).toBe(5000);
    expect(fieldValue("usd", "")).toBeUndefined();
    expect(fieldValue("list", "a.b.c\n*.email")).toEqual(["a.b.c", "*.email"]);
    expect(fieldText("list", ["x", "y"])).toBe("x\ny");
    expect(setField({ alpha: 0.05, max_rows: 10 }, "max_rows", undefined)).toEqual({ alpha: 0.05 });
    expect(autonomyInWords(3)).toMatch(/^Executes; publish needs approval: /);
  });

  it("edits common fields in the form, shows the same document as JSON under Advanced, and saves it", async () => {
    const f = mockFetch((method, path) => (method === "PUT" && path.endsWith("/policy") ? { status: 200, body: { policy_version: 3 } } : null));
    const { container } = renderAt(`/w/${WS}/settings/policy`);
    const form = await screen.findByRole("form", { name: "Workspace policy" });
    const rows = within(form).getByLabelText("Most rows a query may return") as HTMLInputElement;
    expect(rows.value).toBe("5000");
    fireEvent.change(rows, { target: { value: "1000" } });
    fireEvent.change(within(form).getByLabelText("Personal data"), { target: { value: "restricted" } });
    const json = within(form).getByLabelText("Policy document (JSON)") as HTMLTextAreaElement;
    expect(json.closest("details")?.hasAttribute("open")).toBe(false);
    expect(JSON.parse(json.value)).toMatchObject({ max_rows: 1000, pii_access: "restricted", publish_destinations: ["superset"] });
    fireEvent.click(within(form).getByRole("button", { name: "Save policy" }));
    expect(await within(form).findByText("Saved as policy version 3.")).toBeTruthy();
    const [, init] = calls(f, "PUT", /\/policy$/)[0];
    expect(JSON.parse(String(init!.body))).toMatchObject({ max_rows: 1000, pii_access: "restricted", run_token_budget: 200000 });
    // autonomy lives here, in words, with the level picker under Advanced
    const autonomy = screen.getByRole("heading", { name: "How much agents do alone" }).closest("section")!;
    expect(within(autonomy).getByText(autonomyInWords(3))).toBeTruthy();
    expect(within(autonomy).getByText(/Advanced: change the autonomy level/).closest("details")?.hasAttribute("open")).toBe(false);
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => v.id)).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------ wave-1 panels
describe("wave-1 features as panels in the new areas", () => {
  it("Why this number? traces every link and returns focus to its button", async () => {
    mockFetch();
    renderAt(`/w/${WS}/outputs/findings/${INSIGHT}`);
    const numbers = await screen.findByRole("list", { name: "Numbers in this finding" });
    const button = within(numbers).getByRole("button", { name: /Why this number\?/ });
    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog", { name: "Why 2.1x?" });
    for (const t of ["The fact behind it", "The step that produced it", "The query that read the data", "The data it was read from",
      "The metric definition", "The verification"]) expect(within(dialog).getByText(t)).toBeTruthy();
    expect(within(dialog).getByText(/still holds/)).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(button);
  });

  it("a void finding shows its cause in Outputs, on its page and in its Why drawer", async () => {
    mockFetch();
    renderAt(`/w/${WS}/outputs?type=finding`);
    const list = await screen.findByRole("list", { name: "Outputs" });
    await within(list).findByText(/Change freezes add a day/);
    expect(within(list).getByText(/Why void: data: the snapshot of stg_sn.incident changed/)).toBeTruthy();
    fireEvent.click(within(list).getByText(/Change freezes add a day/));
    expect(await screen.findByText(/This finding is void: data: the snapshot/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /Why this number\?/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("the snapshot of stg_sn.incident changed")).toBeTruthy();
    expect(within(dialog).getAllByText("no longer valid").length).toBeGreaterThan(0);
    expect(INSIGHT_VOID_ID).toBe("ins_void");
  });

  it("schedule pins: upgrade available, what would change, accept bound to revision and upgrade", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/operate/schedules`);
    const pins = await screen.findByLabelText("Pinned versions");
    expect(await within(pins).findByText("upgrade available")).toBeTruthy();
    fireEvent.click(within(pins).getByRole("button", { name: "What would change" }));
    expect(within(pins).getByText("steps.verify.second_method")).toBeTruthy();
    fireEvent.click(within(pins).getByRole("button", { name: "Accept upgrade" }));
    expect(await screen.findByText(/Upgraded. The next run uses the new versions/)).toBeTruthy();
    const [, init] = calls(f, "POST", /\/schedules\/sch_1\/upgrade$/)[0];
    expect((init!.headers as Record<string, string>)["If-Match"]).toBe('"1"');
    expect(JSON.parse(String(init!.body))).toEqual({ upgrade_hash: "up_7f3a9c" });
  });

  it("Data → Definitions: joins to confirm (measured), the model diff and draft → publish", async () => {
    const f = mockFetch();
    const { container } = renderAt(`/w/${WS}/data/catalog?tab=definitions`);
    // Definitions open on the suggested model; the proposed changes are the "Data model" section
    fireEvent.click(await screen.findByRole("button", { name: /^Data model/ }));
    expect(await screen.findByRole("table", { name: "Data model changes" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Joins to confirm/ }));
    const join = await screen.findByRole("article", { name: "Join stg_sn.incident to stg_sn.sys_user_group" });
    expect(within(join).getByText("many to one")).toBeTruthy();
    expect(within(join).getByText(/98% of values match/)).toBeTruthy();
    fireEvent.change(within(join).getByPlaceholderText("Reason (optional)"), { target: { value: "matches the CMDB" } });
    fireEvent.click(within(join).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(calls(f, "POST", /\/candidates\/rlc_1\/accept$/)).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: /Saved definitions/ }));
    const draft = await screen.findByRole("article", { name: "Definition P1 weekly review version 2" });
    fireEvent.click(within(draft).getByRole("button", { name: "Changes" }));
    expect(await within(draft).findByText("window_days")).toBeTruthy();
    fireEvent.click(within(draft).getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(calls(f, "POST", /\/definitions\/def_2\/publish$/)).toHaveLength(1));
    expect((calls(f, "POST", /\/publish$/)[0][1]!.headers as Record<string, string>)["If-Match"]).toBe('"1"');
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`)).toEqual([]);
  }, 15000);

  it("Work → Prepare data: preview writes nothing; a run's output is listed in Outputs", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=prepare`);
    fireEvent.click(await screen.findByRole("button", { name: /p1_incidents_clean/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));
    expect(await screen.findByText(/Preview — nothing was written/)).toBeTruthy();
    expect(screen.getByRole("table", { name: "Preview of clean" })).toBeTruthy();
    const [, init] = calls(f, "POST", /\/recipes\/rcp_1\/runs$/)[0];
    expect(JSON.parse(String(init!.body))).toEqual({ mode: "preview" });
    cleanup();
    renderAt(`/w/${WS}/outputs?type=prepared`);
    const list = await screen.findByRole("list", { name: "Outputs" });
    expect(within(list).getByText("p1_incidents_clean")).toBeTruthy();
  });

  it("loads a file into the file source through upload then ingest", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=prepare`);
    const form = await screen.findByRole("form", { name: "Load a file" });
    const file = new File(["a,b\n1,2"], "orders.csv", { type: "text/csv" });
    fireEvent.change(within(form).getByLabelText("File"), { target: { files: [file] } });
    fireEvent.change(within(form).getByLabelText("Table name"), { target: { value: "orders" } });
    fireEvent.change(within(form).getByLabelText("If the table exists"), { target: { value: "append" } });
    fireEvent.click(within(form).getByRole("button", { name: "Load file" }));
    expect(await within(form).findByText("Loaded into stg_files.orders (append).")).toBeTruthy();
    const [, init] = calls(f, "POST", /\/sources\/src_files\/ingest$/)[0];
    expect(JSON.parse(String(init!.body))).toEqual({ path: `/data/uploads/${WS}/orders.csv`, table: "orders", mode: "append", keys: [] });
  });

  it("dashboards are an Outputs filter with preview and publication, not a second list", async () => {
    mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    const list = await screen.findByRole("list", { name: "Outputs" });
    const items = within(list).getAllByRole("listitem");
    expect(items.every((i) => /dashboard/.test(i.textContent ?? ""))).toBe(true);
    expect(screen.getByRole("link", { name: /Dashboards \(\d+\)/ }).getAttribute("aria-current")).toBe("page");
  });
});
