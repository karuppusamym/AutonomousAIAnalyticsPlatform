import { RUN, WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

test("investigation brief shows evidence, limitations and an approval handoff", async ({ page, api }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await signIn(page, `/w/${WS}/work/investigations/${RUN}`);
  const overview = page.getByRole("region", { name: "Investigation overview" });
  await expect(overview.getByRole("heading", { name: "The answer, with the evidence" })).toBeVisible();
  const conversation = page.getByRole("region", { name: "Investigation conversation" });
  await conversation.getByRole("button", { name: "Summarize the findings" }).click();
  await conversation.getByRole("button", { name: "Send", exact: true }).click();
  await expect(conversation.getByText(/One finding is currently verified/)).toBeVisible();
  await page.reload();
  await expect(conversation.getByText(/One finding is currently verified/)).toBeVisible();
  await expect(overview.getByRole("link", { name: "Open evidence for F1" })).toBeVisible();
  await overview.getByText(/Limitations to keep in mind/).click();
  await expect(overview.getByText("Q3 only")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("investigation-desktop.png"), fullPage: true });
  expect(await axeViolations(page)).toEqual([]);
  await overview.getByRole("button", { name: "Review approvals" }).click();
  await expect(page.locator('[aria-label="Investigation approvals"]')).toBeFocused();
  await overview.getByRole("link", { name: "Open evidence for F1" }).click();
  await expect(page).toHaveURL(/outputs\/findings\/ins_demo/);
  expect(api.unmatched).toEqual([]);
});

test("investigation brief fits a phone and respects the dark theme", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark" });
  await signIn(page, `/w/${WS}/work/investigations/${RUN}`);
  const overview = page.getByRole("region", { name: "Investigation overview" });
  await expect(overview).toBeVisible();
  const conversation = page.getByRole("region", { name: "Investigation conversation" });
  await conversation.getByRole("button", { name: "Summarize the findings" }).click();
  await conversation.getByRole("button", { name: "Send", exact: true }).click();
  await expect(conversation.getByText(/One finding is currently verified/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  expect(await axeViolations(page)).toEqual([]);
  await overview.screenshot({ path: testInfo.outputPath("investigation-mobile-dark.png") });
  await conversation.screenshot({ path: testInfo.outputPath("conversation-mobile-dark.png") });
});
