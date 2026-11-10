/** P1-7 / P2-9 UI: the catalog trust badge and rules panel (run, recommend into the review queue), and a data
 * product's contract state and access requests (request, approver decides). Every decision is the server's. */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { QualityRuleRow, QualityTrust } from "../api";
import { ProductContract } from "../components/ProductContract";
import { QualityRulesPanel, QualityTrustBadge } from "../components/QualityRules";

vi.setConfig({ testTimeout: 20000 });

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
type Route = [string, RegExp, unknown];

function mockApi(routes: Route[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const method = (init?.method ?? "GET").toUpperCase();
    for (const [m, re, out] of routes) if (m === method && re.test(url)) return json(out);
    return json([]);
  });
}

const TRUST: QualityTrust = { level: "failing", rules: 2, passing: 1, failing: 1, errors: 0, unrun: 0, score: 0.5, mean_pass_rate: 0.99,
  by_dimension: {}, last_run_at: "2026-11-05T10:00:00Z", failing_rules: [{ id: "qrule_2", name: "unique(id)", severity: "critical", pass_rate: 0.98 }] };
const RULE = (over: Partial<QualityRuleRow>): QualityRuleRow => ({
  id: "qrule_1", workspace_id: "ws1", asset: "shop.orders", column: "status", name: "not_null(shop.orders.status)", type: "not_null",
  dimension: "completeness", severity: "warning", threshold: 0.99, status: "active", origin: "recommended", approved_by: "u1",
  definition: { type: "not_null", asset: "shop.orders", column: "status" } as QualityRuleRow["definition"],
  last_status: "passed", last_pass_rate: 0.995, last_run_at: "2026-11-05T10:00:00Z", ...over });

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("quality rules on a catalog table", () => {
  it("shows the trust level and nothing when no rule is declared", () => {
    render(<><QualityTrustBadge trust={TRUST} /><QualityTrustBadge trust={null} /></>);
    expect(screen.getByText("quality: failing")).toBeTruthy();
    expect(screen.getAllByText(/quality:/)).toHaveLength(1);
  });

  it("runs the declared rules and queues profile recommendations as drafts", async () => {
    const f = mockApi([
      ["GET", /quality-rules\?asset=shop\.orders/, [RULE({}), RULE({ id: "qrule_2", name: "unique(id)", type: "unique", last_status: "failed", last_pass_rate: 0.98 })]],
      ["POST", /quality-rules:run/, { results: [], trust: {} }],
      ["POST", /assets\/ast1\/quality-rules:recommend/, { asset: "shop.orders", queued: [{ id: "k1", type: "range", column: "amount", statement: "s" }], skipped: 0 }],
    ]);
    render(<QualityRulesPanel wsId="ws1" assetId="ast1" fq="shop.orders" trust={TRUST} canEdit />);
    expect(await screen.findByText("unique(id)")).toBeTruthy();
    expect(screen.getByText(/failed · 98/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Run 2 rules now" }));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => /quality-rules:run/.test(String(u)) && i?.method === "POST")).toBe(true));
    const run = f.mock.calls.find(([u]) => /quality-rules:run/.test(String(u)))!;
    expect(JSON.parse(String((run[1] as RequestInit).body))).toEqual({ assets: ["shop.orders"] });
    fireEvent.click(screen.getByRole("button", { name: "Recommend rules from profile" }));
    expect(await screen.findByText(/1 draft rule added to the Review queue/)).toBeTruthy();
  });

  it("hides owner actions from people who cannot edit", async () => {
    mockApi([["GET", /quality-rules/, []]]);
    render(<QualityRulesPanel wsId="ws1" assetId="ast1" fq="shop.orders" trust={null} canEdit={false} />);
    expect(await screen.findByText(/Editors and owners can declare rules/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Recommend/ })).toBeNull();
  });
});

describe("a data product's contract and access", () => {
  const STATUS = { tool_id: "verified_query.totals", version: "1.0.0", has_contract: true, ok: false, unknown: 0,
    contract: { freshness: { max_age_hours: 24, assets: ["shop.orders"] }, quality_rules: ["qrule_1"], min_pass_rate: null },
    breaches: [{ key: "freshness:shop.orders", kind: "freshness", state: "breach", severity: "critical", message: "shop.orders loaded 60.0 h ago, over its 24 h SLA" }],
    checks: [{ key: "freshness:shop.orders", kind: "freshness", state: "breach", severity: "critical", message: "shop.orders loaded 60.0 h ago, over its 24 h SLA" },
      { key: "rule:qrule_1", kind: "quality_rule", state: "ok", severity: "info", message: "not_null passes" }] };
  const REQ = { approval_id: "apr_1", product: "verified_query.totals", version: "1.0.0", requester: "u9", role: "analyst", grant: null,
    status: "pending", risk_tier: "medium", reason: "weekly review", decided_by: null, decided_at: null, created_at: null, expires_at: null };

  it("shows breaches first and lets an approver decide a request", async () => {
    const f = mockApi([
      ["GET", /\/workspaces\/ws1$/, { id: "ws1", name: "x", role: "approver" }],
      ["GET", /tools\/verified_query\.totals\/contract/, STATUS],
      ["GET", /tools\/verified_query\.totals\/access-requests/, [REQ]],
      ["POST", /access-requests\/apr_1:decide/, { approval_id: "apr_1", status: "executed" }],
    ]);
    render(<ProductContract ws="ws1" toolId="verified_query.totals" />);
    expect(await screen.findByText("1 breach")).toBeTruthy();
    expect(screen.getByText(/over its 24 h SLA/)).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(f.mock.calls.some(([u]) => /apr_1:decide/.test(String(u)))).toBe(true));
    const call = f.mock.calls.find(([u]) => /apr_1:decide/.test(String(u)))!;
    expect(JSON.parse(String((call[1] as RequestInit).body))).toEqual({ approve: true, reason: null });
  });

  it("a consumer requests access and sees no decision buttons", async () => {
    const f = mockApi([
      ["GET", /\/workspaces\/ws1$/, { id: "ws1", name: "x", role: "viewer" }],
      ["GET", /contract/, { ...STATUS, ok: true, breaches: [], checks: [] }],
      ["GET", /access-requests/, [REQ]],
      ["POST", /tools\/verified_query\.totals\/access-requests/, { approval_id: "apr_2", status: "pending", risk_tier: "medium" }],
    ]);
    render(<ProductContract ws="ws1" toolId="verified_query.totals" />);
    expect(await screen.findByText("contract met")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Reason for access"), { target: { value: "monthly report" } });
    fireEvent.click(screen.getByRole("button", { name: "Request access" }));
    expect(await screen.findByText(/Request apr_2 sent/)).toBeTruthy();
    const call = f.mock.calls.find(([u, i]) => /access-requests/.test(String(u)) && i?.method === "POST")!;
    expect(JSON.parse(String((call[1] as RequestInit).body))).toEqual({ workspace_id: "ws1", reason: "monthly report" });
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });
});
