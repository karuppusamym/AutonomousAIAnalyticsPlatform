import { WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

/**
 * CONV-01/02: describe a dashboard, get a draft built from governed questions, refine it by asking (a question the
 * platform cannot ground gets a clarifying reply, never a guessed chart), then request publication (an approval).
 */
test("prompt to draft, refine by asking, request publication", async ({ page, api }, info) => {
  await page.addInitScript(() => {
    try { localStorage.removeItem("analystos.expert"); } catch { /* no storage */ }  // business view, as people see it
  });
  await signIn(page, `/w/${WS}/outputs?type=dashboard`);
  await page.getByText("Build a dashboard", { exact: true }).click();
  const request = page.getByRole("form", { name: "Build a dashboard from a request" });
  await request.getByLabel("What should the dashboard show?").fill("Build an incident operations dashboard");
  await request.getByRole("button", { name: "Build a draft" }).click();

  const result = page.getByLabel("Draft result");
  await expect(result.getByText(/5 of 6 tiles built/)).toBeVisible();
  await expect(result.getByRole("list", { name: "Not built" })).toContainText("SLA breach rate by priority");
  await expect(page).toHaveURL(/artifact=art_draft_1/);
  await expect(page.getByLabel("Dashboard layout")).toBeVisible();
  await expect(page.getByText("priority: any")).toBeVisible();
  await expect(page.getByText(/query qx_/)).toHaveCount(0);  // query detail only in expert view

  const say = page.getByLabel("Describe a change");
  await say.fill("add weather by city");
  await page.getByRole("button", { name: "Send" }).click();
  const clarify = page.getByRole("status", { name: "Clarifying question" });
  await expect(clarify).toContainText("no table in your scope has weather data");
  await say.fill("add resolution time by priority");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText(/Added a bar for “resolution time by priority”, from an approved metric/)).toBeVisible();
  await expect(page.getByText("Applied: now version 2.")).toBeVisible();
  await expect(page.getByLabel("Dashboard layout").getByRole("button", { name: /Resolution time by priority/ })).toBeVisible();

  await page.getByRole("button", { name: "Request publication" }).click();
  await expect(page.getByText(/Publication requested \(approval apr_draft_pub, medium risk\)/)).toBeVisible();

  expect(await axeViolations(page)).toEqual([]);
  await page.screenshot({ path: info.outputPath("dashboard-draft.png"), fullPage: true });
  expect(api.unmatched).toEqual([]);
});
