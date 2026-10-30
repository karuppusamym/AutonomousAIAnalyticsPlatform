/**
 * PILOT-FIX-04 and the readiness friction from the 2026-10-30 rehearsal: the meaning review shows every question at
 * once, data the catalog lacks as a stated limitation, and what was understood without asking; readiness gaps are
 * plain actions with a link to the screen that fixes them, and analysts see a read-only summary on the home page.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen, within } from "@testing-library/react";
import { session } from "../api";
import { mockFetch, renderAt } from "./harness";
import { mockBackend, resetMockState, RUN, USER, WS } from "./mockBackend";

const base = (path: string) => JSON.parse(mockBackend("GET", `http://x${path}`).body as string);

const REVIEW = {
  round: 1,
  questions: [{ key: "delivery late", term: "late delivery", question: "What counts as a late delivery?", kind: "model",
    columns: ["r.orders.promised_date", "r.orders.delivered_date"] }],
  limitations: [{ key: "reason return", term: "return reason", kind: "absent_data",
    statement: "No field in the selected data records return reason. Findings can show what goes with the outcome, but not return reason." }],
  not_asked: [], answered: [], pinned: [], binding_revisions: {},
  resolved_without_asking: [{ term: "Sales Channel", by: "catalog", columns: ["r.orders.sales_channel"] },
    { term: "Region", by: "catalog", columns: ["r.customers.region"] }, { term: "Category", by: "catalog", columns: ["r.products.category"] }],
};
const REPORT = { question: "Why are some orders returned?", resolutions: [], missing: ["What counts as a late delivery?"],
  needs_clarification: true, model_status: "used", used_count: 3, available_count: 3,
  clarification: ["What counts as a late delivery?"], metric_proposals: [], meaning_review: REVIEW, limitations: [REVIEW.limitations[0].statement] };

const READINESS = {
  workspace_id: WS, workspace_name: "ITSM", verdict: "not_ready", missing: 2, failing: 0, generated_at: "2026-10-30T10:00:00Z",
  checks: [
    { check: "owner.business", subject: "workspace", status: "missing", reason: "no named business owner",
      remediation: `a workspace owner names one: PUT /api/workspaces/${WS}/owners`, evidence: null,
      action: "Name the business owner of this workspace", who: "workspace_owner", screen: "members_policy" },
    { check: "connector.health", subject: "source:src_sn (ServiceNow)", status: "missing", reason: "the source was never discovered",
      remediation: "discover the source", evidence: null, action: "Check the ServiceNow source so its tables are known", who: "editor", screen: "sources" },
    { check: "owner.technical", subject: "workspace", status: "pass", reason: "Tom Platform <tom@pilot.test>", remediation: null, evidence: null },
  ],
};

beforeEach(() => { resetMockState(); session.set("mock-token", USER); window.localStorage.clear(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); });

describe("meaning review (PILOT-FIX-04)", () => {
  it("asks every open meaning at once, states what the data cannot show, and lists what needed nobody", async () => {
    mockFetch((m, p) => (m === "POST" && p.endsWith("/grounding/resolve") ? { status: 200, body: REPORT } : null));
    renderAt(`/w/${WS}/data/catalog?tab=grounding`);
    fireEvent.change(await screen.findByLabelText("Business question"), { target: { value: "Why are some orders returned?" } });
    fireEvent.click(screen.getByRole("button", { name: "Resolve meaning" }));
    const asks = await screen.findByRole("region", { name: "Meanings to confirm" });
    expect(within(asks).getByText("One meaning to confirm")).toBeTruthy();
    expect(within(asks).getByText("What counts as a late delivery?")).toBeTruthy();
    const limits = screen.getByRole("region", { name: "What the data cannot show" });
    expect(within(limits).getByText(/No field in the selected data records return reason/)).toBeTruthy();
    expect(within(limits).getByText(/does not stop the work/)).toBeTruthy();
    expect(screen.getByText("Understood without asking (3)")).toBeTruthy();
    expect(screen.queryByText(/r\.orders\.promised_date/)).toBeNull();  // column references are expert detail
    fireEvent.click(within(asks).getByRole("button", { name: "Define late delivery" }));
    const form = await screen.findByRole("region", { name: "Guided domain onboarding" });
    expect((within(form).getByLabelText("Business term") as HTMLInputElement).value).toBe("late delivery");
  });

  it("shows column references with expert tools on", async () => {
    window.localStorage.setItem("analystos.expert", "1");
    mockFetch((m, p) => (m === "POST" && p.endsWith("/grounding/resolve") ? { status: 200, body: REPORT } : null));
    renderAt(`/w/${WS}/data/catalog?tab=grounding`);
    fireEvent.change(await screen.findByLabelText("Business question"), { target: { value: "Why are some orders returned?" } });
    fireEvent.click(screen.getByRole("button", { name: "Resolve meaning" }));
    expect(await screen.findByText(/r\.orders\.promised_date, r\.orders\.delivered_date/)).toBeTruthy();
  });

  it("on a recheck shows the round, the owner time so far, and what was not asked again", async () => {
    const round2 = { ...REVIEW, round: 2, questions: [], answered: ["Late delivery"],
      limitations: [...REVIEW.limitations, { key: "lead shipping time", term: "shipping lead time", kind: "unreviewed",
        statement: "Not reviewed by an owner: shipping lead time. The analysis uses the data as recorded." }],
      not_asked: [{ key: "lead shipping time", term: "shipping lead time", question: "How is shipping lead time measured?", kind: "model", columns: [] }] };
    mockFetch((m, p) => (m === "GET" && p.includes(`/grounding/runs/${RUN}`) ? { status: 200, body: {
      grounding: { ...REPORT, needs_clarification: false, clarification: [], meaning_review: round2 },
      rounds: [{ round: 1, questions: [{ term: "late delivery", question: "What counts as a late delivery?" }], asked_at: "2026-10-30T09:00:00Z",
        rechecked_at: "2026-10-30T09:04:30Z", owner_minutes: 4.5 }], brief_receipts: [], coverage: null } } : null));
    renderAt(`/w/${WS}/work/investigations/${RUN}`);
    const card = await screen.findByRole("region", { name: "Run context explanation" });
    expect(await within(card).findByText(/Meaning review round 2: only what is still open, or what an answer changed/)).toBeTruthy();
    expect(within(card).getByText(/Owner time so far: 4\.5 min/)).toBeTruthy();
    expect(within(card).getByText("Answered since the last round: Late delivery.")).toBeTruthy();
    expect(within(card).getByText("Not asked: raised after the first round (1)")).toBeTruthy();
    expect(within(card).queryByRole("button", { name: "Recheck reviewed context" })).toBeNull();
  });
});

describe("readiness in plain words", () => {
  it("the owner page shows what to do and where, and keeps API paths behind expert tools", async () => {
    mockFetch((m, p) => (m === "GET" && p === `/api/workspaces/${WS}/pilot-readiness` ? { status: 200, body: READINESS } : null));
    renderAt(`/w/${WS}/settings/policy`);
    const card = await screen.findByRole("region", { name: "Pilot readiness" });
    expect(within(card).getByText(/Name the business owner of this workspace/)).toBeTruthy();
    expect(within(card).getByText("ServiceNow source")).toBeTruthy();
    expect(within(card).getByRole("link", { name: "Open Sources" }).getAttribute("href")).toBe(`/w/${WS}/data/sources`);
    expect(within(card).queryByText(/PUT \/api\//)).toBeNull();
    cleanup();
    window.localStorage.setItem("analystos.expert", "1");
    renderAt(`/w/${WS}/settings/policy`);
    const expertCard = await screen.findByRole("region", { name: "Pilot readiness" });
    expect(within(expertCard).getByText(/PUT \/api\/workspaces\//)).toBeTruthy();
  });

  it("an analyst sees a read-only “Is this workspace ready?” summary on the workspace home", async () => {
    mockFetch((m, p) => {
      if (m === "GET" && p === `/api/workspaces/${WS}`) return { status: 200, body: { ...base(p), role: "analyst" } };
      if (m === "GET" && p === `/api/workspaces/${WS}/pilot-readiness`) return { status: 200, body: READINESS };
      return null;
    });
    renderAt(`/w/${WS}`);
    const summary = await screen.findByText(/Is this workspace ready for the pilot\?/);
    expect(within(summary).getByText("Not yet: 2 to do")).toBeTruthy();
    const list = screen.getByRole("list", { name: "What the pilot still needs" });
    expect(within(list).getByText(/Name the business owner of this workspace/)).toBeTruthy();
    expect(within(list).getByText(/A workspace owner does this/)).toBeTruthy();
    expect(within(list).queryByRole("link", { name: "Open Members & policy" })).toBeNull();  // owner-only screen
    expect(within(list).getByRole("link", { name: "Open Sources" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Save owners/ })).toBeNull();
  });
});
