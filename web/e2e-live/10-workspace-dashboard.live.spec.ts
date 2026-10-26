import { ADMIN, expect, requireLive, signIn, test } from "./live";

test.describe("workspace dashboard collection", () => {
  requireLive();
  test.skip(!process.env.LIVE_DASHBOARD_WORKSPACE || !process.env.LIVE_DASHBOARD_ARTIFACT,
    "Set LIVE_DASHBOARD_WORKSPACE and LIVE_DASHBOARD_ARTIFACT to a prepared topic draft");

  test("opens the workspace-labelled topic draft and its saved chart previews", async ({page}, info) => {
    await signIn(page, ADMIN, `/w/${process.env.LIVE_DASHBOARD_WORKSPACE}/outputs?type=dashboard&artifact=${process.env.LIVE_DASHBOARD_ARTIFACT}`);
    await expect(page.getByRole("heading",{level:2,name:/Retail Orders.*Delivery reliability/}).last()).toBeVisible();
    await expect(page.getByRole("option",{name:/Retail Orders.*Delivery reliability/})).toHaveCount(1);
    await expect(page.getByLabel("Dashboard preview")).toBeVisible();
    await expect(page.getByText(/This draft reuses saved investigation chart snapshots/)).toBeVisible();
    await expect(page.getByText("Chart not found",{exact:true})).toHaveCount(0);
    await page.screenshot({path:info.outputPath("workspace-dashboard-draft.png")});
  });
});
