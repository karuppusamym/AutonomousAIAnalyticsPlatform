import { ADMIN, expect, requireLive, signIn, test } from "./live";

test.describe("saved sample answer", () => {
  requireLive();
  test.skip(!process.env.LIVE_SAMPLE_THREAD || !process.env.LIVE_SAMPLE_WORKSPACE,
    "Set LIVE_SAMPLE_THREAD and LIVE_SAMPLE_WORKSPACE to the repaired sample conversation");

  test("displays five related records and the answer inspector after reload", async ({page}, info) => {
    const question = "Show 5 sample orders with related details";
    await signIn(page, ADMIN, `/w/${process.env.LIVE_SAMPLE_WORKSPACE}/work/ask?thread=${process.env.LIVE_SAMPLE_THREAD}`);
    const answer = page.locator("article.ask-turn").filter({has:page.getByText(question,{exact:true})}).last();
    await expect(answer.getByRole("table", {name:question})).toBeVisible();
    await expect(answer.getByRole("table", {name:question}).getByRole("row")).toHaveCount(6);
    await expect(answer.getByText(/Related details use validated/)).toBeVisible();
    await answer.getByRole("button",{name:"Inspect",exact:true}).click();
    await expect(page.getByRole("complementary",{name:"Answer inspector"}).getByText("5",{exact:true})).toBeVisible();
    await answer.scrollIntoViewIfNeeded();
    await page.screenshot({path:info.outputPath("sample-related-answer.png")});
  });
});
