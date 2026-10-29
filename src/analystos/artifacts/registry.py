"""Artifact registry + lineage (ART-001..003). Every query, profile, dataset, metric, chart,
dashboard and report is persisted and versioned; provenance is a generic edge list that the
graph projection mirrors into Neo4j."""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from sqlalchemy import Integer, String, and_, case, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased

from analystos.core.ids import new_id, ordered_utcnow, stable_hash
from analystos.db.models import AnalysisRun, Artifact, ArtifactVersion, Insight, LineageEdge, QueryExecution

ARTIFACT_TYPES = {"query", "profile", "quality_report", "relationship_map", "context_package", "plan", "dataset",
                  "semantic_model", "metric", "chart", "dashboard", "report", "narrative", "python_script", "ml_model",
                  "forecast", "alert", "schedule", "export", "data_quality_rule", "notebook", "transformation",
                  "agent_output", "step_result", "ml_spec", "ml_split_manifest", "ml_trials", "ml_evaluation", "ml_model_card",
                  "process_analysis", "report_document"}

# (run_id, plan_version) of the task currently executing; set by the engine around each task.
producing_plan: ContextVar[tuple[str, int] | None] = ContextVar("producing_plan", default=None)


def _plan_version(session: Session, run_id: str | None) -> int | None:
    if run_id is None:
        return None
    producing = producing_plan.get()
    if producing and producing[0] == run_id:
        return producing[1]
    run = session.get(AnalysisRun, run_id)
    return run.plan_version if run is not None else None


def current_plan_filter(run: AnalysisRun):
    """Artifacts written under the run's current plan version (unstamped rows predate stamping)."""
    return or_(Artifact.plan_version == run.plan_version, Artifact.plan_version.is_(None))


def save_artifact(session: Session, *, workspace_id: str, type_: str, name: str, content: dict[str, Any],
                  run_id: str | None = None, creator_agent: str | None = None, creator_user: str | None = None,
                  status: str = "draft") -> Artifact:
    """Upsert by (workspace, run, type, name): unchanged content is a no-op, changed content is a new version.

    An agent writing inside a run passes the ``artifact.write`` tool gate (workspace ``tool_denylist``,
    role, autonomy; spec v2 §6) and its manifest's ``output_contract`` (the type must be declared and the
    content must match the declared schema, else OutputContractViolation and nothing is written).
    Writes by a signed-in user are governed by their route's role check.
    Run artifacts are stamped with the writing task's plan version; a task of a superseded plan cannot
    overwrite what the current plan already wrote."""
    if type_ not in ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type {type_}")
    if creator_agent and run_id:
        from analystos.capabilities.agents import contract_for, enforce_output
        from analystos.tools.registry import gate_agent_write

        gate_agent_write(session, workspace_id=workspace_id, run_id=run_id, agent_id=creator_agent,
                         inputs={"type": type_, "name": name})
        # The agent's output contract (FND-006), as the run bound it: declared type, declared schema.
        enforce_output(contract_for(session.get(AnalysisRun, run_id), creator_agent), creator_agent, type_, content)
    content_hash = stable_hash(content)
    plan_version = _plan_version(session, run_id)
    existing = session.scalar(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.run_id == run_id,
                                                     Artifact.type == type_, Artifact.name == name))
    if existing:
        if plan_version is not None and (existing.plan_version or 0) > plan_version:
            return existing
        if plan_version is not None:
            existing.plan_version = plan_version
        if existing.content_hash != content_hash:
            existing.version += 1
            existing.content = content
            existing.content_hash = content_hash
            existing.status = status
            session.add(ArtifactVersion(artifact_id=existing.id, version=existing.version, content=content,
                                        content_hash=content_hash, created_by=creator_agent or creator_user))
        return existing
    # Stamped per row, not with the transaction time: an agent writes its metrics or charts in one
    # transaction, and every `ORDER BY created_at` reader must get them back in the order written.
    artifact = Artifact(id=new_id("art"), workspace_id=workspace_id, run_id=run_id, type=type_, name=name, version=1,
                        plan_version=plan_version, status=status, creator_agent=creator_agent, creator_user=creator_user,
                        content=content, content_hash=content_hash, created_at=ordered_utcnow())
    session.add(artifact)
    session.flush()
    session.add(ArtifactVersion(artifact_id=artifact.id, version=1, content=content, content_hash=content_hash,
                                created_by=creator_agent or creator_user))
    return artifact


def node_ref(session: Session, node: tuple[str, str], run_id: str | None = None) -> tuple[str, str]:
    """The lineage key of a node. A finding is keyed by its id: its code (I-1) restarts every run, so
    ("insight", "I-1") would join every run's first finding. A code is resolved within `run_id`."""
    kind, ref = node[0], str(node[1])
    if kind == "insight" and run_id and not ref.startswith("ins_"):
        found = session.scalar(select(Insight.id).where(Insight.run_id == run_id, Insight.code == ref))
        if found:
            return kind, found
    return kind, ref


def link(session: Session, workspace_id: str, from_: tuple[str, str], relation: str, to: tuple[str, str],
         run_id: str | None = None) -> None:
    from_, to = node_ref(session, from_, run_id), node_ref(session, to, run_id)
    stmt = insert(LineageEdge).values(workspace_id=workspace_id, run_id=run_id, from_type=from_[0], from_id=from_[1],
                                      relation=relation, to_type=to[0], to_id=to[1])
    session.execute(stmt.on_conflict_do_nothing(index_elements=["workspace_id", "from_type", "from_id", "relation",
                                                                 "to_type", "to_id"]))


