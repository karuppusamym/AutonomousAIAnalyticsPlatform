import type { Page } from "@playwright/test";
import { BUILD_NEW, BUILD_PREV, CAPABILITIES, INSIGHT, RUN, THREAD_NEW, USER, WORKSPACE, WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

const ANALYST_EMAIL = "analyst@analystos.local";

/** Sign in as an analyst member (not a platform admin); every other request stays on the shared mock. */
async function asAnalyst(page: Page): Promise<void> {
  const analyst = { ...USER, id: "usr_analyst", email: ANALYST_EMAIL, name: "Ana Analyst", is_admin: false };
  await page.route("**/api/auth/login", (r) => r.fulfill({ json: { access_token: "mock-token", token_type: "bearer", user: analyst } }));
  await page.route("**/api/auth/me", (r) => r.fulfill({ json: analyst }));
  await page.route(`**/api/workspaces/${WS}`, (r) => (r.request().method() === "GET" ? r.fulfill({ json: { ...WORKSPACE, role: "analyst" } }) : r.fallback()));
}

/** A registry with a query engine installed, so Prepare data is available. */
async function withEngine(page: Page): Promise<void> {
  await page.route("**/api/capabilities?**", (r) => r.fulfill({ json: { digest: "d", capabilities: [...CAPABILITIES, {
    ...CAPABILITIES[0], id: "engine.duckdb", kind: "Engine", ref: "engine.duckdb@1.0.0", summary: "DuckDB engine" }].map((c) => ({ ...c, enabled: true })) } }));
}

test.describe("light IA (spec v4 §15)", () => {
  test("login → Overview → Work (Ask, an investigation) → Data → the gear's platform settings", async ({ page, api }) => {
    await signIn(page);

    // Overview: workspace picker, then only what needs the person and one Start work.
    await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: /IT Service Management/ }).click();
    await expect(page).toHaveURL(`/w/${WS}`);
    const needs = page.locator("section", { has: page.getByRole("heading", { name: "What needs you" }) });
    await expect(needs.getByText("Pending approvals")).toBeVisible();
    await expect(needs.locator(".stat", { hasText: "Open alerts" }).locator(".stat-value")).toHaveText("1");
    await expect(needs.locator(".stat", { hasText: "Void findings" }).locator(".stat-value")).toHaveText("1");
    await expect(page.getByRole("button", { name: "Start work" })).toHaveCount(1);

    // Work → Ask: a question through the side nav, answered by the governed SQL path.
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("group", { name: "Work" }).getByRole("link", { name: "Ask" }).click();
    await expect(page).toHaveURL(`/w/${WS}/work/ask`);
    await page.getByLabel("Question", { exact: true }).fill("How many P1 incidents per assignment group?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    await expect(page.getByText("P1 incidents by assignment group.")).toBeVisible();
    await expect(page.getByText(/SELECT assignment_group/)).toBeVisible();

    // Work: open an investigation from the list.
    await nav.getByRole("group", { name: "Work" }).getByRole("link", { name: "Work", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Work", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: "Why are P1 resolution times rising?" }).first().click();
    await expect(page).toHaveURL(`/w/${WS}/work/investigations/${RUN}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Why are P1 resolution times rising?");

    // Data: the catalog is the first tab of Catalog & definitions.
    await nav.getByRole("group", { name: "Data" }).getByRole("link", { name: "Catalog & definitions" }).click();
    await expect(page).toHaveURL(`/w/${WS}/data/catalog`);
    await expect(page.getByRole("tab", { name: "Catalog", selected: true })).toBeVisible();
    await expect(page.getByText("Incidents").first()).toBeVisible();

    // The gear: platform settings (admin), reached through the command palette.
    await page.keyboard.press("Control+k");
    const palette = page.getByRole("dialog");
    await expect(palette).toBeVisible();
    await palette.getByRole("combobox").fill("platform settings");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL("/settings/platform");
    await expect(page.getByRole("heading", { name: "Platform settings", level: 1 })).toBeVisible();
    await expect(palette).toBeHidden();

    expect(api.unmatched).toEqual([]);
  });

  test("old URLs redirect into the new areas", async ({ page }) => {
    await signIn(page, `/w/${WS}/monitoring?tab=alerts`);
    await expect(page).toHaveURL(`/w/${WS}/operate/autonomy?view=monitors&tab=alerts`);
    await page.goto("/admin");
    await expect(page).toHaveURL("/settings/registry");
    await page.goto("/operate/settings");
    await expect(page).toHaveURL("/settings/platform");
    await page.goto(`/w/${WS}/insights/${INSIGHT}`);
    await expect(page).toHaveURL(`/w/${WS}/outputs/findings/${INSIGHT}`);
    await page.goto(`/w/${WS}/investigate/${RUN}`);
    await expect(page).toHaveURL(`/w/${WS}/work/investigations/${RUN}`);
    await page.goto(`/w/${WS}/build/studio?tab=kpis`);
    await expect(page).toHaveURL(`/w/${WS}/data/catalog?tab=metrics`);
    await page.goto(`/w/${WS}/build/studio?tab=dashboards&dashboard=art_dash`);
    await expect(page).toHaveURL(`/w/${WS}/outputs?type=dashboard&dashboard=art_dash`);
  });

  test("theme toggle persists across reloads", async ({ page }) => {
    await signIn(page);
    await expect(page.getByRole("heading", { name: "Workspaces", level: 1 })).toBeVisible();
    const html = page.locator("html");
    await page.getByRole("button", { name: /System theme/ }).click();
    await expect(html).toHaveAttribute("data-theme", "light");
    await page.getByRole("button", { name: /Light theme/ }).click();
    await expect(html).toHaveAttribute("data-theme", "dark");
    await page.reload();
    await expect(html).toHaveAttribute("data-theme", "dark");
  });
});

/** Top-level nav entries: job kinds and panels must never add one. */
async function navEntries(page: Page): Promise<number> {
  // counted once the workspace nav is rendered (right after sign-in the page may still be on /login)
  await expect(page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Outputs" })).toBeVisible();
  return page.getByRole("navigation", { name: "Main" }).getByRole("link").count();
}

test.describe("job-kind journeys without new top-level screens", () => {
  test("analyst (keyboard only): Start work → Explain → preflight → the investigation; no admin screens", async ({ page, api }) => {
    await asAnalyst(page);
    await signIn(page, `/w/${WS}`, ANALYST_EMAIL);
    await expect(page.getByRole("heading", { name: "What needs you" })).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Main" });
    for (const t of ["Capability registry", "Platform settings", "Usage & cost", "Members & policy"]) {
      await expect(nav.getByRole("link", { name: t })).toHaveCount(0);
    }
    const before = await navEntries(page);

    const start = page.getByRole("button", { name: "Start work" });
    await start.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "Start work" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Close" })).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(dialog.getByRole("button", { name: /^Explain/ })).toBeFocused();
    await page.keyboard.press("Enter");
    const form = dialog.getByRole("form", { name: "Start explain" });
    await expect(form.getByRole("heading", { name: "Before it starts" })).toBeVisible();
    await expect(form.getByText(/Executes; publish needs approval/)).toBeVisible();
    await form.getByRole("button", { name: "Start investigation" }).focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(`/w/${WS}/work/investigations/${RUN}`);
    expect(await navEntries(page)).toBe(before);

    await page.goto("/settings/platform");
    await expect(page.getByText("Platform settings is for platform administrators")).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("engineer: Start work → Prepare data → load a file and preview a recipe; the output is listed in Outputs", async ({ page, api }) => {
    await withEngine(page);
    await signIn(page, `/w/${WS}/work`);
    const before = await navEntries(page);
    await page.getByRole("button", { name: "Start work" }).click();
    await page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /Prepare data/ }).click();
    await expect(page).toHaveURL(`/w/${WS}/work?tab=prepare`);
    await expect(page.getByRole("tab", { name: "Prepare data", selected: true })).toBeVisible();

    const load = page.getByRole("form", { name: "Load a file" });
    await load.getByLabel("File").setInputFiles({ name: "orders.csv", mimeType: "text/csv", buffer: Buffer.from("id,total\n1,10\n") });
    await load.getByLabel("Table name").fill("orders");
    await load.getByRole("button", { name: "Load file" }).click();
    await expect(load.getByText("Loaded into stg_files.orders (replace).")).toBeVisible();

    await page.getByRole("list", { name: "Recipes" }).getByRole("button", { name: /p1_incidents_clean/ }).click();
    await page.getByRole("button", { name: "Preview" }).click();
    await expect(page.getByText(/Preview — nothing was written/)).toBeVisible();
    await expect(page.getByRole("table", { name: "Preview of clean" })).toContainText("INC001");

    await page.getByRole("link", { name: "Outputs → Prepared data" }).click();
    await expect(page).toHaveURL(`/w/${WS}/outputs?type=prepared`);
    await expect(page.getByRole("list", { name: "Outputs" }).getByText("p1_incidents_clean")).toBeVisible();
    expect(await navEntries(page)).toBe(before);
    expect(api.unmatched).toEqual([]);
  });

  test("a kind the server cannot run (Forecast) shows its reasons and remediation and never starts a pretend run", async ({ page, api }) => {
    const posts: string[] = [];
    page.on("request", (r) => { if (r.method() === "POST") posts.push(new URL(r.url()).pathname); });
    await signIn(page, `/w/${WS}`);
    const before = await navEntries(page);
    await page.getByRole("button", { name: "Start work" }).click();
    const kinds = page.getByRole("list", { name: "Job kinds" });
    const forecast = kinds.locator("li.job-kind").filter({ hasText: "Forecast" });
    const why = forecast.getByRole("list", { name: "Why Forecast cannot start" });
    await expect(why.getByText("method.ml.forecast is turned off in this workspace")).toBeVisible();
    await expect(why.getByText(/A workspace owner enables it/)).toBeVisible();
    await expect(forecast.getByRole("button")).toHaveCount(0);
    await forecast.click();
    await expect(page.getByRole("dialog", { name: "Start work" })).toBeVisible();
    await expect(page.getByRole("form")).toHaveCount(0);
    expect(posts.filter((p) => p.endsWith("/analysis"))).toEqual([]);
    await page.keyboard.press("Escape");
    await expect(page.getByRole("button", { name: "Start work" })).toBeFocused();
    expect(await navEntries(page)).toBe(before);
    expect(api.unmatched).toEqual([]);
  });

  test("narrow layout: the nav collapses behind a toggle and nothing scrolls sideways", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page, `/w/${WS}`);
    await expect(page.getByRole("heading", { name: "What needs you" })).toBeVisible();
    const toggle = page.getByRole("button", { name: "Toggle navigation" });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.click();
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Outputs" }).click();
    await expect(page).toHaveURL(`/w/${WS}/outputs`);
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    for (const path of [`/w/${WS}/outputs`, `/w/${WS}/data/catalog?tab=definitions`, `/w/${WS}/operate/schedules`]) {
      await page.goto(path);
      await expect(page.locator("main h1")).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, path).toBeLessThanOrEqual(1);
    }
  });
});

test.describe("Data Thread (P7-04, P7-05)", () => {
  test("analyst (keyboard): open an investigation's thread → edit a step → dependents re-run, v1 stays readable → fork → compare → merge", async ({ page, api }) => {
    await asAnalyst(page);
    await signIn(page, `/w/${WS}/work`, ANALYST_EMAIL);
    const before = await navEntries(page);
    await page.getByRole("link", { name: "Data Thread of Why are P1 resolution times rising?" }).click();
    await expect(page).toHaveURL(`/w/${WS}/work?tab=thread&container=run%3Arun_demo`);
    const step = (name: string) => page.getByRole("listitem", { name: new RegExp(`^Step \\d+: ${name}`) });
    const mttr = step("Mean P1 resolution hours by group");
    await expect(mttr.getByText("verified")).toBeVisible();

    // edit with the keyboard: the new version re-runs and so do the steps that read it
    await mttr.getByRole("button", { name: /^Edit/ }).focus();
    await page.keyboard.press("Enter");
    const sql = mttr.getByLabel("SQL");
    await sql.fill("SELECT assignment_group, AVG(resolution_hours) AS mttr_hours FROM stg_sn.incident WHERE priority = '1' AND close_code != 'auto' GROUP BY 1");
    await mttr.getByRole("button", { name: "Save and re-run" }).press("Enter");
    await expect(page.getByText(/is now version 2\. 2 steps that read it re-ran; 3 earlier verdicts are now void/)).toBeVisible();
    await expect(step("Network is the slowest group").getByText("re-ran: flagged")).toBeVisible();
    await mttr.getByRole("button", { name: /^Versions \(2\)/ }).click();
    const v1 = mttr.getByRole("listitem", { name: "Version 1" });
    await expect(v1.getByText(/Why void: edit: the step was edited to v2/)).toBeVisible();
    await v1.getByRole("button", { name: "Show the result of v1" }).click();
    await expect(v1.getByRole("table", { name: "Result of version 1" })).toContainText("9.4");

    // fork a "what if" from the count step, compare with main side by side, merge into a report
    await step("P1 incidents by assignment group").getByRole("button", { name: /Fork from here/ }).click();
    await page.getByLabel("Branch name").fill("what if");
    await page.getByRole("button", { name: "Fork", exact: true }).click();
    await expect(page).toHaveURL(/branch=brn_whatif/);
    await expect(step("Plan: why").getByText("from the parent branch")).toBeVisible();
    await page.getByLabel("Compare with").selectOption({ label: "main" });
    await page.getByRole("button", { name: "Compare side by side" }).click();
    await expect(page.getByRole("group", { name: "Compare main with what if" })).toBeVisible();
    await page.getByLabel("Report title").fill("P1 thread");
    await page.getByRole("button", { name: "Merge what if into a report" }).click();
    await expect(page.getByText(/Merged into the report "P1 thread"/)).toBeVisible();
    // an analyst cannot pin (editor), so no Pin control is offered
    await expect(page.getByRole("button", { name: /^Pin/ })).toHaveCount(0);
    expect(await navEntries(page)).toBe(before);
    expect(api.unmatched).toEqual([]);
  });
});

/** An approver decides in the inbox in another tab (the requester's page keeps its state). */
async function approveInInbox(page: Page, action: RegExp): Promise<void> {
  const inbox = await page.context().newPage();
  await inbox.goto(`/w/${WS}/operate/approvals`);
  const card = inbox.locator("article.approval", { hasText: action });
  await card.getByLabel("Reason").fill("reviewed");
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(card).toHaveCount(0);
  await inbox.close();
}

test.describe("governed ML (P5-03)", () => {
  test("spec → evaluate → promote → score, without a new top-level screen", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/work`);
    const before = await navEntries(page);
    await page.getByRole("button", { name: "Start work" }).click();
    await page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /Predict/ }).click();
    await page.getByRole("form", { name: "Start predict" }).getByRole("button", { name: "Write a new spec" }).click();
    await expect(page).toHaveURL(`/w/${WS}/work?tab=experiments&new=predict`);

    // spec: prefilled from the rules-first proposal, the baseline mandatory, the metric's direction stated
    const what = page.getByRole("form", { name: "What to predict" });
    await what.getByLabel("Table").fill("stg_sn.incident");
    await what.getByLabel("Target column").fill("breached_sla");
    await what.getByRole("button", { name: "Propose a spec" }).click();
    const spec = page.getByRole("form", { name: "ML spec" });
    await expect(spec.getByLabel("Feature 1", { exact: true })).toHaveValue("priority");
    await expect(spec.getByRole("checkbox", { name: /dummy_prior \(baseline, always trained\)/ })).toBeDisabled();
    await spec.getByRole("button", { name: "Save, publish and train" }).click();

    // evaluate: split diagram, trials on one split with the metric direction, a consumed holdout, the card
    const exp = page.getByRole("region", { name: "Experiment mlx_1" });
    await expect(exp.getByRole("img", { name: /chronological split/ })).toBeVisible();
    await expect(exp.getByRole("table", { name: /Baseline and candidates on split/ })).toContainText("higher is better");
    await expect(exp.getByRole("note", { name: "Holdout consumed" })).toBeVisible();
    await expect(exp.getByRole("region", { name: "Model card" })).toContainText("Intended use");

    // promote: an approval decided in the inbox
    await exp.getByRole("button", { name: "Request promotion of v2" }).click();
    await approveInInbox(page, /Promote a model version to champion/);
    await exp.getByRole("button", { name: "Continue with the approved request" }).click();
    await expect(exp.getByText("Promoted: p1_breach v2 is now the champion.")).toBeVisible();

    // score approved data with the champion in Outputs; rejected rows are shown, not hidden in a green count
    await page.goto(`/w/${WS}/outputs?type=model`);
    const model = page.getByRole("region", { name: "Model p1_breach" });
    await model.getByText("Score approved data with v2").click();
    await model.getByRole("button", { name: "Prepare the scoring definition" }).click();
    await model.getByRole("button", { name: "Request approval to score" }).click();
    await approveInInbox(page, /Score data with the champion model/);
    await model.getByRole("button", { name: "Continue with the approved request" }).click();
    await expect(page.getByText(/Scored 4,198 of 4,210 rows/)).toBeVisible();
    await expect(page.getByText(/12 rejected rows are kept in/)).toBeVisible();
    expect(await navEntries(page)).toBe(before);
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("pipelines (P6-03)", () => {
  test("engineer: dry run → approve → materialize → rollback, with quarantined rows visible", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/work`);
    const before = await navEntries(page);
    await page.getByRole("button", { name: "Start work" }).click();
    await page.getByRole("list", { name: "Job kinds" }).getByRole("button", { name: /Prepare data/ }).click();
    await expect(page).toHaveURL(`/w/${WS}/work?tab=prepare`);
    await page.getByRole("list", { name: "Pipelines" }).getByRole("button", { name: /p1_clean v2/ }).click();
    const detail = page.getByRole("region", { name: "Pipeline p1_clean v2" });
    await expect(detail.getByRole("list", { name: "Source to output" })).toContainText("aos_out.p1_clean");

    await detail.getByRole("button", { name: "Dry run" }).click();
    const run = detail.getByRole("region", { name: "Dry run prn_1" });
    const ledger = run.getByRole("group", { name: "Rows" });
    await expect(ledger.locator(".stat", { hasText: "Quarantined rows" })).toContainText("20");
    await expect(ledger.locator(".stat", { hasText: "Late rows" })).toContainText("not reported");
    await expect(run.getByRole("table", { name: "Join diagnostics" })).toBeVisible();
    await expect(run.getByRole("table", { name: "Reconciliation" })).toContainText("hours_total");

    await detail.getByRole("button", { name: "Materialize this candidate" }).click();
    await approveInInbox(page, /Materialize a pipeline's output/);
    await detail.getByRole("button", { name: "Continue with the approved request" }).click();
    await expect(detail.getByText(/aos_out\.p1_clean version 3 \(4,190 rows\)/)).toBeVisible();

    await detail.getByRole("link", { name: "Outputs → Managed tables" }).click();
    await expect(page).toHaveURL(`/w/${WS}/outputs?type=table&table=p1_clean`);
    page.once("dialog", (d) => void d.accept());
    await page.getByRole("button", { name: "Roll back v3" }).click();
    await expect(page.getByText(/serves version 2 again; version 3 is kept as rolled back/)).toBeVisible();
    expect(await navEntries(page)).toBe(before);
    expect(api.unmatched).toEqual([]);
  });

  test("narrow layout: the dry-run result and the thread do not scroll sideways", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page, `/w/${WS}/work?tab=prepare&pipeline=pip_2`);
    await expect(page.getByRole("region", { name: "Dry run prn_0" })).toBeVisible();
    for (const path of [`/w/${WS}/work?tab=prepare&pipeline=pip_2`, `/w/${WS}/work?tab=thread&container=run:run_demo`,
      `/w/${WS}/work?tab=experiments&experiment=mlx_0`, `/w/${WS}/data/catalog?tab=brief`, `/w/${WS}/outputs?type=model`]) {
      await page.goto(path);
      await expect(page.locator("main h1")).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, path).toBeLessThanOrEqual(1);
    }
  });
});

test.describe("wave-1 panels", () => {
  test("Why this number? and a void finding's cause", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/outputs?type=finding`);
    const list = page.getByRole("list", { name: "Outputs" });
    await expect(list.getByText(/Why void: data: the snapshot of stg_sn.incident changed/)).toBeVisible();
    await list.getByText(/Network group drives P1 breaches/).click();
    const why = page.getByRole("list", { name: "Numbers in this finding" }).getByRole("button", { name: /Why this number/ });
    await why.click();
    const drawer = page.getByRole("dialog", { name: "Why 2.1x?" });
    await expect(drawer.getByText("The query that read the data")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(why).toBeFocused();
    expect(api.unmatched).toEqual([]);
  });

  test("schedule upgrade available → review → accept", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/operate/schedules`);
    const pins = page.getByLabel("Pinned versions");
    await expect(pins.getByText("upgrade available")).toBeVisible();
    await pins.getByRole("button", { name: "What would change" }).click();
    await expect(pins.getByText("steps.verify.second_method")).toBeVisible();
    await pins.getByRole("button", { name: "Accept upgrade" }).click();
    await expect(page.getByText(/Upgraded. The next run uses the new versions/)).toBeVisible();
    await expect(pins.getByText("up to date")).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("Ask (P4-U02)", () => {
  test("ask → promote to monitor → investigate", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question", { exact: true }).fill("How many P1 incidents per assignment group?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const answer = page.getByRole("article", { name: "Question 1" });
    await expect(answer.getByText("P1 incidents by assignment group.")).toBeVisible();
    await expect(answer.getByRole("list", { name: "Provenance" }).getByText("Validated by the query gateway")).toBeVisible();
    await expect(answer.getByRole("img", { name: /bar chart/ })).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`thread=${THREAD_NEW}`));

    const inspector = page.getByRole("complementary", { name: "Answer inspector" });
    await inspector.getByRole("tab", { name: "Decision" }).click();
    await expect(inspector.getByText(/ask_route: generate — decided by rule/)).toBeVisible();

    const promote = answer.getByRole("region", { name: "Promote this answer" });
    await promote.getByRole("button", { name: "Monitor this" }).click();
    await promote.getByLabel("Every").selectOption("month");
    await promote.getByRole("button", { name: "Create monitor" }).click();
    await expect(promote.getByText(/Monitor "P1 incidents per assignment group" created/)).toBeVisible();

    await promote.getByRole("button", { name: "Investigate why" }).click();
    await promote.getByRole("button", { name: "Start investigation" }).click();
    await expect(page).toHaveURL(`/w/${WS}/work/investigations/${RUN}`);
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Why are P1 resolution times rising?");
    expect(api.unmatched).toEqual([]);
  });

  test("a distribution is answered by the rules without a model, and a follow-up grouping is one click", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question", { exact: true }).fill("distribution of incident");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const answer = page.getByRole("article").first();
    await expect(answer.getByText("Built from the catalog · no model")).toBeVisible();
    await answer.getByRole("region", { name: "Follow-up questions" }).getByRole("button", { name: "distribution of incident by contact channel" }).click();
    await expect(page.getByRole("article")).toHaveCount(2);
    expect(api.unmatched).toEqual([]);
  });

  test("no provider key: the refusal says which key to set and to restart the containers", async ({ page }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question", { exact: true }).fill("Which configuration items had incidents in two consecutive weeks?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const turn = page.getByRole("article", { name: "Question 1" });
    await expect(turn.getByText("No model provider key is set for the API")).toBeVisible();
    await expect(turn.getByText(/Set OPENROUTER_API_KEY for the api and worker containers/)).toBeVisible();
  });

  test("a vague question gets the clarify refusal with its remedy", async ({ page }) => {
    await signIn(page, `/w/${WS}/ask`);
    await page.getByLabel("Question", { exact: true }).fill("what about it?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    const turn = page.getByRole("article", { name: "Question 1" });
    await expect(turn.getByText("The question needs more detail")).toBeVisible();
    await expect(turn.getByRole("button", { name: "Rephrase the question" })).toBeVisible();
  });
});

test.describe("investigation board (P4-U03)", () => {
  test("board → why trust this → reject a finding → redirect by chat", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/investigate/${RUN}`);
    await expect(page.getByRole("radio", { name: /Supported \(1\)/ })).toBeVisible();
    await expect(page.getByRole("article", { name: "Hypothesis H1" }).getByText("Network resolves P1s slower")).toBeVisible();
    // Raw JSON is never on the default path: every .json block sits in a closed Technical details.
    const stray = await page.locator(".json").evaluateAll((els) => els.filter((e) => !e.closest("details[data-technical]:not([open])")).length);
    expect(stray).toBe(0);

    const f1 = page.getByRole("article", { name: "Finding F1" });
    await f1.getByRole("button", { name: "Why trust this" }).click();
    const dialog = page.getByRole("dialog", { name: "Why trust F1?" });
    await expect(dialog.getByText(/identical result hash on re-run/)).toBeVisible();
    await expect(dialog.getByText(/q = 0\.0010/)).toBeVisible();
    await expect(dialog.getByText(/representative \(time_window\)/)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(f1.getByRole("button", { name: "Why trust this" })).toBeFocused();

    await f1.getByRole("button", { name: "Reject" }).click();
    await f1.getByLabel(/Why is F1 wrong/).fill("Network was reorganised in August.");
    await f1.getByRole("button", { name: "Reject finding" }).click();
    await expect(f1.getByText(/Replanned to plan v2/)).toBeVisible();

    await page.getByLabel("Message").fill("Focus on the Network group");
    // The composer defaults to a question about saved evidence; a redirect is chosen explicitly.
    await page.getByLabel("What would you like to do?").selectOption({ label: "Redirect — change focus / filters" });
    await page.getByRole("button", { name: "Send" }).click();
    await expect(page.getByText(/Focus on the Network group\./)).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("build (P4-U05)", () => {
  test("plan build → diff and dry run → approval in the inbox → approved → status", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/work?tab=builds`);
    await expect(page.getByRole("tab", { name: "dbt builds", selected: true })).toBeVisible();
    const jobs = page.getByRole("list", { name: "Build jobs" });
    await expect(jobs.getByRole("listitem")).toHaveCount(1);

    // Plan: the elt_build run generates and dry-runs the project, then asks for an approval.
    const plan = page.getByRole("form", { name: "Plan a build" });
    await expect(plan.getByLabel("Target schema")).toHaveValue("aos_mart");
    await plan.getByRole("button", { name: "Plan build" }).click();
    await expect(jobs.getByRole("listitem")).toHaveCount(2);

    // Review: the files against the previous job for this target, the dry run and the estimate.
    await expect(page.getByText(`Compared with job ${BUILD_PREV}`)).toBeVisible();
    await expect(page.getByText("1 added, 2 modified, 0 removed")).toBeVisible();
    await expect(page.getByLabel("Diff of models/p1_incidents.sql")).toContainText("+where priority = '1'");
    await expect(page.getByLabel("Estimate")).toContainText("4,210");
    await expect(page.getByText("fails: dropped")).toBeVisible();
    const steps = page.getByRole("list", { name: "Build status" });
    await expect(steps.locator("li[aria-current=step]")).toContainText("waiting for an approver in the inbox");
    await expect(page.getByRole("button", { name: /^Approve/ })).toHaveCount(0);

    // Approve: in the approvals inbox, bound to the payload hash like every other side effect.
    await page.getByRole("link", { name: "Review in the approvals inbox" }).click();
    await expect(page).toHaveURL(`/w/${WS}/operate/approvals`);
    const card = page.locator("article.approval", { hasText: "Build with dbt" });
    await expect(card.getByText("postgres:analytics/aos_mart")).toBeVisible();
    await expect(card.getByText("risk: high")).toBeVisible();
    await card.getByLabel("Reason").fill("diff reviewed");
    await card.getByRole("button", { name: "Approve" }).click();
    await expect(card).toHaveCount(0); // decided: it leaves the Pending tab
    await page.getByRole("tab", { name: "All" }).click();
    await expect(card.locator(".badge", { hasText: "approved" })).toBeVisible();

    // Status: back in Build, the resumed run has built the tables.
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("group", { name: "Work" }).getByRole("link", { name: "Work", exact: true }).click();
    await page.getByRole("tab", { name: "dbt builds" }).click();
    await jobs.getByRole("listitem").first().getByRole("button").click();
    await expect(page).toHaveURL(new RegExp(`job=${BUILD_NEW}`));
    await expect(page.getByRole("heading", { name: "Result" })).toBeVisible();
    await expect(page.getByText("success: 2")).toBeVisible();
    await expect(steps.locator("li.step-done")).toHaveCount(4);
    expect(api.unmatched).toEqual([]);
  });

  test("KPI editor: live validation, propose, separation of duties", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/data/catalog?tab=metrics`);
    const form = page.getByRole("form", { name: "Propose a KPI" });
    await form.getByLabel("Name", { exact: true }).fill("p1_count");
    await form.getByLabel("Expression").fill("priority");
    await expect(form.getByText("not an aggregate expression")).toBeVisible();
    await form.getByLabel("Expression").fill("COUNT(*)");
    await expect(form.getByText(/well-formed aggregate/)).toBeVisible();
    await form.getByRole("button", { name: "Propose KPI" }).click();
    await expect(form.getByText(/waits for an approver who is not you/)).toBeVisible();
    await expect(page.getByText(/You proposed this version/)).toBeVisible();

    await page.getByRole("list", { name: "KPIs" }).getByRole("button", { name: /mttr hours/ }).click();
    await page.getByRole("button", { name: "Approve v2" }).click();
    await expect(page.getByRole("button", { name: "Approve v2" })).toHaveCount(0);
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("knowledge studio (P4-U04)", () => {
  test("review an AI suggestion → approve into the pack → ask → it is a receipt on the Evidence tab", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/data/catalog?tab=review`);
    await expect(page.getByRole("tab", { name: "Review queue", selected: true })).toBeVisible();

    // Review: the model's draft with each field's confidence and provenance.
    const draft = page.getByRole("article", { name: "Draft: Reopen rate" });
    await expect(draft.getByRole("meter", { name: "Confidence" }).first()).toHaveAttribute("aria-valuenow", "55");
    await expect(draft.getByText("crawl-enrich-v2")).toBeVisible();
    await expect(draft.getByText("openrouter/auto").first()).toBeVisible();

    // Publish: approve it; the batch is one workspace-pack revision.
    await page.getByLabel("Select Reopen rate").check();
    await page.getByRole("button", { name: "Approve selected (1)" }).click();
    await expect(page.getByText(/Workspace pack revision 2: 1 approved, 0 rejected/)).toBeVisible();
    await expect(draft).toHaveCount(0);
    await page.getByRole("button", { name: "Open glossary/reopen-rate.md" }).click();
    await expect(page.getByRole("heading", { name: "Reopen rate", level: 2 })).toBeVisible();
    await expect(page.getByRole("list", { name: "Verified by" })).toContainText("human:usr_admin");

    // Ask: the approved document is in the context of the answer, as a receipt.
    const nav = page.getByRole("navigation", { name: "Main" });
    await nav.getByRole("group", { name: "Work" }).getByRole("link", { name: "Ask" }).click();
    await page.getByLabel("Question", { exact: true }).fill("What is the reopen rate for P1 incidents?");
    await page.getByRole("button", { name: "Ask", exact: true }).click();
    await expect(page.getByRole("article", { name: "Question 1" })).toBeVisible();
    const inspector = page.getByRole("complementary", { name: "Answer inspector" });
    await inspector.getByRole("tab", { name: "Evidence" }).click();
    const receipt = inspector.getByRole("list", { name: "Context receipts" }).getByRole("listitem").filter({ hasText: "Reopen rate" });
    await expect(receipt).toContainText("glossary/reopen-rate.md#definition");
    await expect(receipt.getByText("reviewed", { exact: true })).toBeVisible();

    // …and the receipt opens its document in the studio.
    await receipt.getByRole("link", { name: "Open Reopen rate in the knowledge studio" }).click();
    await expect(page).toHaveURL(/tab=documents/);
    await expect(page.getByRole("heading", { name: "Reopen rate", level: 2 })).toBeVisible();
    expect(api.unmatched).toEqual([]);
  });

  test("edit a document's trust fields as a new revision; platform packs stay read-only", async ({ page, api }) => {
    await signIn(page, `/w/${WS}/data/catalog?tab=documents&path=glossary/p1.md`);
    const card = page.locator("section.card", { has: page.getByRole("heading", { name: "P1", level: 2 }) });
    await expect(card.getByText("human-reviewed")).toBeVisible();
    await card.getByRole("button", { name: "Edit" }).click();
    const form = page.getByRole("form", { name: "Edit glossary/p1.md" });
    await form.getByLabel("Status").selectOption("deprecated");
    await form.getByLabel(/Mark as reviewed by me/).check();
    await form.getByLabel("Reason for this revision").fill("P1 renamed to critical");
    await form.getByRole("button", { name: "Save revision" }).click();
    const history = page.getByRole("list", { name: "Revisions of this document" });
    await expect(history.getByText(/P1 renamed to critical/)).toBeVisible();
    await expect(card.getByText("deprecated")).toBeVisible();

    await page.getByLabel("Pack").selectOption({ label: "Platform knowledge · platform (read-only)" });
    await page.getByRole("button", { name: /SLA breach/ }).click();
    await expect(page.getByText(/platform pack is read-only/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0);
    expect(api.unmatched).toEqual([]);
  });
});

test.describe("operate and the gear (P4-U06, P4-U07)", () => {
  test("registry lists a newly installed plugin and runs it from its generated form", async ({ page, api }) => {
    await signIn(page, "/settings/registry");
    const methods = page.getByRole("table", { name: "Analysis methods" });
    await expect(methods.getByText("method.acme_funnel")).toBeVisible();
    await expect(methods.getByText("entrypoint:acme-methods")).toBeVisible();
    await page.getByLabel("Workspace for enablement").selectOption(WS);
    await expect(page).toHaveURL(new RegExp(`ws=${WS}`));
    const toggle = page.getByRole("switch", { name: "Enable method.acme_funnel in this workspace" });
    await expect(toggle).not.toBeChecked();
    await page.locator("label.switch", { has: toggle }).click();
    await expect(toggle).toBeChecked();

    await page.getByRole("button", { name: "Open method.acme_funnel" }).click();
    const run = page.getByRole("region", { name: "Run this capability" });
    await run.getByRole("button", { name: "Run" }).click();
    await expect(run.getByText("This field is required.")).toBeVisible();
    await run.getByLabel(/^Asset/).fill("events");
    await run.getByRole("button", { name: "Add steps" }).click();
    await run.getByRole("button", { name: "Add steps" }).click();
    await run.getByRole("textbox", { name: "Steps 1" }).fill("visit");
    await run.getByRole("textbox", { name: "Steps 2" }).fill("buy");
    await run.getByRole("button", { name: "Run" }).click();
    const result = page.locator("[data-renderer='renderer.stat_result']");
    await expect(result.getByText("0.0042", { exact: true })).toBeVisible();
    await expect(result.getByText("Cramér's V effect", { exact: true })).toBeVisible(); // effect codes in plain words (P2-17)
    expect(api.unmatched).toEqual([]);
  });

  test("approval inbox shows the payload diff, hashes and policy version", async ({ page }) => {
    await signIn(page, `/w/${WS}/operate/approvals`);
    const diff = page.getByRole("table", { name: "Payload changes" });
    await expect(diff.getByText("dashboards[key=p1_resolution].title")).toBeVisible();
    await expect(diff.getByText("P1 resolution (weekly)")).toBeVisible();
    await expect(page.getByText("(current v2)")).toBeVisible();
  });

  test("alerts explain their triage in plain words (rule, decision model, escalate-only)", async ({ page }) => {
    await signIn(page, `/w/${WS}/operate/monitoring?tab=alerts`);
    const triage = page.getByLabel("Triage explanation");
    await expect(triage.getByText(/Rule: MTTR 9\.4h > 8h/)).toBeVisible();
    await expect(triage.getByText(/The decision model can only raise severity/)).toBeVisible();
  });
});

/** Main screen of each area and panel: [name, path, text that shows the data has loaded]. */
test("a business context is drafted, reviewed and published for a source", async ({ page }) => {
  let row: Record<string, unknown> | null = null;
  await page.route(new RegExp(`/api/workspaces/${WS}/definitions(?:/.*)?(?:\\?.*)?$`), async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (req.method() === "GET" && url.pathname.endsWith("/definitions")) {
      await route.fulfill({ json: { items: row ? [row] : [], next_cursor: null } }); return;
    }
    if (req.method() === "GET" && url.pathname.endsWith("/defn_context")) {
      await route.fulfill({ json: row }); return;
    }
    if (req.method() === "POST" && url.pathname.endsWith("/definitions")) {
      const body = req.postDataJSON() as { key: string; spec: { source_ids: string[]; purpose: string }; title: string };
      expect(body.spec.source_ids).toEqual(["src_sn"]);
      row = { ...body, id: "defn_context", workspace_id: WS, kind: "analysis_context", version: 1, revision: 1,
        status: "draft", content_hash: "hash", created_by: USER.id, published_by: null, retired_by: null, reason: null,
        created_at: "2026-09-27T00:00:00Z", updated_at: "2026-09-27T00:00:00Z", published_at: null, retired_at: null };
      await route.fulfill({ status: 201, json: row }); return;
    }
    if (req.method() === "POST" && url.pathname.endsWith("/defn_context/publish")) {
      row = { ...row, status: "published", revision: 2, published_by: USER.id };
      await route.fulfill({ json: row }); return;
    }
    await route.fallback();
  });
  await signIn(page, `/w/${WS}/data/catalog?tab=contexts`);
  await expect(page.getByRole("heading", { name: "Analysis contexts" })).toBeVisible();
  await page.getByLabel("Name").fill("P1 service health");
  await page.getByLabel("Business purpose").fill("Understand service health by priority");
  await page.getByLabel("Starting question").fill("Why are P1 incidents taking longer to resolve?");
  await page.getByRole("checkbox", { name: /ServiceNow/ }).check();
  await page.getByRole("button", { name: "Save draft" }).click();
  await expect(page.getByText(/Draft saved/)).toBeVisible();
  await page.getByRole("button", { name: "Publish reviewed version" }).click();
  await expect(page.getByText(/Version 1 published/)).toBeVisible();
  expect(await axeViolations(page)).toEqual([]);
});

const SCREENS: [string, string, RegExp][] = [
  ["Overview", `/w/${WS}`, /What needs you/],
  ["Work · Ask", `/w/${WS}/work/ask`, /SQL console/],
  ["Work · Ask thread", `/w/${WS}/work/ask?thread=ask_old`, /How many P1 incidents per week/],
  ["Work · investigations", `/w/${WS}/work`, /Why are P1 resolution times rising/],
  ["Work · investigation", `/w/${WS}/work/investigations/${RUN}`, /Why are P1 resolution times rising/],
  ["Work · prepare data", `/w/${WS}/work?tab=prepare`, /p1_incidents_clean/],
  ["Work · Data Thread", `/w/${WS}/work?tab=thread&container=run:run_demo`, /Mean P1 resolution hours by group/],
  ["Work · notebook", `/w/${WS}/work?tab=notebooks&notebook=nb_1`, /No cells yet/],
  ["Work · experiment", `/w/${WS}/work?tab=experiments&experiment=mlx_0`, /Holdout consumed/],
  ["Work · ML spec form", `/w/${WS}/work?tab=experiments&new=predict`, /Propose a spec/],
  ["Outputs · models", `/w/${WS}/outputs?type=model`, /each version's own holdout/],
  ["Outputs · managed tables", `/w/${WS}/outputs?type=table`, /p1_clean__v2/],
  ["Work · pipeline", `/w/${WS}/work?tab=prepare&pipeline=pip_2`, /Blocked: a check failed/],
  ["Operate · models & pipelines", `/w/${WS}/operate/monitoring?tab=health`, /drift alone does not show/],
  ["Work · dbt build", `/w/${WS}/work?tab=builds&job=${BUILD_PREV}`, /No earlier build of this target/],
  ["Data · catalog", `/w/${WS}/data/catalog`, /One row per incident/],
  ["Data · brief & readiness", `/w/${WS}/data/catalog?tab=brief`, /Open questions \(2\)/],
  ["Data · documents", `/w/${WS}/data/catalog?tab=documents&path=glossary/p1.md`, /Revision history/],
  ["Data · review queue", `/w/${WS}/data/catalog?tab=review`, /crawl-enrich-v2/],
  ["Data · metrics", `/w/${WS}/data/catalog?tab=metrics&kpi=mttr_hours`, /Approve v2/],
  ["Data · definitions", `/w/${WS}/data/catalog?tab=definitions`, /Review proposed structure and changes/],
  ["Data · import & export", `/w/${WS}/data/catalog?tab=transfer`, /Push to a git remote/],
  ["Outputs", `/w/${WS}/outputs`, /P1 resolution/],
  ["Outputs · finding", `/w/${WS}/outputs/findings/${INSIGHT}`, /How it was checked/],
  ["Outputs · dashboard", `/w/${WS}/outputs?type=dashboard&dashboard=art_dash`, /P1 MTTR by assignment group/],
  ["Operate · approvals", `/w/${WS}/operate/approvals`, /Publish dashboards/],
  ["Operate · alerts", `/w/${WS}/operate/monitoring?tab=alerts`, /can only raise severity/],
  ["Operate · schedules", `/w/${WS}/operate/schedules`, /upgrade available/],
  ["Settings · policy", `/w/${WS}/settings/policy`, /Effective policy/],
  ["Settings · platform", "/settings/platform", /Purpose/],
  ["Settings · usage", "/settings/usage", /Spend by rung not reported/],
  ["Settings · registry", `/settings/registry?ws=${WS}&cap=method.acme_funnel`, /acme_methods\/tests\/test_funnel\.py/],
];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`accessibility (${scheme})`, () => {
    test.use({ colorScheme: scheme });
    for (const [journey, path, ready] of SCREENS) {
      test(`${journey} has no axe violations`, async ({ page }) => {
        await signIn(page, path);
        await expect(page.locator("main")).toContainText(ready);
        expect(await axeViolations(page)).toEqual([]);
      });
    }
    test("why-trust drawer has no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}/investigate/${RUN}`);
      await page.getByRole("article", { name: "Finding F1" }).getByRole("button", { name: "Why trust this" }).click();
      await expect(page.getByRole("dialog")).toContainText(/identical result hash/);
      expect(await axeViolations(page)).toEqual([]);
    });
    test("Start work and Why this number? drawers have no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}`);
      await page.getByRole("button", { name: "Start work" }).click();
      await expect(page.getByRole("list", { name: "Job kinds" })).toBeVisible();
      expect(await axeViolations(page)).toEqual([]);
      await page.goto(`/w/${WS}/outputs/findings/${INSIGHT}`);
      await page.getByRole("list", { name: "Numbers in this finding" }).getByRole("button", { name: /Why this number/ }).click();
      await expect(page.getByRole("dialog")).toContainText(/The verification/);
      expect(await axeViolations(page)).toEqual([]);
    });
    test("command palette has no axe violations", async ({ page }) => {
      await signIn(page, `/w/${WS}`);
      await expect(page.locator("main")).toContainText(/What needs you/);
      await page.keyboard.press("Control+k");
      await expect(page.getByRole("dialog")).toBeVisible();
      expect(await axeViolations(page)).toEqual([]);
    });
  });
}
