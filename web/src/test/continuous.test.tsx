import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { session, type Alert, type AppNotification, type Artifact, type RunChanges, type Schedule } from "../api";
import { ChangesPanel, deltaArrow } from "../components/ChangesPanel";
import { NotificationBell } from "../components/NotificationBell";
import { AlertItem } from "../pages/Monitoring";
import { ReportRow } from "../pages/Reports";
import { ScheduleForm } from "../pages/Schedules";
import { OriginBadge } from "../pages/RunView";
import { buildMonitorOption, driftBaseline } from "../lib/charts";
import { buildMonitorConfig, emptyMonitorForm, monitorOverlay, validateMonitorForm } from "../lib/monitors";
import { notificationHref } from "../lib/notifications";
import {
  CRON_PRESETS, buildScheduleConfig, buildScheduleInput, cronForPreset, describeCron, emptyScheduleForm, expandMinutes, formFromSchedule,
  mergeScheduleConfig, presetForCron, validateCron, validateScheduleForm,
} from "../lib/schedules";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const USER = { id: "u1", email: "a@b", name: "A", is_admin: false, active: true, attributes: {}, created_at: "" };

function lastCall(fetchMock: ReturnType<typeof vi.spyOn>, match: (url: string, init: RequestInit) => boolean) {
  const calls = fetchMock.mock.calls as [string, RequestInit][];
  return [...calls].reverse().find(([u, i]) => match(String(u), i ?? {}));
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="location">{loc.pathname + loc.search}</div>;
}

beforeEach(() => session.set("tok123", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  session.clear();
});

// ------------------------------------------------------------------------------------ schedule helpers
describe("cron presets", () => {
  it("maps presets to cron and back", () => {
    expect(cronForPreset("weekly_monday_0700")).toBe("0 7 * * 1");
    expect(cronForPreset("daily_0600")).toBe("0 6 * * *");
    expect(cronForPreset("monthly_1st_0700")).toBe("0 7 1 * *");
    expect(cronForPreset("nope")).toBeNull();
    for (const p of CRON_PRESETS) expect(presetForCron(`  ${p.cron.replace(/ /g, "   ")} `)).toBe(p.id);
    expect(presetForCron("30 7 * * 1")).toBe("custom");
  });

  it("describes common cron shapes", () => {
    expect(describeCron("0 7 * * 1")).toBe("Every Monday at 07:00");
    expect(describeCron("0 6 * * *")).toBe("Every day at 06:00");
    expect(describeCron("0 7 1 * *")).toBe("Monthly on the 1st at 07:00");
    expect(describeCron("15 9 * * 1-5")).toBe("Every weekday at 09:15");
    expect(describeCron("5 * * * *")).toBe("Every hour at :05");
    expect(describeCron("0 0 1 1 *")).toContain("cron");
  });

  it("rejects malformed and too-frequent cron expressions", () => {
    expect(validateCron("")).toMatch(/required/);
    expect(validateCron("0 7 * *")).toMatch(/5 fields/);
    expect(validateCron("*/5 * * * *")).toMatch(/15 minutes/);
    expect(validateCron("* 7 * * *")).toMatch(/15 minutes/);
    expect(validateCron("0,10 * * * *")).toMatch(/15 minutes/);
    expect(validateCron("*/15 * * * *")).toBeNull();
    expect(validateCron("0 7 * * 1")).toBeNull();
    expect(expandMinutes("0-30/10")).toEqual([0, 10, 20, 30]);
  });
});

