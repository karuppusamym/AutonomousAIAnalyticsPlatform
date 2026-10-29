import { WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

/** CONV-04: a report shaped by conversation on the Reports page, then an export request that waits for an approval. */
test.describe("report builder (CONV-04)", () => {
  test("build a report by chat → an unbacked number is refused → preview → export request", async ({ page, api }) => {
    await page.addInitScript(() => {
      try { localStorage.removeItem("analystos.expert"); } catch { /* no storage */ }
    });  // business view, as a report author opens the app
    await signIn(page, `/w/${WS}/outputs?type=report`);
    await expect(page.getByText("Write a report by asking")).toBeVisible();  // open while there is no report yet
    await page.getByLabel("Report title").fill("Weekly operations");
    await page.getByRole("button", { name: "Start report" }).click();

    const outline = page.getByRole("list", { name: "Report sections" });
    await expect(outline).toContainText("Summary");
    const say = async (text: string) => {
      await page.getByLabel("Describe a change to the report").fill(text);
      await page.getByRole("button", { name: "Send" }).click();
    };
    await say("add a section on EMEA backlog");
    await expect(page.getByText("Done: the report is now version 2.")).toBeVisible();
    await expect(outline).toContainText("EMEA backlog by priority");
    await say("include the resolution trend chart");
    await expect(page.getByText("Done: the report is now version 3.")).toBeVisible();
    await expect(outline).toContainText("Resolution time trend");
    await say("set the summary to: We closed 99 tickets.");
    await expect(page.getByText(/Not done — the text quotes numbers this report cannot back: 99/)).toBeVisible();
    await say("set the summary to: The P2 backlog is 31.");
    await expect(page.getByText("Done: the report is now version 4.")).toBeVisible();
    await expect(outline).toContainText("The P2 backlog is 31.");
    // engineering detail stays behind expert tools
    await expect(page.getByText("chart_emea_backlog")).toHaveCount(0);

    await page.getByRole("button", { name: "Show preview" }).click();
    await expect(page.frameLocator('iframe[title="Preview: Weekly operations"]').getByRole("heading", { name: "EMEA backlog", exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Close preview" }).click();  // axe cannot enter a fully sandboxed frame

    await say("export it as a PDF");
    await expect(page.getByText(/Export requested: approval apr_report_1 waits for an approver/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Download PDF" })).toBeVisible();
    expect(await axeViolations(page)).toEqual([]);
    expect(api.unmatched).toEqual([]);
  });
});
