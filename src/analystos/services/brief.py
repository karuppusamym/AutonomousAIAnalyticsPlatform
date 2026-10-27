"""The versioned workspace brief (workspace spec §2, P4-04).

Every change writes a new brief version; each assertion also carries its own version, bumped when
its value or review state changes. Three writers, three rules:

* **Suggestions** (`refresh`) come from deterministic code over what the platform already knows —
  crawler table semantics (grain, entity: origin `rule`), declared primary keys (`source`), discovered
  or declared relationships, date columns, approved metric versions. A suggestion never replaces an
  assertion a person stated, reviewed or rejected (the crawler's owner-wins invariant).
* **Checks** (`validate`) turn a suggestion into `validated` only when a deterministic check over
  profiles or validated relationships confirms it; a failed check is recorded as evidence and the
  assertion stays a suggestion.
* **People** (`patch`) state assertions (origin `user`, reviewed), review or reject suggestions, or
  remove assertions, under an `If-Match` on the brief version.

Nothing in the brief grants access: `constraints.source_scope` only narrows what readiness and planning
consider, and `memory` returns only what the caller's resolved scope may see.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.brief import (
    SUGGESTION_FIELDS,
    Assertion,
    AssertionIn,
    BriefPatch,
    EvidenceRef,
    WorkspaceBriefDoc,
    assertion_key,
)
from analystos.core.errors import InvalidInput, NotFound, PreconditionFailed
from analystos.core.ids import new_id, ordered_utcnow, stable_hash, utcnow
from analystos.db.models import (
    Relationship,
    SemanticMetric,
    SourceAsset,
    SourceColumn,
    User,
    WorkspaceBrief,
)
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role

REL_ORIGIN = {"user": "user", "context": "source", "discovered": "rule"}


# ------------------------------------------------------------------------------------ read
def head(session: Session, workspace_id: str) -> WorkspaceBrief | None:
    return session.scalar(select(WorkspaceBrief).where(WorkspaceBrief.workspace_id == workspace_id)
                          .order_by(WorkspaceBrief.version.desc()).limit(1))


def assertions_of(row: WorkspaceBrief | None) -> list[Assertion]:
    return [Assertion.model_validate(a) for a in (row.assertions if row is not None else [])]


def doc(row: WorkspaceBrief | None, workspace_id: str) -> dict[str, Any]:
    if row is None:
        return WorkspaceBriefDoc(workspace_id=workspace_id, version=0).model_dump(mode="json")
    return WorkspaceBriefDoc(workspace_id=workspace_id, version=row.version, content_hash=row.content_hash,
                             created_by=row.created_by, created_at=row.created_at.isoformat() if row.created_at else None,
                             reason=row.reason, assertions=assertions_of(row)).model_dump(mode="json")


def get(session: Session, user: User, workspace_id: str, version: int | None = None) -> dict[str, Any]:
    require_role(session, user, workspace_id, "viewer")
    get_workspace(session, workspace_id)
    if version is None:
        return doc(head(session, workspace_id), workspace_id)
    row = session.scalar(select(WorkspaceBrief).where(WorkspaceBrief.workspace_id == workspace_id,
                                                      WorkspaceBrief.version == version))
    if row is None:
        raise NotFound(f"brief version {version} not found")
    return doc(row, workspace_id)


def history(session: Session, user: User, workspace_id: str, limit: int = 50) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    rows = session.scalars(select(WorkspaceBrief).where(WorkspaceBrief.workspace_id == workspace_id)
                           .order_by(WorkspaceBrief.version.desc()).limit(min(max(limit, 1), 200)))
    return [{"version": r.version, "content_hash": r.content_hash, "reason": r.reason, "created_by": r.created_by,
             "created_at": r.created_at.isoformat() if r.created_at else None, "assertions": len(r.assertions or [])}
            for r in rows]


def effective(assertions: Iterable[Assertion], group: str, field: str, subject: str | None = None) -> Assertion | None:
    """The reviewed or validated assertion for (group, field, subject); suggestions are not facts."""
    key = assertion_key(group, field, subject)
    return next((a for a in assertions if a.key == key and a.effective), None)


def find(assertions: Iterable[Assertion], group: str, field: str, subject: str | None = None) -> Assertion | None:
    key = assertion_key(group, field, subject)
    return next((a for a in assertions if a.key == key), None)


# ------------------------------------------------------------------------------------ write
def _content_hash(items: list[Assertion]) -> str:
    return stable_hash([a.model_dump(mode="json", exclude={"updated_at"}) for a in sorted(items, key=lambda a: a.key)])


def _write(session: Session, workspace_id: str, items: list[Assertion], *, actor: str, reason: str,
           current: WorkspaceBrief | None) -> tuple[WorkspaceBrief, bool]:
    """A new version when the content changed; otherwise the current one (no empty versions)."""
    digest = _content_hash(items)
    if (current is not None and current.content_hash == digest) or (current is None and not items):
        return current, False  # type: ignore[return-value]
    version = (current.version if current is not None else 0) + 1
    row = WorkspaceBrief(id=new_id("brf"), workspace_id=workspace_id, version=version,
                         assertions=[a.model_dump(mode="json") for a in sorted(items, key=lambda a: a.key)],
                         content_hash=digest, reason=reason[:2000], created_by=actor, created_at=ordered_utcnow())
    session.add(row)
    session.flush()
    emit(workspace_id, "brief.updated", {"version": version, "reason": reason[:300], "assertions": len(items)}, actor=actor,
         session=session)
    return row, True


def _bump(a: Assertion, *, actor: str, **changes: Any) -> Assertion:
    return a.model_copy(update={**changes, "version": a.version + 1, "updated_by": actor, "updated_at": utcnow().isoformat()})


def _check_version(current: WorkspaceBrief | None, expected: int | None) -> None:
    have = current.version if current is not None else 0
    if expected is not None and expected != have:
        raise PreconditionFailed(f"the brief is at version {have}, not {expected}", details={"current_version": have})


def patch(session: Session, user: User, workspace_id: str, body: BriefPatch, expected_version: int | None) -> dict[str, Any]:
    """People's changes: a new brief version, and what it makes out of date (readiness assessments)."""
    from analystos.db.models import ReadinessAssessment

    require_role(session, user, workspace_id, "editor")
    current = head(session, workspace_id)
    _check_version(current, expected_version)
    actor = f"user:{user.id}"
    items = {a.key: a for a in assertions_of(current)}
    changed: list[str] = []
    for op in body.ops:
        if op.op == "set":
            a_in: AssertionIn = op.assertion  # type: ignore[assignment]
            key = assertion_key(a_in.group, a_in.field, a_in.subject)
            new = Assertion(key=key, group=a_in.group, field=a_in.field, subject=a_in.subject, value=a_in.value, origin="user",
                            evidence=[*a_in.evidence, EvidenceRef(kind="user_note", ref=user.id)], review_state="reviewed",
                            note=a_in.note or op.note, updated_by=actor, updated_at=utcnow().isoformat())
            old = items.get(key)
            if old is not None:
                new = new.model_copy(update={"version": old.version + 1})
            items[key] = new
            changed.append(key)
            continue
        old = items.get(op.key or "")
        if old is None:
            raise NotFound(f"no assertion {op.key!r} in the brief")
        if op.op == "remove":
            items.pop(old.key)
        elif op.op == "review":
            items[old.key] = _bump(old, actor=actor, review_state="reviewed", note=op.note or old.note)
        elif op.op == "reject":
            items[old.key] = _bump(old, actor=actor, review_state="rejected", note=op.note or old.note)
        changed.append(old.key)
    row, wrote = _write(session, workspace_id, list(items.values()), actor=actor,
                        reason=body.reason or f"{len(changed)} assertion(s) changed", current=current)
    stale = 0
    if wrote:
        stale = session.scalar(select(func.count(ReadinessAssessment.id)).where(
            ReadinessAssessment.workspace_id == workspace_id, ReadinessAssessment.brief_version < row.version)) or 0
        audit(actor, "brief.updated", workspace_id=workspace_id, target=row.id,
              details={"version": row.version, "changed": changed[:50]}, session=session)
    return {**doc(row, workspace_id), "changed": changed,
            "impact": {"readiness_assessments_outdated": int(stale),
                       "note": "assessments made on an earlier brief version must be re-run before work starts"}}


