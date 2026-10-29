import { useId, useState, type FormEvent } from "react";
import { api, type AskPromotion, type AskTurn, type BuilderDraft, type BuilderSummary, type DraftTile } from "../api";
import { useExpertView } from "../lib/expert";
import { useAction } from "../lib/hooks";
import { ErrorBox, Field, Notice } from "./ui";

/** How a tile's numbers were found, in plain words (CONV-01: the governed question path). */
export function answeredByText(t: Pick<DraftTile, "answered_by" | "governance">): string {
  switch (t.answered_by) {
    case "semantic": return "from an approved metric";
    case "registry": return "from a verified query";
    case "rules": return "from the catalog, without a model";
    case "model": return "a new calculation, checked before it ran";
    default: return t.governance === "governed" ? "from an approved definition" : "checked before it ran";
  }
}

const GROUNDED: Record<string, string> = {
  semantic: "approved metrics", semantic_all: "all approved metrics", catalog: "the data catalog", none: "nothing",
};

/** What a draft request produced: the dashboard, the tiles that could not be built and why, or a clarifying reply. */
export function DraftSummary({ draft, onExample }: { draft: BuilderDraft; onExample?: (prompt: string) => void }) {
  const expert = useExpertView();
  if (draft.status === "clarify") {
    return (
      <Notice tone="warning">
        <p>{draft.clarify}</p>
        {draft.failed.length > 0 && <TileList title="Could not build" tiles={draft.failed} />}
        {(draft.examples ?? []).length > 0 && (
          <div className="row wrap">
            {draft.examples!.map((e) => <button key={e} type="button" className="btn btn-sm" onClick={() => onExample?.(e)}>{e}</button>)}
          </div>
        )}
      </Notice>
    );
  }
  const total = draft.built.length + draft.failed.length;
  return (
    <div className="stack small" aria-label="Draft result">
      <Notice tone={draft.failed.length ? "warning" : "success"}>
        Draft “{draft.dashboard?.document.title}” is ready: {draft.built.length} of {total} tiles built. Change it by asking in the panel below;
        nobody else sees it until a publication is approved.
      </Notice>
      {draft.failed.length > 0 && <TileList title="Not built" tiles={draft.failed} />}
      {expert && (
        <p className="muted">Planned from {GROUNDED[draft.plan.grounded_on] ?? draft.plan.grounded_on}; order by {draft.plan.ranked_by === "rules" ? "rules" : "a small model (reorder only)"}.
          {" "}{draft.built.map((b) => `${b.title}: ${b.chart_type ?? b.kind}${b.query_id ? ` (query ${b.query_id})` : ""}`).join(" · ")}</p>
      )}
    </div>
  );
}

function TileList({ title, tiles }: { title: string; tiles: DraftTile[] }) {
  return (
    <div>
      <strong>{title}:</strong>
      <ul aria-label={title}>
        {tiles.map((t) => <li key={t.tile}>{t.title} — {t.reason}{t.clarify ? ` ${t.clarify}` : ""}</li>)}
      </ul>
    </div>
  );
}

/**
 * "Build a dashboard" from one request (CONV-02): KPIs, charts and filter controls chosen from what the workspace has
 * reviewed, each answered through the governed question path. The result is a draft (version 1) to refine by asking.
 */
