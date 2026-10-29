import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, matchPath, Route, Routes, useLocation } from "react-router-dom";
import axe from "axe-core";
import { session } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { filterItems, screenItems } from "../components/CommandPalette";
import { ThemeToggle } from "../components/Layout";
import { ConfidenceBar, Stat, StateView, Value } from "../components/ui";
import { fmtMs, fmtUsd, formatKnown } from "../lib/format";
import { getThemePref, nextThemePref, resolveTheme, setThemePref } from "../lib/theme";
import { AREAS, fillPath, LEGACY_REDIRECTS, legacyTarget, SCREEN_BUDGET, SCREENS, to } from "../routes";
import { INSIGHT, mockBackend, RUN, USER, WS } from "./mockBackend";

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend(init?.method ?? "GET", String(input), typeof init?.body === "string" ? init.body : null);
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

beforeEach(() => session.set("mock-token", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
  setThemePref("system");
});

// ------------------------------------------------------------------------------------ information architecture
describe("route manifest (spec v4 §15)", () => {
  it(`has at most ${SCREEN_BUDGET} screens, each with a unique id and path`, () => {
    expect(SCREEN_BUDGET).toBe(20);
    expect(SCREENS.length).toBeLessThanOrEqual(SCREEN_BUDGET);
    expect(new Set(SCREENS.map((s) => s.id)).size).toBe(SCREENS.length);
    expect(new Set(SCREENS.map((s) => s.path)).size).toBe(SCREENS.length);
  });

  it("has five areas and the gear, each with at least one navigable screen, in the spec's order", () => {
    expect(AREAS.map((a) => a.label)).toEqual(["Overview", "Data", "Analyze", "Results", "Approvals & automation", "Settings"]);
    for (const a of AREAS) expect(SCREENS.some((s) => s.area === a.id && s.nav)).toBe(true);
  });

  it("puts each concept in its area: metrics in Data, dashboards in Outputs, model configuration in Settings", () => {
    const home = (c: string) => SCREENS.find((s) => s.owns.includes(c as never));
    expect(home("metric")?.area).toBe("data");
    expect(home("relationship")?.area).toBe("data");
    expect(home("definition")?.area).toBe("data");
    expect(home("dashboard")?.area).toBe("outputs");
    expect(home("report")?.area).toBe("outputs");
    expect(home("recipe")?.area).toBe("work");
    expect(home("model-config")?.area).toBe("settings");
    expect(home("autonomy")?.id).toBe("policy");
    expect(home("audit")?.id).toBe("policy");
    expect(home("schedule")?.area).toBe("operate");
  });

  it("maps every legacy route (and every legacy tab) onto a screen in the manifest", () => {
    for (const r of LEGACY_REDIRECTS) {
      for (const target of [r.to, ...Object.values(r.tabs ?? {})]) {
        const filled = fillPath(target.split("?")[0], { wsId: "ws", runId: "r", insightId: "i" });
        expect(SCREENS.some((s) => matchPath(s.path, filled)), `${r.from} -> ${target}`).toBe(true);
      }
    }
  });

  it.each([
    ["/admin", "/settings/registry"],
    ["/operate/registry", "/settings/registry"],
    ["/operate/settings", "/settings/platform"],
    ["/operate/usage?tab=savings", "/settings/usage?tab=savings"],
    ["/w/ws_1/sources", "/w/ws_1/data/sources"],
    ["/w/ws_1/knowledge/sources", "/w/ws_1/data/sources"],
    ["/w/ws_1/catalog", "/w/ws_1/data/catalog"],
    ["/w/ws_1/knowledge/catalog?tab=review", "/w/ws_1/data/catalog?tab=review"],
    ["/w/ws_1/knowledge/catalog?tab=graph", "/w/ws_1/data/catalog?tab=definitions"],
    ["/w/ws_1/runs", "/w/ws_1/work"],
    ["/w/ws_1/ask", "/w/ws_1/work/ask"],
    ["/w/ws_1/investigate", "/w/ws_1/work"],
    ["/w/ws_1/runs/run_9", "/w/ws_1/work/investigations/run_9"],
    ["/w/ws_1/investigate/run_9/console", "/w/ws_1/work/investigations/run_9/console"],
    ["/w/ws_1/insights/ins_2", "/w/ws_1/outputs/findings/ins_2"],
    ["/w/ws_1/investigate/findings/ins_2", "/w/ws_1/outputs/findings/ins_2"],
    ["/w/ws_1/studio?artifact=art_1", "/w/ws_1/outputs?artifact=art_1"],
    ["/w/ws_1/build/studio?tab=builds&job=bj_1", "/w/ws_1/work?tab=builds&job=bj_1"],
    ["/w/ws_1/build/studio?tab=kpis&kpi=mttr", "/w/ws_1/data/catalog?tab=metrics&kpi=mttr"],
    ["/w/ws_1/build/studio?tab=dashboards&dashboard=art_d", "/w/ws_1/outputs?type=dashboard&dashboard=art_d"],
    ["/w/ws_1/reports?artifact=art_2", "/w/ws_1/outputs?type=report&artifact=art_2"],
    ["/w/ws_1/build/reports?artifact=art_2", "/w/ws_1/outputs?type=report&artifact=art_2"],
    ["/w/ws_1/schedules?schedule=sch_1", "/w/ws_1/operate/autonomy?view=schedules&schedule=sch_1"],
    ["/w/ws_1/operate/schedules?schedule=sch_1", "/w/ws_1/operate/autonomy?view=schedules&schedule=sch_1"],
    ["/w/ws_1/monitoring?tab=alerts&alert=alr_1", "/w/ws_1/operate/autonomy?view=monitors&tab=alerts&alert=alr_1"],
    ["/w/ws_1/operate/monitoring?tab=alerts&alert=alr_1", "/w/ws_1/operate/autonomy?view=monitors&tab=alerts&alert=alr_1"],
    ["/w/ws_1/governance", "/w/ws_1/settings/policy"],
    ["/w/ws_1/operate/governance", "/w/ws_1/settings/policy"],
  ])("redirects %s to %s, keeping the query", async (from, expected) => {
    mockFetch();
    renderAt(from);
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(expected));
  });

  it("builds links with encoded parameters and optional segments", () => {
    expect(to.findings("ws 1")).toBe("/w/ws%201/outputs/findings");
    expect(to.findings("ws", "ins_1")).toBe("/w/ws/outputs/findings/ins_1");
    expect(to.monitoring("ws", { tab: "alerts" })).toBe("/w/ws/operate/autonomy?view=monitors&tab=alerts");
    expect(to.schedules("ws", "s1")).toBe("/w/ws/operate/autonomy?view=schedules&schedule=s1");
    expect(to.autonomy("ws")).toBe("/w/ws/operate/autonomy");
    expect(to.autonomy("ws", "goals")).toBe("/w/ws/operate/autonomy?view=goals");
    expect(to.settings()).toBe("/settings/platform");
    expect(to.outputs("ws", { type: "dashboard" })).toBe("/w/ws/outputs?type=dashboard");
    expect(to.work("ws", "prepare")).toBe("/w/ws/work?tab=prepare");
    expect(legacyTarget({ from: "/x", to: "/y?type=report" }, {}, "?type=chart&artifact=a")).toBe("/y?type=report&artifact=a");
  });

  it("groups the side nav by area, with the gear last", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    const nav = screen.getByRole("navigation", { name: "Main" });
    await waitFor(() => expect(within(nav).getByRole("group", { name: /Settings/ })).toBeTruthy());
    const groups = within(nav).getAllByRole("group").map((g) => g.getAttribute("aria-labelledby"));
    expect(groups).toEqual(["nav-a-overview", "nav-a-data", "nav-a-work", "nav-a-outputs", "nav-a-operate", "nav-a-settings"]);
    expect(within(within(nav).getByRole("group", { name: "Data" })).getByRole("link", { name: "Data dictionary" }).getAttribute("href"))
      .toBe(`/w/${WS}/data/catalog`);
    await screen.findByRole("heading", { name: "IT Service Management", level: 1 });
  });
});

