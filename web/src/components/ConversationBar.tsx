/**
 * The conversation strip above the Ask answers (S11-T2): where a branch came from, the session state as chips
 * (filters, time window, tables, metric bindings), the memory the next answer will use (this conversation,
 * personal, team — each labelled and coloured differently), typed corrections and their promotion.
 *
 * Everything here reads and writes the v2 conversation API (`/api/v2/conversations/{id}/…`); a thread is a
 * conversation, so the ids are the same.
 */
import { useId, useState, type FormEvent } from "react";
import {
  api, errorMessage, type AskThread, type Correction, type CorrectionScope, type ConversationDetail, type ConversationMemory,
  type ConversationState,
} from "../api";
import { MEMORY_LABELS, type MemoryScope } from "../lib/ask";
import { fmtDate } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { ErrorBox, Notice } from "./ui";

const SCOPE_CHOICES: { value: CorrectionScope; label: string; hint: string }[] = [
  { value: "session", label: "Only this conversation", hint: "Applies at once, here only." },
  { value: "personal", label: "Remember for me", hint: "Your own memory for 180 days; nobody else sees it." },
  { value: "workspace_proposed", label: "Propose as the team definition",
    hint: "Applies here at once; everyone else gets it only after a knowledge reviewer approves it." },
];

function timeWindowText(tw: ConversationState["state"]["time_window"]): string | null {
  if (!tw) return null;
  if (typeof tw === "string") return tw;
  const t = tw as Record<string, unknown>;
  if (typeof t.label === "string" && t.label) return t.label;
  return [t.start, t.end].filter(Boolean).join(" – ") + (t.grain ? ` by ${String(t.grain)}` : "") || JSON.stringify(tw);
}

function MemoryChip({ scope, term, meaning, pending, children }: {
  scope: MemoryScope; term: string; meaning: string; pending?: boolean; children?: React.ReactNode;
}) {
  const m = MEMORY_LABELS[scope];
  return (
    <li className={`memory-chip ${m.className}${pending ? " memory-pending" : ""}`} title={m.title}>
      <span className="memory-scope">{pending ? "Team · waiting for review" : m.label}</span>
      <span><strong>{term}</strong> = {meaning}</span>
      {children}
    </li>
  );
}