export function DraftFromPrompt({ wsId, onCreated }: { wsId: string; onCreated: (artifactId: string) => void }) {
  const [prompt, setPrompt] = useState("");
  const [draft, setDraft] = useState<BuilderDraft | null>(null);
  const act = useAction();
  const id = useId();
  const build = async (e?: FormEvent, text = prompt) => {
    e?.preventDefault();
    if (text.trim().length < 3) return;
    const r = await act.run(() => api.builderDraft(wsId, { prompt: text.trim() }));
    if (!r) return;
    setDraft(r);
    if (r.status === "created" && r.dashboard) onCreated(r.dashboard.id);
  };
  return (
    <div className="stack">
      <form className="form" aria-label="Build a dashboard from a request" onSubmit={(e) => void build(e)}>
        <Field label="What should the dashboard show?" htmlFor={`${id}-prompt`}
          hint="For example: “Build an incident operations dashboard”. Charts come only from data you can see.">
          <textarea id={`${id}-prompt`} rows={2} maxLength={2000} value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </Field>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || prompt.trim().length < 3}>
            {act.busy ? "Building the draft…" : "Build a draft"}</button>
        </div>
      </form>
      <ErrorBox error={act.error} />
      {draft && <DraftSummary draft={draft} onExample={(e) => { setPrompt(e); void build(undefined, e); }} />}
    </div>
  );
}

type Where = "new" | "existing" | "publish";

/**
 * "Add to dashboard" on an Ask answer (CONV-03): the recommended chart goes into a new or an existing draft to keep
 * editing, or (as before) to the BI tool through an approval bound to the exact chart.
 */
export function AddAnswerToDashboard({ turn, wsId, onRecorded, onPublish, onClose }: {
  turn: AskTurn; wsId: string; onRecorded: (p: AskPromotion) => void; onPublish: () => void; onClose: () => void;
}) {
  const [where, setWhere] = useState<Where>("new");
  const [title, setTitle] = useState(turn.question.slice(0, 200));
  const [drafts, setDrafts] = useState<BuilderSummary[] | null>(null);
  const [target, setTarget] = useState("");
  const act = useAction();
  const id = useId();
  const choose = async (w: Where) => {
    setWhere(w);
    if (w === "existing" && drafts === null) {
      const list = await act.run(() => api.builderDashboards(wsId));
      if (list) {
        setDrafts(list);
        setTarget(list[0]?.id ?? "");
      }
    }
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (where === "publish") {
      onPublish();
      onClose();
      return;
    }
    const body = where === "new" ? { target: "draft" as const, title: title.trim() || undefined } : { target: "draft" as const, dashboard_id: target };
    const p = await act.run(() => api.promoteTurn(turn.id, body));
    if (p) {
      onRecorded(p);
      onClose();
    }
  };
  return (
    <form className="form" aria-label="Add to dashboard" onSubmit={(e) => void submit(e)}>
      <fieldset>
        <legend className="small">Where should the chart go?</legend>
        <label className="row"><input type="radio" name={`${id}-where`} checked={where === "new"} onChange={() => void choose("new")} /> A new draft dashboard</label>
        <label className="row"><input type="radio" name={`${id}-where`} checked={where === "existing"} onChange={() => void choose("existing")} /> An existing draft</label>
        <label className="row"><input type="radio" name={`${id}-where`} checked={where === "publish"} onChange={() => void choose("publish")} /> Straight to the BI tool (needs approval)</label>
      </fieldset>
      {where === "new" && (
        <Field label="Draft title" htmlFor={`${id}-title`}>
          <input id={`${id}-title`} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
        </Field>
      )}
      {where === "existing" && drafts !== null && (drafts.length ? (
        <Field label="Dashboard" htmlFor={`${id}-target`} hint="A published dashboard gets a draft copy; viewers keep seeing the approved version.">
          <select id={`${id}-target`} value={target} onChange={(e) => setTarget(e.target.value)}>
            {drafts.map((d) => <option key={d.id} value={d.id}>{d.title ?? d.name} (v{d.version}, {d.status})</option>)}
          </select>
        </Field>
      ) : <p className="small muted">No dashboards yet: start a new draft instead.</p>)}
      <p className="small muted">The chart uses this answer’s numbers and keeps a link to where they came from.</p>
      <div className="form-actions">
        <button type="button" className="btn btn-sm" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn btn-primary btn-sm" disabled={act.busy || (where === "existing" && !target)}>
          {where === "publish" ? "Request approval" : "Add the chart"}</button>
      </div>
      <ErrorBox error={act.error} />
    </form>
  );
}