describe("schedule form validation and config", () => {
  it("requires a name, a valid cron, a known time zone and a format when reporting", () => {
    const f = { ...emptyScheduleForm("UTC"), name: " ", cron: "*/5 * * * *", timezone: "Mars/Olympus", formats: [] };
    const errs = validateScheduleForm(f);
    expect(Object.keys(errs).sort()).toEqual(["cron", "formats", "name", "timezone"]);
    expect(validateScheduleForm({ ...emptyScheduleForm("Europe/Berlin"), name: "Weekly" })).toEqual({});
    // no report requested: formats are irrelevant
    expect(validateScheduleForm({ ...emptyScheduleForm("UTC"), name: "x", includeReport: false, formats: [] })).toEqual({});
  });

  it("builds kind-specific config", () => {
    const base = { ...emptyScheduleForm("UTC"), name: "Weekly" };
    expect(buildScheduleConfig({ ...base, objective: "  MTTR  ", publish: "propose" })).toEqual({
      refresh_first: true, publish: "propose", objective: "MTTR", report: { kind: "weekly_summary", formats: ["html", "pdf", "xlsx"] },
    });
    expect(buildScheduleConfig({ ...base, includeReport: false }).report).toBeNull();
    expect(buildScheduleConfig({ ...base, kind: "report", reportKind: "executive", formats: ["pdf"], runId: "run_1" }))
      .toEqual({ kind: "executive", formats: ["pdf"], run_id: "run_1" });
    expect(buildScheduleConfig({ ...base, kind: "monitor" })).toEqual({});
    expect(buildScheduleConfig({ ...base, kind: "monitor", monitorIds: ["mon_1"] })).toEqual({ monitor_ids: ["mon_1"] });
    expect(buildScheduleConfig({ ...base, kind: "dataset_refresh", sourceIds: ["src_1"] })).toEqual({ source_ids: ["src_1"] });
    expect(buildScheduleInput({ ...base, cron: " 0  7 * * 1 " }).cron).toBe("0 7 * * 1");
  });

  it("round-trips an existing schedule into the form", () => {
    const s = {
      id: "sch_1", workspace_id: "ws", name: "Weekly", kind: "reanalysis", cron: "0 6 * * *", timezone: "Europe/Berlin",
      config: { refresh_first: false, publish: "propose", report: null }, enabled: true, owner_id: "u1", next_run_at: null,
      last_run_at: null, created_at: "",
    } as Schedule;
    const f = formFromSchedule(s);
    expect(f.preset).toBe("daily_0600");
    expect(f.refreshFirst).toBe(false);
    expect(f.includeReport).toBe(false);
    expect(buildScheduleConfig(f)).toEqual({ refresh_first: false, publish: "propose", report: null });
  });

  it("keeps backend-managed config keys when editing", () => {
    const original = { baseline_run_id: "run_0", run_id: "run_old", kind: "executive", formats: ["pdf"] };
    expect(mergeScheduleConfig(original, { kind: "statistical", formats: ["html"] }))
      .toEqual({ baseline_run_id: "run_0", kind: "statistical", formats: ["html"] });
  });
});

describe("ScheduleForm", () => {
  it("applies a preset, blocks invalid input client-side, and shows the server's validation error", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      if (String(url).endsWith("/schedules") && init?.method === "POST") {
        return jsonResponse({ error: { code: "invalid_input", message: "schedules may not fire more often than every 15 minutes", details: {} } }, 422);
      }
      return jsonResponse([]);
    });
    const onSaved = vi.fn();
    render(<MemoryRouter><ScheduleForm wsId="ws_1" onSaved={onSaved} /></MemoryRouter>);
    const cron = screen.getByLabelText("Cron expression") as HTMLInputElement;
    expect(cron.value).toBe("0 7 * * 1");
    fireEvent.change(screen.getByLabelText("Frequency"), { target: { value: "monthly_1st_0700" } });
    expect(cron.value).toBe("0 7 1 * *");
    expect(screen.getByText(/Monthly on the 1st at 07:00/)).toBeTruthy();
    fireEvent.change(cron, { target: { value: "30 7 * * 1" } });
    expect((screen.getByLabelText("Frequency") as HTMLSelectElement).value).toBe("custom");

    // client-side: missing name blocks the request
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    expect(await screen.findByText("Name is required.")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([, i]) => (i as RequestInit | undefined)?.method === "POST")).toBe(false);

    // valid locally -> server rejects -> message shown
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Weekly review" } });
    fireEvent.change(screen.getByLabelText(/Time zone/), { target: { value: "UTC" } });
    fireEvent.click(screen.getByRole("button", { name: "Create schedule" }));
    expect(await screen.findByText("schedules may not fire more often than every 15 minutes")).toBeTruthy();
    const post = lastCall(fetchMock, (_u, i) => i.method === "POST")!;
    expect(post[0]).toBe("/api/workspaces/ws_1/schedules");
    const body = JSON.parse(String(post[1].body));
    expect(body).toMatchObject({ name: "Weekly review", kind: "reanalysis", cron: "30 7 * * 1", timezone: "UTC" });
    expect(body.config).toMatchObject({ refresh_first: true, publish: "skip", report: { kind: "weekly_summary" } });
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("shows kind-specific fields for report schedules", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => jsonResponse(String(url).includes("/dashboards") ? [] : [
      { id: "run_ok", status: "COMPLETED", objective: "Incidents", created_at: "2026-01-01T00:00:00Z", finished_at: "2026-01-01T01:00:00Z" },
      { id: "run_bad", status: "FAILED", objective: "Broken", created_at: "2026-01-01T00:00:00Z", finished_at: null },
    ]));
    render(<MemoryRouter><ScheduleForm wsId="ws_1" onSaved={() => undefined} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "report" } });
    const run = await screen.findByLabelText("Investigation");
    await waitFor(() => expect(within(run).getAllByRole("option")).toHaveLength(2)); // latest + the one completed run
    expect((screen.getByLabelText("Report kind") as HTMLSelectElement).value).toBe("executive");
    expect(screen.queryByLabelText(/Objective/)).toBeNull();
  });
});

