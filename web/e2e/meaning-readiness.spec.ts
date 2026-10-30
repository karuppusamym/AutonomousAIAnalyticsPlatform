/**
 * PILOT-FIX-04 and the readiness friction (rehearsal 2026-10-30) in a real browser: the meaning review asks once and
 * states what the data cannot show; readiness gaps are plain actions; analysts see a read-only summary on the home.
 * Accessibility (axe, WCAG 2.1 AA) and phone width without horizontal scroll, in business view.
 */
import type { Page } from "@playwright/test";
import { mockBackend, RUN, WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

const LIMIT = "No field in the selected data records return reason. Findings can show what goes with the outcome, but not return reason.";
const REVIEW = {
  round: 1, not_asked: [], answered: [], pinned: [], binding_revisions: {},
  questions: [{ key: "delivery late", term: "late delivery", question: "What counts as a late delivery?", kind: "model",
    columns: ["r.orders.promised_date", "r.orders.delivered_date"] }],
  limitations: [{ key: "reason return", term: "return reason", kind: "absent_data", statement: LIMIT }],
  resolved_without_asking: [{ term: "Sales Channel", by: "catalog", columns: ["r.orders.sales_channel"] },
    { term: "Region", by: "catalog", columns: ["r.customers.region"] }],
};
const REPORT = { question: "Why are some orders returned?", resolutions: [], missing: [], needs_clarification: true, model_status: "used",
  used_count: 2, available_count: 2, clarification: ["What counts as a late delivery?"], metric_proposals: [], meaning_review: REVIEW,
  limitations: [LIMIT] };
const READINESS = {
  workspace_id: WS, workspace_name: "ITSM", verdict: "not_ready", missing: 2, failing: 0, generated_at: "2026-10-30T10:00:00Z",
  checks: [
    { check: "owner.business", subject: "workspace", status: "missing", reason: "no named business owner",
      remediation: `a workspace owner names one: PUT /api/workspaces/${WS}/owners`, evidence: null,
      action: "Name the business owner of this workspace", who: "workspace_owner", screen: "members_policy" },
    { check: "connector.health", subject: "source:src_sn (ServiceNow)", status: "missing", reason: "the source was never discovered",
      remediation: "discover the source", evidence: null, action: "Check the ServiceNow source so its tables are known",
      who: "editor", screen: "sources" },
  ],
};

async function businessView(page: Page) {
  await page.addInitScript(() => { try { localStorage.removeItem("analystos.expert"); } catch { /* no storage */ } });
}

async function phoneChecks(page: Page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  expect(await axeViolations(page)).toEqual([]);
}

test("the meaning review asks once and says what the data cannot show", async ({ page }, info) => {
  await businessView(page);
  await page.route("**/api/workspaces/*/grounding/resolve", (route) => route.fulfill({ json: REPORT }));
  await signIn(page, `/w/${WS}/data/catalog?tab=grounding`);
  await page.getByLabel("Business question", { exact: true }).fill("Why are some orders returned?");
  await page.getByRole("button", { name: "Resolve meaning", exact: true }).click();
  const asks = page.getByRole("region", { name: "Meanings to confirm" });
  await expect(asks.getByText("What counts as a late delivery?")).toBeVisible();
  await expect(page.getByRole("region", { name: "What the data cannot show" }).getByText(LIMIT)).toBeVisible();
  await expect(page.getByText("r.orders.promised_date")).toHaveCount(0);
  await asks.getByRole("button", { name: "Define late delivery" }).click();
  await expect(page.getByRole("region", { name: "Guided domain onboarding" }).getByLabel("Business term", { exact: true })).toHaveValue("late delivery");
  expect(await axeViolations(page)).toEqual([]);
  await page.screenshot({ path: info.outputPath("meaning-review.png"), fullPage: true });
  await phoneChecks(page);
});

test("a rechecked run shows the round, the owner time and the limitation", async ({ page }, info) => {
  await businessView(page);
  await page.route(`**/api/workspaces/*/grounding/runs/${RUN}`, (route) => route.fulfill({ json: {
    grounding: { ...REPORT, needs_clarification: false, clarification: [], meaning_review: { ...REVIEW, round: 2, questions: [], answered: ["Late delivery"] } },
    rounds: [{ round: 1, questions: [], asked_at: "2026-10-30T09:00:00Z", rechecked_at: "2026-10-30T09:02:00Z", owner_minutes: 2 }],
    brief_receipts: [], coverage: null } }));
  await signIn(page, `/w/${WS}/work/investigations/${RUN}`);
  const card = page.getByRole("region", { name: "Run context explanation" });
  await expect(card.getByText(/Meaning review round 2/)).toBeVisible();
  await expect(card.getByText(/Owner time so far: 2\.0 min/)).toBeVisible();
  await expect(card.getByText(LIMIT)).toBeVisible();
  expect(await axeViolations(page)).toEqual([]);
  await card.screenshot({ path: info.outputPath("run-meaning-review.png") });
});

test("readiness: plain actions for the owner, a read-only summary for an analyst", async ({ page }, info) => {
  await businessView(page);
  await page.route("**/api/workspaces/*/pilot-readiness", (route) => route.fulfill({ json: READINESS }));
  await signIn(page, `/w/${WS}/settings/policy`);
  const owner = page.getByRole("region", { name: "Pilot readiness" });
  await expect(owner.getByText(/Name the business owner of this workspace/)).toBeVisible();
  await expect(owner.getByRole("link", { name: "Open Sources" })).toBeVisible();
  await expect(owner.getByText(/PUT \/api\//)).toHaveCount(0);
  expect(await axeViolations(page)).toEqual([]);

  await page.route(`**/api/workspaces/${WS}`, (route) => {
    const body = JSON.parse(mockBackend("GET", `http://x/api/workspaces/${WS}`).body as string);
    return route.fulfill({ json: { ...body, role: "analyst" } });
  });
  await page.goto(`/w/${WS}`);
  const summary = page.locator("details.workspace-readiness");
  await summary.locator("summary").click();
  await expect(summary.getByText("Not yet: 2 to do")).toBeVisible();
  await expect(summary.getByText(/A workspace owner does this/)).toBeVisible();
  await expect(summary.getByRole("link", { name: "Open Members & policy" })).toHaveCount(0);
  await expect(summary.getByRole("link", { name: "Open Sources" })).toBeVisible();
  expect(await axeViolations(page)).toEqual([]);
  await summary.screenshot({ path: info.outputPath("workspace-readiness.png") });
  await phoneChecks(page);
});
