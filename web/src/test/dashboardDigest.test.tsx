/**
 * PILOT-FIX-07: "Send a summary of this dashboard" attaches the dashboard's digest to a schedule from a screen. The
 * control edits only `config.digest_dashboards`, keeps every other config key (PATCH replaces the whole config), says
 * where the summary goes, and a viewer can read it but not change it. The schedule form has the same choice.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, type BuilderDashboard, type Schedule } from "../api";
import { DashboardDigest } from "../components/DashboardDigest";
import { ScheduleForm } from "../pages/Schedules";
import { buildScheduleConfig, digestDashboards, emptyScheduleForm, formFromSchedule, mergeScheduleConfig, withDigest } from "../lib/schedules";
import { bodyOf, calls, mockFetch } from "./harness";
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
});

const dash = { id: "art_d", workspace_id: WS, name: "dashboard:ops", title: "Ops", version: 2, status: "draft", platform: null,
  external_url: null, content_hash: "h", updated_at: null,
  document: { id: "ops", title: "Ops", layout: { items: [] }, charts: [], kpis: [], text_blocks: [], filters: [] } } as unknown as BuilderDashboard;

describe("digest config helpers", () => {
  it("adds and removes one dashboard and keeps every other key", () => {
    const config = { deliver_to: ["dst_1"], kind: "executive", digest_dashboards: ["art_a"] };
    expect(withDigest(config, "art_d", true)).toEqual({ deliver_to: ["dst_1"], kind: "executive", digest_dashboards: ["art_a", "art_d"] });
    expect(withDigest(config, "art_a", true)).toEqual(config);  // already there: unchanged
    expect(withDigest(config, "art_a", false)).toEqual({ deliver_to: ["dst_1"], kind: "executive" });  // empty list: no key
    expect(digestDashboards({ digest_dashboards: ["art_a", 3 as unknown as string] })).toEqual(["art_a"]);
    expect(digestDashboards(undefined)).toEqual([]);
  });

  it("round-trips through the schedule form, and unticking every dashboard removes the key", () => {
    const s = { id: "sch_1", workspace_id: "ws", name: "Monday", kind: "monitor", cron: "0 7 * * 1", timezone: "UTC",
      config: { digest_dashboards: ["art_d"], deliver_to: ["dst_1"] }, enabled: true, owner_id: "u1", next_run_at: null,
      last_run_at: null, created_at: "" } as Schedule;
    const f = formFromSchedule(s);
    expect(f.digestDashboards).toEqual(["art_d"]);
    expect(buildScheduleConfig(f)).toEqual({ digest_dashboards: ["art_d"] });
    expect(mergeScheduleConfig(s.config, buildScheduleConfig({ ...f, digestDashboards: [] }))).toEqual({ deliver_to: ["dst_1"] });
    expect(buildScheduleConfig(emptyScheduleForm("UTC")).digest_dashboards).toBeUndefined();
  });
});

describe("DashboardDigest", () => {
  it("sends the summary with a chosen schedule, says where it goes, and stops on request", async () => {
    const f = mockFetch();
    render(<MemoryRouter><DashboardDigest dashboard={dash} canEdit /></MemoryRouter>);
    const region = await screen.findByRole("region", { name: "Send a summary of this dashboard" });
    const select = await within(region).findByLabelText("Schedule");
    fireEvent.change(select, { target: { value: "sch_1" } });
    fireEvent.click(within(region).getByRole("button", { name: "Add to schedule" }));
    expect(await within(region).findByText(/Each time “Weekly re-analysis” runs, a summary of this dashboard is saved under Reports; it has no delivery destination, so nothing is sent/)).toBeTruthy();
    const patches = calls(f, "PATCH", /\/api\/schedules\/sch_1$/);
    expect(patches).toHaveLength(1);
    expect(bodyOf(patches[0][1])).toEqual({ config: { digest_dashboards: ["art_d"] } });
    const stop = await within(region).findByRole("button", { name: "Stop sending with Weekly re-analysis" });
    fireEvent.click(stop);
    await waitFor(() => expect(within(region).getByText("“Weekly re-analysis” no longer sends a summary of this dashboard.")).toBeTruthy());
    expect(bodyOf(calls(f, "PATCH", /\/api\/schedules\/sch_1$/)[1][1])).toEqual({ config: {} });
  });

  it("keeps the schedule's other settings, read fresh before the edit", async () => {
    const f = mockFetch((method, path) => (method === "GET" && path === `/api/workspaces/${WS}/schedules`
      ? { status: 200, body: [{ id: "sch_1", workspace_id: WS, name: "Monday delivery digest", kind: "report", cron: "0 7 * * 1",
        timezone: "UTC", config: { kind: "executive", formats: ["pdf"], deliver_to: ["dst_mail"] }, enabled: true, owner_id: USER.id,
        next_run_at: null, last_run_at: null, created_at: "" }] }
      : null));
    render(<MemoryRouter><DashboardDigest dashboard={dash} canEdit /></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Schedule"), { target: { value: "sch_1" } });
    fireEvent.click(screen.getByRole("button", { name: "Add to schedule" }));
    await waitFor(() => expect(calls(f, "PATCH", /\/api\/schedules\/sch_1$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "PATCH", /\/api\/schedules\/sch_1$/)[0][1])).toEqual(
      { config: { kind: "executive", formats: ["pdf"], deliver_to: ["dst_mail"], digest_dashboards: ["art_d"] } });
  });

  it("shows a viewer which schedules send it, without controls", async () => {
    mockFetch((method, path) => (method === "GET" && path === `/api/workspaces/${WS}/schedules`
      ? { status: 200, body: [{ id: "sch_1", workspace_id: WS, name: "Monday delivery digest", kind: "report", cron: "0 7 * * 1",
        timezone: "UTC", config: { deliver_to: ["dst_mail"], digest_dashboards: ["art_d"] }, enabled: true, owner_id: USER.id,
        next_run_at: null, last_run_at: null, created_at: "" }] }
      : null));
    render(<MemoryRouter><DashboardDigest dashboard={dash} canEdit={false} /></MemoryRouter>);
    expect(await screen.findByText(/sent to its 1 delivery destination \(each sends only while its approval stands\)/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Stop sending/ })).toBeNull();
    expect(screen.queryByLabelText("Schedule")).toBeNull();
  });
});

describe("ScheduleForm", () => {
  it("offers the workspace's dashboards and sends the ticked ones as digest_dashboards", async () => {
    const f = mockFetch((method, path) => {
      if (method === "GET" && path === `/api/v2/workspaces/${WS}/dashboards`) {
        return { status: 200, body: [{ id: "art_d", workspace_id: WS, name: "dashboard:ops", title: "Incident operations", version: 2,
          status: "draft", platform: null, external_url: null, content_hash: "h", updated_at: null }] };
      }
      if (method === "POST" && path === `/api/workspaces/${WS}/schedules`) return { status: 201, body: { id: "sch_new" } };
      return null;
    });
    render(<MemoryRouter><ScheduleForm wsId={WS} onSaved={() => undefined} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Monday delivery digest" } });
    fireEvent.change(screen.getByLabelText(/Time zone/), { target: { value: "UTC" } });
    const group = await screen.findByRole("group", { name: "Send a summary of these dashboards" });
    fireEvent.click(within(group).getByRole("checkbox", { name: "Incident operations" }));
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    await waitFor(() => expect(calls(f, "POST", /\/schedules$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "POST", /\/schedules$/)[0][1]).config).toMatchObject({ digest_dashboards: ["art_d"] });
  });
});
