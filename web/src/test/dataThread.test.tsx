/**
 * The Data Thread (P7-04, P7-05) in Work: step cards with version, checks, verdict (void with its
 * cause), result and "Why this number?"; edit and re-run with dependents re-running or voiding; old
 * versions readable; pins through an approval; fork, compare side by side and merge into a report;
 * and working preview filters that re-query through the gateway (P4-07).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session } from "../api";
import { filteredSql } from "../components/FilteredPreview";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { mockBackend, resetMockState, USER, WS } from "./mockBackend";
import { approvePin, SQL_MTTR } from "./mockThread";

const THREAD = `/w/${WS}/work?tab=thread&container=run:run_demo`;

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

/** A step card by its title (cards are named "Step <n>: <title>"; other list items may mention a title too). */
const card = async (title: RegExp) => screen.findByRole("listitem", { name: new RegExp(`^Step \\d+: .*${title.source.replace(/^Step \\d\+: /, "")}`) });

describe("step cards", () => {
  it("show version, checks (a corrected one says so), verdict and the result", async () => {
    mockFetch();
    renderAt(THREAD);
    expect(await screen.findByRole("tab", { name: "Data Thread", selected: true })).toBeTruthy();
    const count = await card(/P1 incidents by assignment group/);
    expect(within(count).getByText("v1")).toBeTruthy();
    expect(within(count).getByText("verified")).toBeTruthy();
    const checks = within(count).getByRole("list", { name: "Checks of P1 incidents by assignment group" });
    expect(checks.textContent).toMatch(/no join fan-out: failed, corrected and re-run/);
    expect(within(count).getByText(/Corrected: a join multiplied rows 1\.4x/)).toBeTruthy();
    expect(await within(count).findByRole("table", { name: "Result of P1 incidents by assignment group" })).toBeTruthy();
    const plan = await card(/Plan: why/);
    expect(within(plan).getByText(/plans are not verified/)).toBeTruthy(); // P3-6: says why, not "no verdict recorded (recorded)"
    // a plan has no number: no "Why this number?"
    expect(within(plan).queryByRole("button", { name: /Why this number/ })).toBeNull();
  });

  it("Why this number? traces the number through fact, step, query receipt, data version and verdict", async () => {
    mockFetch();
    renderAt(THREAD);
    const mttr = await card(/Mean P1 resolution hours by group/);
    fireEvent.click(await within(mttr).findByRole("button", { name: /Why this number/ }));
    const drawer = await screen.findByRole("dialog", { name: "Why 9.4?" });
    const trail = within(drawer).getByRole("list", { name: "How this number is traced" });
    for (const t of ["The fact behind it", "The step that produced it", "The query that read the data", "The data it was read from",
      "The definition and the verification"]) expect(within(trail).getByText(t)).toBeTruthy();
    expect(trail.textContent).toMatch(/row for Network/);
    expect(trail.textContent).toMatch(/stg_sn\.incident@2026-09-26/);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(within(mttr).getByRole("button", { name: /Why this number/ })).toBe(document.activeElement));
  });
});

describe("edit and re-run (P7-04)", () => {
  it("an edit makes v2, re-runs its dependents, voids earlier verdicts and keeps v1 readable", async () => {
    const f = mockFetch();
    renderAt(THREAD);
    const mttr = await card(/Mean P1 resolution hours by group/);
    fireEvent.click(within(mttr).getByRole("button", { name: /^Edit/ }));
    const form = within(mttr).getByRole("form", { name: "Edit Mean P1 resolution hours by group" });
    fireEvent.change(within(form).getByLabelText("SQL"), { target: { value: SQL_MTTR.replace("WHERE priority = '1'", "WHERE priority = '1' AND close_code != 'auto'") } });
    fireEvent.click(within(form).getByRole("button", { name: "Save and re-run" }));
    expect(await screen.findByText(/is now version 2\. 2 steps that read it re-ran; 3 earlier verdicts are now void\./)).toBeTruthy();
    const [[, init]] = calls(f, "PATCH", /\/steps\/stp_mttr$/);
    expect(new Headers(init!.headers).get("If-Match")).toBe('"1"');
    expect(String(bodyOf(init).spec && (bodyOf(init).spec as { sql: string }).sql)).toMatch(/close_code != 'auto'/);

    const claim = await card(/Network is the slowest group/);
    expect(await within(claim).findByText("re-ran: flagged")).toBeTruthy();
    expect(within(claim).getByText("failed verification")).toBeTruthy();
    const edited = await card(/Mean P1 resolution hours by group/);
    expect(within(edited).getByText("now v2")).toBeTruthy();

    fireEvent.click(within(edited).getByRole("button", { name: /^Versions \(2\)/ }));
    const v1 = await within(edited).findByRole("listitem", { name: "Version 1" });
    expect(within(v1).getByText(/Why void: edit: the step was edited to v2/)).toBeTruthy();
    fireEvent.click(within(v1).getByRole("button", { name: "Show the result of v1" }));
    const old = await within(v1).findByRole("table", { name: "Result of version 1" });
    expect(old.textContent).toMatch(/9\.4/);
  });

  it("a stale edit (someone re-ran the step meanwhile) says so and overwrites nothing", async () => {
    mockFetch();
    renderAt(THREAD);
    const mttr = await card(/Mean P1 resolution hours by group/);
    fireEvent.click(within(mttr).getByRole("button", { name: /^Edit/ }));
    mockBackend("POST", `/api/workspaces/${WS}/steps/stp_mttr/runs`, "{}"); // another analyst re-runs it: v2
    fireEvent.click(within(mttr).getByRole("button", { name: "Save and re-run" }));
    const alert = await within(mttr).findByText(/Someone changed this\./);
    fireEvent.click(within(alert.closest(".stale-edit") as HTMLElement).getByRole("button", { name: "Compare with mine" }));
    expect(await within(mttr).findByRole("table", { name: /Differences between your version and the current one/ })).toBeTruthy();
  });

  it("re-run makes a new version on today's data", async () => {
    mockFetch();
    renderAt(THREAD);
    const count = await card(/P1 incidents by assignment group/);
    fireEvent.click(within(count).getByRole("button", { name: /^Re-run/ }));
    expect(await screen.findByText(/P1 incidents by assignment group is now version 2\. No other step reads it; 1 earlier verdict is now void/)).toBeTruthy();
  });
});