export function ConversationBar({ conversation, threads, refreshKey, busy, onOpen, onChanged }: {
  conversation: ConversationDetail | null | undefined;
  threads: AskThread[];
  refreshKey: number;
  busy: boolean;
  onOpen: (id: string) => void;
  onChanged: () => void;
}) {
  const id = conversation?.id ?? "";
  const state = useAsync<ConversationState | undefined>(() => (id ? api.conversationState(id) : Promise.resolve(undefined)), [id, refreshKey]);
  const memory = useAsync<ConversationMemory | undefined>(() => (id ? api.conversationMemory(id) : Promise.resolve(undefined)), [id, refreshKey]);
  const local = useAsync<Correction[] | undefined>(() => (id ? api.conversationCorrections(id) : Promise.resolve(undefined)), [id, refreshKey]);
  const [error, setError] = useState<unknown>(null);
  const [working, setWorking] = useState(false);
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const [meaning, setMeaning] = useState("");
  const [scope, setScope] = useState<CorrectionScope>("session");
  const formId = useId();

  if (!conversation) return null;
  const run = async (fn: () => Promise<unknown>) => {
    setWorking(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (err) {
      setError(err);
    } finally {
      setWorking(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void run(async () => {
      await api.addCorrection(conversation.id, { term, meaning, scope });
      setTerm("");
      setMeaning("");
      setOpen(false);
    });
  };

  const s = state.data?.state;
  const mine = new Map((local.data ?? []).map((c) => [c.id, c]));
  const proposed = (local.data ?? []).filter((c) => c.scope === "workspace_proposed" && c.status === "proposed");
  const rejected = (local.data ?? []).filter((c) => c.scope === "workspace_proposed" && (c.status === "rejected" || c.status === "not_proposed"));
  const parent = conversation.parent_conversation_id ? threads.find((t) => t.id === conversation.parent_conversation_id) : undefined;
  const inherited = conversation.messages.filter((m) => m.inherited);
  const children = threads.filter((t) => t.parent_conversation_id === conversation.id);
  const tw = s ? timeWindowText(s.time_window) : null;
  const disabled = busy || working;
  const removeFilter = (i: number) => void run(() => api.patchConversationState(conversation.id,
    { filters: (s?.filters ?? []).filter((_, j) => j !== i).map((f) => ({ column: f.column, op: f.op, value: f.value })) }));

  return (
    <section className="conversation-bar stack" aria-label="Conversation state and memory">
      {conversation.parent_conversation_id && (
        <Notice tone="info">
          Branch of <strong>{parent?.title ?? "the original conversation"}</strong>
          {inherited.length ? <> after question {inherited[inherited.length - 1].seq}</> : <> before its first question</>}.
          The original conversation is unchanged.{" "}
          <button type="button" className="btn btn-xs btn-ghost" onClick={() => onOpen(conversation.parent_conversation_id!)}>Open the original</button>
        </Notice>
      )}
      {children.length > 0 && (
        <div className="chip-row" aria-label="Branches of this conversation">
          <span className="small muted">Branches:</span>
          {children.map((c) => (
            <button key={c.id} type="button" className="chip" onClick={() => onOpen(c.id)} title={`Started ${fmtDate(c.created_at)}`}>{c.title}</button>
          ))}
        </div>
      )}

      {s && (s.filters.length > 0 || tw || s.sources.length > 0 || Object.keys(s.metric_bindings).length > 0) && (
        <ul className="pill-row state-chips" aria-label={`Conversation state, version ${state.data?.version}`}>
          {s.filters.map((f, i) => (
            <li key={`f${i}`} className="badge badge-info">
              Filter: {f.column} {f.op} {String(f.value ?? "")}
              <button type="button" className="chip-remove" aria-label={`Remove filter ${f.column}`} disabled={disabled}
                onClick={() => removeFilter(i)}>×</button>
            </li>
          ))}
          {tw && (
            <li className="badge badge-info">
              Period: {tw}
              <button type="button" className="chip-remove" aria-label="Clear the period" disabled={disabled}
                onClick={() => void run(() => api.patchConversationState(conversation.id, { time_window: null }))}>×</button>
            </li>
          )}
          {Object.entries(s.metric_bindings).map(([phrase, metric]) => (
            <li key={`m${phrase}`} className="badge badge-neutral">“{phrase}” → {metric}</li>
          ))}
          {s.sources.map((src) => <li key={`s${src}`} className="badge badge-neutral" title="A table this conversation has read">{src}</li>)}
        </ul>
      )}

      {memory.data && (memory.data.items.length > 0 || proposed.length > 0) && (
        <ul className="memory-chips" aria-label="Meanings the next answer will use">
          {memory.data.items.map((item) => {
            const own = item.correction_id ? mine.get(item.correction_id) : undefined;
            // a meaning remembered from this conversation is personal memory, and looks like it
            const shown: MemoryScope = item.scope === "session" && item.saved_as === "personal" ? "personal" : item.scope;
            return (
              <MemoryChip key={`${item.scope}:${item.term_key}`} scope={shown} term={item.term} meaning={item.meaning}>
                {item.scope === "session" && own && own.scope === "session" && (
                  <button type="button" className="btn btn-xs btn-ghost" disabled={disabled}
                    onClick={() => void run(() => api.promoteCorrection(conversation.id, own.id, "personal"))}>Remember</button>
                )}
                {(item.scope === "session" || item.scope === "personal") && own && own.status === "active" && (
                  <button type="button" className="btn btn-xs btn-ghost" disabled={disabled}
                    onClick={() => void run(() => api.promoteCorrection(conversation.id, own.id, "workspace_proposed"))}>Propose for team</button>
                )}
                {shown === "personal" && item.correction_id && (
                  <button type="button" className="btn btn-xs btn-ghost" disabled={disabled}
                    onClick={() => void run(() => api.forgetCorrection(item.correction_id!))}>Forget</button>
                )}
              </MemoryChip>
            );
          })}
          {proposed.map((c) => <MemoryChip key={c.id} scope="workspace" term={c.term} meaning={c.meaning} pending />)}
        </ul>
      )}
      {memory.data?.overrides.map((o) => (
        <Notice key={o.term} tone="warning">
          Using your meaning of <strong>{o.term}</strong> ({o.meaning}), not the team definition ({o.team_meaning}).
        </Notice>
      ))}
      {rejected.map((c) => (
        <p key={c.id} className="small muted">
          Team definition of <strong>{c.term}</strong> {c.status === "rejected" ? "was not approved" : "was not proposed (already decided or owned content)"};
          it still applies in this conversation.
        </p>
      ))}

      <details open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
        <summary>Correct a term</summary>
        <form className="form correction-form" onSubmit={submit} aria-label="Correct a term">
          <div className="form-row">
            <label htmlFor={`${formId}-term`}>Term
              <input id={`${formId}-term`} value={term} maxLength={200} required onChange={(e) => setTerm(e.target.value)}
                placeholder="active customer" />
            </label>
            <label htmlFor={`${formId}-meaning`}>Means
              <input id={`${formId}-meaning`} value={meaning} maxLength={2000} required onChange={(e) => setMeaning(e.target.value)}
                placeholder="logged in within the last 30 days" />
            </label>
          </div>
          <fieldset className="scope-choices">
            <legend>Where it applies</legend>
            {SCOPE_CHOICES.map((c) => (
              <label key={c.value} className="radio">
                <input type="radio" name={`${formId}-scope`} value={c.value} checked={scope === c.value} onChange={() => setScope(c.value)} />
                <span>{c.label}</span> <span className="small muted">{c.hint}</span>
              </label>
            ))}
          </fieldset>
          <div className="form-actions">
            <button type="submit" className="btn btn-primary btn-sm" disabled={disabled || !term.trim() || !meaning.trim()}>
              {working ? "Saving…" : "Apply correction"}
            </button>
          </div>
        </form>
      </details>
      <ErrorBox error={state.error ?? memory.error ?? local.error} />
      {error ? <ErrorBox error={errorMessage(error)} /> : null}
    </section>
  );
}
