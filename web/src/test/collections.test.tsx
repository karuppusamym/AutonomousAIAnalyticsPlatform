import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { decideCollection, mockBackend, resetMockState, USER, WS } from "./mockBackend";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend((init?.method ?? "GET").toUpperCase(), String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function calls(f: ReturnType<typeof mockFetch>, method: string, re: RegExp): [string, RequestInit][] {
  return (f.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u))).map(([u, i]) => [String(u), i ?? {}]);
}

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

async function axeClean(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
}

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

const DASHBOARD = `/w/${WS}/outputs?artifact=art_dash`;

describe("BI-VIEWS-02: publish a chosen collection on its own", () => {
  it("asks for an approval of exactly the chosen dashboards and publishes only after it", async () => {
    const f = mockFetch();
    const { container } = renderAt(DASHBOARD);
    const own = await screen.findByRole("checkbox", { name: /P1 resolution \(weekly\)/ });
    const topic = screen.getByRole("checkbox", { name: /Demo \/ Delivery reliability/ });
    expect((own as HTMLInputElement).checked).toBe(true);  // the open dashboard is chosen by default
    expect((topic as HTMLInputElement).checked).toBe(false);
    expect(screen.getByText(/Topic view · 2 charts/)).toBeTruthy();
    fireEvent.click(topic);
    fireEvent.click(screen.getByRole("button", { name: "Ask for approval to publish 2 dashboards" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeTruthy();
    const [, init] = calls(f, "POST", /\/collections\/publish-request$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ keys: ["P1 resolution", "topic_delivery"] });
    expect(calls(f, "POST", /\/v2\/collections\/publish$/)).toEqual([]);
    expect(screen.getByRole("link", { name: "Open the approval inbox" })).toBeTruthy();
    expect(await axeClean(container)).toEqual([]);

    // a different selection is a different request: the ask button comes back for it
    fireEvent.click(screen.getByRole("checkbox", { name: /Demo \/ Delivery reliability/ }));
    expect(await screen.findByRole("button", { name: "Ask for approval to publish 1 dashboard" })).toBeTruthy();
  });

  it("publishes an approved collection with one click and shows where it went", async () => {
    const f = mockFetch();
    renderAt(DASHBOARD);
    fireEvent.click(await screen.findByRole("button", { name: "Ask for approval to publish 1 dashboard" }));
    expect(await screen.findByText(/Waiting for approval/)).toBeTruthy();
    cleanup();
    decideCollection("approved");  // an approver said yes in the inbox
    const { container } = renderAt(DASHBOARD);
    fireEvent.click(await screen.findByRole("button", { name: "Publish now" }));
    expect(await screen.findByText("Published.")).toBeTruthy();
    const [, init] = calls(f, "POST", /\/v2\/collections\/publish$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ approval_id: "apr_col_1" });
    expect(screen.getAllByRole("link", { name: "open ↗" }).some((a) => a.getAttribute("href")?.includes("/dashboard/41/"))).toBe(true);
    expect(await axeClean(container)).toEqual([]);
  });

  it("keeps the whole-investigation proposal available", async () => {
    const f = mockFetch();
    renderAt(DASHBOARD);
    fireEvent.click(await screen.findByRole("button", { name: "Request publication" }));
    expect(await screen.findByRole("link", { name: "Decide in the approvals inbox" })).toBeTruthy();
    expect(calls(f, "POST", /\/artifacts\/art_dash\/publish$/)).toHaveLength(1);
  });
});

describe("BI-VIEWS-02: group existing BI dashboards under the workspace name", () => {
  it("renames only after an approval and shows the old and new titles", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/outputs?type=dashboard`);
    const openGrouping = async () => {
      (await screen.findByText("Group published dashboards under this workspace’s name")).closest("details")!.open = true;
    };
    await openGrouping();
    expect(await screen.findByRole("checkbox", { name: /Executive overview: Why are P1 incidents slow\? → Demo \/ Executive/ })).toBeTruthy();
    expect(screen.getByText("1 already grouped.")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ask for approval to rename 1" }));
    expect(await screen.findByText(/Waiting for approval to rename 1 dashboard/)).toBeTruthy();
    const [, init] = calls(f, "POST", /\/dashboard-grouping\/request$/)[0];
    expect(JSON.parse(String(init.body))).toEqual({ dashboard_ids: [16] });
    expect(calls(f, "POST", /\/dashboard-grouping\/apply$/)).toEqual([]);
    expect(screen.queryByRole("button", { name: "Rename now" })).toBeNull();

    cleanup();
    decideCollection("approved");
    const { container } = renderAt(`/w/${WS}/outputs?type=dashboard`);
    await openGrouping();
    fireEvent.click(await screen.findByRole("button", { name: "Rename now" }));
    expect(await screen.findByText("Renamed 1 dashboard.")).toBeTruthy();
    expect(await screen.findByText(/All 2 published dashboards are already grouped under “Demo”/)).toBeTruthy();
    expect(calls(f, "POST", /\/dashboard-grouping\/apply$/)).toHaveLength(1);
    expect(await axeClean(container)).toEqual([]);
  });
});
