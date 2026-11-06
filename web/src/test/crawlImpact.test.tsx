import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { session, type CrawlImpact } from "../api";
import { ImpactPanel } from "../components/CrawlPanel";

const USER = { id: "u1", email: "a@b", name: "A", is_admin: false, active: true, attributes: {}, created_at: "" };

const IMPACT: CrawlImpact = {
  crawl_id: "crawl_1", source_id: "src_1",
  changed: [{ asset: "raw.customers", asset_id: "ast_1", key: "raw.customers", column: "email", change: "removed" }],
  downstream: [{ asset: "recipes.r_contacts", column: "mail", change: "downstream", hops: 1, from: "raw.customers.email", origins: ["recipe_run:r1"] }],
  metrics: [{ id: "met_1", name: "contactable", reads: [{ asset: "raw.customers", column: "email", change: "removed" }] }],
  verified_queries: [], dashboards: [], monitors: [],
  schedules: [
    { id: "sch_1", name: "evaluate monitors", kind: "monitor", enabled: true, revision: 1, reads: [], via: ["monitor:mon_1"] },
    { id: "sch_2", name: "old refresh", kind: "dataset_refresh", enabled: false, revision: 3, reads: [], via: ["metric:contactable"] },
  ],
  counts: { changed: 1, downstream: 1, metrics: 1, verified_queries: 0, dashboards: 0, monitors: 0, schedules: 2 },
};

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => session.set("tok", USER));
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

describe("crawl impact panel (P1-5)", () => {
  it("lists dependents and pauses schedules only through an approval", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      if (method === "GET" && /\/api\/crawls\/crawl_1\/impact$/.test(url)) return json(IMPACT);
      if (method === "POST" && /\/impact\/pause$/.test(url)) {
        return json({ approval_id: "apr_9", status: "pending", payload_hash: "h", schedules: [{ id: "sch_1", revision: 1 }], expires_at: null });
      }
      if (method === "POST" && /\/approvals\/apr_9\/execute-schedule-pause$/.test(url)) return json({ paused: ["sch_1"], approval_id: "apr_9" });
      return json({});
    });
    render(<ImpactPanel crawlId="crawl_1" />);
    expect(await screen.findByText("1 changed columns")).toBeTruthy();
    expect(screen.getByText("recipes.r_contacts.mail")).toBeTruthy();
    expect(screen.getByText("contactable")).toBeTruthy();
    expect((screen.getByLabelText("Pause old refresh") as HTMLInputElement).disabled).toBe(true); // already paused
    fireEvent.click(screen.getByLabelText("Pause evaluate monitors"));
    fireEvent.click(screen.getByRole("button", { name: /Request approval to pause 1 schedule/ }));
    expect(await screen.findByText("apr_9")).toBeTruthy();
    const pauseCall = fetchMock.mock.calls.find(([u, i]) => /\/impact\/pause$/.test(String(u)) && i?.method === "POST");
    expect(JSON.parse(String(pauseCall?.[1]?.body))).toEqual({ schedule_ids: ["sch_1"] });
    fireEvent.click(screen.getByRole("button", { name: "Pause approved schedules" }));
    await waitFor(() => expect(screen.getByText("Paused 1 schedule.")).toBeTruthy());
  });

  it("says when nothing can break", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json({ ...IMPACT, changed: [], downstream: [], metrics: [], schedules: [] }));
    render(<ImpactPanel crawlId="crawl_1" />);
    expect(await screen.findByText(/nothing downstream can break/)).toBeTruthy();
  });
});