// ------------------------------------------------------------------------------------ notifications
describe("NotificationBell", () => {
  const notes: AppNotification[] = [
    { id: 1, workspace_id: "ws_1", user_id: null, kind: "alert", title: "[critical] MTTR drift", body: "", link: { type: "alert", id: "alr_1" },
      read_by: [], read: false, created_at: "2026-09-01T00:00:00Z" },
    { id: 2, workspace_id: "ws_1", user_id: null, kind: "report", title: "Report ready", body: "", link: { type: "artifact", id: "art_1" },
      read_by: [], read: false, created_at: "2026-09-01T00:00:00Z" },
  ];

  it("shows the unread count, polls every 30 s, and marks all read", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let unread = [notes[0]];
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const u = String(url);
      if (u === "/api/notifications/read") {
        const ids = JSON.parse(String(init?.body)).ids as number[];
        unread = unread.filter((n) => !ids.includes(n.id));
        return jsonResponse({ marked: ids.length });
      }
      if (u === "/api/notifications?unread=true") return jsonResponse(unread);
      return jsonResponse(notes.map((n) => ({ ...n, read: !unread.some((x) => x.id === n.id) })));
    });
    render(<MemoryRouter><NotificationBell /></MemoryRouter>);
    expect((await screen.findByTestId("bell-count")).textContent).toBe("1");
    const firstCall = fetchMock.mock.calls[0];
    expect(((firstCall[1] as RequestInit).headers as Record<string, string>).Authorization).toBe("Bearer tok123");

    unread = [...notes];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    await waitFor(() => expect(screen.getByTestId("bell-count").textContent).toBe("2"));
    expect(fetchMock.mock.calls.filter(([u]) => u === "/api/notifications?unread=true").length).toBeGreaterThanOrEqual(2);

    fireEvent.click(screen.getByRole("button", { name: /Notifications, 2 unread/ }));
    expect(await screen.findByText("Report ready")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Mark all read" }));
    await waitFor(() => expect(screen.queryByTestId("bell-count")).toBeNull());
    const post = lastCall(fetchMock, (u) => u === "/api/notifications/read")!;
    expect(post[1].method).toBe("POST");
    expect(JSON.parse(String(post[1].body))).toEqual({ ids: [1, 2] });
  });

  it("marks one read and navigates to its link", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      String(url) === "/api/notifications/read" ? jsonResponse({ marked: 1 }) : jsonResponse(notes));
    render(
      <MemoryRouter initialEntries={["/"]}>
        <NotificationBell />
        <Routes><Route path="*" element={<LocationProbe />} /></Routes>
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByRole("button", { name: /Notifications, 2 unread/ }));
    fireEvent.click(await screen.findByText("[critical] MTTR drift"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/w/ws_1/operate/autonomy?view=monitors&tab=alerts&alert=alr_1"));
    const post = lastCall(fetchMock, (u) => u === "/api/notifications/read")!;
    expect(JSON.parse(String(post[1].body))).toEqual({ ids: [1] });
  });

  it("maps notification links to screens", () => {
    const n = (type: string, id: string, kind = "x") => ({ workspace_id: "ws", kind, link: { type, id } });
    expect(notificationHref(n("alert", "a1"))).toBe("/w/ws/operate/autonomy?view=monitors&tab=alerts&alert=a1");
    expect(notificationHref(n("artifact", "r1", "report"))).toBe("/w/ws/outputs?type=report&artifact=r1");
    expect(notificationHref(n("artifact", "c1", "other"))).toBe("/w/ws/outputs?artifact=c1");
    expect(notificationHref(n("run", "run1"))).toBe("/w/ws/work/investigations/run1");
    expect(notificationHref(n("schedule", "s1"))).toBe("/w/ws/operate/autonomy?view=schedules&schedule=s1");
  });
});