# ------------------------------------------------------------------------------------ suggestions and checks
def _fq(asset: SourceAsset) -> str:
    return f"{asset.schema_name}.{asset.name}"


TIME_ROLES = ("date", "timestamp")  # catalog column roles (skills/catalog ColumnSemantics.semantic_role)


def unique_keys(asset: SourceAsset) -> list[dict[str, Any]]:
    """Keys known to be unique on this asset's data, strongest evidence first: a measured key check
    (`stats.key_check`, the model suggestion's validation), then profile candidates marked `unique`. A
    name-hinted column with duplicates is never a key suggestion."""
    stats = asset.stats or {}
    out: list[dict[str, Any]] = []
    check = stats.get("key_check")
    if isinstance(check, dict) and check.get("unique") is True and check.get("columns"):
        out.append({**check, "evidence": "measured_unique"})
    for k in stats.get("candidate_keys") or []:
        if isinstance(k, dict) and k.get("unique") is True and k.get("columns") \
                and all(sorted(k["columns"]) != sorted(o["columns"]) for o in out):
            out.append(k)
    return out


def _suggestion(group: str, field: str, subject: str | None, value: Any, origin: str, evidence: list[EvidenceRef],
                confidence: float | None = None, review_state: str | None = None) -> Assertion:
    state = review_state or ("suggested" if field in SUGGESTION_FIELDS or origin in ("rule", "model") else "reviewed")
    return Assertion(key=assertion_key(group, field, subject), group=group, field=field, subject=subject, value=value,
                     origin=origin, evidence=evidence, review_state=state, confidence=confidence,
                     updated_by="system:brief", updated_at=utcnow().isoformat())


