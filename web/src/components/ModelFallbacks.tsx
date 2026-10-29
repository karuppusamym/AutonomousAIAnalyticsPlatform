import type { ModelFallback } from "../api";
import { useExpertView } from "../lib/expert";
import { Notice } from "./ui";

/** The plain sentence for one fallback: the server's, else one built from the purpose. */
export function fallbackSentence(fb: ModelFallback): string {
  return fb.message || `Answered by ${fb.rung || "rules"}: no model answered ${fb.purpose.replace(/_/g, " ")}.`;
}

/**
 * N-14: the steps this investigation answered by rules because no model was allowed (models switched off in
 * the platform settings, a spend cap, a missing price...). Business view gets one plain sentence per purpose;
 * the router's detail, cause code, steps and count are expert-only.
 */
export function ModelFallbacks({ fallbacks }: { fallbacks: ModelFallback[] | undefined }) {
  const expert = useExpertView();
  if (!fallbacks?.length) return null;
  return (
    <Notice tone="info">
      <div role="group" aria-label="Answered without a model">
        <ul className="plain-list">
          {fallbacks.map((fb) => <li key={`${fb.purpose}:${fb.cause}`}>{fallbackSentence(fb)}</li>)}
        </ul>
        {expert && (
          <ul className="small muted" aria-label="Model fallback details">
            {fallbacks.map((fb) => (
              <li key={`${fb.purpose}:${fb.cause}`}>
                <code>{fb.purpose}</code> · cause <code>{fb.cause}</code> · rung <code>{fb.rung}</code>
                {fb.count > 1 && <> · {fb.count} times</>}
                {fb.tasks.length > 0 && <> · steps {fb.tasks.join(", ")}</>} — {fb.detail}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Notice>
  );
}
