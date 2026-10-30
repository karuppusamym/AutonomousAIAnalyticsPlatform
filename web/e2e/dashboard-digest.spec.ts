import { WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

/**
 * PILOT-FIX-07 (rehearsal C09): a business user attaches a dashboard's summary to a schedule from the dashboard's
 * Summary panel, reads where it goes (no destination: saved under Reports, nothing sent), sees it on the schedule
 * screen, and stops it again. Axe-clean on both screens.
 */
test("send a dashboard summary with a schedule, then stop it", async ({ page, api }) => {
  await page.addInitScript(() => {
    try { localStorage.removeItem("analystos.expert"); } catch { /* no storage */ }  // business view
  });
  await signIn(page, `/w/${WS}/outputs?type=dashboard`);
  await page.getByText("Build a dashboard", { exact: true }).click();
  const request = page.getByRole("form", { name: "Build a dashboard from a request" });
  await request.getByLabel("What should the dashboard show?").fill("Build an incident operations dashboard");
  await request.getByRole("button", { name: "Build a draft" }).click();

  const digest = page.getByRole("region", { name: "Send a summary of this dashboard" });
  await digest.scrollIntoViewIfNeeded();
  await digest.getByLabel("Schedule").selectOption("sch_1");
  await digest.getByRole("button", { name: "Add to schedule" }).click();
  await expect(digest.getByText(/Each time “Weekly re-analysis” runs, a summary of this dashboard is saved under Reports; it has no delivery destination, so nothing is sent/)).toBeVisible();
  await expect(digest.getByRole("button", { name: "Stop sending with Weekly re-analysis" })).toBeVisible();
  expect(await axeViolations(page)).toEqual([]);

  // stop it, then send it again
  await digest.getByRole("button", { name: "Stop sending with Weekly re-analysis" }).click();
  await expect(digest.getByText("“Weekly re-analysis” no longer sends a summary of this dashboard.")).toBeVisible();
  await digest.getByLabel("Schedule").selectOption("sch_1");
  await digest.getByRole("button", { name: "Add to schedule" }).click();
  await expect(digest.getByRole("button", { name: "Stop sending with Weekly re-analysis" })).toBeVisible();

  // the schedule screen shows it; the link in the panel goes there
  await digest.getByRole("link", { name: "Weekly re-analysis" }).click();
  const card = page.getByRole("region", { name: "Schedule Weekly re-analysis" });
  await expect(card).toContainText("sends a summary of 1 dashboard");
  expect(await axeViolations(page)).toEqual([]);
  expect(api.unmatched).toEqual([]);
});
