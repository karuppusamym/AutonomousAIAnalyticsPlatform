/**
 * Schedule form helpers: cron presets, a plain-language cron description, client-side validation
 * and the kind-specific config the API expects (services/schedules.py). Pure functions (unit-tested).
 *
 * The server is authoritative (croniter + the 15-minute minimum interval); these checks only catch
 * obvious mistakes before the round trip, and the form always shows the server's message too.
 */
import { splitList } from "./sourceKinds";
import type { ReportFormat, ReportKind, Schedule, ScheduleConfig, ScheduleInput, ScheduleKind } from "../api";

export const SCHEDULE_KINDS: { id: ScheduleKind; label: string; description: string }[] = [
  { id: "reanalysis", label: "Re-analysis", description: "Refresh data, re-run the analysis, compare with the previous run and build a report." },
  { id: "dataset_refresh", label: "Dataset refresh", description: "Re-load staged sources so analyses and monitors see fresh data." },
  { id: "report", label: "Report", description: "Generate a report from the latest (or a chosen) completed run." },
  { id: "monitor", label: "Monitor evaluation", description: "Evaluate monitors and raise alerts on material changes." },
  { id: "crawl", label: "Metadata crawl", description: "Re-crawl source metadata to catch schema drift (new, changed, missing and renamed tables)." },
];

export const REPORT_KINDS: { id: ReportKind; label: string }[] = [
  { id: "executive", label: "Executive" },
  { id: "operational", label: "Operational" },
  { id: "statistical", label: "Statistical" },
  { id: "exception", label: "Exception" },
  { id: "weekly_summary", label: "Weekly summary" },
];

export const REPORT_FORMATS: ReportFormat[] = ["html", "pdf", "xlsx", "docx", "pptx", "md"];

export type PresetId = "weekly_monday_0700" | "daily_0600" | "monthly_1st_0700";

export const CRON_PRESETS: { id: PresetId; label: string; cron: string }[] = [
  { id: "weekly_monday_0700", label: "Weekly — Monday 07:00", cron: "0 7 * * 1" },
  { id: "daily_0600", label: "Daily — 06:00", cron: "0 6 * * *" },
  { id: "monthly_1st_0700", label: "Monthly — 1st at 07:00", cron: "0 7 1 * *" },
];

export const MIN_INTERVAL_MINUTES = 15;

const normalise = (cron: string) => cron.trim().split(/\s+/).join(" ");

export function cronForPreset(id: string): string | null {
  return CRON_PRESETS.find((p) => p.id === id)?.cron ?? null;
}

/** The preset a cron expression corresponds to, or "custom". */
export function presetForCron(cron: string): PresetId | "custom" {
  const c = normalise(cron);
  return CRON_PRESETS.find((p) => p.cron === c)?.id ?? "custom";
}

const DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const DAY_NAMES: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };

function ordinal(n: number): string {
  const s = n % 100 >= 11 && n % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `${n}${s}`;
}

/** Plain-language description of common cron shapes; falls back to the raw expression. */
export function describeCron(cron: string): string {
  const f = normalise(cron).split(" ");
  if (f.length !== 5) return `cron “${cron.trim()}”`;
  const [m, h, dom, mon, dow] = f;
  const isNum = (x: string) => /^\d+$/.test(x);
  if (isNum(m) && isNum(h)) {
    const at = `${h.padStart(2, "0")}:${m.padStart(2, "0")}`;
    if (dom === "*" && mon === "*" && dow === "*") return `Every day at ${at}`;
    if (dom === "*" && mon === "*" && dow === "1-5") return `Every weekday at ${at}`;
    const d = isNum(dow) ? Number(dow) : DAY_NAMES[dow.toLowerCase()];
    if (dom === "*" && mon === "*" && d !== undefined && d >= 0 && d <= 7) return `Every ${DAYS[d]} at ${at}`;
    if (isNum(dom) && mon === "*" && dow === "*") return `Monthly on the ${ordinal(Number(dom))} at ${at}`;
  }
  if (isNum(m) && h === "*" && dom === "*" && mon === "*" && dow === "*") return `Every hour at :${m.padStart(2, "0")}`;
  const step = /^\*\/(\d+)$/.exec(h);
  if (isNum(m) && step && dom === "*" && mon === "*" && dow === "*") return `Every ${step[1]} hours at :${m.padStart(2, "0")}`;
  return `cron “${normalise(cron)}”`;
}

/** Expand a cron minute field into the minutes it fires on (null when it cannot be parsed). */
export function expandMinutes(field: string): number[] | null {
  const out = new Set<number>();
  for (const part of field.split(",")) {
    const m = /^(\*|\d+(?:-\d+)?)(?:\/(\d+))?$/.exec(part);
    if (!m) return null;
    let lo = 0;
    let hi = 59;
    if (m[1] !== "*") {
      const [a, b] = m[1].split("-").map(Number);
      lo = a;
      hi = b ?? (m[2] ? 59 : a);
    }
    const step = m[2] ? Number(m[2]) : 1;
    if (lo > 59 || hi > 59 || lo > hi || step < 1) return null;
    for (let v = lo; v <= hi; v += step) out.add(v);
  }
  return [...out].sort((a, b) => a - b);
}

