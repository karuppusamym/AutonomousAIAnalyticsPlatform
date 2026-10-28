/**
 * Business view (the default) and the Show expert tools switch: business people see answers,
 * dashboards and approvals; the SQL console, engineering tabs, agent links and the lineage graph
 * appear once the switch is on. A link that opens an expert tab still works in business view.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { getExpertView, setExpertView } from "../lib/expert";
import { mockFetch, renderAt } from "./harness";
import { RUN, resetMockState, USER, WS } from "./mockBackend";

beforeEach(() => { resetMockState(); session.set("mock-token", USER); window.localStorage.clear(); setExpertView(false); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

describe("business view", () => {
  it("starts off and is remembered in this browser", () => {
    window.localStorage.clear();
    expect(getExpertView()).toBe(false);
    setExpertView(true);
    expect(window.localStorage.getItem("analystos.expert")).toBe("1");
    expect(getExpertView()).toBe(true);
  });

  it("names the menu in business words", async () => {
    mockFetch();
    renderAt(`/w/${WS}`);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    const analyze = await within(nav).findByRole("group", { name: "Analyze" });
    expect(within(analyze).getAllByRole("link").map((l) => l.textContent)).toEqual(["Ask a question", "Investigations", "Requests"]);
    expect(within(nav).getByRole("link", { name: "Data dictionary" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Dashboards & reports" })).toBeTruthy();
    expect(within(nav).getByRole("link", { name: "Automations" })).toBeTruthy();
  });

  it("hides the SQL console on Ask until expert tools are turned on", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work/ask`);
    expect(await screen.findByRole("heading", { name: "Ask a question", level: 1 })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "SQL console" })).toBeNull();
    const toggle = screen.getByRole("switch", { name: "Show expert tools" });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    fireEvent.click(toggle);
    expect(await screen.findByRole("heading", { name: "SQL console" })).toBeTruthy();
    expect(getExpertView()).toBe(true);
  });

  it("shows only the business tabs of Investigations and the Data dictionary, and no agent links", async () => {
    mockFetch();
    const { unmount } = renderAt(`/w/${WS}/work`);
    expect(await screen.findByRole("heading", { name: "Investigations", level: 1 })).toBeTruthy();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Investigations", "Prepare data"]);
    await screen.findAllByRole("link", { name: /P1 resolution/ });
    expect(screen.queryByRole("link", { name: /Agent console/ })).toBeNull();
    unmount();
    renderAt(`/w/${WS}/data/catalog`);
    expect(await screen.findByRole("heading", { name: "Data dictionary", level: 1 })).toBeTruthy();
    const tabs = screen.getAllByRole("tab").map((t) => t.textContent);
    expect(tabs).toContain("Metrics");
    expect(tabs).not.toContain("Import & export");
    expect(tabs).not.toContain("Context & mappings");
  });

  it("still opens an expert tab from a link", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work?tab=builds`);
    expect(await screen.findByRole("tab", { name: "dbt builds", selected: true })).toBeTruthy();
    expect(screen.queryByRole("tab", { name: "Notebooks" })).toBeNull();
  });

  it("keeps an investigation reachable from the list", async () => {
    mockFetch();
    renderAt(`/w/${WS}/work`);
    const link = (await screen.findAllByRole("link", { name: /P1 resolution/ }))[0];
    expect(link.getAttribute("href")).toBe(`/w/${WS}/work/investigations/${RUN}`);
  });
});
