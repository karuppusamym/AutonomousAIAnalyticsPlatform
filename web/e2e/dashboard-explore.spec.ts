import { WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

/**
 * CONV-06: explore a draft dashboard by keyboard. Filtering by a priority re-queries the tiles that share the field
 * and marks the others "Not filtered"; a month drills to its days with breadcrumbs back; "Show rows" lists the rows
 * behind a selection; a chip removes its filter; "Apply as dashboard filter" saves it as a new version. Axe-clean with
 * filters, a drill and the rows panel open.
 */
test("cross-filter, drill and rows on a draft dashboard", async ({ page, api }, info) => {
  await page.addInitScript(() => {
    try { localStorage.removeItem("analystos.expert"); } catch { /* no storage */ }  // business view
  });
  await signIn(page, `/w/${WS}/outputs?type=dashboard`);
  await page.getByText("Build a dashboard", { exact: true }).click();
  const request = page.getByRole("form", { name: "Build a dashboard from a request" });
  await request.getByLabel("What should the dashboard show?").fill("Build an incident operations dashboard");
  await request.getByRole("button", { name: "Build a draft" }).click();
  const bar = page.getByRole("region", { name: "Dashboard filters" });
  await expect(bar).toContainText("Click a bar, slice or legend item");

  // ---- cross-filter (keyboard path: the list under the chart does what a click on a bar does)
  await page.getByLabel("Filter the dashboard by Priority").selectOption({ label: "Priority: 1 - Critical" });
  const chip = bar.getByRole("button", { name: "Remove filter Priority = 1 - Critical" });
  await expect(chip).toBeVisible();
  await expect(page.getByRole("group", { name: "Resolution time by category" }).getByText("Filtered", { exact: true })).toBeVisible();
  await expect(page.getByRole("group", { name: "Incident count per month" }).getByText("Not filtered")).toBeVisible();
  await expect(page.getByRole("group", { name: "Incident count by priority" }).getByText("Your selection")).toBeVisible();
  await expect(bar).toContainText("3 filtered, 1 not filtered");
  await expect(page.getByText(/qx_view_/)).toHaveCount(0);  // query ids only in expert view

  // ---- drill a month to its days, breadcrumbs back
  const trend = page.getByRole("group", { name: "Incident count per month" });
  await trend.getByLabel("Drill into day for Incident count per month").selectOption({ label: "2026-09-01" });
  const crumbs = trend.getByRole("navigation", { name: "Drill path for Incident count per month" });
  await expect(crumbs).toContainText("Sep 2026");
  await expect(trend.getByText("Drilled")).toBeVisible();

  // ---- rows behind a selection: only the columns the viewer may see
  await page.getByRole("group", { name: "Incident count by priority" }).getByRole("button", { name: "Show rows" }).click();
  const rows = page.getByRole("region", { name: /Rows behind Incident count by priority/ });
  await expect(rows.getByText("INC00012")).toBeVisible();
  await expect(rows).toContainText("1 column is hidden by your access policy.");
  await expect(rows.getByRole("button", { name: "Close rows" })).toBeFocused();

  expect(await axeViolations(page)).toEqual([]);
  await page.screenshot({ path: info.outputPath("dashboard-explore.png"), fullPage: true });

  await rows.getByRole("button", { name: "Close rows" }).press("Enter");
  await expect(rows).toHaveCount(0);
  await crumbs.getByRole("button", { name: "All" }).click();
  await expect(trend.getByText("Drilled")).toHaveCount(0);

  // ---- a chip removes its filter from the keyboard; then save a filter as a new version
  await chip.focus();
  await page.keyboard.press("Enter");
  await expect(bar.getByRole("button", { name: /Remove filter/ })).toHaveCount(0);
  await page.getByLabel("Filter the dashboard by Category").selectOption({ label: "Category: Network" });
  await expect(bar.getByRole("button", { name: "Remove filter Category = Network" })).toBeVisible();
  await expect(bar).toContainText(/filtered/);
  await bar.getByRole("button", { name: "Apply as dashboard filter" }).click();
  await expect(page.getByText("· v2")).toBeVisible();
  await expect(page.getByText('incident.category eq "Network"', { exact: true })).toBeVisible();  // the saved filter tag
  await expect(page.getByRole("group", { name: "Resolution time by category" }).getByText("Dashboard filter")).toBeVisible();

  expect(await axeViolations(page)).toEqual([]);
  expect(api.unmatched).toEqual([]);
});
