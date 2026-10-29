import type { Page } from "@playwright/test";
import { RUN, USER, WS } from "../src/test/mockBackend";
import { axeViolations, expect, signIn, test } from "./fixtures";

/**
 * P7-19 journey (mocked API): in Work → Workflows an owner creates an agent from the form, publishes it,
 * adds it to a workflow step, simulates the workflow, has its signature approved (S06-T6), publishes it and runs
 * that exact version.
 */
const OPTIONS = {
  capabilities: [
    { id: "skill.catalog_lookup", ref: "skill.catalog_lookup@1.0.0", summary: "Catalog lookup", side_effect: "none", tool: "metadata.read",
      certification: "certified", enabled: true, grantable: true, reason: null, default_input: { assets: "$scope.assets" }, default_reason: null },
    { id: "skill.row_count", ref: "skill.row_count@1.0.0", summary: "Row count", side_effect: "read_source", tool: "sql.execute",
      certification: "certified", enabled: false, grantable: false, reason: "skill.row_count@1.0.0 is not enabled in this workspace",
      default_input: null, default_reason: "needs asset, which only a model proposal can fill" },
  ],
  knowledge_sections: ["glossary", "business_rules"], output_types: ["agent_output"],
  limits: { llm_calls: 20, usd: 2, queries: 200, max_rows: 50000, max_steps: 20, pii_access: ["none", "restricted"] },
};

type Row = Record<string, unknown>;

/** A small stateful definitions store; everything else stays on the shared mock backend. */
async function definitionsStore(page: Page): Promise<{ defs: Row[]; started: Row[] }> {
  const defs: Row[] = [];
  const started: Row[] = [];
  const row = (over: Row): Row => ({ workspace_id: WS, version: 1, revision: 1, status: "draft", content_hash: "h", created_by: USER.id,
    published_by: null, retired_by: null, reason: null, published_at: null, retired_at: null, created_at: null, updated_at: null, ...over });
  await page.route(new RegExp(`/api/workspaces/${WS}/(agent-form|definitions|analysis|playbook-editor)(/[^?]*)?(\\?.*)?$`), async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname.replace(`/api/workspaces/${WS}`, "");
    const method = req.method();
    if (method === "GET" && path === "/agent-form") { await route.fulfill({ json: OPTIONS }); return; }
    if (method === "POST" && path === "/agent-form") {
      const form = req.postDataJSON() as { key: string; title: string };
      const d = row({ id: "defn_agent", kind: "agent", key: form.key, title: form.title, form, spec: { kind: "Agent", id: form.key } });
      defs.push(d);
      await route.fulfill({ status: 201, json: d }); return;
    }
    if (method === "GET" && path === "/definitions") {
      const kind = new URL(req.url()).searchParams.get("kind");
      await route.fulfill({ json: { items: defs.filter((d) => !kind || d.kind === kind), next_cursor: null } }); return;
    }
    if (method === "POST" && path === "/definitions") {
      const d = row({ ...(req.postDataJSON() as Row), id: "defn_workflow" });
      defs.push(d);
      await route.fulfill({ status: 201, json: d }); return;
    }
    // S06-T6: a workflow (playbook) is simulated, signed off by someone else, then published
    const draft = /^\/playbook-editor\/drafts\/([^/]+)(\/simulate|\/request-signature)?$/.exec(path);
    if (draft) {
      const d = defs.find((x) => x.id === draft[1])!;
      if (method === "POST" && draft[2] === "/simulate") {
        Object.assign(d, { status: "tested", revision: Number(d.revision) + 1, test_evidence: { content_hash: d.content_hash, simulations: [] } });
        await route.fulfill({ json: d }); return;
      }
      if (method === "POST" && draft[2] === "/request-signature") {
        Object.assign(d, { revision: Number(d.revision) + 1, signature: { state: "requested", approval_id: "apr_sign" } });
        await route.fulfill({ status: 201, json: { definition: d, approval: { id: "apr_sign", status: "pending" } } }); return;
      }
      if (method === "GET" && !draft[2]) {  // the approver has signed off in the inbox meanwhile
        if (d.status !== "published") Object.assign(d, { signature: { state: "approved", approval_id: "apr_sign" } });
        await route.fulfill({ json: d }); return;
      }
    }
    const pub = /^\/definitions\/([^/]+)\/publish$/.exec(path);
    if (method === "POST" && pub) {
      const d = defs.find((x) => x.id === pub[1])!;
      Object.assign(d, { status: "published", revision: Number(d.revision) + 1,
        ...(d.kind === "playbook" ? { signature: { state: "signed" } } : {}) });
      await route.fulfill({ json: d }); return;
    }
    const one = /^\/definitions\/([^/]+)$/.exec(path);
    if (method === "GET" && one) { await route.fulfill({ json: defs.find((x) => x.id === one[1]) }); return; }
    if (method === "POST" && path === "/analysis") {
      started.push(req.postDataJSON() as Row);
      await route.fulfill({ json: { id: RUN } }); return;
    }
    await route.fallback();
  });
  await page.route("**/api/capabilities?**", (r) => r.fulfill({ json: { digest: "d", capabilities: [
    { id: "agent.metadata", kind: "Agent", ref: "agent.metadata@1.0.0", summary: "Discover metadata", entry: "python:analystos.agents.metadata:run",
      enabled: true, available: true, side_effect: "read_source", certification: { status: "certified" }, tags: [], ui: {}, input_schema: {} }] } }));
  return { defs, started };
}

