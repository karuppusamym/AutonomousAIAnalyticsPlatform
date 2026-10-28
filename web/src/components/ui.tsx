import { useId, useState, type ReactNode } from "react";
import { fmtValue, formatKnown, type ValueFormat } from "../lib/format";
import { statusLabel, toneFor } from "../lib/status";

export function StatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  const tone = toneFor(status);
  return (
    <span className={`badge badge-${tone}`} data-tone={tone} data-status={status ?? undefined}>
      <span className="badge-dot" aria-hidden="true" />
      {label ?? statusLabel(status)}
    </span>
  );
}

export function Tag({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function ErrorBox({ error, onRetry }: { error: string | null | undefined; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="alert alert-danger" role="alert">
      <strong>Error:</strong> <span>{error}</span>
      {onRetry && (
        <button type="button" className="btn btn-sm btn-ghost" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Notice({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "warning" | "success" | "danger" }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "danger" ? "alert" : "status"}>
      {children}
    </div>
  );
}

/** An empty list: what is missing, why, and (with `action`) the one next step. */
export function EmptyState({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {children && <div className="empty-body">{children}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}
    </div>
  );
}

/** A card; `label` names it as a region (for cards a person or a test needs to find by name). */
export function Card({ title, actions, children, className = "", label }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; label?: string;
}) {
  return (
    <section className={`card ${className}`} aria-label={label}>
      {(title || actions) && (
        <header className="card-header">
          {title && <h2 className="card-title">{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  );
}

/** `level={2}`: the header of a page shown inside another screen (the Autonomy Center's sections). */
export function PageHeader({ title, subtitle, actions, level = 1 }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; level?: 1 | 2 }) {
  return (
    <div className="page-header">
      <div>
        {level === 1 ? <h1 data-tour="page-title">{title}</h1> : <h2>{title}</h2>}
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
      {hint && <div className="stat-hint">{hint}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ state families (spec v3 §9)

/**
 * A number the platform did not report (null, undefined, NaN, "") is unknown, and unknown is
 * never shown as 0: a missing cost is not a free run, and a missing count is not an empty one.
 */
export function Value({ value, format = "number", digits, suffix, unknownLabel = "unknown", unknownTitle }: {
  value: unknown;
  format?: ValueFormat;
  digits?: number;
  suffix?: string;
  unknownLabel?: string;
  unknownTitle?: string;
}) {
  const text = formatKnown(value, format, digits);
  if (text === null) {
    return (
      <span className="value value-unknown" data-state="unknown" title={unknownTitle ?? "Not reported — unknown, not zero"}>
        <span aria-hidden="true">?</span> {unknownLabel}
      </span>
    );
  }
  return <span className="value" data-state="known">{text}{suffix ? ` ${suffix}` : ""}</span>;
}

export type StateKind = "loading" | "empty" | "refused" | "failed" | "not-entitled" | "stale" | "unknown";

const STATE_TONE: Record<StateKind, string> = {
  loading: "running", empty: "neutral", refused: "warning", failed: "danger", "not-entitled": "warning", stale: "warning", unknown: "neutral",
};

/** One visual for each state family, so "refused" or "stale" reads the same on every screen. */
export function StateView({ kind, title, children, action }: { kind: StateKind; title: string; children?: ReactNode; action?: ReactNode }) {
  const tone = STATE_TONE[kind];
  return (
    <div className={`state state-${kind} state-tone-${tone}`} data-state={kind} role={kind === "failed" ? "alert" : "status"}>
      <p className="state-title"><span className={`badge badge-${tone}`}><span className="badge-dot" aria-hidden="true" />{kind.replace("-", " ")}</span> {title}</p>
      {children && <div className="state-body">{children}</div>}
      {action && <div className="state-action">{action}</div>}
    </div>
  );
}

export function ConfidenceBar({ value }: { value: number | null | undefined }) {
  if (formatKnown(value, "number") === null) return <Value value={null} unknownTitle="Confidence not reported" />;
  const v = Math.max(0, Math.min(1, Number(value)));
  const tone = v >= 0.7 ? "success" : v >= 0.45 ? "warning" : "danger";
  return (
    <div className="confidence" title={`confidence ${(v * 100).toFixed(0)}%`}>
      <div className="confidence-track" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)}
        aria-label="Confidence">
        <div className={`confidence-fill fill-${tone}`} style={{ width: `${v * 100}%` }} />
      </div>
      <span className="confidence-label">{(v * 100).toFixed(0)}%</span>
    </div>
  );
}

export function DataTable({ columns, rows, maxRows = 200, caption }: { columns: string[]; rows: unknown[][]; maxRows?: number; caption?: string }) {
  if (!columns.length) return <EmptyState title="No columns" />;
  const shown = rows.slice(0, maxRows);
  return (
    <div className="table-wrap">
      <table className="table table-compact">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c} scope="col">{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>
              {columns.map((_, j) => (
                <td key={j} className={typeof r[j] === "number" ? "num" : undefined}>{fmtValue(r[j])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && <p className="muted small">Showing {shown.length} of {rows.length} rows.</p>}
      {rows.length === 0 && <p className="muted small">No rows.</p>}
    </div>
  );
}

/** Render records (list of objects) as a table. */
export function RecordTable({ records, maxRows = 50 }: { records: Record<string, unknown>[]; maxRows?: number }) {
  if (!records.length) return <p className="muted small">No rows.</p>;
  const cols = [...new Set(records.flatMap((r) => Object.keys(r)))];
  return <DataTable columns={cols} rows={records.map((r) => cols.map((c) => r[c]))} maxRows={maxRows} />;
}

/** Normalise a result preview that may be list-of-lists or list-of-objects. */
export function PreviewTable({ columns, preview, maxRows = 50 }: { columns: string[]; preview: unknown[]; maxRows?: number }) {
  if (!preview?.length) return <p className="muted small">No preview rows recorded.</p>;
  if (Array.isArray(preview[0])) return <DataTable columns={columns} rows={preview as unknown[][]} maxRows={maxRows} />;
  return <RecordTable records={preview as Record<string, unknown>[]} maxRows={maxRows} />;
}

export function CodeBlock({ code, label }: { code: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="code">
      <div className="code-bar">
        <span className="muted small">{label ?? "SQL"}</span>
        <button type="button" className="btn btn-xs btn-ghost" onClick={() => {
          void navigator.clipboard?.writeText(code).then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          });
        }}>{copied ? "Copied" : "Copy"}</button>
      </div>
      <pre><code>{code}</code></pre>
    </div>
  );
}

export function JsonView({ value, collapsed = false, label = "JSON" }: { value: unknown; collapsed?: boolean; label?: string }) {
  const text = JSON.stringify(value, null, 2);
  if (collapsed) {
    return (
      <details className="json">
        <summary>{label}</summary>
        <pre>{text}</pre>
      </details>
    );
  }
  return <pre className="json">{text}</pre>;
}

/**
 * Raw JSON lives only here (spec v3 §9): a closed "Technical details" disclosure, so the default
 * path of a screen reads as prose, numbers and badges. Tests assert every `.json` block on the
 * board sits inside one of these.
 */
export function TechnicalDetails({ value, label = "Technical details", children }: { value?: unknown; label?: string; children?: ReactNode }) {
  return (
    <details className="technical" data-technical="">
      <summary>{label}</summary>
      {children}
      {value !== undefined && <JsonView value={value} />}
    </details>
  );
}

/** An accessible on/off switch (role=switch) for enablement flags. */
export function EnabledToggle({ enabled, disabled, onChange, label }: { enabled: boolean; disabled: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={enabled} disabled={disabled} onChange={(e) => onChange(e.target.checked)} aria-label={label} />
      <span className="switch-track" aria-hidden="true"><span className="switch-thumb" /></span>
      <span className="small">{enabled ? "enabled" : "disabled"}</span>
    </label>
  );
}

export function KeyValue({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <div key={k} className="kv-row">
          <dt>{k}</dt>
          <dd>{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: ReactNode }[]; value: T; onChange: (t: T) => void }) {
  const id = useId();
  return (
    <div className="tabs" role="tablist" aria-label="Sections">
      {tabs.map((t) => (
        <button key={t.id} id={`${id}-${t.id}`} role="tab" type="button" aria-selected={value === t.id}
          data-tour={`tab-${t.id}`} className={`tab ${value === t.id ? "tab-active" : ""}`} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div className="field">
      <label htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="field-hint">{hint}</div>}
    </div>
  );
}