def derive(session: Session, workspace_id: str) -> list[Assertion]:
    """What deterministic code can say about the selected data now (never a model, never a query)."""
    assets = list(session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                            SourceAsset.selected.is_(True)).order_by(SourceAsset.id)))
    by_id = {a.id: a for a in assets}
    out: list[Assertion] = []
    for a in assets:
        fq, sem = _fq(a), dict(a.semantics or {})
        ev = [EvidenceRef(kind="asset", ref=fq, detail={"asset_id": a.id})]
        if sem.get("grain"):
            out.append(_suggestion("data_semantics", "grain", fq, sem["grain"], "rule", ev, sem.get("confidence")))
        if sem.get("entity"):
            # the source's own label ("Configuration Item") names the entity better than one read from the table name
            # ("cmdb ci"); a person's business name wins too
            label = a.business_name if a.business_name and a.business_name_origin in ("source", "user") else sem["entity"]
            out.append(_suggestion("domain", "entity", fq, label, "rule", ev, sem.get("confidence")))
        cols = list(session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal)))
        keys = [c.name for c in cols if c.is_key]
        if keys:
            out.append(_suggestion("data_semantics", "entity_key", fq, keys, "source",
                                   [EvidenceRef(kind="column", ref=f"{fq}.{k}", detail={"declared": "primary key"}) for k in keys]))
        else:
            cand = unique_keys(a)
            if cand:
                out.append(_suggestion("data_semantics", "entity_key", fq, list(cand[0]["columns"]), "rule",
                                       [EvidenceRef(kind="profile", ref=fq, detail={"candidate_key": cand[0]})]))
        times = [c for c in cols if (c.semantic_type == "datetime" or (c.semantics or {}).get("semantic_role") in TIME_ROLES)
                 and "pii" not in (c.tags or [])]
        if times and sem.get("role") not in ("dimension", "reference"):  # a dimension's last-edited time is not, not an event
            out.append(_suggestion("time_measures", "event_time", fq, times[0].name, "rule",
                                   [EvidenceRef(kind="column", ref=f"{fq}.{times[0].name}")]))
    for r in session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id).order_by(Relationship.id)):
        f, t = by_id.get(r.from_asset_id), by_id.get(r.to_asset_id)
        if f is None or t is None:
            continue
        subject = f"{_fq(f)}.{r.from_column}->{_fq(t)}.{r.to_column}"
        origin = REL_ORIGIN.get(r.origin, "rule")
        state = "validated" if r.validated else ("reviewed" if origin == "user" else "suggested")
        out.append(_suggestion("data_semantics", "join_cardinality", subject, r.cardinality, origin,
                               [EvidenceRef(kind="relationship", ref=r.id, detail={"validated": r.validated,
                                                                                    "confidence": r.confidence})],
                               r.confidence, review_state=state))
    approved = {}
    for m in session.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id,
                                                          SemanticMetric.status == "approved")):
        if m.name not in approved or m.version > approved[m.name]:
            approved[m.name] = m.version
    if approved:
        out.append(_suggestion("knowledge", "metric_versions", None, dict(sorted(approved.items())), "source",
                               [EvidenceRef(kind="metric", ref=n) for n in sorted(approved)], review_state="reviewed"))
    return out