/** Smallest gap in minutes between firings within an hour (60 for a single minute). */
export function minuteGap(minutes: number[]): number {
  if (minutes.length <= 1) return 60;
  let gap = 60 - minutes[minutes.length - 1] + minutes[0];
  for (let i = 1; i < minutes.length; i++) gap = Math.min(gap, minutes[i] - minutes[i - 1]);
  return gap;
}

/** Client-side cron check. Returns an error message or null. */
export function validateCron(cron: string): string | null {
  const c = normalise(cron);
  if (!c) return "Cron expression is required.";
  const fields = c.split(" ");
  if (fields.length !== 5) return "Cron needs 5 fields: minute hour day-of-month month day-of-week.";
  if (!fields.every((x) => /^[\w*/,\-?#]+$/.test(x))) return "Cron contains unsupported characters.";
  const minutes = expandMinutes(fields[0]);
  if (minutes && minuteGap(minutes) < MIN_INTERVAL_MINUTES) {
    return `Schedules may not fire more often than every ${MIN_INTERVAL_MINUTES} minutes.`;
  }
  return null;
}

export function isValidTimeZone(tz: string): boolean {
  if (!tz.trim()) return false;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

export function timeZoneOptions(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  try {
    const list = intl.supportedValuesOf?.("timeZone") ?? [];
    return list.includes("UTC") ? list : ["UTC", ...list];
  } catch {
    return ["UTC"];
  }
}

/** Format an instant in a given IANA zone, e.g. "Sep 29, 2026, 07:00 GMT+2". */
export function fmtInZone(iso: string | null | undefined, timeZone?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const opts: Intl.DateTimeFormatOptions = {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZoneName: "short",
  };
  try {
    return d.toLocaleString(undefined, timeZone ? { ...opts, timeZone } : opts);
  } catch {
    return d.toLocaleString(undefined, opts);
  }
}

// ---------------------------------------------------------------------------------- form state
export interface ScheduleFormState {
  name: string;
  kind: ScheduleKind;
  preset: PresetId | "custom";
  cron: string;
  timezone: string;
  // reanalysis
  objective: string;
  refreshFirst: boolean;
  publish: "skip" | "propose";
  includeReport: boolean;
  // reanalysis + report
  reportKind: ReportKind;
  formats: ReportFormat[];
  // report
  runId: string;
  // monitor
  monitorIds: string[];
  // dataset_refresh + crawl
  sourceIds: string[];
  // crawl
  crawlMode: "" | "full" | "incremental";
  crawlInclude: string;
  crawlExclude: string;
  // any kind: builder dashboards whose summary (digest) each successful run saves and delivers (CONV-05)
  digestDashboards: string[];
}

export function emptyScheduleForm(timezone = browserTimeZone()): ScheduleFormState {
  return {
    name: "", kind: "reanalysis", preset: "weekly_monday_0700", cron: CRON_PRESETS[0].cron, timezone,
    objective: "", refreshFirst: true, publish: "skip", includeReport: true,
    reportKind: "weekly_summary", formats: ["html", "pdf", "xlsx"], runId: "", monitorIds: [], sourceIds: [],
    crawlMode: "", crawlInclude: "", crawlExclude: "", digestDashboards: [],
  };
}

/**
 * Whether the schedule form can edit this kind. A saved-analysis, step or pipeline schedule is created
 * from its source (an approved Ask calculation, a step pin, a pipeline) with a config the form does not
 * know; editing it here would send re-analysis keys, so it is changed where it was made.
 */
export function scheduleEditable(kind: string): boolean {
  return SCHEDULE_KINDS.some((k) => k.id === kind);
}

/** Pre-fill the form from an existing schedule (edit). */
export function formFromSchedule(s: Schedule): ScheduleFormState {
  const base = emptyScheduleForm(s.timezone);
  const c = s.config ?? {};
  const kind = (SCHEDULE_KINDS.some((k) => k.id === s.kind) ? s.kind : "reanalysis") as ScheduleKind;
  const report = kind === "report" ? { kind: c.kind, formats: c.formats } : c.report;
  return {
    ...base,
    name: s.name, kind, cron: s.cron, preset: presetForCron(s.cron), timezone: s.timezone,
    objective: c.objective ?? "",
    refreshFirst: c.refresh_first ?? true,
    publish: c.publish === "propose" ? "propose" : "skip",
    includeReport: kind === "reanalysis" ? c.report !== null : true,
    reportKind: (report?.kind as ReportKind | undefined) ?? (kind === "report" ? "executive" : base.reportKind),
    formats: ((report?.formats as ReportFormat[] | undefined) ?? base.formats).filter((f) => REPORT_FORMATS.includes(f)),
    runId: c.run_id ?? "",
    monitorIds: c.monitor_ids ?? [],
    sourceIds: c.source_ids ?? [],
    crawlMode: kind === "crawl" && (c.mode === "full" || c.mode === "incremental") ? c.mode : "",
    crawlInclude: kind === "crawl" ? (c.include ?? []).join(", ") : "",
    crawlExclude: kind === "crawl" ? (c.exclude ?? []).join(", ") : "",
    digestDashboards: digestDashboards(c),
  };
}

/** The kind-specific `config` object sent to the API. */
export function buildScheduleConfig(f: ScheduleFormState): ScheduleConfig {
  const cfg = kindConfig(f);
  return f.digestDashboards.length ? { ...cfg, digest_dashboards: [...f.digestDashboards] } : cfg;
}

function kindConfig(f: ScheduleFormState): ScheduleConfig {
  switch (f.kind) {
    case "reanalysis": {
      const cfg: ScheduleConfig = { refresh_first: f.refreshFirst, publish: f.publish };
      if (f.objective.trim()) cfg.objective = f.objective.trim();
      // The API defaults `report` to a weekly summary when the key is absent; null turns it off.
      cfg.report = f.includeReport ? { kind: f.reportKind, formats: [...f.formats] } : null;
      return cfg;
    }
    case "report": {
      const cfg: ScheduleConfig = { kind: f.reportKind, formats: [...f.formats] };
      if (f.runId) cfg.run_id = f.runId;
      return cfg;
    }
    case "monitor":
      return f.monitorIds.length ? { monitor_ids: [...f.monitorIds] } : {};
    case "dataset_refresh":
      return f.sourceIds.length ? { source_ids: [...f.sourceIds] } : {};
    case "crawl": {
      const cfg: ScheduleConfig = {};
      if (f.sourceIds.length) cfg.source_ids = [...f.sourceIds];
      if (f.crawlMode) cfg.mode = f.crawlMode;
      const include = splitList(f.crawlInclude);
      const exclude = splitList(f.crawlExclude);
      if (include.length) cfg.include = include;
      if (exclude.length) cfg.exclude = exclude;
      return cfg;
    }
    default:
      return {};
  }
}

export function buildScheduleInput(f: ScheduleFormState): ScheduleInput {
  return { name: f.name.trim(), kind: f.kind, cron: normalise(f.cron), timezone: f.timezone.trim(), config: buildScheduleConfig(f) };
}

export type ScheduleFormErrors = Partial<Record<"name" | "cron" | "timezone" | "formats", string>>;

export function validateScheduleForm(f: ScheduleFormState): ScheduleFormErrors {
  const errors: ScheduleFormErrors = {};
  if (!f.name.trim()) errors.name = "Name is required.";
  const cronErr = validateCron(f.cron);
  if (cronErr) errors.cron = cronErr;
  if (!isValidTimeZone(f.timezone)) errors.timezone = `Unknown time zone “${f.timezone}”. Use an IANA name such as Europe/Berlin.`;
  const needsFormats = f.kind === "report" || (f.kind === "reanalysis" && f.includeReport);
  if (needsFormats && f.formats.length === 0) errors.formats = "Choose at least one report format.";
  return errors;
}

const MANAGED_KEYS = new Set([
  "objective", "refresh_first", "publish", "report", "kind", "formats", "run_id", "monitor_ids", "source_ids", "mode", "include", "exclude",
  "digest_dashboards",
]);

/**
 * PATCH replaces `config` wholesale, so keep keys the form does not manage (e.g. the backend's
 * `baseline_run_id`) when editing, while letting the form clear the keys it owns.
 */
export function mergeScheduleConfig(original: ScheduleConfig | undefined, built: ScheduleConfig): ScheduleConfig {
  const kept = Object.fromEntries(Object.entries(original ?? {}).filter(([k]) => !MANAGED_KEYS.has(k)));
  return { ...kept, ...built };
}

/** The builder dashboards whose summary a schedule sends with each successful run (`config.digest_dashboards`). */
export function digestDashboards(config: ScheduleConfig | undefined): string[] {
  const ids = (config ?? {}).digest_dashboards;
  return Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : [];
}

/**
 * The schedule's whole config with `dashboardId`'s summary added or removed. PATCH replaces `config`, so every other
 * key (destinations, pins, report settings) is kept as it is; an empty list removes the key.
 */
export function withDigest(config: ScheduleConfig | undefined, dashboardId: string, on: boolean): ScheduleConfig {
  const current = digestDashboards(config);
  const next = on ? [...new Set([...current, dashboardId])] : current.filter((id) => id !== dashboardId);
  const { digest_dashboards: _old, ...rest } = config ?? {};
  return next.length ? { ...rest, digest_dashboards: next } : rest;
}
