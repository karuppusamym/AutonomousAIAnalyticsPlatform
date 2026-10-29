/** Display helpers. Pure functions (unit-tested). */

export function fmtNumber(v: unknown, digits = 2): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (Number.isInteger(n)) return n.toLocaleString("en-US");
  const abs = Math.abs(n);
  if (abs !== 0 && abs < 0.001) return n.toExponential(2);
  return n.toLocaleString("en-US", { maximumFractionDigits: digits });
}

export function fmtPct(v: unknown, digits = 1): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function fmtP(v: unknown): string {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  if (n < 0.0001) return "< 0.0001";
  return n.toFixed(4);
}

/** Absent or non-numeric input: the display state for values the platform did not report. */
export const UNKNOWN = "unknown";

function knownNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Unknown (null/undefined/NaN) is "unknown", never "$0.0000". */
export function fmtUsd(v: unknown): string {
  const n = knownNumber(v);
  if (n === null) return UNKNOWN;
  if (n > 0 && n < 0.0001) return "<$0.0001";
  return `$${n < 1 ? n.toFixed(4) : n.toFixed(2)}`;
}

export function fmtMs(v: unknown): string {
  const n = knownNumber(v);
  if (n === null) return UNKNOWN;
  return n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`;
}

export type ValueFormat = "number" | "int" | "usd" | "pct" | "ms" | "text";

/** Format a known value, or return null when it is unknown (the caller renders the unknown state). */
export function formatKnown(v: unknown, format: ValueFormat = "number", digits?: number): string | null {
  if (format === "text") return v === null || v === undefined || v === "" ? null : String(v);
  const n = knownNumber(v);
  if (n === null) return null;
  switch (format) {
    case "int": return Math.round(n).toLocaleString("en-US");
    case "usd": return fmtUsd(n);
    case "pct": return fmtPct(n, digits ?? 1);
    case "ms": return fmtMs(n);
    default: return fmtNumber(n, digits ?? 2);
  }
}

export function fmtDate(v: string | null | undefined): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtTime(v: string | null | undefined): string {
  if (!v) return "—";
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return v;
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "number") return fmtNumber(v, 4);
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function shortHash(h: string | null | undefined, n = 12): string {
  return h ? h.slice(0, n) : "—";
}

/** One-line option text: a native dropdown's popup is as wide as its longest option, so long free text (an investigation's goal) is clipped. */
export function optionLabel(text: string | null | undefined, max = 80): string {
  const t = (text ?? "").replace(/\s+/g, " ").trim();
  return t.length > max ? `${t.slice(0, max - 1).trimEnd()}…` : t;
}

export function titleCase(s: string): string {
  return s.replace(/[_.]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function durationBetween(a: string | null | undefined, b: string | null | undefined): string {
  if (!a) return "—";
  const start = new Date(a).getTime();
  const end = b ? new Date(b).getTime() : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
  const s = Math.max(0, Math.round((end - start) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** "1 source", "2 sources": a count with its noun in the right number. */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
