/**
 * One trust label for every number (blue ocean discovery, 2026-10-30): the product's promise is "every number comes
 * with proof, or goes to a person", and readers should not have to learn registry / semantic / rules / governed /
 * ad hoc to know how far to trust a number. Three levels, the same words on every screen:
 *
 * * Certified: computed from a definition an owner approved (a verified query or an approved metric).
 * * Checked: computed here through the governed path and checked, but no owner has approved the definition yet.
 * * Unchecked: not computed here (a number quoted from elsewhere, or a question that was not answered).
 */
export type TrustLevel = "certified" | "checked" | "unchecked";

export const TRUST: Record<TrustLevel, { label: string; hint: string; tone: "success" | "info" | "neutral" }> = {
  certified: { label: "Certified", hint: "From a definition an owner approved", tone: "success" },
  checked: { label: "Checked", hint: "Computed and checked here; no owner-approved definition yet", tone: "info" },
  unchecked: { label: "Unchecked", hint: "Not computed here", tone: "neutral" },
};

const CERTIFIED_BY = new Set(["registry", "semantic"]);

/** The trust level of an Ask answer (or anything shaped like one). */
export function trustOf(x: { status?: string | null; answered_by?: string | null; governance?: string | null }): TrustLevel {
  if (x.status && x.status !== "answered") return "unchecked";
  if (CERTIFIED_BY.has(x.answered_by ?? "") || x.governance === "governed") return "certified";
  return x.status === "answered" ? "checked" : "unchecked";
}

/** The trust level of a number check's computed side (`authority` from evidence/number_check.py). */
export function trustOfAuthority(authority: "certified" | "ad_hoc" | null | undefined): TrustLevel {
  return authority === "certified" ? "certified" : authority === "ad_hoc" ? "checked" : "unchecked";
}
