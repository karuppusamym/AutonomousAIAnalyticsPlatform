"""Duplicate requests (spec S13 §3.1): an open request in the same workspace from the last 7 days with the same
normalized intent and similar text (cosine ≥ 0.92 on the platform's feature-hash embedding) is the original;
the new one is linked to it and its requester follows it. Deterministic: no model, no network."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.desk.models import Request
from analystos.desk.states import OPEN

WINDOW = timedelta(days=7)
THRESHOLD = 0.92
MAX_CANDIDATES = 500  # bounded scan (ADR-014): the newest open requests of the workspace


def similarity(a: str, b: str) -> float:
    from analystos.context.embeddings import embed

    va, vb = embed((a or "").lower()), embed((b or "").lower())
    return max(0.0, min(1.0, sum(x * y for x, y in zip(va, vb, strict=True)) / (
        (math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(y * y for y in vb))) or 1.0)))


def find_original(session: Session, workspace_id: str, text: str, intent_key: str | None, now: datetime,
                  exclude_id: str | None = None) -> tuple[Request, float] | None:
    if not intent_key:
        return None
    stmt = (select(Request).where(Request.workspace_id == workspace_id, Request.state.in_(sorted(OPEN)),
                                  Request.duplicate_of.is_(None), Request.created_at >= now - WINDOW)
            .order_by(Request.created_at.desc()).limit(MAX_CANDIDATES))
    best: tuple[Request, float] | None = None
    for cand in session.scalars(stmt):
        if cand.id == exclude_id or (cand.normalized_intent or {}).get("key") != intent_key:
            continue
        score = similarity(text, cand.text)
        if score >= THRESHOLD and (best is None or score > best[1]):
            best = (cand, score)
    return best