// ------------------------------------------------------------------------------------ state families
describe("unknown is never shown as 0", () => {
  it("renders absent values as an explicit unknown state and real zeros as 0", () => {
    const { container } = render(<div>
      <span data-testid="none"><Value value={null} format="usd" /></span>
      <span data-testid="undef"><Value value={undefined} format="int" /></span>
      <span data-testid="nan"><Value value={Number.NaN} /></span>
      <span data-testid="zero"><Value value={0} format="int" /></span>
      <span data-testid="usd"><Value value={0.0421} format="usd" /></span>
      <span data-testid="suffix"><Value value={1200} format="int" suffix="tokens" /></span>
    </div>);
    for (const id of ["none", "undef", "nan"]) {
      const el = screen.getByTestId(id);
      expect(el.textContent).toMatch(/unknown/);
      expect(el.textContent).not.toMatch(/\b0\b|\$0/);
      expect(el.querySelector("[data-state=unknown]")).toBeTruthy();
    }
    expect(screen.getByTestId("zero").textContent).toBe("0");
    expect(screen.getByTestId("usd").textContent).toBe("$0.0421");
    expect(screen.getByTestId("suffix").textContent).toBe("1,200 tokens");
    expect(container.querySelectorAll("[data-state=known]").length).toBe(3);
  });

  it("formats with the same rule outside components", () => {
    expect(fmtUsd(null)).toBe("unknown");
    expect(fmtUsd(undefined)).toBe("unknown");
    expect(fmtUsd(0)).toBe("$0.0000");
    expect(fmtMs(undefined)).toBe("unknown");
    expect(formatKnown("", "int")).toBeNull();
    expect(formatKnown(true, "int")).toBeNull();
    expect(formatKnown(0.25, "pct")).toBe("25.0%");
  });

  it("shows unknown KPI tiles and confidence as unknown", () => {
    render(<div><Stat label="Cost" value={<Value value={undefined} format="usd" />} /><ConfidenceBar value={null} /></div>);
    expect(screen.getAllByText(/unknown/)).toHaveLength(2);
    expect(screen.queryByText("0%")).toBeNull();
  });

  it("marks Overview needs unknown when their request fails, instead of 0 or hiding them", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).includes("/alerts")) return new Response("{}", { status: 500 });
      const r = mockBackend(init?.method ?? "GET", String(input));
      return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
    });
    renderAt(`/w/${WS}`);
    const card = (await screen.findByRole("heading", { name: "What needs you" })).closest("section")!;
    await waitFor(() => expect(within(card).getByText("Pending approvals").previousElementSibling?.textContent).toBe("1"));
    expect(within(card).getByText("Open alerts").previousElementSibling?.textContent).toMatch(/unknown/);
  });

  it("gives each state family its own labelled view", () => {
    render(<StateView kind="not-entitled" title="Admins only">Ask an administrator.</StateView>);
    expect(screen.getByRole("status").getAttribute("data-state")).toBe("not-entitled");
    expect(screen.getByText("not entitled")).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ command palette
describe("Ctrl/Cmd-K command palette", () => {
  it("lists every navigable screen for the current workspace and filters by terms", () => {
    const items = screenItems(WS, "ITSM");
    expect(items.map((i) => i.label)).toContain("Data dictionary");
    expect(items.find((i) => i.label === "Platform settings")?.href).toBe("/settings/platform");
    expect(screenItems(undefined).some((i) => i.href.includes(":wsId"))).toBe(false);
    expect(filterItems(items, "token").map((i) => i.label)).toEqual(["Usage & cost"]);
    expect(filterItems(items, "data cat").map((i) => i.label)).toEqual(["Data dictionary"]);
    // the old names still find the renamed screens
    expect(filterItems(items, "autonomy center").map((i) => i.label)).toEqual(["Automations"]);
    expect(filterItems(items, "outputs").map((i) => i.label)).toContain("Dashboards & reports");
    // the gear's admin screens are not offered to analysts or viewers
    for (const role of ["analyst", "viewer"]) {
      const labels = screenItems(WS, "ITSM", { isAdmin: false, role }).map((i) => i.label);
      expect(labels).not.toContain("Platform settings");
      expect(labels).not.toContain("Capability registry");
      expect(labels).not.toContain("Usage & cost");
      expect(labels).not.toContain("Members & policy");
    }
  });

  it("opens with Ctrl-K as a modal dialog, navigates with the keyboard and closes with Esc", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    await screen.findByRole("heading", { name: "IT Service Management", level: 1 });
    const trigger = screen.getByRole("button", { name: /Go to/ });
    trigger.focus();
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const input = within(dialog).getByRole("combobox");
    expect(document.activeElement).toBe(input);
    // recent run and workspace entries load in
    await within(dialog).findByRole("option", { name: /Why are P1 resolution times rising/ });
    fireEvent.change(input, { target: { value: "catalog" } });
    expect(within(dialog).getAllByRole("option")).toHaveLength(1);
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe(`/w/${WS}/data/catalog`));
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(window, { key: "K", metaKey: true });
    const again = await screen.findByRole("dialog");
    fireEvent.keyDown(within(again).getByRole("combobox"), { key: "ArrowDown" });
    expect(within(again).getAllByRole("option")[1].getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(within(again).getByRole("combobox"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("traps Tab focus inside the dialog", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByRole("combobox");
    const close = within(dialog).getByRole("button", { name: /close/ });
    close.focus();
    fireEvent.keyDown(close, { key: "Tab" });
    expect(document.activeElement).toBe(input);
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);
  });
});

// ------------------------------------------------------------------------------------ theme
describe("theme tokens", () => {
  it("cycles system → light → dark, sets data-theme and persists the choice", () => {
    render(<ThemeToggle />);
    expect(getThemePref()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(window.localStorage.getItem("analystos.theme")).toBe("light");
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    fireEvent.click(screen.getByRole("button"));
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(window.localStorage.getItem("analystos.theme")).toBeNull();
    expect(nextThemePref("dark")).toBe("system");
    expect(resolveTheme("system", true)).toBe("dark");
    expect(resolveTheme("light", true)).toBe("light");
  });

  it("still switches when storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    act(() => setThemePref("dark"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

// ------------------------------------------------------------------------------------ accessibility (axe)
/** The main screen of each area, rendered in the full shell with the mock backend. */
const JOURNEY_SCREENS: [string, string, RegExp][] = [
  ["Overview", `/w/${WS}`, /What needs you/],
  ["Work · Ask", `/w/${WS}/work/ask`, /SQL console/],
  ["Work · investigation", `/w/${WS}/work/investigations/${RUN}`, /Why are P1 resolution times rising/],
  ["Work · prepare data", `/w/${WS}/work?tab=prepare`, /p1_incidents_clean/],
  ["Outputs · finding", `/w/${WS}/outputs/findings/${INSIGHT}`, /Network group drives P1 breaches/],
  ["Data", `/w/${WS}/data/catalog`, /Incidents/],
  ["Data · definitions", `/w/${WS}/data/catalog?tab=definitions`, /Joins to confirm/],
  ["Outputs", `/w/${WS}/outputs`, /P1 resolution/],
  ["Operate", `/w/${WS}/operate/approvals`, /Publish dashboards/],
  ["Operate · schedules", `/w/${WS}/operate/schedules`, /upgrade available/],
  ["Settings · platform", "/settings/platform", /Platform settings/],
  ["Settings · policy", `/w/${WS}/settings/policy`, /Effective policy/],
];

describe("accessibility (axe-core, jsdom)", () => {
  it.each(JOURNEY_SCREENS)("%s screen has no axe violations", async (_name, path, ready) => {
    mockFetch();
    const { container } = renderAt(path);
    await waitFor(() => expect(container.textContent).toMatch(ready), { timeout: 4000 });
    await new Promise((r) => setTimeout(r, 50));
    // Colour contrast needs real layout; the Playwright axe run covers it in a browser.
    const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
    expect(result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`)).toEqual([]);
  }, 15000);
});
