import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import axe from "axe-core";
import { session, type KnowledgeDocument, type KnowledgeSuggestion } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { receiptView } from "../lib/ask";
import { LIGHT } from "../lib/charts";
import {
  batchDecisions, draftFrom, draftProblems, editableFields, editedFields, emptyDraft, filterGraph, frontmatterOf, gapSummary, graphOption, groupByFolder,
  provenanceLine,
} from "../lib/knowledge";
import { SCREEN_BUDGET, SCREENS, to } from "../routes";
import { mockBackend, resetMockState, USER, WS } from "./mockBackend";
import { SUGGESTION_TERM, WS_PACK } from "./mockKnowledge";

vi.setConfig({ testTimeout: 20000 });

function mockFetch() {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const r = mockBackend((init?.method ?? "GET").toUpperCase(), String(input), typeof init?.body === "string" ? init.body : null);
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function calls(f: ReturnType<typeof mockFetch>, method: string, re: RegExp): [string, RequestInit][] {
  return (f.mock.calls as [string, RequestInit | undefined][])
    .filter(([u, i]) => (i?.method ?? "GET").toUpperCase() === method && re.test(String(u))).map(([u, i]) => [String(u), i ?? {}]);
}

const bodyOf = (init: RequestInit) => JSON.parse(String(init.body)) as Record<string, unknown>;

function renderAt(path: string) {
  return render(<AuthProvider><MemoryRouter initialEntries={[path]}><AppRoutes /></MemoryRouter></AuthProvider>);
}

async function axeClean(container: HTMLElement) {
  const result = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
  return result.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
}

beforeEach(() => {
  resetMockState();
  session.set("mock-token", USER);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

// ------------------------------------------------------------------------------------ pure helpers
describe("knowledge helpers", () => {
  const doc = (fm: Record<string, unknown>, body = "# Definition\n\nText."): KnowledgeDocument => ({
    path: "glossary/x.md", document_id: "kdoc_x", sha256: "a".repeat(64), size: 10, markdown: true, reserved: false, type: String(fm.type ?? ""),
    title: "X", pack_id: "kpk", revision: 3, text: "", frontmatter: fm, body, sections: [], links: [],
    trust: { tier: "human-reviewed", verified: [{ by: "human:u1", at: "2026-09-01T00:00:00+00:00" }], status: "stable", stale_after: null, stale: false, trusted: true },
  });

  it("edits the trust fields and round-trips everything else as JSON", () => {
    const d = draftFrom(doc({ type: "Glossary Term", title: "X", status: "stable", tags: ["a"], stale_after: "2027-01-01T00:00:00+00:00",
      verified: [{ by: "human:u1", at: "2026-09-01T00:00:00+00:00" }], analystos: { kind: "term", review: { suggestion_id: "s" } } }));
    expect(d.staleAfter).toBe("2027-01-01");
    expect(JSON.parse(d.other)).toEqual({ analystos: { kind: "term", review: { suggestion_id: "s" } } });
    const fm = frontmatterOf({ ...d, status: "draft", tags: "a, b", verified: d.verified.map((v) => ({ ...v, keep: false })) });
    expect(fm).toEqual({ type: "Glossary Term", title: "X", status: "draft", tags: ["a", "b"], stale_after: "2027-01-01T00:00:00+00:00",
      analystos: { kind: "term", review: { suggestion_id: "s" } } });
    expect(frontmatterOf(d).verified).toEqual([{ by: "human:u1", at: "2026-09-01T00:00:00+00:00" }]);
  });

  it("checks path, type and the JSON extensions before a round trip", () => {
    expect(draftProblems(emptyDraft("notes/a.md"))).toEqual({});
    expect(draftProblems({ ...emptyDraft("../a.md") }).path).toMatch(/folder\/name\.md/);
    expect(draftProblems({ ...emptyDraft("notes/index.md") }).path).toMatch(/reserved/);
    expect(draftProblems({ ...emptyDraft("a.md"), type: " " }).type).toBeTruthy();
    expect(draftProblems({ ...emptyDraft("a.md"), other: "[1]" }).other).toMatch(/JSON object/);
    expect(draftProblems({ ...emptyDraft("a.md"), other: "{" }).other).toMatch(/Not valid JSON/);
    expect(groupByFolder([{ path: "b/x.md" }, { path: "r.md" }, { path: "a/y.md" }] as never).map(([f]) => f)).toEqual(["", "a", "b"]);
  });

  it("summarises a knowledge-gap draft and lets only its note be edited", () => {
    const f = (value: unknown) => ({ value, confidence: 0.8, provenance: { source: "rule" } });
    const s = { kind: "knowledge_gap", fields: { body: f("Note text"), term: f("zorblat"),
      evidence: f({ term: "zorblat", conversations: 4, outcomes: { clarify: 3, low_confidence: 1 }, rephrased: 2, window_days: 14 }),
      routed_to: f({ name: "Bea Owner", email: "bea@example.test", role: "business" }) } } as unknown as KnowledgeSuggestion;
    expect(gapSummary(s)).toBe("“zorblat” came up in 4 conversations in the last 14 days (3 clarify, 1 low confidence; 2 rephrased). Routed to Bea Owner (business owner).");
    expect(editableFields(s)).toEqual(["body"]);
    expect(gapSummary({ kind: "knowledge_gap", fields: {} } as unknown as KnowledgeSuggestion)).toBe("");
  });

  it("builds review decisions: only changed fields are sent, lists stay lists", () => {
    const s = { kind: "term", fields: { body: { value: "old", confidence: 0.5, provenance: { source: "model", model: "m", purpose: "p" } },
      synonyms: { value: ["a"], confidence: 0.8, provenance: { source: "rule" } }, computation: { value: { q: 1 }, confidence: 1, provenance: {} } } } as unknown as KnowledgeSuggestion;
    expect(editableFields(s)).toEqual(["body", "synonyms"]);
    expect(editedFields(s, { body: "old", synonyms: "a, b" })).toEqual({ synonyms: ["a", "b"] });
    const domain = { ...s, kind: "domain_candidate" } as KnowledgeSuggestion;
    expect(editableFields(domain)).toContain("keyword");
    expect(editedFields(domain, { keyword: "orders" })).toEqual({ keyword: "orders" });
    expect(batchDecisions(["1", "2"], "reject", " no ")).toEqual([{ id: "1", action: "reject", reason: "no" }, { id: "2", action: "reject", reason: "no" }]);
    expect(batchDecisions(["1"], "approve")).toEqual([{ id: "1", action: "approve" }]);
    expect(provenanceLine(s.fields.body.provenance).map(([k]) => k)).toEqual(["source", "model", "purpose"]);
  });

  it("reads a receipt: where it is and whether a person reviewed it", () => {
    expect(receiptView({ name: "Reopen rate", path: "glossary/reopen-rate.md", anchor: "definition", source: "review:crawler.enrichment", document_id: "kdoc_1" }))
      .toMatchObject({ title: "Reopen rate", where: "glossary/reopen-rate.md#definition", reviewed: true, documentId: "kdoc_1" });
    expect(receiptView({ title: "Atlas", trusted: false, section: "external" })).toMatchObject({ where: null, reviewed: false, untrusted: true });
  });

  it("draws governed edges solid and inferred edges dashed, and filters both", () => {
    const g = { nodes: [{ id: "a", kind: "table", label: "A" }, { id: "b", kind: "metric", label: "B" }, { id: "c", kind: "suggestion", label: "C" }],
      edges: [{ source: "b", target: "a", kind: "reads", governed: true, why: "", label: "" }, { source: "c", target: "a", kind: "suggests", governed: false, why: "", label: "" }],
      truncated: false, governed: 1, inferred: 1 } as const;
    const opt = graphOption(g as never, LIGHT) as { series: { links: { lineStyle: { type: string } }[]; data: { symbol: string }[] }[] };
    expect(opt.series[0].links.map((l) => l.lineStyle.type)).toEqual(["solid", "dashed"]);
    expect(opt.series[0].data[2].symbol).toBe("diamond");
    const noInferred = filterGraph(g as never, new Set(["table", "metric", "suggestion"]), false);
    expect([noInferred.governed, noInferred.inferred]).toEqual([1, 0]);
    expect(filterGraph(g as never, new Set(["table"]), true).edges).toEqual([]);
  });

  it("keeps Data's catalog and definitions as one screen inside the budget", () => {
    expect(SCREENS.filter((s) => s.id !== "login").length).toBeLessThanOrEqual(SCREEN_BUDGET - 1);
    expect(to.knowledge("ws", "documents", { doc: "kdoc_1" })).toBe("/w/ws/data/catalog?tab=documents&doc=kdoc_1");
    expect(to.knowledge("ws", "catalog")).toBe("/w/ws/data/catalog");
    expect(to.knowledge("ws", "graph")).toBe("/w/ws/data/catalog?tab=definitions");
  });
});

// ------------------------------------------------------------------------------------ documents
describe("Knowledge studio: documents", () => {
  it("shows trust fields, edits the workspace pack as one revision, and keeps other packs read-only", async () => {
    const f = mockFetch();
    const { container } = renderAt(`/w/${WS}/knowledge/catalog?tab=documents`);
    expect(await screen.findByRole("heading", { name: "Data dictionary", level: 1 })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Documents", selected: true })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /^P1/ }));
    const card = (await screen.findByRole("heading", { name: "P1", level: 2 })).closest("section")!;
    await within(card).findByText("human-reviewed");
    expect(within(card).getByRole("list", { name: "Verified by" }).textContent).toContain("human:usr_admin");
    expect(within(card).getByText("resolves")).toBeTruthy(); // the MTTR link resolves
    expect(await axeClean(container)).toEqual([]);

    fireEvent.click(within(card).getByRole("button", { name: "Edit" }));
    const form = screen.getByRole("form", { name: "Edit glossary/p1.md" });
    fireEvent.change(within(form).getByLabelText("Status"), { target: { value: "deprecated" } });
    fireEvent.change(within(form).getByLabelText("Stale after"), { target: { value: "2027-03-01" } });
    fireEvent.click(within(form).getByLabelText(/Keep/));
    fireEvent.click(within(form).getByLabelText(/Mark as reviewed by me/));
    fireEvent.change(within(form).getByLabelText("Reason for this revision"), { target: { value: "P1 retired" } });
    expect(await axeClean(container)).toEqual([]);
    fireEvent.click(within(form).getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(calls(f, "PUT", /\/knowledge\/packs\/kpk_ws\/document$/)).toHaveLength(1));
    const sent = bodyOf(calls(f, "PUT", /\/document$/)[0][1]);
    expect(sent).toMatchObject({ path: "glossary/p1.md", mark_reviewed: true, reason: "P1 retired" });
    expect(sent.base_sha256).toMatch(/^[0-9a-f]{64}$/);
    expect((sent.frontmatter as Record<string, unknown>).status).toBe("deprecated");
    expect((sent.frontmatter as Record<string, unknown>).verified).toBeUndefined(); // the old entry was dropped; the server adds mine
    const history = await screen.findByRole("list", { name: "Revisions of this document" });
    await within(history).findByText(/P1 retired/);

    fireEvent.click(within(history).getByRole("button", { name: "View revision 1" }));
    expect(await screen.findByText(/Viewing revision 1/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull(); // history is read-only

    fireEvent.change(screen.getByLabelText("Pack"), { target: { value: "kpk_platform" } });
    fireEvent.click(await screen.findByRole("button", { name: /SLA breach/ }));
    expect((await screen.findAllByText("machine-confirmed")).length).toBeGreaterThan(0);
    expect(screen.getByText(/platform pack is read-only/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.queryByRole("button", { name: "New document" })).toBeNull();
  });

  it("refuses a stale edit with a conflict and validates a new document before sending it", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/knowledge/catalog?tab=documents&path=metrics/mttr.md`);
    const card = (await screen.findByRole("heading", { name: "MTTR", level: 2 })).closest("section")!;
    fireEvent.click(await within(card).findByRole("button", { name: "Edit" }));
    // someone else saves first
    mockBackend("PUT", `/api/workspaces/${WS}/knowledge/packs/${WS_PACK}/document`, JSON.stringify({
      path: "metrics/mttr.md", frontmatter: { type: "Metric", title: "MTTR" }, body: "# Definition\n\nHours.",
      base_sha256: (await (await fetch(`/api/workspaces/${WS}/knowledge/packs/${WS_PACK}/document?path=metrics%2Fmttr.md`)).json() as { sha256: string }).sha256,
    }));
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    expect(await screen.findByText(/changed since you opened it/)).toBeTruthy();
    expect(screen.getByText(/Someone saved this document after you opened it/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(screen.getByRole("button", { name: "New document" }));
    const form = screen.getByRole("form", { name: "New document" });
    fireEvent.change(within(form).getByLabelText("Path"), { target: { value: "notes/index.md" } });
    fireEvent.click(within(form).getByRole("button", { name: "Save revision" }));
    expect(await within(form).findByText(/reserved OKF files/)).toBeTruthy();
    expect(calls(f, "PUT", /\/document$/)).toHaveLength(1); // only the conflicting attempt went out
  });
});

// ------------------------------------------------------------------------------------ review queue
describe("Knowledge studio: review queue", () => {
  it("shows per-field confidence and provenance, needs a reason to reject, and approves a batch as one revision", async () => {
    const f = mockFetch();
    const { container } = renderAt(`/w/${WS}/knowledge/catalog?tab=review`);
    const drafts = await screen.findByRole("list", { name: "Knowledge drafts" });
    const reopen = within(drafts).getByRole("article", { name: "Draft: Reopen rate" });
    expect(within(reopen).getByText("glossary term")).toBeTruthy();
    expect(within(reopen).getAllByRole("meter", { name: "Confidence" }).map((m) => m.getAttribute("aria-valuenow"))).toEqual(["55", "55", "80"]);
    expect(within(reopen).getAllByText("openrouter/auto").length).toBeGreaterThan(0);
    expect(within(reopen).getByText("crawl-enrich-v2")).toBeTruthy();
    const incident = within(drafts).getByRole("article", { name: "Draft: incident" });
    expect(within(incident).getByText(/was: One row per incident\./)).toBeTruthy();
    expect(await axeClean(container)).toEqual([]);

    fireEvent.click(screen.getByLabelText("Select incident"));
    fireEvent.click(screen.getByRole("button", { name: "Reject selected (1)" }));
    expect(await screen.findByText(/a rejection is kept as negative knowledge/)).toBeTruthy();
    expect(calls(f, "POST", /suggestions\/review$/)).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("Reason for rejecting the selection"), { target: { value: "Too vague" } });
    fireEvent.click(screen.getByRole("button", { name: "Reject selected (1)" }));
    expect(await screen.findByText(/1 rejected/)).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /suggestions\/review$/)[0][1])).toEqual({ decisions: [{ id: "ksug_incident", action: "reject", reason: "Too vague" }] });

    fireEvent.click(await screen.findByLabelText("Select all (2)"));
    fireEvent.click(screen.getByRole("button", { name: "Approve selected (2)" }));
    expect(await screen.findByText(/revision 3: 2 approved, 0 rejected/)).toBeTruthy();
    expect(bodyOf(calls(f, "POST", /suggestions\/review$/)[1][1]).decisions).toHaveLength(2);
    expect(await screen.findByText("Nothing to review")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open glossary/reopen-rate.md" }));
    expect(await screen.findByRole("heading", { name: "Reopen rate", level: 2 })).toBeTruthy();
    expect(screen.getAllByText("from review queue").length).toBeGreaterThan(0);
  });

  it("edits a draft, then approves it: only the edited field is sent", async () => {
    const f = mockFetch();
    renderAt(`/w/${WS}/knowledge/catalog?tab=review`);
    const reopen = await screen.findByRole("article", { name: "Draft: Reopen rate" });
    fireEvent.click(within(reopen).getByRole("button", { name: "Edit, then approve" }));
    const form = within(reopen).getByRole("form", { name: "Edit Reopen rate" });
    fireEvent.change(within(form).getByLabelText("body"), { target: { value: "Share of resolved incidents reopened within 14 days." } });
    fireEvent.click(within(form).getByRole("button", { name: "Approve with edits" }));
    await waitFor(() => expect(calls(f, "POST", /suggestions\/review$/)).toHaveLength(1));
    expect(bodyOf(calls(f, "POST", /suggestions\/review$/)[0][1])).toEqual({
      decisions: [{ id: SUGGESTION_TERM, action: "edit", fields: { body: "Share of resolved incidents reopened within 14 days." } }] });
    expect(await screen.findByText(/1 approved/)).toBeTruthy();
  });
});

// ------------------------------------------------------------------------------------ graph, metrics, transfer
describe("Knowledge studio: graph, metrics, import and export", () => {
  it("lists governed and inferred edges where no canvas is available", async () => {
    mockFetch();
    const { container } = renderAt(`/w/${WS}/knowledge/catalog?tab=graph`);
    // Definitions open on "Data model"; the graph is its own section
    fireEvent.click(await screen.findByRole("button", { name: /Semantic graph/ }));
    const table = await screen.findByRole("table", { name: "Semantic graph edges" });
    const rows = within(table).getAllByRole("row").slice(1);
    const governed = rows.filter((r) => r.getAttribute("data-governed") === "true").length;
    expect(governed).toBeGreaterThan(0);
    expect(rows.length - governed).toBeGreaterThan(0);
    expect(within(table).getByText("suggests")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Edge legend" }).textContent).toMatch(/Governed.*Inferred/s);
    fireEvent.click(screen.getByLabelText("Show inferred edges"));
    await waitFor(() => expect(within(table).queryAllByText("inferred")).toHaveLength(0));
    expect(await axeClean(container)).toEqual([]);
  });

  it("edits Ossie metrics with the shared KPI editor", async () => {
    mockFetch();
    renderAt(`/w/${WS}/knowledge/catalog?tab=metrics`);
    expect(await screen.findByRole("form", { name: "Propose a KPI" })).toBeTruthy();
    expect(await screen.findByRole("list", { name: "KPIs" })).toBeTruthy();
  });

  it("imports a bundle, downloads a pack and asks for the push approval", async () => {
    const f = mockFetch();
    const createObjectURL = vi.fn(() => "blob:mock-okf");
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
    const { container } = renderAt(`/w/${WS}/knowledge/catalog?tab=transfer`);
    const form = await screen.findByRole("form", { name: "Import a knowledge bundle" });
    fireEvent.click(within(form).getByRole("button", { name: "Import" }));
    expect(await within(form).findByText("Choose a .zip bundle.")).toBeTruthy();
    const file = new File(["PK"], "Atlas Sample.zip", { type: "application/zip" });
    fireEvent.change(within(form).getByLabelText("Bundle (.zip)"), { target: { files: [file] } });
    expect((within(form).getByLabelText("Pack name (slug)") as HTMLInputElement).value).toBe("atlas-sample");
    fireEvent.click(within(form).getByRole("button", { name: "Import" }));
    const report = await screen.findByLabelText("Import report");
    expect(within(report).getByText(/counted as claims/)).toBeTruthy();
    expect(within(report).getByText(/never executed/)).toBeTruthy();
    const sent = calls(f, "POST", /\/knowledge\/import$/)[0][1].body as FormData;
    expect(sent.get("slug")).toBe("atlas-sample");
    await waitFor(() => expect(screen.getByRole("list", { name: "Packs to export" }).textContent).toContain("imported"));

    fireEvent.click(screen.getByRole("button", { name: "Download Workspace knowledge as a zip" }));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Downloaded/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Request push approval" }));
    expect(await screen.findByText(/apr_kpush/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "approvals inbox" }).getAttribute("href")).toBe(`/w/${WS}/operate/approvals`);
    fireEvent.click(screen.getByRole("button", { name: "Push" }));
    expect(await screen.findByText(/pending, not approved/)).toBeTruthy(); // nothing leaves before the approval
    expect(await axeClean(container)).toEqual([]);
  });
});

// ------------------------------------------------------------------------------------ the journey
describe("review an AI suggestion → publish → it is a receipt in Ask", () => {
  it("shows the approved document on the Evidence tab and opens it in the studio", async () => {
    mockFetch();
    mockBackend("POST", `/api/workspaces/${WS}/knowledge/suggestions/review`, JSON.stringify({ decisions: [{ id: SUGGESTION_TERM, action: "approve" }] }));
    renderAt(`/w/${WS}/ask`);
    fireEvent.change(await screen.findByLabelText("Question"), { target: { value: "What is the reopen rate for P1 incidents?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await screen.findByRole("article", { name: "Question 1" });
    const inspector = screen.getByRole("complementary", { name: "Answer inspector" });
    fireEvent.click(await within(inspector).findByRole("tab", { name: "Evidence" }));
    const receipts = await within(inspector).findByRole("list", { name: "Context receipts" });
    const item = within(receipts).getByText(/Reopen rate/).closest("li")!;
    expect(item.textContent).toContain("glossary/reopen-rate.md#definition");
    expect(within(item).getByText("reviewed")).toBeTruthy();
    fireEvent.click(within(item).getByRole("link", { name: "Open Reopen rate in the knowledge studio" }));
    expect(await screen.findByRole("heading", { name: "Reopen rate", level: 2 })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Documents", selected: true })).toBeTruthy();
  });
});