test("an owner creates an agent from a form, publishes it, adds it to a workflow and runs it", async ({ page, api }) => {
  const store = await definitionsStore(page);
  await signIn(page, `/w/${WS}/work?tab=workflows`);
  await page.getByRole("button", { name: "Create agent" }).click();
  const form = page.getByRole("form", { name: "Agent form" });
  await expect(form.getByText("skill.row_count@1.0.0 is not enabled in this workspace")).toBeVisible();
  await expect(form.getByRole("checkbox", { name: /skill\.row_count/ })).toBeDisabled();
  await form.getByLabel("Name", { exact: true }).fill("Table notes");
  await form.getByLabel("Key", { exact: true }).fill("agent.table_notes");
  await form.getByLabel("Purpose", { exact: true }).fill("Note the columns of every selected table");
  await form.getByRole("checkbox", { name: /skill\.catalog_lookup/ }).check();
  await form.getByRole("checkbox", { name: "Run skill.catalog_lookup by default" }).check();
  await form.getByRole("checkbox", { name: "glossary" }).check();
  await form.getByLabel("Output name").fill("Table notes");
  expect(await axeViolations(page)).toEqual([]);
  await form.getByRole("button", { name: "Save agent draft" }).click();
  await page.getByRole("button", { name: "Publish agent.table_notes" }).click();
  await expect(page.getByRole("button", { name: "New version of agent.table_notes" })).toBeVisible();
  expect(store.defs[0]).toMatchObject({ kind: "agent", status: "published" });

  await page.getByRole("button", { name: "Build workflow" }).click();
  const builder = page.getByRole("form", { name: "Workflow builder" });
  await builder.getByLabel("Title", { exact: true }).first().fill("Table notes");
  await builder.getByLabel("Key", { exact: true }).fill("table_notes");
  await builder.getByLabel("Purpose", { exact: true }).fill("Note every selected table");
  await builder.getByLabel("Step key").fill("notes");
  await builder.getByLabel("Title", { exact: true }).nth(1).fill("Write table notes");
  await builder.getByLabel("Agent", { exact: true }).selectOption("agent.table_notes");
  await builder.getByRole("button", { name: "Save draft" }).click();
  await page.getByRole("button", { name: "Simulate draft" }).click();
  await page.getByRole("button", { name: "Request signature" }).click();
  await page.getByRole("button", { name: "Check status" }).click();
  await page.getByRole("button", { name: "Publish and enable" }).click();
  expect(store.defs[1]).toMatchObject({ kind: "playbook", status: "published", signature: { state: "signed" } });
  const workflows = page.getByRole("list", { name: "Workflows" });
  await workflows.getByRole("button", { name: "Run" }).click();
  const run = page.getByRole("form", { name: "Run workflow" });
  await run.getByLabel("Goal").fill("Note the columns of the incident table");
  await run.getByRole("button", { name: "Start workflow" }).click();
  await expect(page).toHaveURL(new RegExp(`/w/${WS}/work/investigations/${RUN}`));
  const steps = (store.defs[1].spec as { spec: { steps: Row[] } }).spec.steps;
  expect(steps).toEqual([{ key: "notes", title: "Write table notes", use: "agent.table_notes", after: [], optional: false }]);
  expect(store.started).toEqual([expect.objectContaining({ definition: "defn_workflow", autonomy_level: 2 })]);
  expect(api.unmatched).toEqual([]);
});
