"""Certify-on-close (spec S13 §3.5, task S13-T5): an analyst's answer becomes reusable context in the S03 ledger.

An analyst (never the requester alone, never a model) certifies an answered request as one of:

* ``verified_query`` — a successful Ask/console statement the analyst ran, promoted with the request's text as
  its phrasing (``registries/verified_queries.promote``: the gateway validator checks it for the analyst first).
  The ledger lists it as ``certified``; the next similar request triages R1 and the Ask registry rung answers it
  without a model (AT-20).
* ``glossary`` — a reviewed definition (``context_entry`` with origin ``desk_certified``) → ledger ``approved``.
  Mapped columns must be columns the analyst may read.
* ``metric`` — a metric *suggestion* for the owner's review queue → ledger ``suggested``: a metric is certified
  only through the semantic layer's own approval.
* ``playbook`` — the steps that answered it, kept on the request → ledger ``approved``.

Text is data: anything that reads like an instruction to a model is refused. Each certification is recorded on
the request (``answer_ref.certified``), in its timeline and audit trail, and projected into the ledger in the same
transaction (``context/ledger.py``).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import Conflict, InvalidInput
from analystos.core.ids import utcnow
from analystos.security.injection import is_injection

KINDS = ("verified_query", "glossary", "metric", "playbook")
CERTIFIABLE_STATES = ("answered", "accepted")
MAX_TEXT = 4000


def _clean(value: Any, name: str, *, limit: int = MAX_TEXT, required: bool = True) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if required and not text:
        raise InvalidInput(f"{name} is required")
    if text and is_injection(text):
        raise InvalidInput(f"{name} must describe the answer, not instruct a model")
    return text


def _query_id(req: Any, payload: dict[str, Any]) -> str:
    qid = str(payload.get("query_id") or "")
    if not qid:
        qids = [e.get("id") for e in (req.answer_ref or {}).get("evidence") or [] if e.get("type") == "query"]
        qid = str(qids[0]) if qids else ""
    if not qid:
        raise InvalidInput("a verified query needs query_id (a successful Ask or console statement behind the answer)")
    return qid


def certify(session: Session, req: Any, user: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Certify one answered request; returns the record kept on the request."""
    from analystos.governance.audit import audit

    kind = str(payload.get("as") or "")
    if kind not in KINDS:
        raise InvalidInput(f"`as` must be one of {', '.join(KINDS)}")
    if req.state not in CERTIFIABLE_STATES:
        raise Conflict(f"only an answered request can be certified (this one is {req.state})")
    ref = req.answer_ref or {}
    if ref.get("via") not in ("human", "auto_with_review"):
        raise Conflict("certify an analyst's answer: this one was not answered or reviewed by an analyst")
    title = _clean(payload.get("title") or payload.get("term") or payload.get("name") or req.text, "title", limit=300)
    if kind == "verified_query":
        record = _verified_query(session, req, user, payload, title)
    elif kind == "glossary":
        record = _glossary(session, req, user, payload, title)
    elif kind == "metric":
        record = _metric(session, req, user, payload, title)
    else:
        record = _playbook(req, user, payload, title)
    record.update({"as": kind, "by": user.id, "at": utcnow().isoformat(timespec="seconds")})
    req.answer_ref = {**(req.answer_ref or {}), "certified": [*((req.answer_ref or {}).get("certified") or []), record]}
    audit(f"user:{user.id}", "request.certified", workspace_id=req.workspace_id, target=req.id, decision="allow",
          details=record, session=session)
    return record


def _verified_query(session: Session, req: Any, user: Any, payload: dict[str, Any], title: str) -> dict[str, Any]:
    from analystos.registries import verified_queries

    patterns = [_clean(p, "pattern", limit=500) for p in payload.get("patterns") or [] if str(p or "").strip()][:10]
    vq = verified_queries.promote(session, user, req.workspace_id, question=_clean(req.text, "request text"),
                                  name=title if payload.get("title") or payload.get("name") else None,
                                  query_id=_query_id(req, payload), patterns=patterns,
                                  description=_clean(payload.get("description") or (req.answer_ref or {}).get("text")
                                                     or req.text, "description", required=False))
    vq.origin = {**(vq.origin or {}), "via": "desk", "request_id": req.id}  # where the analyst certified it
    return {"ref_table": "verified_query", "ref_id": vq.id, "name": vq.name, "trust": "certified"}


def _glossary(session: Session, req: Any, user: Any, payload: dict[str, Any], title: str) -> dict[str, Any]:
    from analystos.context.service import add_entry
    from analystos.governance.policy import require_role, resolve_scope
    from analystos.services.context_grounding import allowed_ref

    definition = _clean(payload.get("definition") or (req.answer_ref or {}).get("text"), "definition")
    columns = [str(c) for c in payload.get("mapped_columns") or []][:20]
    if columns:
        require_role(session, user, req.workspace_id, "analyst")
        scope = resolve_scope(session, session.merge(user), req.workspace_id)
        if any(not allowed_ref(c, scope) for c in columns):
            raise InvalidInput("every mapped column must be a catalog column you may read")
    synonyms = [_clean(s, "synonym", limit=200) for s in payload.get("synonyms") or [] if str(s or "").strip()][:10]
    entry = add_entry(session, workspace_id=req.workspace_id, kind="definition", name=title, body=definition,
                      synonyms=synonyms, mapped_columns=columns, origin="desk_certified", trusted=True)
    session.flush()
    return {"ref_table": "context_entry", "ref_id": entry.id, "name": title, "trust": "approved"}


def _metric(session: Session, req: Any, user: Any, payload: dict[str, Any], title: str) -> dict[str, Any]:
    from analystos.knowledge.suggestions import field, primary_field, propose

    definition = _clean(payload.get("definition") or (req.answer_ref or {}).get("text"), "definition")
    fields = {primary_field("metric"): field(definition, 1.0, source="desk", request_id=req.id)}
    if payload.get("expression"):
        fields["expression"] = field(_clean(payload["expression"], "expression", limit=2000), 1.0, source="desk")
    row = propose(session, req.workspace_id, kind="metric", subject=f"request:{req.id}", title=title, fields=fields,
                  origin="desk.certify", proposed_by=f"user:{user.id}", batch=req.id)
    if row is None:
        raise Conflict("this metric suggestion was already decided, or its definition was rejected before")
    return {"ref_table": "knowledge_suggestion", "ref_id": row.id, "name": title, "trust": "suggested"}


def _playbook(req: Any, user: Any, payload: dict[str, Any], title: str) -> dict[str, Any]:
    steps = _clean(payload.get("steps") or (req.answer_ref or {}).get("text"), "steps")
    req.answer_ref = {**(req.answer_ref or {}), "playbook": {"title": title, "steps": steps, "certified_by": user.id,
                                                             "at": utcnow().isoformat(timespec="seconds")}}
    return {"ref_table": "request", "ref_id": req.id, "name": title, "trust": "approved"}
