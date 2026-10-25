import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { RunDetail } from "../api";
import { session } from "../api";
import { InvestigationOverview } from "../components/InvestigationOverview";
import { mockBackend, resetMockState, RUN, USER, WS } from "./mockBackend";
import { mockFetch, renderAt } from "./harness";

function fixture(): RunDetail {
  return JSON.parse(mockBackend("GET", `/api/workspaces/${WS}/analysis/${RUN}`, null, {}).body);
}
function show(run: RunDetail) {
  const actions = { onExplore: vi.fn(), onPlan: vi.fn(), onApprovals: vi.fn() };
  render(<MemoryRouter><InvestigationOverview run={run} {...actions} /></MemoryRouter>);
  return actions;
}
beforeEach(() => { resetMockState(); session.set("mock-token", USER); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

it("does not count void, pending, failed, legacy or rejected findings as currently verified", () => {
  const run = fixture();
  const finding = run.insights[0];
  run.insights = [finding, ...["void", "pending", "failed", "legacy", "superseded"].map((badge) => ({
    ...finding, id: badge, verification_state: { ...finding.verification_state!, badge },
  })), { ...finding, id: "rejected", status: "rejected" }];
  show(run);
  const region = screen.getByRole("region", { name: "Investigation overview" });
  const metric = (name: string) => within(region).getByText(name).parentElement!.querySelector("dd")!.textContent;
  expect(metric("Verified findings")).toBe("1");
  expect(metric("Findings to check")).toBe("4");
  expect(screen.getAllByRole("link", { name: /Open evidence for/ })).toHaveLength(3);
});

it("links evidence to the original finding and takes the reviewer to pending approvals", () => {
  const run = fixture();
  const actions = show(run);
  expect(screen.getByRole("link", { name: "Open evidence for F1" }).getAttribute("href")).toContain("ins_demo");
  fireEvent.click(screen.getByRole("button", { name: "Review approvals" }));
  expect(actions.onApprovals).toHaveBeenCalledOnce();
  expect(screen.getByRole("progressbar").getAttribute("value")).toBe("1");
  fireEvent.click(screen.getByText(/Limitations to keep in mind/));
  expect(screen.getByText("Q3 only")).toBeTruthy();
});

it.each(["FAILED", "CANCELLED", "REJECTED"])("shows an incomplete-work action for a %s run even when an approval remains", (status) => {
  const run = fixture(); run.status = status;
  const actions = show(run);
  expect(screen.queryByRole("button", { name: "Review approvals" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Inspect steps" }));
  expect(actions.onPlan).toHaveBeenCalledOnce();
});

it("does not invent progress or a conclusion when planning has not produced work", () => {
  const run = fixture(); run.status = "RUNNING"; run.tasks = []; run.insights = []; run.approvals = [];
  run.summary = {};
  const actions = show(run);
  expect(screen.getByText(/Findings will appear here/)).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("value")).toBe("0");
  expect(screen.getByText(/Waiting for the investigation plan/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Follow progress" }));
  expect(actions.onPlan).toHaveBeenCalledOnce();
});

it("renders the brief on the real investigation route alongside the evidence workbench", async () => {
  mockFetch();
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  expect(await screen.findByRole("region", { name: "Investigation overview" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "The answer, with the evidence" })).toBeTruthy();
  expect(screen.getByRole("tab", { name: /Hypotheses/ })).toBeTruthy();
});
