import { Link } from "react-router-dom";
import type { SavedRunAnswer } from "../api";
import { fmtDate } from "../lib/format";
import { to } from "../routes";

export function isSavedRunAnswer(value: unknown): value is SavedRunAnswer {
  if (!value || typeof value !== "object") return false;
  const a = value as SavedRunAnswer;
  return a.mode === "saved_evidence" && Array.isArray(a.paragraphs) && a.paragraphs.every((p) => typeof p === "string")
    && Array.isArray(a.references) && a.references.every((r) => r && typeof r.id === "string" && typeof r.code === "string" && typeof r.title === "string");
}

export function SavedEvidenceAnswer({ answer, wsId }: { answer: SavedRunAnswer; wsId: string }) {
  return <div className="saved-evidence-answer">
    <p className="small muted">Saved evidence · {answer.recorded_at ? fmtDate(answer.recorded_at) : "recorded answer"} · no new analysis</p>
    {answer.paragraphs.map((p, index) => <p key={index}>{p}</p>)}
    {answer.references.length > 0 && <ul className="conversation-references" aria-label="Evidence behind this answer">
      {answer.references.map((r) => <li key={r.id}><Link to={to.findings(wsId, r.id)}>{r.code} · {r.title}</Link></li>)}
    </ul>}
    <p className="small muted">This is a record of the evidence when you asked. Open a finding to see its current verification status.</p>
  </div>;
}