// ------------------------------------------------------------------------------------ run changes
describe("ChangesPanel", () => {
  const changes: RunChanges = {
    previous_run_id: "run_prev",
    new: [{ code: "F3", title: "Network backlog grew", finding: "P1 backlog up", id: "ins_3" }],
    persisting: [{ code: "F1", title: "Weekend MTTR", id: "ins_1", effect: 0.3, previous_effect: 0.31 }],
    changed: [{ code: "F2", title: "Reassignment effect", id: "ins_2", effect: 0.5, previous_effect: 0.2 }],
    resolved: [],
    metrics: [
      { name: "mttr_hours", value: 12, previous_value: 10, pct_change: 0.2 },
      { name: "backlog", value: 80, previous_value: 100, pct_change: -0.2 },
      { name: "new_kpi", value: 3, previous_value: null, pct_change: null },
    ],
  };

  it("renders the four groups, KPI deltas with arrows and links", () => {
    render(<MemoryRouter><ChangesPanel changes={changes} wsId="ws_1" reportArtifactId="art_9" /></MemoryRouter>);
    expect(screen.getByText("New: 1")).toBeTruthy();
    expect(screen.getByText("Persisting: 1")).toBeTruthy();
    expect(screen.getByText("Changed: 1")).toBeTruthy();
    expect(screen.getByText("Resolved: 0")).toBeTruthy();
    const newGroup = screen.getByRole("region", { name: "New findings" });
    expect(within(newGroup).getByRole("link").getAttribute("href")).toBe("/w/ws_1/outputs/findings/ins_3");
    const changed = screen.getByRole("region", { name: "Changed findings" });
    expect(changed.textContent).toMatch(/effect 0\.2 → 0\.5/);
    expect(within(screen.getByRole("region", { name: "Resolved findings" })).getByText("None.")).toBeTruthy();
    expect(screen.getByLabelText("up 20.0%").textContent).toContain("▲");
    expect(screen.getByLabelText("down -20.0%").textContent).toContain("▼");
    expect(screen.getByRole("link", { name: "Generated report" }).getAttribute("href")).toBe("/w/ws_1/outputs?type=report&artifact=art_9");
    expect(screen.getByRole("link", { name: "Previous investigation" }).getAttribute("href")).toBe("/w/ws_1/work/investigations/run_prev");
  });

  it("delta arrows", () => {
    expect(deltaArrow(null)).toBeNull();
    expect(deltaArrow(0)?.dir).toBe("flat");
    expect(deltaArrow(0.1)?.arrow).toBe("▲");
    expect(deltaArrow(-0.1)?.arrow).toBe("▼");
  });

  it("origin badge links scheduled runs and alert investigations", () => {
    const { rerender } = render(<MemoryRouter><OriginBadge wsId="ws" origin={{ type: "schedule", schedule_id: "sch_1", previous_run_id: "r0" }} /></MemoryRouter>);
    expect(screen.getByText("scheduled")).toBeTruthy();
    expect(screen.getByRole("link", { name: "schedule" }).getAttribute("href")).toBe("/w/ws/operate/autonomy?view=schedules&schedule=sch_1");
    rerender(<MemoryRouter><OriginBadge wsId="ws" origin={{ type: "alert", alert_id: "alr_1" }} /></MemoryRouter>);
    expect(screen.getByText("alert investigation")).toBeTruthy();
    expect(screen.getByRole("link", { name: "alert" }).getAttribute("href")).toBe("/w/ws/operate/autonomy?view=monitors&tab=alerts&alert=alr_1");
    rerender(<MemoryRouter><OriginBadge wsId="ws" origin={{ type: "user" }} /></MemoryRouter>);
    expect(screen.queryByText("scheduled")).toBeNull();
  });
});

