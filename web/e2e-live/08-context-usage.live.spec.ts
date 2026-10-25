import { ADMIN, expect, requireLive, signIn, test } from "./live";

test.describe("context usage on an existing workspace", () => {
  requireLive();
  test.skip(!process.env.LIVE_GROUNDING_WORKSPACE, "Set LIVE_GROUNDING_WORKSPACE to a workspace with reviewed SLA knowledge");

  test("checks meaning and opens the preflight without starting or approving an investigation", async ({page}, info) => {
    const ws = process.env.LIVE_GROUNDING_WORKSPACE!;
    const question = "Compare SLA breach rates by assignment group using the existing made_sla field.";
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await signIn(page, ADMIN, `/w/${ws}/data/catalog?tab=grounding`);
    await page.getByText("New here? How to get an answer you can trust", {exact:true}).click();
    await expect(page.getByText("Explain the business meaning.", {exact:true})).toBeVisible();
    await page.getByLabel("Business question", {exact:true}).fill(question);
    await page.getByRole("button", {name:"Resolve meaning",exact:true}).click();
    await expect(page.getByRole("button", {name:"Investigate this question"})).toBeVisible({timeout:90_000});
    await page.getByRole("button", {name:"Investigate this question"}).click();
    await expect(page.getByRole("form", {name:"Start explain"}).getByLabel("What should the investigation find out?")).toHaveValue(question);
    await page.getByRole("button", {name:"Close",exact:true}).click();
    await page.setViewportSize({width:390,height:844});
    await expect(page.getByLabel("Choose a data section")).toBeVisible();
    await page.getByLabel("Choose a data section").selectOption("brief");
    await expect(page).toHaveURL(/tab=brief/);
    await page.getByLabel("Choose a data section").selectOption("grounding");
    expect(await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({path:info.outputPath("live-context-mobile.png"),fullPage:true});
    expect(errors).toEqual([]);
  });
});
