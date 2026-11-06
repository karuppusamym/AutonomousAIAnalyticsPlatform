"""What a knowledge review batch does to the owner's golden questions, before it is approved.

A reviewer approving drafts should know whether answers get better or worse (Knowledge Catalog review
2026-11-05, P0-1). The batch is applied for real (`suggestions.review`) inside a savepoint, the golden
suite runs with no model so the result is deterministic and free, and the savepoint is rolled back. The
projected score therefore comes from the same code that will apply the batch, not from a second
implementation of it.

Gate: a batch that makes a passing golden case fail is refused unless the reviewer writes a reason. The
reason, the cases and both scores are audited. Models never decide here: nothing in this path calls one.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import Conflict
from analystos.core.ids import stable_hash

CHANGES = ("newly_failing", "fixed", "still_failing", "still_passing")


def _score(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total, passed = len(cases), sum(1 for c in cases if c["passed"])
    return {"passed": passed, "total": total, "rate": round(passed / total, 4) if total else None}


def compare(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-case change between two golden runs, matched by case id. Newly failing cases come first."""
    after_by_id = {c["id"]: c for c in after}
    cases = []
    for b in before:
        a = after_by_id.get(b["id"])
        if a is None:
            continue
        change = ("still_passing" if a["passed"] else "newly_failing") if b["passed"] else \
            ("fixed" if a["passed"] else "still_failing")
        cases.append({"id": b["id"], "question": b["question"], "before": b["passed"], "after": a["passed"],
                      "change": change, "expected_columns": b["expected"].get("expected_columns", []),
                      "columns_before": b.get("actual_columns", []), "columns_after": a.get("actual_columns", []),
                      "served_before": b.get("served_query"), "served_after": a.get("served_query")})
    cases.sort(key=lambda c: (CHANGES.index(c["change"]), c["id"]))
    baseline, projected = _score([{"passed": c["before"]} for c in cases]), _score([{"passed": c["after"]} for c in cases])
    delta = round((projected["passed"] - baseline["passed"]) * 100 / len(cases), 1) if cases else 0.0
    counts = {k: sum(1 for c in cases if c["change"] == k) for k in CHANGES}
    return {"baseline": baseline, "projected": projected, "delta_points": delta, "counts": counts, "cases": cases}


def preview(session: Session, workspace_id: str, user: Any, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Golden scores now and with `decisions` applied. Changes nothing: the batch runs in a savepoint."""
    from analystos.knowledge import suggestions
    from analystos.services.context_grounding import run_golden

    before = run_golden(session, user, workspace_id)
    if not before:
        return {"evaluated": False, "reason": "no_golden_questions", "baseline": _score([]), "projected": _score([]),
                "delta_points": 0.0, "counts": dict.fromkeys(CHANGES, 0), "cases": [], "errors": []}
    from analystos.registries.verified_queries import forget_lexicon

    savepoint = session.begin_nested()
    try:
        outcome = suggestions.review(session, workspace_id, user, decisions)
        session.flush()
        forget_lexicon(workspace_id)  # match against the batch's synonyms, not a cached copy
        after = run_golden(session, user, workspace_id)
    finally:
        savepoint.rollback()
        forget_lexicon(workspace_id)  # never leave the unapproved batch's synonyms cached for Ask
    effect = compare(before, after)
    fingerprint = stable_hash({"decisions": sorted((str(d.get("id")), str(d.get("action"))) for d in decisions),
                               "cases": [(c["id"], c["before"], c["after"]) for c in effect["cases"]]})
    return {"evaluated": True, **effect, "errors": outcome.get("errors", []), "fingerprint": fingerprint}


def review_checked(session: Session, workspace_id: str, user: Any, decisions: list[dict[str, Any]],
                   override_reason: str | None = None) -> dict[str, Any]:
    """`suggestions.review`, refused when the batch breaks a passing golden case and no reason is given."""
    from analystos.governance.audit import audit
    from analystos.knowledge import suggestions

    effect = preview(session, workspace_id, user, decisions)
    reason = (override_reason or "").strip()
    broken = [c for c in effect["cases"] if c["change"] == "newly_failing"]
    if broken and not reason:
        raise Conflict(f"This batch makes {len(broken)} golden question(s) fail that pass today. "
                       "Remove the drafts that cause it, or approve with a written reason.",
                       details={"code": "golden_regression", "effect": effect})
    result = suggestions.review(session, workspace_id, user, decisions)
    if effect["evaluated"]:
        audit(f"user:{user.id}", "knowledge.review.golden_effect", workspace_id=workspace_id,
              target=effect["fingerprint"], decision="override" if broken else "allow",
              reasons=[reason[:2000]] if broken else [],
              details={"baseline": effect["baseline"], "projected": effect["projected"], "delta_points": effect["delta_points"],
                       "newly_failing": [c["id"] for c in broken], "fixed": [c["id"] for c in effect["cases"] if c["change"] == "fixed"],
                       "revision": result.get("revision")}, session=session)
    return {**result, "effect": effect}
