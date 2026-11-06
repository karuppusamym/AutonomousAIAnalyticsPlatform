"""Impact of a schema change (review 2026-11-05 P1-5): what reads the columns a crawl saw change or disappear.

Input: a crawl run's diff (removed and retyped columns of changed tables; every column of a deprecated table). The
report lists, deterministically and read-only:

* `downstream`: columns that receive values from a changed column along recorded column lineage (recipes, dbt),
* `metrics`, `verified_queries`, `dashboards`, `monitors`, `schedules` whose definition reads a changed or downstream
  column. Verified-query SQL is parsed (`evidence/lineage/sql.py`); other definitions are matched on the identifiers
  they mention (the asset by id or name, then the column), so the list errs on the side of showing too much.
  An object that names an impacted object (a schedule of an impacted monitor) is listed `via` that object.

Pausing the impacted schedules is a side effect, so it is an approval bound to the exact schedule ids and revisions
(`request_pause`); `execute_pause` re-derives the impact, re-checks the binding with `verify_for_execution`, and only
then disables them. A schedule edited after the request no longer matches its hash and needs a new approval.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.db.models import (
    Approval,
    Artifact,
    CrawlRun,
    Monitor,
    Schedule,
    SemanticMetric,
    SourceAsset,
    User,
    VerifiedQuery,
)
from analystos.evidence.lineage import propagation as prop

PAUSE_ACTION = "schedule.pause_for_change"
MAX_STRINGS = 5_000
MAX_CHARS = 400_000
MAX_ROWS = 2_000
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z_][A-Za-z0-9_$]*)*")
_PLACEHOLDER = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}")


# ------------------------------------------------------------------------------------ changed columns
def _asset_for_key(session: Session, source_id: str, key: str) -> SourceAsset | None:
    for a in session.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id)):
        if key in ((a.semantics or {}).get("source_key"), f"{a.schema_name}.{a.name}", a.source_name, a.name):
            return a
    return None


def changed_columns(session: Session, crawl: CrawlRun) -> list[dict[str, Any]]:
    """Removed / retyped columns of changed tables and every column of deprecated ones (added columns break nothing)."""
    changes = crawl.changes or {}
    out: list[dict[str, Any]] = []
    for ch in changes.get("changed") or []:
        a = _asset_for_key(session, crawl.source_id, ch.get("key") or "")
        fq = prop.norm_asset(f"{a.schema_name}.{a.name}") if a else prop.norm_asset(ch.get("key"))
        for name in ch.get("removed") or []:
            out.append({"asset": fq, "asset_id": a.id if a else None, "key": ch.get("key"), "column": name.lower(),
                        "change": "removed"})
        for r in ch.get("retyped") or []:
            out.append({"asset": fq, "asset_id": a.id if a else None, "key": ch.get("key"), "column": r["name"].lower(),
                        "change": "retyped", "from": r.get("previous_type"), "to": r.get("current_type")})
    for key in changes.get("deprecated") or []:
        a = _asset_for_key(session, crawl.source_id, key)
        fq = prop.norm_asset(f"{a.schema_name}.{a.name}") if a else prop.norm_asset(key)
        out.append({"asset": fq, "asset_id": a.id if a else None, "key": key, "column": "*", "change": "table_deprecated"})
    return out


# ------------------------------------------------------------------------------------ matching
def _strings(obj: Any, out: list[str], budget: list[int]) -> None:
    if len(out) >= MAX_STRINGS or budget[0] <= 0:
        return
    if isinstance(obj, str):
        out.append(obj)
        budget[0] -= len(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _strings(k, out, budget)
            _strings(v, out, budget)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _strings(v, out, budget)


def tokens(*objs: Any) -> set[str]:
    """Lower-case identifiers (and every dotted prefix/part of them) mentioned anywhere in `objs`."""
    strings: list[str] = []
    budget = [MAX_CHARS]
    for o in objs:
        _strings(o, strings, budget)
    out: set[str] = set()
    for s in strings:
        for m in _IDENT.findall(re.sub(r'["`\[\]]', "", s)):
            low = m.lower()
            parts = low.split(".")
            out.add(low)
            out.update(parts)
            out.update(".".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 2, len(parts) + 1))
    return out


class _Targets:
    """Changed + downstream columns, indexed for matching."""

    def __init__(self, cols: Iterable[dict[str, Any]], asset_ids: dict[str, str]) -> None:
        self.cols = list(cols)
        self.asset_ids = asset_ids  # fq -> asset id

    def hits(self, toks: set[str]) -> list[dict[str, Any]]:
        out = []
        for c in self.cols:
            fq = c["asset"]
            name = fq.split(".")[-1]
            aid = (self.asset_ids.get(fq) or "").lower()
            if not (fq in toks or name in toks or (aid and aid in toks)):
                continue
            if c["column"] == "*" or c["column"] in toks or f"{name}.{c['column']}" in toks:
                out.append({"asset": fq, "column": c["column"], "change": c["change"]})
        return out


def _sql_hits(sql: str, dialect: str, targets: _Targets) -> list[dict[str, Any]]:
    """Columns a verified query reads, from its parsed lineage (placeholders bound to NULL). Falls back to tokens."""
    from analystos.evidence.lineage.sql import parse_lineage

    text = _PLACEHOLDER.sub("NULL", sql or "")
    try:
        parsed = parse_lineage(text, dialect)
    except Exception:  # noqa: BLE001 - an unparseable template is matched on its identifiers instead
        parsed = None
    if parsed is None or parsed.errors or not parsed.edges:
        return targets.hits(tokens(text))
    read = {(prop.norm_asset(e.source_table), e.source_column.lower()) for e in parsed.edges if e.source_resolved}
    if not read:  # unqualified columns the parser could not place: identifiers decide
        return targets.hits(tokens(text))
    tables = {a for a, _ in read}
    out = []
    for c in targets.cols:
        if c["asset"] in tables and (c["column"] == "*" or (c["asset"], c["column"]) in read):
            out.append({"asset": c["asset"], "column": c["column"], "change": c["change"]})
    # columns used only in joins / grouping are not projected edges: the token match covers them
    seen = {(h["asset"], h["column"]) for h in out}
    return out + [h for h in targets.hits(tokens(text)) if (h["asset"], h["column"]) not in seen and h["asset"] in tables]


def _via(toks: set[str], impacted: dict[str, list[str]]) -> list[str]:
    return sorted({f"{kind}:{ident}" for kind, ids in impacted.items() for ident in ids if ident.lower() in toks})


# ------------------------------------------------------------------------------------ the report
def impact(session: Session, workspace_id: str, changed: list[dict[str, Any]]) -> dict[str, Any]:
    from analystos.services.pii_lineage import recorded_edges

    edges = recorded_edges(session, workspace_id)
    starts = {(c["asset"], c["column"]) for c in changed if c["column"] != "*"}
    whole = {c["asset"] for c in changed if c["column"] == "*"}
    starts |= {e.source for e in edges if e.source[0] in whole}
    down = prop.downstream(edges, starts)
    downstream = [{"asset": t[0], "column": t[1], "change": "downstream", "hops": len(p),
                   "from": f"{p[0].source[0]}.{p[0].source[1]}", "origins": sorted({e.origin for e in p})}
                  for t, p in sorted(down.items())]
    asset_ids = {c["asset"]: c["asset_id"] for c in changed if c.get("asset_id")}
    targets = _Targets(changed + downstream, asset_ids)

    metrics = []
    from analystos.semantic.service import current_model

    model = current_model(session, workspace_id)
    datasets = {d.get("name"): d for d in (model.datasets if model else []) or [] if isinstance(d, dict)}
    for m in session.scalars(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id,
                                                          SemanticMetric.status.in_(["approved", "proposed", "draft"]))
                             .limit(MAX_ROWS)):
        d = m.definition or {}
        hits = targets.hits(tokens(d, m.expression, datasets.get(d.get("dataset")) or {}))
        if hits:
            metrics.append({"id": m.id, "name": m.name, "version": m.version, "status": m.status, "reads": hits})
    queries = []
    for q in session.scalars(select(VerifiedQuery).where(VerifiedQuery.workspace_id == workspace_id,
                                                         VerifiedQuery.status == "active").limit(MAX_ROWS)):
        hits = _sql_hits(q.sql_template, q.dialect or "postgres", targets) or targets.hits(tokens(q.spec or {}))
        if hits:
            queries.append({"id": q.id, "name": q.name, "reads": hits})
    impacted = {"metric": [m["name"] for m in metrics] + [m["id"] for m in metrics],
                "verified_query": [q["id"] for q in queries]}
    dashboards = []
    for a in session.scalars(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.type == "dashboard",
                                                    Artifact.status != "archived").limit(MAX_ROWS)):
        toks = tokens(a.content or {})
        hits, via = targets.hits(toks), _via(toks, impacted)
        if hits or via:
            dashboards.append({"id": a.id, "name": a.name, "status": a.status, "reads": hits, "via": via})
    impacted["dashboard"] = [d["id"] for d in dashboards]
    monitors = []
    for mo in session.scalars(select(Monitor).where(Monitor.workspace_id == workspace_id).limit(MAX_ROWS)):
        toks = tokens(mo.config or {})
        hits, via = targets.hits(toks), _via(toks, impacted)
        if hits or via:
            monitors.append({"id": mo.id, "name": mo.name, "kind": mo.kind, "enabled": mo.enabled, "reads": hits,
                             "via": via})
    impacted["monitor"] = [mo["id"] for mo in monitors]
    schedules = []
    for sc in session.scalars(select(Schedule).where(Schedule.workspace_id == workspace_id).limit(MAX_ROWS)):
        toks = tokens(sc.config or {}, sc.pins or {})
        hits, via = targets.hits(toks), _via(toks, impacted)
        if hits or via:
            schedules.append({"id": sc.id, "name": sc.name, "kind": sc.kind, "enabled": sc.enabled,
                              "revision": sc.revision, "reads": hits, "via": via})
    return {"changed": changed, "downstream": downstream, "metrics": metrics, "verified_queries": queries,
            "dashboards": dashboards, "monitors": monitors, "schedules": schedules,
            "counts": {"changed": len(changed), "downstream": len(downstream), "metrics": len(metrics),
                       "verified_queries": len(queries), "dashboards": len(dashboards), "monitors": len(monitors),
                       "schedules": len(schedules)}}


def _crawl(session: Session, user: User, crawl_id: str, minimum: str) -> CrawlRun:
    from analystos.governance.policy import require_role

    crawl = session.get(CrawlRun, crawl_id)
    if crawl is None:
        raise NotFound(f"crawl {crawl_id} not found")
    require_role(session, user, crawl.workspace_id, minimum)
    return crawl


def crawl_impact(session: Session, user: User, crawl_id: str) -> dict[str, Any]:
    crawl = _crawl(session, user, crawl_id, "viewer")
    return {"crawl_id": crawl.id, "source_id": crawl.source_id, **impact(session, crawl.workspace_id,
                                                                          changed_columns(session, crawl))}


def record_crawl_impact(session: Session, crawl: CrawlRun, actor: str) -> dict[str, int]:
    """Called when a crawl finished with a schema change: the counts go on an event so the UI can flag the run."""
    from analystos.events.bus import emit

    changed = changed_columns(session, crawl)
    if not changed:
        return {}
    report = impact(session, crawl.workspace_id, changed)
    counts = report["counts"]
    if any(v for k, v in counts.items() if k != "changed"):
        emit(crawl.workspace_id, "schema.impact_reported",
             {"crawl_id": crawl.id, "source_id": crawl.source_id, "counts": counts,
              "schedules": [s["id"] for s in report["schedules"] if s["enabled"]][:50]}, actor=actor, session=session)
    return counts


# ------------------------------------------------------------------------------------ pause (approval-bound)
def _pause_payload(crawl: CrawlRun, schedules: list[Schedule]) -> dict[str, Any]:
    return {"crawl_id": crawl.id, "source_id": crawl.source_id,
            "schedules": [{"id": s.id, "revision": s.revision} for s in sorted(schedules, key=lambda s: s.id)]}


def _impacted_enabled(session: Session, crawl: CrawlRun, ids: Iterable[str]) -> list[Schedule]:
    report = impact(session, crawl.workspace_id, changed_columns(session, crawl))
    allowed = {s["id"] for s in report["schedules"]}
    wanted = sorted(set(ids))
    if not wanted:
        raise InvalidInput("name at least one schedule to pause")
    stray = [i for i in wanted if i not in allowed]
    if stray:
        raise InvalidInput(f"not impacted by this crawl's changes: {', '.join(stray)}")
    rows = [session.get(Schedule, i) for i in wanted]
    return [r for r in rows if r is not None and r.workspace_id == crawl.workspace_id]


def request_pause(session: Session, user: User, crawl_id: str, schedule_ids: list[str]) -> dict[str, Any]:
    """Ask for approval to disable impacted schedules. Nothing changes until an approver decides and it is executed."""
    from analystos.governance.approvals import request_approval
    from analystos.governance.policy import get_workspace

    crawl = _crawl(session, user, crawl_id, "editor")
    rows = [s for s in _impacted_enabled(session, crawl, schedule_ids) if s.enabled]
    if not rows:
        raise InvalidInput("every named schedule is already paused")
    ws = get_workspace(session, crawl.workspace_id)
    payload = _pause_payload(crawl, rows)
    changed = changed_columns(session, crawl)
    apr = request_approval(session, workspace_id=crawl.workspace_id, run_id=None, action=PAUSE_ACTION, payload=payload,
                           plan_hash=None, policy_version=ws.policy_version, requested_by=user.id, risk_tier="medium",
                           destination=None, affected_assets=sorted({c["asset"] for c in changed})[:50],
                           evidence={"changed": changed[:50], "schedules": [{"id": s.id, "name": s.name} for s in rows]})
    return {"approval_id": apr.id, "status": apr.status, "payload_hash": apr.payload_hash, "schedules": payload["schedules"],
            "expires_at": apr.expires_at.isoformat() if apr.expires_at else None}


def execute_pause(session: Session, user: User, approval_id: str) -> dict[str, Any]:
    """Disable exactly the approved schedules, after re-deriving the payload and verifying the binding."""
    from analystos.events.bus import emit
    from analystos.governance.approvals import consume, verify_for_execution
    from analystos.governance.audit import audit

    apr = session.get(Approval, approval_id)
    if apr is None or apr.action != PAUSE_ACTION:
        raise NotFound(f"approval {approval_id} not found")
    approved = apr.payload or {}
    crawl = _crawl(session, user, str(approved.get("crawl_id") or ""), "editor")
    if crawl.workspace_id != apr.workspace_id:
        raise NotFound(f"approval {approval_id} not found")
    rows = _impacted_enabled(session, crawl, [s["id"] for s in approved.get("schedules") or []])
    payload = _pause_payload(crawl, rows)  # current revisions: an edited schedule changes the hash
    verify_for_execution(session, approval_id, payload=payload, plan_hash=None)
    consume(session, apr)
    paused = []
    for s in rows:
        if s.enabled:
            s.enabled, s.next_run_at, s.revision = False, None, (s.revision or 1) + 1
            paused.append(s.id)
    audit(f"user:{user.id}", "schedule.paused_for_change", workspace_id=crawl.workspace_id, target=crawl.id,
          decision="allow", details={"approval_id": approval_id, "schedules": paused}, session=session)
    emit(crawl.workspace_id, "schedule.paused_for_change", {"crawl_id": crawl.id, "approval_id": approval_id,
                                                            "schedules": paused, "at": utcnow().isoformat()},
         actor=f"user:{user.id}", session=session)
    return {"paused": paused, "approval_id": approval_id}