def key_uniqueness(session: Session, workspace_id: str, asset_fq: str, columns: list[str]) -> dict[str, Any]:
    """Is a declared key unique, from the profiles (no query): `unique`, `duplicates` or `unknown`."""
    schema, _, name = asset_fq.partition(".")
    asset = session.scalar(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.schema_name == schema,
                                                     SourceAsset.name == name))
    if asset is None:
        return {"state": "unknown", "detail": f"{asset_fq} is not a known asset"}
    rows = asset.row_count if asset.row_count is not None else (asset.stats or {}).get("row_count")
    check = (asset.stats or {}).get("key_check")
    if isinstance(check, dict) and sorted(check.get("columns") or []) == sorted(columns) and check.get("unique") is not None:
        state = "unique" if check["unique"] else "duplicates"
        return {"state": state, "detail": f"measured: {check.get('distinct_keys')} distinct keys in {check.get('rows')} rows",
                "rows": check.get("rows"), "measured_at": check.get("measured_at")}
    for cand in (asset.stats or {}).get("candidate_keys") or []:
        if not isinstance(cand, dict) or sorted(cand.get("columns") or []) != sorted(columns):
            continue
        if cand.get("unique") is True:
            return {"state": "unique", "detail": f"profiled candidate key over {rows} rows", "rows": rows}
        if cand.get("unique") is False:
            return {"state": "duplicates", "detail": f"profiled: {cand.get('distinct')} distinct values and "
                    f"{cand.get('null_count')} nulls in {rows} rows", "rows": rows, "distinct": cand.get("distinct"),
                    "nulls": cand.get("null_count")}
    if len(columns) != 1 or rows is None:
        return {"state": "unknown", "detail": "no profile covers this key", "rows": rows}
    col = session.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset.id, SourceColumn.name == columns[0]))
    prof = (col.profile or {}) if col is not None else {}
    if not prof or prof.get("distinct") is None:
        return {"state": "unknown", "detail": f"{columns[0]} is not profiled", "rows": rows}
    distinct, nulls = int(prof.get("distinct") or 0), int(prof.get("null_count") or 0)
    if distinct == int(rows) and nulls == 0:
        return {"state": "unique", "detail": f"{distinct} distinct values in {rows} rows, no nulls", "rows": rows}
    return {"state": "duplicates", "detail": f"{distinct} distinct values and {nulls} nulls in {rows} rows", "rows": rows,
            "distinct": distinct, "nulls": nulls}


def validate(session: Session, workspace_id: str, items: list[Assertion]) -> list[Assertion]:
    """Deterministic checks promote suggestions to `validated`; they never demote a person's review."""
    out = []
    for a in items:
        if a.review_state == "suggested" and a.field == "entity_key" and a.subject and isinstance(a.value, list):
            res = key_uniqueness(session, workspace_id, a.subject, [str(c) for c in a.value])
            ev = [e for e in a.evidence if e.kind != "check"] + [EvidenceRef(kind="check", ref="key_uniqueness", detail=res)]
            a = a.model_copy(update={"evidence": ev, "review_state": "validated" if res["state"] == "unique" else "suggested"})
        out.append(a)
    return out


