/**
 * The workspace policy form (spec v4 §15): the common fields as plain controls, the whole document
 * as raw JSON under Advanced. Both edit the same object; saving sends the merged document, so a field
 * the form does not know is kept, never dropped.
 */
import type { WorkspacePolicy } from "../api";

/** Parse a JSON object; returns an error string instead of throwing. */
export function parsePolicy(text: string): { value?: Record<string, unknown>; error?: string } {
  try {
    const v = JSON.parse(text) as unknown;
    if (!v || typeof v !== "object" || Array.isArray(v)) return { error: "policy must be a JSON object" };
    return { value: v as Record<string, unknown> };
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

export type PolicyFieldKind = "int" | "usd" | "bool" | "pii" | "list";

export interface PolicyField {
  key: keyof WorkspacePolicy & string;
  label: string;
  hint: string;
  kind: PolicyFieldKind;
}

export const POLICY_FIELDS: PolicyField[] = [
  { key: "max_rows", label: "Most rows a query may return", hint: "Larger results are cut off and labelled as partial.", kind: "int" },
  { key: "run_cost_budget_usd", label: "Spend limit per investigation (USD)", hint: "Model calls stop at this amount.", kind: "usd" },
  { key: "workspace_monthly_cost_budget_usd", label: "Monthly spend limit (USD)", hint: "For the whole workspace.", kind: "usd" },
  { key: "pii_access", label: "Personal data", hint: "Whether agents may read columns tagged as personal data.", kind: "pii" },
  { key: "restricted_columns", label: "Columns agents may never read", hint: "One per line: schema.table.column, or *.column for every table.", kind: "list" },
  { key: "publish_destinations", label: "Where outputs may be published", hint: "One destination per line, e.g. superset.", kind: "list" },
  { key: "publish_requires_approval", label: "Publishing needs an approval", hint: "Recommended; the platform may require it regardless.", kind: "bool" },
  { key: "separation_of_duties", label: "The person who asks cannot approve", hint: "When on, KPI and publication reviewers must differ from the requester. When off, an authorized requester may approve their own KPI or publication. Model/relationship review and external destination authorization still require a different reviewer.", kind: "bool" },
];

/** A form value → the policy value (empty clears the field so the platform default applies). */
export function fieldValue(kind: PolicyFieldKind, raw: string | boolean): unknown {
  if (kind === "bool") return Boolean(raw);
  const text = String(raw).trim();
  if (kind === "list") return text ? text.split(/[\n,]/).map((s) => s.trim()).filter(Boolean) : [];
  if (kind === "pii") return text || undefined;
  if (!text) return undefined;
  const n = Number(text);
  return Number.isFinite(n) ? (kind === "int" ? Math.trunc(n) : n) : undefined;
}

/** The policy value → what the form control shows. */
export function fieldText(kind: PolicyFieldKind, v: unknown): string | boolean {
  if (kind === "bool") return v === true;
  if (kind === "list") return Array.isArray(v) ? v.map(String).join("\n") : "";
  return v === undefined || v === null ? "" : String(v);
}

/** Apply one form edit to the document; undefined removes the key. */
export function setField(policy: Record<string, unknown>, key: string, value: unknown): Record<string, unknown> {
  const next = { ...policy };
  if (value === undefined) delete next[key];
  else next[key] = value;
  return next;
}
