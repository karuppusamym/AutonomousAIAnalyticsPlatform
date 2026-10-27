import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { session } from "../api";
import { ModelsAdmin } from "../pages/AdminModels";
import { USER } from "./mockBackend";

const EFFECTIVE = {
  tenant_id: "tnt_default", workspace_id: null, scope: "tenant", version: 2,
  allowlist: ["qwen/qwen3-32b", "deepseek/deepseek-v4-flash"], blocked_families: ["anthropic", "google"],
  residency: ["eu"], budgets: { per_run_usd: 1.5, per_day_usd: 2, per_user_day_usd: null },
  purposes: {
    sql_generation: { ladder: ["cache", "registry", "llm_small", "rules"], mode: "auto", profile: "coding", provider: "openrouter",
      models: ["qwen/qwen3-32b"], rules_fallback: true },
    planning: { ladder: ["cache", "llm_large", "rules"], mode: "always", profile: "reasoning_strong", provider: "openrouter",
      models: [], rules_fallback: true, model_unavailable: { purpose: "planning", reason: "residency" } },
  },
  profile_models: {},
  sources: { allowlist: "tenant", residency: "tenant", "budgets.per_run_usd": "tenant", "budgets.per_day_usd": "platform" },
  excluded_models: { "anthropic/claude-sonnet-5": { layer: "file", reason: "family anthropic is blocked on this platform" } },
  violations: [], layers: [{ layer: "file" }, { layer: "platform" }, { layer: "tenant", version: 2, present: true }],
};

const GATE = {
  gated: true, blocked: true, tolerance: 0.1,
  evaluations: [{ purpose: "sql_generation", profile: "coding", from: "qwen/qwen3-32b", to: "deepseek/deepseek-v4-flash",
    verdict: "blocked", reason: "score 0.33 regresses more than 0.10 from 1.00 on qwen/qwen3-32b",
    new: { id: "mev_2", suite: "sql", suite_version: 1, purpose: "sql_generation", profile: "coding", model: "deepseek/deepseek-v4-flash",
      status: "scored", score: 0.3333, cases: 3, passed: 1, cost_usd: 0, created_at: null, reused: false },
    current: { id: "mev_1", suite: "sql", suite_version: 1, purpose: "sql_generation", profile: "coding", model: "qwen/qwen3-32b",
      status: "scored", score: 1, cases: 3, passed: 3, cost_usd: 0, created_at: null, reused: true } }],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mockFetch(puts: { body: Record<string, unknown>; ifMatch: string | null }[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    if (url.includes("/api/admin/models/effective")) return json(EFFECTIVE);
    if (url.includes("/api/admin/models/config") && method === "GET") {
      return json({ scope: "tenant", tenant_id: "tnt_default", workspace_id: null, version: 2, document: { residency: ["eu"] },
        history: [{ version: 2, note: "eu only", created_by: "usr_admin", created_at: "2026-10-27T10:00:00Z", document: { residency: ["eu"] } },
          { version: 1, note: "", created_by: "usr_admin", created_at: "2026-10-27T09:00:00Z", document: {} }] });
    }
    if (url.includes("/api/admin/models/config") && method === "PUT") {
      const body = JSON.parse(String(init?.body));
      const headers = (init?.headers ?? {}) as Record<string, string>;
      puts.push({ body, ifMatch: headers["If-Match"] ?? null });
      if (!body.switch_override_reason) {
        return json({ error: { code: "conflict", message: "model switch blocked by the evaluation gate", retryable: false,
          details: { model_switch_gate: GATE } } }, 409);
      }
      return json({ scope: "tenant", version: 3, changed: true, model_switch_gate: { ...GATE, overridden: true } });
    }
    return json({}, 404);
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("Admin › Models", () => {
  it("shows blocked families as blocked, value sources and fail-closed purposes", async () => {
    session.set("tok", USER);
    mockFetch([]);
    render(<ModelsAdmin purposes={<div>purposes table</div>} />);
    expect(await screen.findByText(/Blocked on this platform/)).toBeTruthy();
    expect(screen.getByText("anthropic/* blocked")).toBeTruthy();
    expect(screen.getByText("google/* blocked")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Budgets & overrides" }));
    expect(await screen.findByText(/no model \(residency\) · rules answer/)).toBeTruthy();
    expect(screen.getByText(/family anthropic is blocked on this platform/)).toBeTruthy();
    expect(screen.getAllByText("tenant").length).toBeGreaterThan(0);  // source layer tags
  });

  it("a blocked model switch shows both scores and saves only with an override reason (If-Match kept)", async () => {
    session.set("tok", USER);
    const puts: { body: Record<string, unknown>; ifMatch: string | null }[] = [];
    mockFetch(puts);
    render(<ModelsAdmin purposes={<div />} />);
    fireEvent.click(await screen.findByRole("tab", { name: "Budgets & overrides" }));
    const editor = await screen.findByLabelText("Override document (JSON)");
    fireEvent.change(editor, { target: { value: JSON.stringify({ profile_models: { coding: ["deepseek/deepseek-v4-flash"] } }) } });
    fireEvent.click(screen.getByText("Save new version"));
    expect(await screen.findByText("Model switch blocked by the evaluation gate")).toBeTruthy();
    expect(screen.getByText(/33% \(1\/3\)/)).toBeTruthy();
    expect(screen.getByText(/100% \(3\/3\) · stored/)).toBeTruthy();
    const activate = screen.getByText("Activate anyway") as HTMLButtonElement;
    expect(activate.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Override reason"), { target: { value: "cheaper; owner accepted" } });
    fireEvent.click(activate);
    await waitFor(() => expect(puts.length).toBe(2));
    expect(puts[0].ifMatch).toBe('"2"');
    expect(puts[1].body.switch_override_reason).toBe("cheaper; owner accepted");
  });
});