def refresh(session: Session, user: User | None, workspace_id: str) -> dict[str, Any]:
    """Merge fresh, checked suggestions into the brief; a new version only if anything changed. A person's
    decision (a `user` assertion, a review, a rejection) is never overwritten by a suggestion."""
    if user is not None:
        require_role(session, user, workspace_id, "analyst")
    current = head(session, workspace_id)
    items = {a.key: a for a in assertions_of(current)}
    added, updated = [], []
    fresh = validate(session, workspace_id, derive(session, workspace_id))
    # a system suggestion nobody decided on that the catalog no longer supports (a table became a dimension, a
    # table left the selection) is withdrawn, not left as an open question
    withdrawn = [k for k, a in items.items() if a.updated_by == "system:brief" and a.review_state == "suggested"
                 and a.origin != "user" and k not in {s.key for s in fresh}]
    for k in withdrawn:
        items.pop(k)
    for s in fresh:
        old = items.get(s.key)
        if old is None:
            items[s.key] = s
            added.append(s.key)
        elif old.origin == "user" or old.review_state in ("reviewed", "rejected"):
            continue
        elif _state(old) != _state(s):
            items[s.key] = s.model_copy(update={"version": old.version + 1})
            updated.append(s.key)
    row, wrote = _write(session, workspace_id, list(items.values()), actor=f"user:{user.id}" if user else "system:brief",
                        reason=f"suggestions refreshed: {len(added)} added, {len(updated)} updated, {len(withdrawn)} withdrawn",
                        current=current)
    return {**doc(row, workspace_id), "added": added, "updated": updated, "withdrawn": withdrawn, "new_version": wrote}


def _state(a: Assertion) -> str:
    return stable_hash({"value": a.value, "origin": a.origin, "review_state": a.review_state,
                        "evidence": [e.model_dump(mode="json") for e in a.evidence], "confidence": a.confidence})


# ------------------------------------------------------------------------------------ scoped memory
def in_scope(entry: dict[str, Any], scope: Any) -> bool:
    """A memory item is visible when every column it maps is on an asset in the caller's scope and is not a
    denied column; an item that maps nothing is workspace knowledge and visible to every member."""
    assets, denied = set(scope.assets), set(scope.denied_columns)
    for col in entry.get("mapped_columns") or []:
        parts = str(col).split(".")
        if len(parts) >= 3:
            if ".".join(parts[:2]) not in assets or ".".join(parts[:3]) in denied:
                return False
        elif len(parts) == 2 and ".".join(parts) not in assets:
            return False
    subject = entry.get("subject")
    if subject and "->" not in str(subject):
        parts = str(subject).split(".")
        if len(parts) >= 2 and ".".join(parts[:2]) not in assets:
            return False
    return True


def memory(session: Session, user: User, workspace_id: str, query: str, *, limit: int = 8) -> dict[str, Any]:
    """Workspace-authorized memory for a request: context entries and pack knowledge of *this* workspace
    (context.search is workspace-bound), then only what the caller's resolved scope may see, plus the
    brief's effective assertions on assets in scope. Withheld items are counted, never shown."""
    from analystos.context.service import search
    from analystos.governance.policy import resolve_scope

    if not (query or "").strip():
        raise InvalidInput("give a query to retrieve memory for")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="viewer")
    hits = search(session, workspace_id, query, limit=limit * 2)
    visible = [h for h in hits if in_scope(h, scope)]
    brief = [a.model_dump(mode="json") for a in assertions_of(head(session, workspace_id))
             if a.effective and in_scope({"subject": a.subject}, scope)]
    return {"workspace_id": workspace_id, "query": query, "items": visible[:limit], "withheld": len(hits) - len(visible),
            "brief": brief, "scope_hash": scope.scope_hash()}
