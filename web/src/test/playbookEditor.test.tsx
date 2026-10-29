/**
 * S06-T6 playbook editor (Work → Workflows, expert tools): YAML is validated by the server with every problem
 * listed, a dry run shows the steps, budget and approval gates, and a draft is enabled only through
 * simulate → request signature → someone else approves → publish. Without expert tools it is not offered.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session } from "../api";
import { PlaybookEditor } from "../components/PlaybookEditor";
import { setExpertView } from "../lib/expert";
import { USER, WS } from "./mockBackend";

const BASE = `/api/workspaces/${WS}/playbook-editor`;
const TEMPLATE = "apiVersion: analystos/v1\nkind: Playbook\nid: playbook.my_review\nversion: 1.0.0\nspec:\n  steps: []\n";

const REPORT = {
  outcome: "completed", reason: null, playbook: { id: "playbook.my_review", version: "1.0.0", ref: "playbook.my_review@1.0.0", content_hash: "c" },
  fixture: { name: "supervised" }, waves: [["plan_approval"], ["metadata"]], replans: [], warnings: ["no default_limits: a run is bounded by the goal's limits and policy caps only"],
  mocks: {}, plan_version: 1, plan_hash: "p", digest: "d",
  timeline: [
    { seq: 1, key: "plan_approval", agent: "agent.supervisor", behaviour: "plan_approved", type: "approval_gate", status: "approval_granted",
      detail: "waits for plan approval (WAITING_USER), then the approver approves the plan hash", plan_version: 1, budget: { steps: 1 } },
    { seq: 2, key: "metadata", agent: "agent.metadata", behaviour: null, type: "agent", status: "completed", detail: "mock output",
      plan_version: 1, budget: { steps: 2 } },
  ],
  approvals: [{ id: "sim_apr_1", gate: "plan_approval", payload: "plan", for: null, decision: "approve", status: "approved", binds: "plan hash", plan_version: 1 }],
  approval_gates: [{ key: "plan_approval", payload: "plan", approval_for: null, type: "approval_gate" }],
  budget: { limits: { max_steps: 20 }, used: { steps: 2, retries: 0, replans: 0, llm_calls: 1 }, remaining: { max_steps: 18 }, stopped_by: null },
};

type Json = Record<string, unknown>;

function server() {
  let draft: Json | null = null;
  const log: { method: string; path: string; body: Json | null; ifMatch: string | null }[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const method = init?.method ?? "GET";
    const path = new URL(String(input), "http://x").pathname;
    const body = typeof init?.body === "string" ? JSON.parse(init.body) as Json : null;
    const ifMatch = new Headers(init?.headers).get("If-Match");
    log.push({ method, path, body, ifMatch });
    const ok = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "Content-Type": "application/json" } });
    const bump = (over: Json) => { draft = { ...draft!, ...over, revision: Number(draft!.revision) + 1 }; return ok(draft); };
    if (method === "GET" && path === BASE) {
      return ok({ agents: [], checks: [], replan_reasons: [], fixtures: [{ name: "supervised", description: "" }, { name: "tight_budget", description: "" }],
        schema: {}, template: TEMPLATE });
    }
    if (method === "POST" && path === `${BASE}/validate`) {
      return String(body?.yaml).includes("agent.nope")
        ? ok({ ok: false, problems: ["step metadata uses agent.nope, which is not a registered agent", "step profile depends on unknown step x"] })
        : ok({ ok: true, key: "playbook.my_review", problems: [], warnings: [], content_hash: "c", steps: [], approval_gates: ["plan_approval"],
          diff: { against: null, changes: [] } });
    }
    if (method === "POST" && path === `${BASE}/simulate`) return ok({ ...REPORT, fixture: { name: String(body?.fixture) } });
    if (method === "POST" && path === `${BASE}/drafts`) {
      draft = { id: "defn_pb", workspace_id: WS, kind: "playbook", key: "playbook.my_review", version: 1, status: "draft", title: body?.title,
        content_hash: "c1", revision: 1, yaml: body?.yaml, signature: { state: "unsigned" }, test_evidence: null };
      return ok(draft, 201);
    }
    if (method === "POST" && path === `${BASE}/drafts/defn_pb/simulate`) {
      return bump({ status: "tested", test_evidence: { content_hash: "c1", simulations: [{ fixture: "supervised", outcome: "completed", digest: "d" }] } });
    }
    if (method === "POST" && path === `${BASE}/drafts/defn_pb/request-signature`) {
      bump({ signature: { state: "requested", approval_id: "apr_1", content_hash: "c1" } });
      return ok({ definition: draft, approval: { id: "apr_1", status: "pending" } }, 201);
    }
    if (method === "GET" && path === `${BASE}/drafts/defn_pb`) {
      // the approver signed off in the approvals inbox meanwhile; after publish the version is signed
      if (draft!.status !== "published") draft = { ...draft!, signature: { state: "approved", approval_id: "apr_1", content_hash: "c1" } };
      return ok(draft);
    }
    if (method === "POST" && path === `/api/workspaces/${WS}/definitions/defn_pb/publish`) {
      return bump({ status: "published", signature: { state: "signed", key_id: "tenant-signing-1" } });
    }
    return ok({ error: { code: "not_found", message: `unmocked ${method} ${path}` } }, 404);
  });
  return { spy, log };
}

const renderEditor = () => render(<MemoryRouter><PlaybookEditor wsId={WS} role="editor" /></MemoryRouter>);

beforeEach(() => { session.set("mock-token", USER); window.localStorage.clear(); setExpertView(false); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); session.clear(); setExpertView(false); });

it("is not offered without expert tools", () => {
  const { log } = server();
  renderEditor();
  expect(screen.getByText(/Show expert tools/)).toBeTruthy();
  expect(screen.queryByLabelText("Playbook YAML")).toBeNull();
  expect(log.some((c) => c.path.startsWith(BASE))).toBe(false);
});

it("validates YAML, simulates it and enables a draft only through signing", async () => {
  setExpertView(true);
  const { log } = server();
  renderEditor();
  const textarea = await screen.findByLabelText("Playbook YAML") as HTMLTextAreaElement;
  await waitFor(() => expect(textarea.value).toBe(TEMPLATE));

  // every problem is listed
  fireEvent.change(textarea, { target: { value: TEMPLATE.replace("steps: []", "steps: [{key: metadata, use: agent.nope}]") } });
  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  const problems = await screen.findByRole("list", { name: "Validation problems" });
  expect(within(problems).getAllByRole("listitem")).toHaveLength(2);

  fireEvent.change(textarea, { target: { value: TEMPLATE } });
  fireEvent.click(screen.getByRole("button", { name: "Validate" }));
  expect(await screen.findByText(/Valid playbook/)).toBeTruthy();

  // a dry run of the unsaved YAML: steps, budget and approval gates, nothing executed
  fireEvent.change(screen.getByLabelText("Fixture"), { target: { value: "tight_budget" } });
  fireEvent.click(screen.getByRole("button", { name: "Simulate" }));
  const steps = await screen.findByRole("table", { name: "Simulated steps" });
  expect(within(steps).getByText("plan_approval")).toBeTruthy();
  expect(screen.getByRole("list", { name: "Approval gates hit" }).textContent).toMatch(/plan_approval.*binds the plan hash/);
  expect(screen.getByText(/Steps 2 of 20/).textContent).toMatch(/Nothing was executed/);
  expect(log.find((c) => c.path === `${BASE}/simulate`)?.body).toMatchObject({ yaml: TEMPLATE, fixture: "tight_budget" });

  // save, then simulate → request signature → (approver approves elsewhere) → publish
  fireEvent.change(screen.getByLabelText("Title"), { target: { value: "My review" } });
  fireEvent.click(screen.getByRole("button", { name: "Save as new draft" }));
  const publish = await screen.findByRole("button", { name: "Publish and enable" }) as HTMLButtonElement;
  expect(publish.disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Request signature" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Simulate draft" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Request signature" }) as HTMLButtonElement).disabled).toBe(false));
  expect(log.find((c) => c.path.endsWith("/drafts/defn_pb/simulate"))).toMatchObject({ ifMatch: '"1"', body: { fixtures: ["supervised", "autonomous"] } });
  fireEvent.click(screen.getByRole("button", { name: "Request signature" }));
  expect(await screen.findByText(/An approver other than you must approve it/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "Publish and enable" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Check status" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Publish and enable" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Publish and enable" }));
  await waitFor(() => expect(log.some((c) => c.method === "POST" && c.path.endsWith("/definitions/defn_pb/publish") && c.ifMatch === '"3"')).toBe(true));
  expect(await screen.findByText(/immutable/)).toBeTruthy();  // a published version cannot be edited in place
});
