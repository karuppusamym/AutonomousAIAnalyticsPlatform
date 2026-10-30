import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setExpertView } from "../lib/expert";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { RequestedPeriod } from "../components/RequestedPeriod";
import { session, type RunDetail } from "../api";
import { bodyOf, calls, mockFetch, renderAt } from "./harness";
import { resetMockState, RUN, USER, WS } from "./mockBackend";

beforeEach(() => { resetMockState(); session.set("mock-token", USER); window.localStorage.clear(); setExpertView(true); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

it("starts a guided investigation with an explicit period, baseline and decision", async () => {
  const fetch = mockFetch();
  renderAt(`/w/${WS}`);
  fireEvent.click(await screen.findByRole("button", { name: "Start work" }));
  fireEvent.click(await screen.findByRole("button", { name: /^Explain/ }));
  const form = await screen.findByRole("form", { name: "Start explain" });
  await within(form).findByRole("option", { name: "Explain an SLA increase" });
  fireEvent.change(within(form).getByLabelText("Guided investigation"), { target: { value: "sla_change" } });
  expect((within(form).getByRole("button", { name: "Start investigation" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(within(form).getByLabelText("Period starts"), { target: { value: "2026-07-01" } });
  fireEvent.change(within(form).getByLabelText("Period ends"), { target: { value: "2026-09-30" } });
  fireEvent.change(within(form).getByLabelText("Name for this period (optional)"), { target: { value: "Q3 2026" } });
  fireEvent.change(within(form).getByLabelText("Decision this should support"), { target: { value: "Choose the team to review" } });
  fireEvent.change(within(form).getByLabelText("Compare with"), { target: { value: "custom" } });
  expect((within(form).getByRole("button", { name: "Start investigation" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(within(form).getByLabelText("Baseline starts"), { target: { value: "2026-04-01" } });
  fireEvent.change(within(form).getByLabelText("Baseline ends"), { target: { value: "2026-06-30" } });
  fireEvent.click(within(form).getByRole("button", { name: "Start investigation" }));
  await screen.findByRole("heading", { name: "The answer, with the evidence" });
  expect(bodyOf(calls(fetch, "POST", /\/guided-investigations$/)[0][1])).toMatchObject({ template_id: "sla_change",
    period: { start: "2026-07-01", end: "2026-09-30", label: "Q3 2026" }, comparison_mode: "custom",
    baseline: { start: "2026-04-01", end: "2026-06-30" }, decision: "Choose the team to review" });
});

it("shows the requested period, its baseline and the data facts on the run (PILOT-FIX-01)", () => {
  const run = { constraints: { period: { current: { label: "2026 Q3", requested: "Q3 2026" }, baseline: { label: "2026 Q2", requested: "1 Apr 2026 to 30 Jun 2026" },
    columns: { "src.incident": "opened_at" } } },
    tasks: [{ key: "hypotheses", output: { period: { tables: [{ asset: "src.incident", windows: { current: { label: "2026 Q3", rows: 3421 }, baseline: { label: "2026 Q2", rows: 5029 } },
      facts: ["incident: the requested period 2026 Q3 is incomplete: the data ends on 2026-08-31"] }] } } },
      { key: "verify", output: { period_comparison: [{ hypothesis: "H-1", sentence: "Missed SLA was 14.2% in 2026 Q2 (5,029 records) and 13.5% in 2026 Q3 (3,421 records), a decrease" }] } }] } as unknown as RunDetail;
  render(<RequestedPeriod run={run} />);
  const panel = screen.getByRole("region", { name: "Requested period" });
  expect(within(panel).getByText(/2026 Q3 \(Q3 2026\)/)).toBeTruthy();
  expect(within(panel).getByText(/2026 Q2 \(1 Apr 2026 to 30 Jun 2026\)/)).toBeTruthy();
  expect(within(panel).getByText(/incident.opened_at/)).toBeTruthy();
  expect(within(within(panel).getByRole("note", { name: "Data facts about the period" })).getByText(/is incomplete/)).toBeTruthy();
  expect(within(panel).getByText(/14.2% in 2026 Q2 \(5,029 records\) and 13.5% in 2026 Q3/)).toBeTruthy();
});

it("saves reviewed context through the existing versioned brief with If-Match", async () => {
  const fetch = mockFetch((method, path) => {
    if (method === "GET" && path.endsWith("/reviewed-context")) return { status: 200, body: { version: 3, content_hash: "h", facts: [], open_questions: 2 } };
    if (method === "PATCH" && path.endsWith("/brief")) return { status: 200, body: { version: 4, assertions: [] } };
    return null;
  });
  renderAt(`/w/${WS}`);
  const context = await screen.findByRole("region", { name: "Reusable business context" });
  fireEvent.click(within(context).getByText("Review definitions and corrections"));
  fireEvent.click(within(context).getByRole("button", { name: "Add a definition or correction" }));
  const form = within(context).getByRole("form", { name: "Save reviewed business context" });
  fireEvent.change(within(form).getByLabelText("Term or subject"), { target: { value: "SLA breach" } });
  fireEvent.change(within(form).getByLabelText("Reviewed meaning"), { target: { value: "Use made_sla = false, not priority alone." } });
  fireEvent.click(within(form).getByRole("button", { name: "Save reviewed context" }));
  expect(await within(context).findByText(/Saved as reviewed context/)).toBeTruthy();
  const saved = calls(fetch, "PATCH", /\/brief$/)[0];
  expect(new Headers(saved[1]?.headers).get("If-Match")).toBe('"3"');
  expect(bodyOf(saved[1]).ops).toMatchObject([{ op: "set", assertion: { group: "domain", field: "term", subject: "SLA breach" } }]);
});

it("records an owned follow-up with evidence rather than executing a change", async () => {
  const fetch = mockFetch();
  renderAt(`/w/${WS}/work/investigations/${RUN}`);
  fireEvent.click(await screen.findByRole("button", { name: "Create follow-up action" }));
  const form = screen.getByRole("form", { name: "Follow-up action" });
  fireEvent.change(within(form).getByLabelText("Action title"), { target: { value: "Review Network routing" } });
  fireEvent.change(within(form).getByLabelText("Recommended next step"), { target: { value: "Inspect reassigned incidents with the team" } });
  fireEvent.change(within(form).getByLabelText("Responsible owner"), { target: { value: USER.id } });
  fireEvent.change(within(form).getByLabelText("How will success be measured?"), { target: { value: "Compare next month's breach rate" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save follow-up" }));
  expect(await screen.findByText(/Follow-up saved/)).toBeTruthy();
  expect(screen.getByText("Review Network routing")).toBeTruthy();
  expect(bodyOf(calls(fetch, "POST", /\/findings\/ins_demo\/actions$/)[0][1])).toMatchObject({ owner_id: USER.id, status: "proposed" });
  expect(screen.getByRole("link", { name: "Review supporting evidence" }).getAttribute("href")).toContain("ins_demo");
});

it("keeps rehearsal benefits out of the real analyst pilot score", async () => {
  mockFetch();
  renderAt(`/w/${WS}`);
  const panel = await screen.findByRole("region", { name: "Pilot scorecard" });
  fireEvent.click(within(panel).getByRole("button", { name: "Record an assessment" }));
  const form = within(panel).getByRole("form", { name: "Pilot assessment" });
  fireEvent.change(within(form).getByLabelText("Investigation assessed"), { target: { value: RUN } });
  fireEvent.change(within(form).getByLabelText("Task measured"), { target: { value: "Find the SLA drivers" } });
  fireEvent.change(within(form).getByLabelText("Manual baseline (minutes)"), { target: { value: "60" } });
  fireEvent.change(within(form).getByLabelText("Assisted time including review (minutes)"), { target: { value: "15" } });
  fireEvent.change(within(form).getByLabelText("Accuracy assessment"), { target: { value: "correct" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save assessment" }));
  expect(await within(panel).findByText(/Assessment saved/)).toBeTruthy();
  expect(within(within(panel).getByRole("region", { name: "Rehearsal results" })).getByText(/45/)).toBeTruthy();
  expect(within(within(panel).getByRole("region", { name: "Real pilot results" })).getByText(/No assessments recorded/)).toBeTruthy();
});

it("records a conversational dashboard assessment with review time and step-ins, split by feature", async () => {
  const fetch = mockFetch();
  renderAt(`/w/${WS}`);
  const panel = await screen.findByRole("region", { name: "Pilot scorecard" });
  fireEvent.click(within(panel).getByRole("button", { name: "Record an assessment" }));
  const form = within(panel).getByRole("form", { name: "Pilot assessment" });
  fireEvent.change(within(form).getByLabelText("What was assessed"), { target: { value: "dashboard" } });
  const item = within(form).getByLabelText("Dashboard by conversation assessed");
  await within(item).findByRole("option", { name: /Weekly SLA overview/ });
  fireEvent.change(item, { target: { value: "art_dash" } });
  fireEvent.change(within(form).getByLabelText("Task measured"), { target: { value: "Draft the weekly SLA dashboard" } });
  fireEvent.change(within(form).getByLabelText("Manual baseline (minutes)"), { target: { value: "90" } });
  fireEvent.change(within(form).getByLabelText("Assisted time including review (minutes)"), { target: { value: "30" } });
  fireEvent.change(within(form).getByLabelText("Of which checking and correcting (minutes)"), { target: { value: "12" } });
  fireEvent.change(within(form).getByLabelText("Times you had to step in"), { target: { value: "2" } });
  fireEvent.click(within(form).getByRole("button", { name: "Save assessment" }));
  expect(await within(panel).findByText(/Assessment saved/)).toBeTruthy();
  expect(bodyOf(calls(fetch, "POST", /\/pilot-reviews$/)[0][1])).toMatchObject({ subject_type: "dashboard", subject_id: "art_dash", run_id: null,
    review_minutes: 12, interventions: 2, evidence_kind: "rehearsal" });
  const rehearsal = within(panel).getByRole("region", { name: "Rehearsal results" });
  expect(within(within(rehearsal).getByRole("table", { name: "Results by feature" })).getByText("Dashboard by conversation")).toBeTruthy();
  expect(within(within(panel).getByRole("region", { name: "Real pilot results" })).getByText(/No assessments recorded/)).toBeTruthy();
});
