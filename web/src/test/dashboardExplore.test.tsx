/**
 * CONV-06 in the web client: a selection filters the other tiles (debounced, latest wins, a session view and not a
 * version), chips say what is filtered, "Apply as dashboard filter" is the one edit, drill levels have breadcrumbs,
 * and "Show rows" lists only what the server returns. Everything is keyboard reachable; query ids only in expert view.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { session, type ViewFilter } from "../api";
import { setExpertView } from "../lib/expert";
import { chipText, LruCache, memberOf, membersOf, toggleFilter, viewKey } from "../lib/explore";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 30000 });

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  setExpertView(false);
});

describe("explore helpers", () => {
  it("maps a drawn label back to the value and lists members in drawn order", () => {
    const preview = { columns: ["month", "n"], rows: [["2026-09-01T00:00:00", 3], ["2026-10-01T00:00:00", 4], ["2026-09-01T00:00:00", 1]] };
    expect(memberOf(preview, 0, "2026-09-01")).toBe("2026-09-01T00:00:00");
    expect(memberOf(preview, 0, "2027-01-01")).toBeUndefined();
    expect(membersOf(preview, 0).map((m) => m.text)).toEqual(["2026-09-01", "2026-10-01"]);
  });

  it("keeps one filter per field and the same click again removes it", () => {
    const p1: ViewFilter = { field: "incident.priority", op: "eq", value: "1 - Critical", source: "a", label: "Priority" };
    const p2: ViewFilter = { ...p1, value: "2 - High" };
    expect(toggleFilter([], p1)).toEqual([p1]);
    expect(toggleFilter([p1], p2)).toEqual([p2]);
    expect(toggleFilter([p1], { ...p1, field: "priority" })).toEqual([]);  // same field by its short name
    expect(chipText(p1)).toBe("Priority = 1 - Critical");
    expect(chipText({ ...p1, op: "in", value: ["1 - Critical", "2 - High"] })).toBe("Priority is one of 1 - Critical, 2 - High");
    expect(viewKey(2, [p1], false)).not.toEqual(viewKey(3, [p1], false));
  });

  it("bounds the response cache", () => {
    const c = new LruCache<number>(2);
    c.set("a", 1);
    c.set("b", 2);
    c.get("a");
    c.set("c", 3);
    expect([c.get("a"), c.get("b"), c.get("c"), c.length]).toEqual([1, undefined, 3, 2]);
  });
});

async function openDraft() {
  renderAt(`/w/${WS}/outputs?type=dashboard`);
  fireEvent.click(await screen.findByText("Build a dashboard"));
  const form = await screen.findByRole("form", { name: "Build a dashboard from a request" });
  fireEvent.change(within(form).getByLabelText("What should the dashboard show?"), { target: { value: "Build an incident operations dashboard" } });
  fireEvent.click(within(form).getByRole("button", { name: "Build a draft" }));
  return screen.findByRole("region", { name: "Dashboard filters" });
}

const tile = (name: string) => screen.getByRole("group", { name });

describe("cross-filter", () => {
  it("filters the tiles that share the field, marks the others and sends only names and values", async () => {
    setExpertView(false);
    const f = mockFetch();
    const bar = await openDraft();
    expect(within(bar).getByText(/Click a bar, slice or legend item/)).toBeTruthy();
    const filter = await screen.findByRole("combobox", { name: "Filter the dashboard by Priority" });
    fireEvent.change(filter, { target: { value: "0" } });
    expect(await screen.findByRole("button", { name: "Remove filter Priority = 1 - Critical" })).toBeTruthy();
    await waitFor(() => expect(within(tile("Resolution time by category")).getByText("Filtered")).toBeTruthy());
    // PILOT-FIX-03: the trend does not show priority but reads the same table, so the server filters it too
    expect(within(tile("Incident count per month")).getByText("Filtered")).toBeTruthy();
    expect(within(tile("Incident count by priority")).getByText("Your selection")).toBeTruthy();
    expect(within(tile("Incident count")).getByText("Filtered")).toBeTruthy();
    expect(within(bar).getByText("4 filtered, 0 not filtered")).toBeTruthy();
    const views = calls(f, "POST", /\/view$/);
    expect(views).toHaveLength(1);
    expect(bodyOf(views[0][1])).toEqual({ filters: [{ field: "incident.priority", op: "eq", value: "1 - Critical",
      source: "c_incident_count_by_priority" }], base_version: 1, refresh_saved: false });
    expect(screen.queryByText(/qx_view_/)).toBeNull();  // business view: no query ids
    expect(screen.getByText("· v1")).toBeTruthy();  // a view is not a version
  });

  it("debounces quick selections into one request and clears with the chip", async () => {
    const f = mockFetch();
    await openDraft();
    fireEvent.change(await screen.findByRole("combobox", { name: "Filter the dashboard by Priority" }), { target: { value: "1" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Filter the dashboard by Category" }), { target: { value: "0" } });
    await screen.findByRole("button", { name: "Remove filter Category = Network" });
    await waitFor(() => expect(screen.getByText("Clear all")).toBeTruthy());
    await waitFor(() => expect(calls(f, "POST", /\/view$/)).toHaveLength(1));
    expect((bodyOf(calls(f, "POST", /\/view$/)[0][1]).filters as unknown[]).length).toBe(2);
    fireEvent.click(screen.getByRole("button", { name: "Remove filter Priority = 2 - High" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove filter Category = Network" }));
    await waitFor(() => expect(screen.queryByText("Not filtered")).toBeNull());
    expect(calls(f, "POST", /\/view$/)).toHaveLength(1);  // no filters: nothing to re-query
  });

  it("applies the selection as a dashboard filter through the typed op (a new version)", async () => {
    setExpertView(true);
    const f = mockFetch();
    await openDraft();
    fireEvent.change(await screen.findByRole("combobox", { name: "Filter the dashboard by Priority" }), { target: { value: "0" } });
    await waitFor(() => expect(screen.getAllByText(/qx_view_/).length).toBeGreaterThan(0));  // expert view shows the re-queries
    fireEvent.click(screen.getByRole("button", { name: "Apply as dashboard filter" }));
    await waitFor(() => expect(screen.getByText("· v2")).toBeTruthy());
    const ops = calls(f, "POST", /\/ops$/);
    expect(bodyOf(ops[0][1])).toEqual({ ops: [{ op: "dashboard.add_filter", field: "incident.priority", operator: "eq", value: "1 - Critical",
      scope: ["c_incident_count_by_priority"] }], base_version: 1 });
    expect(screen.queryByRole("button", { name: /Remove filter/ })).toBeNull();
    expect(screen.getByText('incident.priority eq "1 - Critical"')).toBeTruthy();
    await waitFor(() => expect(bodyOf(calls(f, "POST", /\/view$/).at(-1)![1]).refresh_saved).toBe(true));
    await waitFor(() => expect(within(tile("Incident count by priority")).getByText("Dashboard filter")).toBeTruthy());
  });
});

describe("drill-down and rows", () => {
  it("drills a time axis one level with breadcrumbs back", async () => {
    const f = mockFetch();
    await openDraft();
    const trend = tile("Incident count per month");
    fireEvent.change(within(trend).getByRole("combobox", { name: "Drill into day for Incident count per month" }), { target: { value: "1" } });
    const crumbs = await within(trend).findByRole("navigation", { name: "Drill path for Incident count per month" });
    await waitFor(() => expect(within(crumbs).getByText("Sep 2026")).toBeTruthy());
    expect(within(trend).getByText("Drilled")).toBeTruthy();
    const body = bodyOf(calls(f, "POST", /\/drill$/)[0][1]);
    expect(body).toMatchObject({ chart_id: "c_incident_count_trend", path: [{ field: "incident.opened_at", value: "2026-09-01" }], base_version: 1 });
    await waitFor(() => expect(within(trend).getByRole("table")).toBeTruthy());  // jsdom draws the table fallback
    expect(within(trend).getAllByText(/^2026-09-02/).length).toBeGreaterThan(0);
    fireEvent.click(within(crumbs).getByRole("button", { name: "All" }));
    await waitFor(() => expect(within(trend).queryByText("Drilled")).toBeNull());
  });

  it("lists the rows behind a selection with hidden columns counted, and closes back to the chart", async () => {
    const f = mockFetch();
    await openDraft();
    fireEvent.click(within(tile("Incident count by priority")).getByRole("button", { name: "Show rows" }));
    const rows = await screen.findByRole("region", { name: /Rows behind Incident count by priority/ });
    await waitFor(() => expect(within(rows).getByText("INC00012")).toBeTruthy());
    expect(within(rows).getByText("1 column is hidden by your access policy.")).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /\/drill$/)[0][1])).toMatchObject({ chart_id: "c_incident_count_by_priority", detail: true, preview: false });
    expect(document.activeElement?.textContent).toBe("Close rows");  // focus moves to the panel
    fireEvent.click(within(rows).getByRole("button", { name: "Close rows" }));
    expect(screen.queryByRole("region", { name: /Rows behind/ })).toBeNull();
  });

  it("keeps the builder's select toggle for the chat focus", async () => {
    mockFetch();
    await openDraft();
    const select = within(tile("Incident count by priority")).getByRole("button", { name: /Select Incident count by priority/ });
    fireEvent.click(select);
    expect(select.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(/refer to/)).toBeTruthy();
  });
});
