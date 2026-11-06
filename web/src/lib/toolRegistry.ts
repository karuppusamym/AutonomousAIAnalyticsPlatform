/**
 * Pure helpers for the tool registry, external MCP admission and MCP client screens (spec S10).
 * `grantCovers` mirrors `mcp/grants.py::grants_tool` so the catalogue can show which client reaches which
 * tool; the server decides every call regardless.
 */
import type { McpGrantRow, RegistryTool } from "../api";
import type { Tone } from "./status";

export const SOURCE_TYPES = [
  { id: "metric", label: "Certified metric", refHint: "Metric name, e.g. payment_failure_rate" },
  { id: "verified_query", label: "Verified query", refHint: "Verified query id or name" },
  { id: "dataset", label: "Published dataset", refHint: "Dataset artifact id or name" },
  { id: "playbook", label: "Playbook", refHint: "Published playbook key, or a built-in such as investigate" },
] as const;
export type SourceType = (typeof SOURCE_TYPES)[number]["id"];

export const STATUS_TONE: Record<string, Tone> = {
  certified: "success", quarantined: "warning", refused: "danger", deprecated: "neutral", retired: "neutral",
};
export const statusTone = (s: string | null | undefined): Tone => STATUS_TONE[s ?? ""] ?? "neutral";

export const KIND_LABEL: Record<string, string> = {
  data_product: "Data product", playbook: "Playbook", external_mcp: "External MCP", builtin: "Built-in", agent: "Agent",
};
export const kindLabel = (k: string) => KIND_LABEL[k] ?? k;

/** Why the runtime would refuse a manifest, in words (toolreg/signing.py reasons). */
export function signatureInfo(sig: string | null | undefined): { tone: Tone; label: string } {
  switch (sig) {
    case "valid": return { tone: "success", label: "signature valid" };
    case "unsigned": return { tone: "danger", label: "unsigned: refused" };
    case "tampered": return { tone: "danger", label: "changed after signing: refused" };
    case "key": return { tone: "danger", label: "unknown or revoked key: refused" };
    default: return { tone: "danger", label: `refused (${sig ?? "unknown"})` };
  }
}

export const CLASSIFICATIONS = [
  { id: "none", label: "Read only (no side effect)" },
  { id: "write_internal", label: "Writes platform records" },
  { id: "write_external", label: "Writes externally (every call needs an approval)" },
] as const;

/** The platform's built-in MCP tools (mcp/grants.py TOOLS), granted by exact name; `context.read` grants every context tool. */
export const CONTEXT_TOOLS = ["context.search", "context.get", "context.compile", "context.changes"] as const;
export const BUILTIN_MCP_TOOLS = ["ask", "investigate", "get_finding_evidence", "validate_sql", "request.create", "request.status",
  "conversation.start", "conversation.say", "conversation.summary", "number.check", ...CONTEXT_TOOLS] as const;
/** What a grant's `tools` list may name besides registry ids and families. */
export const GRANTABLE_BUILTINS = [...BUILTIN_MCP_TOOLS, "context.read"];
const WRITING_BUILTINS = new Set(["investigate", "request.create", "conversation.start", "conversation.say"]);
const FAMILIES = ["query.", "http.", "metric.", "verified_query.", "dataset.", "playbook.", "mcp."];

const patternable = (tool: string) => FAMILIES.some((f) => tool.startsWith(f) && tool.length > f.length);

/** fnmatch-style glob (`*`, `?`, `[...]`), case-sensitive like the server's `fnmatchcase`. */
export function globMatch(name: string, pattern: string): boolean {
  let re = "";
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === "*") re += ".*";
    else if (c === "?") re += ".";
    else if (c === "[") {
      const end = pattern.indexOf("]", i + 1);
      if (end < 0) re += "\\[";
      else {
        const body = pattern.slice(i + 1, end).replace(/\\/g, "\\\\");
        re += `[${body.startsWith("!") ? `^${body.slice(1)}` : body}]`;
        i = end;
      }
    } else re += c.replace(/[.+^${}()|\\/]/g, "\\$&");
  }
  return new RegExp(`^${re}$`).test(name);
}

export function grantExpired(g: Pick<McpGrantRow, "expires_at">, now = Date.now()): boolean {
  return !!g.expires_at && Date.parse(g.expires_at) <= now;
}

/** Exact id, the family wildcard (`metric.*` in tools) or a tool pattern; patterns never cover built-in tools. */
export function grantCovers(g: Pick<McpGrantRow, "tools" | "tool_patterns" | "expires_at">, tool: string, now = Date.now()): boolean {
  if (grantExpired(g, now)) return false;
  if (g.tools.includes(tool) || ((CONTEXT_TOOLS as readonly string[]).includes(tool) && g.tools.includes("context.read"))) return true;
  if (!patternable(tool)) return false;
  return g.tools.includes(`${tool.split(".", 1)[0]}.*`) || (g.tool_patterns ?? []).some((p) => globMatch(tool, p));
}

/** What a workspace offers over MCP: built-in tools plus certified registry tools exposed over MCP. */
export function mcpCatalogue(tools: RegistryTool[]): { name: string; kind: string; readOnly: boolean; description: string }[] {
  const builtins = BUILTIN_MCP_TOOLS.map((name) => ({ name, kind: "built-in", readOnly: !WRITING_BUILTINS.has(name),
    description: "" }));
  const registry = tools.filter((t) => t.status === "certified" && t.exposure?.mcp)
    .map((t) => ({ name: t.tool_id, kind: kindLabel(t.kind), readOnly: t.side_effect === "none", description: t.description }));
  return [...builtins, ...registry];
}

/** Comma / whitespace separated list → unique non-empty entries. */
export function parseList(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean))];
}