// ------------------------------------------------------------------------------------ alerts
describe("AlertItem", () => {
  const alert: Alert = {
    id: "alr_1", workspace_id: "ws_1", monitor_id: "mon_1", severity: "critical", title: "MTTR drift", message: "MTTR was 14 vs median 10",
    data: { triage: { p_material: 0.87, model: "typesafe" } }, dedupe_key: "k", status: "open", investigation_run_id: null,
    acknowledged_by: null, created_at: "2026-09-01T00:00:00Z", resolved_at: null,
  };

  it("shows severity and triage, and calls acknowledge / resolve endpoints", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) =>
      jsonResponse({ ...alert, status: String(url).endsWith("/acknowledge") ? "acknowledged" : "resolved" }));
    const onChanged = vi.fn();
    render(<MemoryRouter><AlertItem wsId="ws_1" alert={alert} monitorName="MTTR" onChanged={onChanged} /></MemoryRouter>);
    expect(screen.getByText("critical").className).toContain("tag-danger");
    expect(screen.getByText("p(material) 87%")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(expect.objectContaining({ status: "acknowledged" })));
    let [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/alerts/alr_1/acknowledge");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok123");
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    [url] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/alerts/alr_1/resolve");
  });

  it("starts an investigation and navigates to its run", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ run_id: "run_42" }));
    render(
      <MemoryRouter initialEntries={["/w/ws_1/monitoring"]}>
        <AlertItem wsId="ws_1" alert={alert} onChanged={() => undefined} />
        <Routes><Route path="*" element={<LocationProbe />} /></Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Investigate" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/w/ws_1/work/investigations/run_42"));
    expect(fetchMock.mock.calls[0][0]).toBe("/api/alerts/alr_1/investigate");
  });

  it("hides actions that no longer apply and links the investigation", () => {
    render(<MemoryRouter><AlertItem wsId="ws_1" alert={{ ...alert, status: "resolved", investigation_run_id: "run_7" }} onChanged={() => undefined} /></MemoryRouter>);
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Investigate" })).toBeNull();
    expect(screen.getByRole("link", { name: "Investigation" }).getAttribute("href")).toBe("/w/ws_1/work/investigations/run_7");
  });
});

