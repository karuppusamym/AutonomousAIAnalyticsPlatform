import { test as base, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { mockBackend, resetMockState } from "../src/test/mockBackend";

/** Every /api request is answered by the shared in-memory backend; nothing reaches a server. */
async function mockApi(page: Page): Promise<string[]> {
  const unmatched: string[] = [];
  resetMockState(); // the build journey and KPI editor change mock state; every test starts clean
  // On the context, so a second tab (an approver's inbox) is answered by the same backend.
  await page.context().route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const r = mockBackend(req.method(), url.pathname + url.search, req.postData(), await req.allHeaders());
    if (r.status === 404) unmatched.push(`${req.method()} ${url.pathname}`);
    await route.fulfill({ status: r.status, body: r.body, contentType: r.contentType });
  });
  return unmatched;
}

/** `auto`: every test is mocked, whether or not it asks for the fixture, so none can reach a live API. */
export const test = base.extend<{ api: { unmatched: string[] } }>({
  api: [async ({ page }, use) => {
    const unmatched = await mockApi(page);
    // The app opens in business view; these journeys exercise the expert panels (SQL console, builds,
    // notebooks, lineage), so expert tools start on. The business-view journey turns them off.
    await page.addInitScript(() => {
      try { localStorage.setItem("analystos.expert", "1"); } catch { /* no storage */ }
    });
    await use({ unmatched });
  }, { auto: true }],
});

export { expect };

/** Sign in through the real login form (the mock accepts the seeded dev password). */
export async function signIn(page: Page, next = "/", email = "admin@analystos.local"): Promise<void> {
  await page.goto(next);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill("ChangeMe123!");
  await page.getByRole("button", { name: "Sign in" }).click();
}

/** axe-core in a real browser, WCAG 2.1 A/AA rules including colour contrast. */
export async function axeViolations(page: Page): Promise<string[]> {
  const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "best-practice"]).analyze();
  return result.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.slice(0, 6).map((n) => {
    const d = n.any[0]?.data as { fgColor?: string; bgColor?: string; contrastRatio?: number } | undefined;
    return `${n.target.join(" ")}${d?.fgColor ? ` [${d.fgColor} on ${d.bgColor} = ${d.contrastRatio}]` : ""}`;
  }).join(" | ")}`);
}
