import { useId, useState, type FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { roleAtLeast, to } from "../routes";
import { SchedulePins } from "../components/SchedulePins";
import { DeliveryDestinations } from "../components/DeliveryDestinations";
import { ContextWebhooks } from "../components/ContextWebhooks";
import { api, type Schedule, type ScheduleRun } from "../api";
import { Card, EmptyState, ErrorBox, Field, KeyValue, Loading, Notice, PageHeader, StatusBadge, Tag } from "../components/ui";
import { fmtDate } from "../lib/format";
import { useAction, useAsync } from "../lib/hooks";
import {
  CRON_PRESETS, REPORT_FORMATS, REPORT_KINDS, SCHEDULE_KINDS, buildScheduleInput, cronForPreset, describeCron, emptyScheduleForm,
  digestDashboards, fmtInZone, formFromSchedule, mergeScheduleConfig, presetForCron, scheduleEditable, timeZoneOptions, validateScheduleForm, type ScheduleFormErrors,
  type ScheduleFormState,
} from "../lib/schedules";

/** `embedded`: shown as a section of the Autonomy Center rather than as its own page. */
export function SchedulesPage({ embedded = false }: { embedded?: boolean } = {}) {
  const { wsId = "" } = useParams();
  const [params] = useSearchParams();
  const focus = params.get("schedule");
  const list = useAsync(() => api.listSchedules(wsId), [wsId]);
  const ws = useAsync(() => api.getWorkspace(wsId), [wsId]);
  const canEdit = roleAtLeast(ws.data?.role, "editor");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  return (
    <div className={embedded ? "embedded-page" : "page"}>
      <PageHeader level={embedded ? 2 : 1} title="Schedules"
        subtitle="Recurring investigations, data refreshes, reports, monitor checks and catalog crawls. Each runs with its owner's current permissions and the versions it was set up with; publishing always needs an approval."
        actions={!creating && <button type="button" className="btn btn-primary" onClick={() => { setCreating(true); setEditing(null); }}>New schedule</button>} />
      {creating && (
        <Card title="New schedule">
          <ScheduleForm wsId={wsId} onCancel={() => setCreating(false)} onSaved={() => { setCreating(false); void list.reload(); }} />
        </Card>
      )}
      <ErrorBox error={list.error} onRetry={list.reload} />
      {list.loading && !list.data && <Loading />}
      {list.data?.length === 0 && !creating && (
        <EmptyState title="No schedules yet">Create one to repeat an investigation weekly and get a “what changed” report.</EmptyState>
      )}
      <div className="stack">
        {list.data?.map((s) => editing === s.id && scheduleEditable(s.kind) ? (
          <Card key={s.id} title={`Edit “${s.name}”`}>
            <ScheduleForm wsId={wsId} initial={s} onCancel={() => setEditing(null)} onSaved={() => { setEditing(null); void list.reload(); }} />
          </Card>
        ) : (
          <ScheduleCard key={s.id} wsId={wsId} schedule={s} highlighted={focus === s.id} canEdit={canEdit} onEdit={() => { setEditing(s.id); setCreating(false); }}
            onChanged={() => void list.reload()}
            onPatched={(u) => list.setData((prev) => prev?.map((x) => (x.id === u.id ? { ...x, ...u, recent_runs: x.recent_runs } : x)))} />
        ))}
      </div>
      <DeliveryDestinations wsId={wsId} canEdit={canEdit} />
      <ContextWebhooks wsId={wsId} canEdit={canEdit} />
    </div>
  );
}

function kindLabel(kind: string): string {
  return SCHEDULE_KINDS.find((k) => k.id === kind)?.label ?? kind;
}

function ScheduleCard({ wsId, schedule: s, highlighted, canEdit, onEdit, onChanged, onPatched }: {
  wsId: string; schedule: Schedule; highlighted: boolean; canEdit: boolean; onEdit: () => void; onChanged: () => void; onPatched: (s: Schedule) => void;
}) {
  const act = useAction();
  const [notice, setNotice] = useState<string | null>(null);
  const toggle = async (enabled: boolean) => {
    const r = await act.run(() => api.updateSchedule(s.id, { enabled }));
    if (r) onPatched(r);
  };
  const runNow = async () => {
    setNotice(null);
    const r = await act.run(() => api.runScheduleNow(s.id));
    if (r) {
      setNotice(r.status === "failed" ? `Run failed: ${r.error ?? "see recent runs"}` : `Started (${r.status}).`);
      onChanged();
    }
  };
  const remove = async () => {
    if (!window.confirm(`Delete schedule “${s.name}”? Its run history is removed too.`)) return;
    const r = await act.run(() => api.deleteSchedule(s.id));
    if (r) onChanged();
  };
  const tz = s.timezone || "UTC";
  return (
    <section className={`card ${highlighted ? "row-active" : ""}`} id={`schedule-${s.id}`} aria-label={`Schedule ${s.name}`}>
      <header className="card-header">
        <h2 className="card-title">{s.name} <Tag tone="info">{kindLabel(s.kind)}</Tag> {!s.enabled && <StatusBadge status="disabled" />}</h2>
        <div className="card-actions">
          <label className="switch">
            <input type="checkbox" role="switch" checked={s.enabled} disabled={act.busy} onChange={(e) => void toggle(e.target.checked)}
              aria-label={`Enable schedule ${s.name}`} />
            <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
            <span className="small">{s.enabled ? "enabled" : "disabled"}</span>
          </label>
          <button type="button" className="btn btn-sm" onClick={() => void runNow()} disabled={act.busy}>Run now</button>
          {scheduleEditable(s.kind) ? <button type="button" className="btn btn-sm btn-ghost" onClick={onEdit} disabled={act.busy}>Edit</button>
            : <span className="small muted" title="Change it where it was made (its approval binds the calculation); pause or delete it here.">
              Set by its source</span>}
          <button type="button" className="btn btn-sm btn-danger" onClick={() => void remove()} disabled={act.busy}>Delete</button>
        </div>
      </header>
      <div className="card-body">
        <KeyValue items={[
          ["When", <span key="w">{describeCron(s.cron)} <span className="muted small">({tz}) · <code>{s.cron}</code></span></span>],
          ["Next run", s.enabled && s.next_run_at ? (
            <span key="n">{fmtInZone(s.next_run_at, tz)} <span className="muted small">· your time {fmtInZone(s.next_run_at)}</span></span>
          ) : <span key="n" className="muted">{s.enabled ? "—" : "paused"}</span>],
          ["Last run", fmtDate(s.last_run_at)],
          ["Config", <ConfigSummary key="c" schedule={s} />],
        ]} />
        <SchedulePins wsId={wsId} scheduleId={s.id} canEdit={canEdit} onUpgraded={onChanged} />
        <ErrorBox error={act.error} />
        {notice && <Notice tone={notice.startsWith("Run failed") ? "danger" : "success"}>{notice}</Notice>}
        <RecentRuns wsId={wsId} runs={s.recent_runs ?? []} />
      </div>
    </section>
  );
}

function ConfigSummary({ schedule: s }: { schedule: Schedule }) {
  const c = s.config ?? {};
  const parts: string[] = [];
  if (s.kind === "reanalysis") {
    if (c.objective) parts.push(`objective: ${c.objective}`);
    parts.push(c.refresh_first === false ? "no refresh" : "refresh first");
    parts.push(`publish: ${c.publish ?? "skip"}`);
    parts.push(c.report === null ? "no report" : `report: ${c.report?.kind ?? "weekly_summary"} (${(c.report?.formats ?? ["html", "pdf", "xlsx"]).join(", ")})`);
  } else if (s.kind === "report") {
    parts.push(`${c.kind ?? "executive"} (${(c.formats ?? ["html", "pdf"]).join(", ")})`);
    parts.push(c.run_id ? "a chosen investigation" : "latest completed investigation");
    if (c.deliver_to?.length) parts.push(`sent to ${c.deliver_to.length} approved destination(s)`);
  } else if (s.kind === "monitor") {
    parts.push(c.monitor_ids?.length ? `${c.monitor_ids.length} monitor(s)` : "all enabled monitors");
  } else if (s.kind === "dataset_refresh") {
    parts.push(c.source_ids?.length ? `${c.source_ids.length} source(s)` : "all staged sources");
  } else if (s.kind === "crawl") {
    parts.push(c.source_ids?.length ? `${c.source_ids.length} source(s)` : "all discovered sources");
    parts.push(`${c.mode ?? "admin default"} mode`);
    if (c.include?.length) parts.push(`include ${c.include.join(", ")}`);
    if (c.exclude?.length) parts.push(`exclude ${c.exclude.join(", ")}`);
  }
  const digests = digestDashboards(c).length;
  if (digests) parts.push(`sends a summary of ${digests} dashboard${digests === 1 ? "" : "s"}`);
  return <span className="small">{parts.join(" · ") || "—"}</span>;
}

function RecentRuns({ wsId, runs }: { wsId: string; runs: ScheduleRun[] }) {
  if (!runs.length) return <p className="muted small">No runs yet.</p>;
  return (
    <details className="recent-runs" open>
      <summary className="small">Recent runs ({runs.length})</summary>
      <div className="table-wrap">
        <table className="table table-compact">
          <thead><tr><th>Status</th><th>Trigger</th><th>Scheduled for</th><th>Finished</th><th>Result</th><th>Error</th></tr></thead>
          <tbody>
            {runs.map((r) => {
              const res = r.result ?? {};
              const ch = res.changes;
              return (
                <tr key={r.id}>
                  <td><StatusBadge status={r.status} /></td>
                  <td className="small">{r.trigger}</td>
                  <td className="small">{fmtDate(r.scheduled_for)}</td>
                  <td className="small">{fmtDate(r.finished_at)}</td>
                  <td className="small">
                    <div className="chip-row">
                      {res.run_id && <Link to={to.run(wsId, res.run_id)}>Run</Link>}
                      {res.report_artifact_id && <Link to={to.reports(wsId, res.report_artifact_id)}>Report</Link>}
                      {ch && <span className="muted">{ch.new ?? 0} new · {ch.persisting ?? 0} persisting · {ch.changed ?? 0} changed · {ch.resolved ?? 0} resolved</span>}
                      {res.crawls && <CrawlRunSummary wsId={wsId} crawls={res.crawls} />}
                      {!res.run_id && !res.report_artifact_id && !ch && !res.crawls && <span className="muted">—</span>}
                    </div>
                  </td>
                  <td className="small warn-text clamp-2">{r.error ?? ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </details>
  );
}

function CrawlRunSummary({ wsId, crawls }: { wsId: string; crawls: NonNullable<ScheduleRun["result"]["crawls"]> }) {
  const entries = Object.values(crawls);
  const failed = entries.filter((c) => c.error).length;
  const sum = (k: "new" | "changed" | "deprecated") => entries.reduce((n, c) => n + (c[k] ?? 0), 0);
  return (
    <span className="muted">
      <Link to={to.sources(wsId)}>{entries.length} source{entries.length === 1 ? "" : "s"} crawled</Link>
      {" "}· {sum("new")} new · {sum("changed")} changed · {sum("deprecated")} deprecated{failed ? ` · ${failed} failed` : ""}
    </span>
  );
}

// ---------------------------------------------------------------------------------- form
export function ScheduleForm({ wsId, initial, onSaved, onCancel }: {
  wsId: string; initial?: Schedule; onSaved: (s: Schedule) => void; onCancel?: () => void;
}) {
  const id = useId();
  const [f, setF] = useState<ScheduleFormState>(() => (initial ? formFromSchedule(initial) : emptyScheduleForm()));
  const [errors, setErrors] = useState<ScheduleFormErrors>({});
  const [submitted, setSubmitted] = useState(false);
  const act = useAction();
  const set = (patch: Partial<ScheduleFormState>) => {
    const next = { ...f, ...patch };
    setF(next);
    if (submitted) setErrors(validateScheduleForm(next));
  };
  const runs = useAsync(() => (f.kind === "report" ? api.listRuns(wsId) : Promise.resolve([])), [wsId, f.kind]);
  const monitors = useAsync(() => (f.kind === "monitor" ? api.listMonitors(wsId) : Promise.resolve([])), [wsId, f.kind]);
  const sources = useAsync(() => (f.kind === "dataset_refresh" || f.kind === "crawl" ? api.listSources(wsId) : Promise.resolve([])), [wsId, f.kind]);
  const dashboards = useAsync(() => api.builderDashboards(wsId), [wsId]);
  const zones = timeZoneOptions();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    const errs = validateScheduleForm(f);
    setErrors(errs);
    if (Object.keys(errs).length) return;
    const body = buildScheduleInput(f);
    const saved = await act.run(() => initial
      ? api.updateSchedule(initial.id, {
        name: body.name, cron: body.cron, timezone: body.timezone, config: mergeScheduleConfig(initial.config, body.config),
      })
      : api.createSchedule(wsId, body));
    if (saved) onSaved(saved);
  };

  const toggleIn = (key: "formats" | "monitorIds" | "sourceIds" | "digestDashboards", value: string, on: boolean) => {
    const cur = f[key] as string[];
    set({ [key]: on ? [...cur, value] : cur.filter((x) => x !== value) } as Partial<ScheduleFormState>);
  };

  const reportFields = (
    <>
      <Field label="Report kind" htmlFor={`${id}-rkind`}>
        <select id={`${id}-rkind`} value={f.reportKind} onChange={(e) => set({ reportKind: e.target.value as ScheduleFormState["reportKind"] })}>
          {REPORT_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
        </select>
      </Field>
      <fieldset className="autonomy">
        <legend>Formats</legend>
        <div className="toggle-group">
          {REPORT_FORMATS.map((fmt) => (
            <label key={fmt} className="toggle small">
              <input type="checkbox" checked={f.formats.includes(fmt)} onChange={(e) => toggleIn("formats", fmt, e.target.checked)} /> {fmt}
            </label>
          ))}
        </div>
        {errors.formats && <div className="field-error" role="alert">{errors.formats}</div>}
      </fieldset>
    </>
  );

  return (
    <form className="form" onSubmit={submit} noValidate aria-label={initial ? "Edit schedule" : "New schedule"}>
      <div className="form-row">
        <Field label="Name" htmlFor={`${id}-name`}>
          <input id={`${id}-name`} value={f.name} onChange={(e) => set({ name: e.target.value })} placeholder="Weekly incident review"
            aria-invalid={!!errors.name} />
          {errors.name && <div className="field-error" role="alert">{errors.name}</div>}
        </Field>
        <Field label="Kind" htmlFor={`${id}-kind`} hint={SCHEDULE_KINDS.find((k) => k.id === f.kind)?.description}>
          <select id={`${id}-kind`} value={f.kind} disabled={!!initial}
            onChange={(e) => {
              const kind = e.target.value as ScheduleFormState["kind"];
              set({ kind, ...(kind === "report" && f.reportKind === "weekly_summary" ? { reportKind: "executive" } : {}) });
            }}>
            {SCHEDULE_KINDS.map((k) => <option key={k.id} value={k.id}>{k.label}</option>)}
          </select>
        </Field>
      </div>

      <div className="form-row">
        <Field label="Frequency" htmlFor={`${id}-preset`}>
          <select id={`${id}-preset`} value={f.preset}
            onChange={(e) => {
              const preset = e.target.value as ScheduleFormState["preset"];
              const cron = cronForPreset(preset);
              set(cron ? { preset, cron } : { preset });
            }}>
            {CRON_PRESETS.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
            <option value="custom">Custom (cron)</option>
          </select>
        </Field>
        <Field label="Cron expression" htmlFor={`${id}-cron`} hint={<>minute hour day-of-month month day-of-week · {describeCron(f.cron)}</>}>
          <input id={`${id}-cron`} value={f.cron} className="mono" spellCheck={false} aria-invalid={!!errors.cron}
            onChange={(e) => set({ cron: e.target.value, preset: presetForCron(e.target.value) })} />
          {errors.cron && <div className="field-error" role="alert">{errors.cron}</div>}
        </Field>
        <Field label="Time zone" htmlFor={`${id}-tz`} hint="IANA name; the cron is evaluated in this zone (DST-aware).">
          <input id={`${id}-tz`} value={f.timezone} list={`${id}-tzlist`} onChange={(e) => set({ timezone: e.target.value })}
            aria-invalid={!!errors.timezone} />
          <datalist id={`${id}-tzlist`}>{zones.map((z) => <option key={z} value={z} />)}</datalist>
          {errors.timezone && <div className="field-error" role="alert">{errors.timezone}</div>}
        </Field>
      </div>

      {f.kind === "reanalysis" && (
        <>
          <Field label="Objective (optional)" htmlFor={`${id}-obj`} hint="Defaults to the workspace objective.">
            <textarea id={`${id}-obj`} rows={2} value={f.objective} onChange={(e) => set({ objective: e.target.value })} />
          </Field>
          <div className="form-row">
            <label className="toggle">
              <input type="checkbox" checked={f.refreshFirst} onChange={(e) => set({ refreshFirst: e.target.checked })} /> Refresh datasets first
            </label>
            <Field label="Publishing" htmlFor={`${id}-pub`} hint="Publishing is never automatic: “propose” creates an approval request.">
              <select id={`${id}-pub`} value={f.publish} onChange={(e) => set({ publish: e.target.value as "skip" | "propose" })}>
                <option value="skip">Skip publishing</option>
                <option value="propose">Propose (needs approval)</option>
              </select>
            </Field>
            <label className="toggle">
              <input type="checkbox" checked={f.includeReport} onChange={(e) => set({ includeReport: e.target.checked })} /> Generate a report
            </label>
          </div>
          {f.includeReport && <div className="form-row">{reportFields}</div>}
        </>
      )}

      {f.kind === "report" && (
        <div className="form-row">
          <Field label="Investigation" htmlFor={`${id}-run`}>
            <select id={`${id}-run`} value={f.runId} onChange={(e) => set({ runId: e.target.value })}>
              <option value="">Latest completed investigation (at fire time)</option>
              {(runs.data ?? []).filter((r) => r.status === "COMPLETED").map((r) => (
                <option key={r.id} value={r.id}>{fmtDate(r.finished_at ?? r.created_at)} — {r.objective.slice(0, 60)}</option>
              ))}
            </select>
          </Field>
          {reportFields}
        </div>
      )}

      {f.kind === "monitor" && (
        <fieldset className="autonomy">
          <legend>Monitors (none selected = all enabled monitors)</legend>
          <ErrorBox error={monitors.error} />
          {monitors.data?.length === 0 && <p className="muted small">No monitors yet — create them on the Monitoring page.</p>}
          <div className="toggle-group">
            {(monitors.data ?? []).map((m) => (
              <label key={m.id} className="toggle small">
                <input type="checkbox" checked={f.monitorIds.includes(m.id)} onChange={(e) => toggleIn("monitorIds", m.id, e.target.checked)} /> {m.name}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      {f.kind === "dataset_refresh" && (
        <fieldset className="autonomy">
          <legend>Sources (none selected = all staged sources)</legend>
          <ErrorBox error={sources.error} />
          <div className="toggle-group">
            {(sources.data ?? []).map((s) => (
              <label key={s.id} className="toggle small">
                <input type="checkbox" checked={f.sourceIds.includes(s.id)} onChange={(e) => toggleIn("sourceIds", s.id, e.target.checked)} /> {s.name}
                <span className="muted">({s.kind})</span>
              </label>
            ))}
          </div>
        </fieldset>
      )}

      {f.kind === "crawl" && (
        <>
          <fieldset className="autonomy">
            <legend>Sources (none selected = every discovered source)</legend>
            <ErrorBox error={sources.error} />
            <div className="toggle-group">
              {(sources.data ?? []).map((s) => (
                <label key={s.id} className="toggle small">
                  <input type="checkbox" checked={f.sourceIds.includes(s.id)} onChange={(e) => toggleIn("sourceIds", s.id, e.target.checked)} /> {s.name}
                  <span className="muted">({s.kind})</span>
                </label>
              ))}
            </div>
          </fieldset>
          <div className="form-row">
            <Field label="Crawl mode" htmlFor={`${id}-cmode`} hint="Full crawls deprecate tables that disappeared; incremental crawls never do.">
              <select id={`${id}-cmode`} value={f.crawlMode} onChange={(e) => set({ crawlMode: e.target.value as ScheduleFormState["crawlMode"] })}>
                <option value="">Admin default</option>
                <option value="incremental">Incremental</option>
                <option value="full">Full</option>
              </select>
            </Field>
            <Field label="Include patterns" htmlFor={`${id}-cinc`} hint="Comma separated globs on schema.table or table.">
              <input id={`${id}-cinc`} value={f.crawlInclude} onChange={(e) => set({ crawlInclude: e.target.value })} />
            </Field>
            <Field label="Exclude patterns" htmlFor={`${id}-cexc`}>
              <input id={`${id}-cexc`} value={f.crawlExclude} onChange={(e) => set({ crawlExclude: e.target.value })} />
            </Field>
          </div>
        </>
      )}

      {(dashboards.data?.length ?? 0) > 0 && (
        <fieldset className="autonomy">
          <legend>Send a summary of these dashboards</legend>
          <p className="muted small">After each successful run, the summary is saved under Reports and sent to this schedule&apos;s delivery
            destinations; a destination sends only while its approval stands.</p>
          <div className="toggle-group">
            {(dashboards.data ?? []).map((d) => (
              <label key={d.id} className="toggle small">
                <input type="checkbox" checked={f.digestDashboards.includes(d.id)} onChange={(e) => toggleIn("digestDashboards", d.id, e.target.checked)} />
                {" "}{d.title ?? d.name}
              </label>
            ))}
          </div>
        </fieldset>
      )}

      <ErrorBox error={act.error} />
      <div className="form-actions">
        {onCancel && <button type="button" className="btn btn-ghost" onClick={onCancel}>Cancel</button>}
        <button type="submit" className="btn btn-primary" disabled={act.busy}>{act.busy ? "Saving…" : initial ? "Save changes" : "Create schedule"}</button>
      </div>
    </form>
  );
}
