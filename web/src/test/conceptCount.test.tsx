/**
 * First-login weight (P7-18): how many distinct things a person sees on first login to a fresh
 * workspace. Method (the build-right study §3): count the distinct nav entries, headings and
 * controls rendered in the shell, excluding anything inside a closed <details> (Technical details,
 * Advanced) and anything aria-hidden or screen-reader-only.
 *
 * Measured with this method on 2026-09-26: 44 for every role before the light IA (b82a69c: 16 nav
 * entries, the counters, Start analysis with an autonomy picker, members & policy, empty lists);
 * after it, 20 for an analyst and 24 for an admin (the gear adds its three platform screens and
 * Members & policy). The ceilings below keep it from creeping back.
 * 2026-10-27: +2 for the two nav entries the v2 IA adds (spec S11 §2: Work → Request desk, Overview → Value);
 * nothing else on first login changed.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { session, type User, type WorkspaceDetail } from "../api";
import { AppRoutes } from "../App";
import { AuthProvider } from "../auth";
import { mockBackend, USER, WORKSPACE } from "./mockBackend";

const FRESH = "ws_fresh";
export const CEILING = { analyst: 24, admin: 28 } as const;

const ANALYST: User = { ...USER, id: "usr_analyst", email: "analyst@analystos.local", name: "Ana Analyst", is_admin: false };
const FRESH_WS: WorkspaceDetail = {
  ...WORKSPACE, id: FRESH, name: "New workspace", description: "", objective: "", role: "owner",
  counts: { query: 0, dataset: 0, metric: 0, chart: 0, dashboard: 0, runs: 0, verified_insights: 0, sources: 0 },
};

/** A workspace with nothing in it: every collection is empty. */
function freshFetch(user: User) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://x");
    const p = url.pathname.replace(/^\/api/, "");
    const reply = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    if (p === "/auth/me") return reply(user);
    if (p === "/workspaces") return reply([{ ...FRESH_WS }]);
    if (p === `/workspaces/${FRESH}`) return reply({ ...FRESH_WS, role: user.is_admin ? "owner" : "analyst" });
    if (p.startsWith(`/workspaces/${FRESH}/`)) return reply([]);
    const r = mockBackend(init?.method ?? "GET", String(input));
    return new Response(r.body, { status: r.status, headers: { "Content-Type": r.contentType } });
  });
}

function labelOf(el: Element): string {
  const aria = el.getAttribute("aria-label");
  if (aria) return aria.trim();
  if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement) {
    const id = el.id;
    const lab = id ? el.ownerDocument.querySelector(`label[for="${id}"]`) : el.closest("label");
    return (lab?.textContent ?? el.getAttribute("placeholder") ?? el.name ?? "").trim();
  }
  return (el.textContent ?? "").replace(/\s+/g, " ").trim();
}

/** Distinct visible nav entries, headings and controls. */
export function firstLoginConcepts(root: ParentNode = document.body): string[] {
  const seen = new Set<string>();
  for (const el of Array.from(root.querySelectorAll("a[href], button, input, select, textarea, [role=tab], h1, h2, h3"))) {
    if (el.closest("details:not([open]) > :not(summary)") || el.closest("[aria-hidden=true]") || el.closest(".sr-only")) continue;
    if (el instanceof HTMLInputElement && el.type === "hidden") continue;
    const label = labelOf(el);
    if (label) seen.add(`${/^H\d$/.test(el.tagName) ? "heading" : "control"}:${label}`);
  }
  return [...seen].sort();
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  session.clear();
});

async function measure(user: User): Promise<string[]> {
  session.set("mock-token", user);
  freshFetch(user);
  render(<AuthProvider><MemoryRouter initialEntries={[`/w/${FRESH}`]}><AppRoutes /></MemoryRouter></AuthProvider>);
  await screen.findByRole("heading", { name: "New workspace", level: 1 });
  await waitFor(() => expect(document.body.textContent).not.toMatch(/Loading/));
  await new Promise((r) => setTimeout(r, 50));
  return firstLoginConcepts();
}

describe("first-login concept count (P7-18)", () => {
  it.each([["analyst", ANALYST], ["admin", USER]] as const)("a fresh workspace shows few concepts to an %s", async (role, user) => {
    const concepts = await measure(user);
    // Printed so the number can be reported (build-right study §3 measured ≈35 before the light IA).
    console.info(`first-login concepts (${role}): ${concepts.length}\n  ${concepts.join("\n  ")}`);
    expect(concepts.length).toBeLessThanOrEqual(CEILING[role]);
    // only the next step of the checklist offers an action; no counters, cost or autonomy on first login
    expect(concepts.filter((c) => /^control:(Connect data|Choose tables|Review the catalog|Save goal|Start work)$/.test(c))).toEqual(["control:Connect data"]);
    // The Autonomy Center nav entry (S11 §2, v2 spec) is a destination, not an autonomy setting: the
    // autonomy level itself stays under Members & policy › Advanced.
    expect(concepts.some((c) => /Autonomy(?! Center)|Tokens|Cost|Verified|Runs/.test(c))).toBe(false);
  });
});
