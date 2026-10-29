/**
 * Cross-filter and drill-down helpers for the dashboard preview (CONV-06). Pure, so they test without a canvas.
 *
 * The view state lives in the browser session only: filters and drill paths are never saved with the dashboard
 * ("Apply as dashboard filter" is a typed op on the server). The browser sends names and the clicked values; the
 * server validates them and re-queries each tile through its governed path, so nothing here builds a predicate.
 */
import type { BuilderChart, DrillStep, ViewFilter } from "../api";
import { label, type Preview } from "./charts";

export const DEBOUNCE_MS = 250;
export const VIEW_CACHE_SIZE = 20;

/** The raw value behind a drawn label (charts draw `label(value)`; the server needs the value itself). */
export function memberOf(preview: Preview | undefined | null, column: number, drawn: string): unknown {
  for (const r of preview?.rows ?? []) if (label(r[column]) === drawn) return r[column];
  return undefined;
}

/** Distinct members of a preview column in the order drawn (for the keyboard "Filter by" and "Drill into" lists). */
export function membersOf(preview: Preview | undefined | null, column: number, max = 50): { value: unknown; text: string }[] {
  const seen = new Map<string, unknown>();
  for (const r of preview?.rows ?? []) {
    const text = label(r[column]);
    if (!seen.has(text)) seen.set(text, r[column]);
    if (seen.size >= max) break;
  }
  return [...seen.entries()].map(([text, value]) => ({ value, text }));
}

/** Business name of a field: the chart's label, else its last segment in plain words. */
export function fieldLabel(chart: BuilderChart | undefined, field: string): string {
  const f = chart?.fields.find((x) => x.name === field);
  if (f?.label) return f.label;
  const last = field.split(".").pop() ?? field;
  const words = last.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function valueText(v: unknown): string {
  return Array.isArray(v) ? v.map(label).join(", ") : label(v);
}

/** "Priority = 1 - Critical" (a chip's text). */
export function chipText(f: ViewFilter): string {
  const name = f.label ?? fieldLabel(undefined, f.field);
  return f.op === "in" ? `${name} is one of ${valueText(f.value)}` : `${name} = ${valueText(f.value)}`;
}

const canon = (name: string) => (name.split(".").pop() ?? name).toLowerCase();

/** Add or replace the filter on a field (one filter per field; the same click again removes it). */
export function toggleFilter(filters: ViewFilter[], next: ViewFilter): ViewFilter[] {
  const same = filters.find((f) => canon(f.field) === canon(next.field));
  const rest = filters.filter((f) => canon(f.field) !== canon(next.field));
  if (same && JSON.stringify(same.value) === JSON.stringify(next.value)) return rest;
  return [...rest, next];
}

export function viewKey(version: number, filters: ViewFilter[], refreshSaved: boolean): string {
  return JSON.stringify([version, refreshSaved, filters.map((f) => [f.field, f.op, f.value, f.source ?? null])]);
}

export function drillKey(version: number, chartId: string, path: DrillStep[], filters: ViewFilter[], detail: boolean): string {
  return JSON.stringify([version, chartId, detail, path.map((s) => [s.field, s.value]), filters.map((f) => [f.field, f.value, f.source ?? null])]);
}

/** A small least-recently-used cache, so going back to an earlier selection does not re-query (bounded). */
export class LruCache<V> {
  private map = new Map<string, V>();
  constructor(private readonly size = VIEW_CACHE_SIZE) {}
  get(key: string): V | undefined {
    const v = this.map.get(key);
    if (v !== undefined) {
      this.map.delete(key);
      this.map.set(key, v);
    }
    return v;
  }
  set(key: string, value: V): void {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.size) this.map.delete(this.map.keys().next().value as string);
  }
  get length(): number {
    return this.map.size;
  }
}

/** Which preview column a field is drawn from (charts draw column 0 as the axis, column 1 as the series). */
export function columnOf(chart: BuilderChart, field: string): number {
  return chart.preview?.columns?.indexOf(field) ?? -1;
}