def link_queries(session: Session, workspace_id: str, from_: tuple[str, str], query_ids: list[str], *,
                 run_id: str | None = None, assets: list[str] | None = None) -> None:
    """`from_` derived_from each query, and each query reads every table the gateway recorded for it
    (a join reads two; the spec's asset alone would miss one). `assets` covers a query not recorded."""
    ids = [str(q) for q in query_ids if q]
    if not ids:
        return
    seen = {q.id: q.referenced_assets or [] for q in session.scalars(select(QueryExecution).where(QueryExecution.id.in_(ids)))}
    for q in ids:
        link(session, workspace_id, from_, "derived_from", ("query", q), run_id=run_id)
        for fq in sorted(set(seen.get(q) or []) | set(assets or [])):
            link(session, workspace_id, ("query", q), "reads", ("table", fq), run_id=run_id)


_FAMILY = (*sorted(ARTIFACT_TYPES), "artifact")


def _canonical(t):  # noqa: ANN001, ANN202 - SQL expression
    """An artifact is one node whether an edge names it ("artifact", id) or (its type, id) (artifact ids are
    unique): the walk keys every artifact-typed end as ("artifact", id)."""
    return case((t.in_(_FAMILY), literal("artifact", String)), else_=t)


def _is(end_type, end_id, t, i):  # noqa: ANN001, ANN202 - SQL expressions; each branch uses an edge index
    return or_(and_(t != "artifact", end_type == t, end_id == i), and_(t == "artifact", end_type.in_(_FAMILY), end_id == i))


def _edges_named(session: Session, workspace_id: str, where: Any, *, limit: int | None = None) -> list[tuple]:
    """Edges matching `where`, an ("artifact", id) end named by the artifact's type (one statement)."""
    e, fa, ta = LineageEdge, aliased(Artifact), aliased(Artifact)
    stmt = (select(e.id, e.from_type, e.from_id, e.relation, e.to_type, e.to_id, fa.type, ta.type).where(where)
            .outerjoin(fa, and_(e.from_type == "artifact", fa.id == e.from_id, fa.workspace_id == workspace_id))
            .outerjoin(ta, and_(e.to_type == "artifact", ta.id == e.to_id, ta.workspace_id == workspace_id))
            .order_by(e.id))
    return [((ft or f, fi), rel, (tt or t, ti))
            for _, f, fi, rel, t, ti, ft, tt in session.execute(stmt.limit(limit) if limit else stmt)]


def _graph(anchor: tuple[str, str], edges: list[tuple]) -> dict[str, Any]:
    if anchor[0] in _FAMILY:  # one name for the anchor artifact: its own type when any edge knows it
        if anchor[0] == "artifact":
            anchor = next((n for f, _, t in edges for n in (f, t) if n[1] == anchor[1] and n[0] in ARTIFACT_TYPES), anchor)

        def same(n: tuple[str, str]) -> tuple[str, str]:
            return anchor if n[1] == anchor[1] and n[0] in _FAMILY else n

        edges = [(same(f), r, same(t)) for f, r, t in edges]
    nodes = {anchor} | {n for f, _, t in edges for n in (f, t)}
    return {"nodes": [{"type": t, "id": i} for t, i in sorted(nodes)],
            "edges": [{"from": list(f), "relation": rel, "to": list(t)} for f, rel, t in edges]}


def lineage_for(session: Session, workspace_id: str, node: tuple[str, str], *, depth: int = 6) -> dict[str, Any]:
    """Upstream + downstream provenance around a node: every edge touching a node within `depth - 1`
    hops (either direction), and those edges' ends. One recursive CTE over this workspace's edges
    (P4-S02); it reads the neighbourhood only, never the whole workspace. An artifact written as
    ("artifact", id) is the same node as (its type, id), and the result names it by its type."""
    node = (node[0], str(node[1]))
    if depth <= 0:
        return {"nodes": [{"type": node[0], "id": node[1]}], "edges": []}
    e = LineageEdge

    def touches(t, i):  # noqa: ANN001, ANN202 - either end of a workspace edge (both ends are indexed)
        return and_(e.workspace_id == workspace_id, or_(_is(e.from_type, e.from_id, t, i), _is(e.to_type, e.to_id, t, i)))

    start = "artifact" if node[0] in _FAMILY else node[0]
    reach = select(literal(start, String).label("t"), literal(node[1], String).label("i"),
                   literal(0, Integer).label("d")).cte("reach", recursive=True)
    from_end = _is(e.from_type, e.from_id, reach.c.t, reach.c.i)
    reach = reach.union(select(case((from_end, _canonical(e.to_type)), else_=_canonical(e.from_type)),
                               case((from_end, e.to_id), else_=e.from_id), reach.c.d + 1)
                        .join_from(reach, e, touches(reach.c.t, reach.c.i)).where(reach.c.d < depth - 1))
    near = select(reach.c.t, reach.c.i).distinct().subquery("near")
    touching = select(e.id).join_from(near, e, touches(near.c.t, near.c.i))
    return _graph(node, _edges_named(session, workspace_id, e.id.in_(touching)))


def run_lineage(session: Session, workspace_id: str, run_id: str, *, limit: int = 500) -> dict[str, Any]:
    """Every edge a run recorded (objective, hypotheses, experiments, queries, tables, findings, datasets,
    charts, reports), shaped like `lineage_for`. A walk from the run node would leave the run through
    shared tables into other runs; the run's own edges are its lineage."""
    e = LineageEdge
    edges = _edges_named(session, workspace_id, and_(e.workspace_id == workspace_id, e.run_id == run_id), limit=limit)
    return {**_graph(("run", run_id), edges), "truncated": len(edges) >= limit}
