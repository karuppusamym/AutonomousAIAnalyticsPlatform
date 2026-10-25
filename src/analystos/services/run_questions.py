"""Answer a bounded set of investigation questions from saved evidence, without new analysis."""
from __future__ import annotations

import re
from typing import Any


def answer_saved_question(text: str, findings: list[dict[str, Any]], *, target_id: str | None = None) -> dict[str, Any]:
    question = text.lower()
    active = [f for f in findings if f.get("status") not in {"rejected", "superseded"}
              and f.get("badge") != "superseded"]
    verified = [f for f in active if f.get("verified") and f.get("badge") == "verified"]
    review = [f for f in active if f not in verified]
    selected = next((f for f in findings if f["id"] == target_id), None) if target_id else None
    if selected is None and target_id is None:
        codes = [f for f in findings if re.search(rf"\b{re.escape(f['code'])}\b", text, re.IGNORECASE)]
        selected = codes[0] if len(codes) == 1 else None
    refs: list[dict[str, str]] = []
    paragraphs: list[str] = []

    def cite(finding: dict[str, Any]) -> None:
        refs.append({k: finding[k] for k in ("id", "code", "title")})

    def state(finding: dict[str, Any]) -> str:
        if finding.get("status") in {"rejected", "superseded"}:
            return str(finding["status"])
        return "currently verified" if finding in verified else f"needs checking ({finding.get('badge') or 'unverified'})"

    # Action/data requests are deliberately not interpreted as a saved-evidence explanation.
    if re.search(r"\b(publish|delete|drop|send|export|schedule|update|insert|execute)\b", question):
        topic = "unsupported"
        paragraphs.append("This conversation explains saved investigation evidence. Use the approved workflow for actions, or Ask for a new data question. No action was executed.")
    elif selected is not None:
        topic = "finding"
        cite(selected)
        paragraphs.extend([f"{selected['code']}: {selected['title']} — {state(selected)}.", selected["finding"]])
        checks = selected.get("checks") or []
        paragraphs.extend(f"Recorded check: {c['check'].replace('_', ' ')} — {'passed' if c.get('passed') else 'failed'}. {c.get('detail') or ''}" for c in checks)
        if not checks:
            paragraphs.append("No individual verification checks were recorded for this finding.")
        if selected.get("void_reason"):
            paragraphs.append(f"Why it needs checking: {selected['void_reason']}")
        paragraphs.extend(f"Limitation: {c}" for c in selected.get("caveats", []))
        paragraphs.append("These recorded checks do not establish causation. Open the evidence to inspect the queries and methods.")
    elif re.search(r"\b(limitations?|caveats?|missing|trust|reliable)\b", question):
        topic = "limitations"
        caveats = list(dict.fromkeys(c for f in active for c in f.get("caveats", [])))
        paragraphs = [f"Recorded limitation: {c}" for c in caveats]
        if not caveats:
            paragraphs.append("No limitations were recorded on the current findings. That does not mean the data is complete or the conclusions have no limitations.")
        paragraphs.append(f"{len(review)} current finding(s) still need checking. Associations alone do not establish a cause.")
        for f in active:
            if f.get("caveats"):
                cite(f)
    elif re.search(r"\b(checking|unverified|stale|void|verify|verification)\b", question):
        topic = "verification"
        paragraphs.append(f"{len(verified)} finding(s) are currently verified; {len(review)} need checking.")
        for f in review:
            cite(f)
            paragraphs.append(f"{f['code']}: {f['title']} — {state(f)}. {f.get('void_reason') or 'Review the recorded checks before relying on this finding.'}")
    elif re.search(r"\b(summary|summarize|summarise|findings|found)\b", question):
        topic = "summary"
        paragraphs.append(f"The saved investigation has {len(verified)} currently verified finding(s) and {len(review)} finding(s) that need checking.")
        for f in verified[:8]:
            cite(f)
            paragraphs.append(f"{f['code']}: {f['finding']}")
        if len(verified) > 8:
            paragraphs.append("Showing the first eight verified findings; open the investigation board for the rest.")
        if not verified:
            paragraphs.append("There is no currently verified conclusion to summarize. Review the evidence and limitations first.")
    else:
        topic = "unsupported"
        paragraphs.append("I can summarize saved findings, explain a finding by its code, list limitations, or show what needs checking. This question needs analysis beyond that saved evidence; use Ask or select Deeper analysis to investigate it.")
    return {"mode": "saved_evidence", "topic": topic, "paragraphs": paragraphs, "references": refs}


def submit_saved_question(user: Any, run_id: str, *, text: str, workspace_id: str | None, target_id: str | None,
                          classified_by: str = "user", feedback_id: str | None = None,
                          decision_ids: list[str] | None = None) -> dict[str, Any]:
    from sqlalchemy import select

    from analystos.core.errors import InvalidInput, NotFound
    from analystos.core.ids import new_id, utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import Feedback, Insight
    from analystos.events.bus import emit
    from analystos.evidence.verification import insight_states
    from analystos.governance.audit import audit
    from analystos.services.runs import get_run_for

    if not text.strip() or len(text) > 10000:
        raise InvalidInput("question must contain between 1 and 10000 characters")
    with session_scope() as session:
        run = get_run_for(session, user, run_id, "analyst", workspace_id)
        session.refresh(run, with_for_update=True)
        insights = list(session.scalars(select(Insight).where(Insight.run_id == run.id, Insight.workspace_id == run.workspace_id)
                                       .order_by(Insight.created_at, Insight.code)))
        if target_id and not any(i.id == target_id for i in insights):
            raise NotFound("finding not found in this run")
        states = insight_states(session, [i.id for i in insights])
        findings = [{"id": i.id, "code": i.code, "title": i.title, "finding": i.finding, "status": i.status,
                     "verified": i.verified, "badge": states[i.id]["badge"], "caveats": i.caveats or [],
                     "void_reason": (states[i.id].get("void") or {}).get("reason"),
                     "checks": (i.verification or {}).get("evaluate", [])} for i in insights]
        answer = {**answer_saved_question(text, findings, target_id=target_id), "recorded_at": utcnow().isoformat()}
        feedback_id = feedback_id or new_id("fb")
        result = {"kind": "question", "classified_by": classified_by, "consequential_p": None,
                  "feedback_id": feedback_id, "answer": answer, "decision_ids": decision_ids or []}
        session.add(Feedback(id=feedback_id, workspace_id=run.workspace_id, run_id=run.id, user_id=user.id,
                             kind="question", text=text, target_type="insight" if target_id else None, target_id=target_id,
                             data=result, applied=True))
        run.instructions = [*(run.instructions or []), {"kind": "question", "text": text, "at": answer["recorded_at"],
                                                       "feedback_id": feedback_id, "answer": answer}]
        emit(run.workspace_id, "feedback.received", {"kind": "question", "text": text[:300], "feedback_id": feedback_id},
             run_id=run.id, actor=f"user:{user.id}", session=session)
        audit(f"user:{user.id}", "feedback.submitted", workspace_id=run.workspace_id, run_id=run.id,
              details={"kind": "question", "feedback_id": feedback_id, "mode": "saved_evidence"}, session=session)
        return result