describe("pins need an approval bound to the frozen query (P7-04)", () => {
  it("request → still pending → approved → pinned", async () => {
    const f = mockFetch();
    renderAt(THREAD);
    const count = await card(/P1 incidents by assignment group/);
    fireEvent.click(within(count).getByRole("button", { name: /^Pin/ }));
    const form = within(count).getByRole("form", { name: "Pin P1 incidents by assignment group" });
    fireEvent.change(within(form).getByLabelText("Pin to"), { target: { value: "schedule" } });
    fireEvent.click(within(form).getByRole("button", { name: "Request approval to pin" }));
    expect(await within(count).findByText(/Approval requested/)).toBeTruthy();
    expect(within(count).getByRole("link", { name: "approvals inbox" }).getAttribute("href")).toBe(`/w/${WS}/operate/approvals`);
    fireEvent.click(within(count).getByRole("button", { name: "Pin with the approved request" }));
    expect(await within(count).findByText(/still pending/)).toBeTruthy();
    approvePin();
    fireEvent.click(within(count).getByRole("button", { name: "Pin with the approved request" }));
    expect(await within(count).findByText(/Pinned version 1 to the schedule/)).toBeTruthy();
    const pins = calls(f, "POST", /\/steps\/stp_count\/pins$/).map(([, i]) => bodyOf(i));
    expect(pins[0]).toMatchObject({ target: "schedule", version: 1, cron: "0 7 * * 1", timezone: "UTC" });
    expect(pins[0].approval_id).toBeUndefined();
    expect(pins[2]).toMatchObject({ approval_id: "apr_pin" });
  });

  it("a step without an active verified verdict cannot be pinned", async () => {
    mockFetch();
    renderAt(THREAD);
    const plan = await card(/Plan: why/);
    expect((within(plan).getByRole("button", { name: /^Pin/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(plan).getByText(/Only a query or method step can be pinned/)).toBeTruthy();
  });

  it("a verified claim is not offered a pin the server refuses (live regression)", async () => {
    // services/step_pins.py: "only a query step or an AnalysisSpec method step can be pinned"
    mockFetch();
    renderAt(THREAD);
    const claim = await card(/Network is the slowest group/);
    expect(within(claim).getByText("verified")).toBeTruthy();
    expect((within(claim).getByRole("button", { name: /^Pin/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(claim).getByText(/pin the step this one reads/)).toBeTruthy();
  });
});

describe("branches (P7-05): fork, compare side by side, merge into a report", () => {
  it("fork from a step, edit the copy, compare with main and merge", async () => {
    const f = mockFetch();
    renderAt(THREAD);
    const mttr = await card(/Mean P1 resolution hours by group/);
    fireEvent.click(within(mttr).getByRole("button", { name: /Fork from here/ }));
    const form = within(mttr).getByRole("form", { name: "Fork from Mean P1 resolution hours by group" });
    fireEvent.change(within(form).getByLabelText("Branch name"), { target: { value: "without auto-closed" } });
    fireEvent.click(within(form).getByRole("button", { name: "Fork" }));
    expect(await screen.findByText(/Forked "without auto-closed"/)).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /\/forks$/)[0][1])).toEqual({ from_step_id: "stp_mttr", name: "without auto-closed", include_downstream: true });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toMatch(/branch=brn_whatif/));
    const inherited = await card(/P1 incidents by assignment group/);
    expect(within(inherited).getByText("from the parent branch")).toBeTruthy();
    expect(within(inherited).queryByRole("button", { name: /^Edit/ })).toBeNull();

    const copy = await card(/Mean P1 resolution hours by group/);
    fireEvent.click(within(copy).getByRole("button", { name: /^Edit/ }));
    const edit = within(copy).getByRole("form", { name: /Edit Mean P1/ });
    fireEvent.change(within(edit).getByLabelText("SQL"), { target: { value: `${SQL_MTTR} -- excluding auto closed` } });
    fireEvent.click(within(edit).getByRole("button", { name: "Save and re-run" }));
    await screen.findByText(/is now version 2/);

    fireEvent.change(screen.getByLabelText("Compare with"), { target: { value: "brn_main" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare side by side" }));
    const cmp = await screen.findByRole("group", { name: "Compare main with without auto-closed" });
    const diverged = within(cmp).getByRole("group", { name: "Mean P1 resolution hours by group: diverged" });
    expect(diverged.textContent).toMatch(/Headline: 9\.4 → 8\.1/);
    expect(within(diverged).getByRole("table", { name: /Numbers that differ/ })).toBeTruthy();
    expect(within(cmp).getByRole("group", { name: "Plan: why are P1 resolution times rising?: shared" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Report title"), { target: { value: "MTTR without auto-closed" } });
    fireEvent.click(screen.getByRole("button", { name: "Merge without auto-closed into a report" }));
    const merged = await screen.findByText(/Merged into the report "MTTR without auto-closed"/);
    expect(merged.textContent).toMatch(/1 left out \(flagged: failed verification\)/);
    // live regression: the server returns a generated artifact name and `included` as a count
    expect(merged.textContent).toMatch(/\(version 1\) with \d+ steps?[;.]/);
    expect(merged.textContent).not.toMatch(/thread-report-/);
    expect(within(merged.closest(".alert") as HTMLElement).getByRole("link", { name: "Open it in Results" }).getAttribute("href"))
      .toBe(`/w/${WS}/outputs?type=report&artifact=art_thread_report`);
  });

  it("a merge refused because a verdict is void shows the server's reason", async () => {
    mockFetch((method, path) => (method === "POST" && path.endsWith("/merge")
      ? { status: 403, body: { error: { code: "policy_denied", message: "a step's verdict is VOID: MTTR; re-run it first", details: {} } } } : null));
    renderAt(THREAD);
    await card(/Mean P1/);
    fireEvent.click(screen.getByRole("button", { name: "Merge main into a report" }));
    expect(await screen.findByText(/a step's verdict is VOID: MTTR; re-run it first/)).toBeTruthy();
  });
});

describe("working preview filters (P4-07)", () => {
  it("re-query through the gateway and show the applied filter and new evidence", async () => {
    const f = mockFetch();
    renderAt(THREAD);
    const count = await card(/P1 incidents by assignment group/);
    const bar = await within(count).findByRole("form", { name: "Filter Result of P1 incidents by assignment group" });
    fireEvent.change(within(bar).getByLabelText("Value"), { target: { value: "Network" } });
    fireEvent.click(within(bar).getByRole("button", { name: "Apply filter" }));
    const applied = await within(count).findByRole("status", { name: "Applied filters" });
    expect(applied.textContent).toMatch(/Re-queried through the gateway with/);
    expect(applied.textContent).toMatch(/1 row · result fh1c0ffee0/);
    const sent = String(bodyOf(calls(f, "POST", /\/query$/)[0][1]).sql);
    expect(sent).toMatch(/WHERE "assignment_group" = 'Network'$/);
    const table = within(count).getByRole("table", { name: /filtered/ });
    expect(table.textContent).toMatch(/Network/);
    expect(table.textContent).not.toMatch(/Desktop/);
    fireEvent.click(within(applied).getByRole("button", { name: "Clear filters" }));
    await waitFor(() => expect(within(count).getByRole("table", { name: "Result of P1 incidents by assignment group" }).textContent).toMatch(/Desktop/));
  });

  it("builds the filtered SQL with quoted identifiers and escaped literals", () => {
    expect(filteredSql("SELECT a, n FROM t;", [{ column: "a", op: "=", value: "O'Brien" }, { column: "n", op: ">", value: "3" }], new Set(["n"])))
      .toBe(`SELECT * FROM (\nSELECT a, n FROM t\n) AS filtered\nWHERE "a" = 'O''Brien' AND "n" > 3`);
    expect(filteredSql("SELECT 1", [{ column: 'we"ird', op: "contains", value: "x" }], new Set())).toMatch(/CAST\("we""ird" AS TEXT\) LIKE '%x%'/);
  });
});

describe("Ask threads as a Data Thread", () => {
  it("records an Ask thread as steps on request", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/work?tab=thread&container=ask_thread:ask_old`);
    fireEvent.click(await screen.findByRole("button", { name: "Record this Ask thread as steps" }));
    expect(await card(/How many P1 incidents per week/)).toBeTruthy();
    expect(calls(f, "POST", /\/threads\/ask_thread\/ask_old\/ingest$/)).toHaveLength(1);
  });
});
