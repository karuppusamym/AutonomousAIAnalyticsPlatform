import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { type NumberCheck } from "../api";
import { NumberCheckPanel, NumberCheckResult } from "../components/NumberCheck";
import { trustOf, trustOfAuthority } from "../lib/trust";

const BASE: NumberCheck = {
  version: "number_check.v1", verdict: "confirmed", authority: "certified", question: "How many incidents?", source: "Slide 4",
  workspace_id: "ws_1", claimed: { text: "1,204", value: 1204, percent: false }, label: null, column: null, tolerance: 0,
  computed: { value: 1204, column: "n", row_label: null },
  comparison: { agrees: true, scale: 1, compared_to: 1204, difference: 0, relative: 0, allowed: 0.5 },
  reason: null, note: null,
  evidence: { turn_id: "askt_1", thread_id: "ask_1", sql: "SELECT COUNT(*) AS n FROM sn.incident", query_id: "qry_1",
    result_hash: "h", answered_by: "registry", governance: "governed", verified_query: "incident_count",
    semantic_model_version: null, assets: ["sn.incident"], row_count: 1, truncated: false, staleness: null },
};

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("number check", () => {
  it("shows a confirmed certified number with its evidence and opens the answer", () => {
    const open = vi.fn();
    render(<NumberCheckResult check={BASE} onOpenThread={open} />);
    expect(screen.getByText("Confirmed.")).toBeTruthy();
    expect(screen.getByText("Certified")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Send to an analyst" })).toBeNull();  // settled: nothing to hand off
    expect(screen.getByText("sn.incident")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open the answer" }));
    expect(open).toHaveBeenCalledWith("ask_1");
  });

  it("says plainly when an ad hoc answer contradicts the claim", () => {
    render(<NumberCheckResult check={{ ...BASE, verdict: "contradicted", authority: "ad_hoc", claimed: { text: "1300", value: 1300, percent: false },
      comparison: { agrees: false, scale: 1, compared_to: 1204, difference: 96, relative: 96 / 1204, allowed: 0.5 },
      reason: "claimed 1300, computed 1204", note: "Computed from generated SQL, not a certified definition." }} onOpenThread={vi.fn()} />);
    expect(screen.getByText("Contradicted.")).toBeTruthy();
    expect(screen.getByText(/You entered 1300/)).toBeTruthy();
    expect(screen.getByText("Checked")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Open the answer to certify it" })).toBeTruthy();
    expect(screen.getByText(/not a certified definition/)).toBeTruthy();
  });

  it("posts the claim as written and renders an unverifiable answer with its remedy", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ...BASE, verdict: "unverifiable", computed: null, comparison: null,
      reason: "the answer has 4 rows: name the row the number is about with `label`", remedy: "Name the row with `label`." }),
    { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<NumberCheckPanel wsId="ws_1" />);
    fireEvent.change(screen.getByLabelText("What the number measures"), { target: { value: "Incidents by priority" } });
    fireEvent.change(screen.getByLabelText("Claimed number"), { target: { value: "12.5%" } });
    fireEvent.click(screen.getByRole("button", { name: "Check number" }));
    await waitFor(() => expect(screen.getByText("Could not check.")).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v2/number-checks");
    expect(JSON.parse(String(init.body))).toMatchObject({ workspace_id: "ws_1", question: "Incidents by priority", claimed: "12.5%", label: null });
    expect(screen.getByText("Name the row with `label`.")).toBeTruthy();
  });

  it("sends a contradicted check to the Desk with the claim as entered", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ request_id: "rq_9", state: "escalated", assignee_id: "usr_a",
      sla_due_at: null, verdict: "contradicted" }), { status: 201, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<NumberCheckResult check={{ ...BASE, verdict: "contradicted", authority: "ad_hoc", claimed: { text: "1,300", value: 1300, percent: false },
      comparison: { agrees: false, scale: 1, compared_to: 1204, difference: 96, relative: 0.08, allowed: 0.5 } }} />);
    fireEvent.click(screen.getByRole("button", { name: "Send to an analyst" }));
    await waitFor(() => expect(screen.getByText(/Sent to the Request Desk as rq_9/)).toBeTruthy());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/api/v2/number-checks/handoff");
    expect(JSON.parse(String(init.body))).toMatchObject({ workspace_id: "ws_1", turn_id: "askt_1", claimed: "1,300", source: "Slide 4" });
  });
});

describe("trust label", () => {
  it("has three levels with the same words everywhere", () => {
    expect(trustOf({ status: "answered", answered_by: "registry" })).toBe("certified");
    expect(trustOf({ status: "answered", answered_by: "semantic" })).toBe("certified");
    expect(trustOf({ status: "answered", answered_by: "model", governance: "ad_hoc" })).toBe("checked");
    expect(trustOf({ status: "answered", answered_by: "rules" })).toBe("checked");
    expect(trustOf({ status: "refused", answered_by: "registry" })).toBe("unchecked");
    expect([trustOfAuthority("certified"), trustOfAuthority("ad_hoc"), trustOfAuthority(null)]).toEqual(["certified", "checked", "unchecked"]);
  });
});