// ------------------------------------------------------------------------------------ monitors
describe("monitor helpers", () => {
  it("validates and builds kind-specific config", () => {
    const f = { ...emptyMonitorForm(), name: "x", kind: "metric_threshold" as const, metric: "mttr", value: "abc" };
    expect(validateMonitorForm(f).value).toBeTruthy();
    expect(buildMonitorConfig({ ...f, value: "12.5", op: ">=" })).toEqual({ metric: "mttr", grain: "week", op: ">=", value: 12.5, severity: "warning" });
    expect(buildMonitorConfig({ ...f, kind: "metric_drift" })).toEqual({ metric: "mttr", grain: "week", lookback: 8, z_threshold: 3 });
    expect(buildMonitorConfig({ ...f, kind: "change_point" })).toEqual({ metric: "mttr", grain: "week", recent_periods: 4 });
    expect(buildMonitorConfig({ ...f, kind: "data_quality", assets: ["s.a"] })).toEqual({ assets: ["s.a"] });
    expect(validateMonitorForm({ ...emptyMonitorForm(), name: "dq", kind: "data_quality" })).toEqual({});
  });

  it("computes the drift baseline and marks the latest point", () => {
    const pts: [string, number][] = [["w1", 10], ["w2", 12], ["w3", 11], ["w4", 13], ["w5", 30]];
    expect(driftBaseline(pts, 8)).toBe(11.5);
    const m = { kind: "metric_drift", state: "alerting", config: { lookback: 8 }, last_result: {} } as never;
    const overlay = monitorOverlay(m, pts);
    expect(overlay).toEqual({ alerting: true, baselineMedian: 11.5 });
    const evaluated = (period: string) => ({ kind: "metric_drift", state: "ok", config: {}, last_result: { period, baseline_median: 99 } }) as never;
    expect(monitorOverlay(evaluated("w5"), pts).baselineMedian).toBe(99); // evaluator saw the same latest period
    expect(monitorOverlay(evaluated("w9"), pts).baselineMedian).toBe(11.5); // stale result: recompute from the series
    const opt = buildMonitorOption(pts, overlay) as { series: { markPoint: { data: { coord: unknown[] }[] }; markLine: { data: { yAxis: number }[] } }[] };
    expect(opt.series[0].markPoint.data[0].coord).toEqual(["w5", 30]);
    expect(opt.series[0].markLine.data[0].yAxis).toBe(11.5);
  });
});

// ------------------------------------------------------------------------------------ reports
describe("ReportRow", () => {
  const report: Artifact = {
    id: "art_1", workspace_id: "ws_1", run_id: "run_1", type: "report", name: "executive report", version: 1, status: "final",
    platform: null, external_id: null, external_url: null, creator_agent: null, creator_user: "u1",
    content: { kind: "executive", title: "Weekly incidents", files: { pdf: { sha256: "x", bytes: 3, mime: "application/pdf", ext: "pdf" },
      html: { sha256: "y", bytes: 3, mime: "text/html", ext: "html" } } },
    content_hash: "h", created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-01T00:00:00Z",
  };

  it("downloads with the bearer token and saves via a blob URL", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(new TextEncoder().encode("%PDF-1.4"), {
      status: 200, headers: { "Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="executive_report.pdf"' },
    }));
    const createObjectURL = vi.fn(() => "blob:mock-1");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const clicks: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      clicks.push(this);
    });
    render(<MemoryRouter><ReportRow wsId="ws_1" report={report} /></MemoryRouter>);
    expect(screen.queryByRole("button", { name: "Download Excel" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Download PDF" }));
    await waitFor(() => expect(clicks).toHaveLength(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/artifacts/art_1/download?format=pdf");
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok123");
    // fetch's Blob (undici) is not jsdom's Blob class, so check the shape rather than instanceof.
    const blob = (createObjectURL.mock.calls[0] as unknown[])[0] as Blob;
    expect(Object.prototype.toString.call(blob)).toBe("[object Blob]");
    expect(await blob.text()).toBe("%PDF-1.4");
    expect(clicks[0].getAttribute("href")).toBe("blob:mock-1");
    expect(clicks[0].download).toBe("executive_report.pdf");
    expect(document.body.contains(clicks[0])).toBe(false);
  });

  it("previews HTML only inside a sandboxed iframe", async () => {
    const html = "<html><body><h1 id='inj'>Report</h1><script>window.pwned=1</script></body></html>";
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(html, { status: 200, headers: { "Content-Type": "text/html" } }));
    const { container } = render(<MemoryRouter><ReportRow wsId="ws_1" report={report} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    const frame = await screen.findByTitle("Preview: Weekly incidents");
    expect(frame.tagName).toBe("IFRAME");
    expect(frame.getAttribute("sandbox")).toBe("");
    expect(frame.getAttribute("srcdoc")).toBe(html);
    expect(container.querySelector("#inj")).toBeNull();
  });

  it("surfaces a download error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ error: { code: "not_found", message: "format pdf not generated for this report", details: {} } }, 404));
    render(<MemoryRouter><ReportRow wsId="ws_1" report={report} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Download PDF" }));
    expect(await screen.findByText("format pdf not generated for this report")).toBeTruthy();
  });
});
