"""PII along column lineage (review 2026-11-05 P1-5): find tagged values that reached an untagged column.

Rules decide, nothing is applied. The edges are the column lineage the platform already recorded (recipe runs, dbt
build jobs) plus any dbt manifest or SQL a caller passes; the tags are the catalog's (crawler rules, owners). A gap —
a column of a catalogued asset that receives values of a pii / restricted / sensitive column without the tag — becomes:

* a `lineage.pii_gap_detected` event and an audit row (what the UI and the change feed see), and
* one `column_tag` review draft per column (`knowledge/suggestions.propose`) that ADDS the missing tags. A reviewer's
  approval unions them into the column's tags; nothing here ever removes a tag or writes a column directly. That is
  the crawler invariant (tags only tighten) with a person in the loop for the lineage inference.

A target that is not in the catalog (an output nobody crawled) is reported as `uncatalogued`; there is nothing to tag.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.db.models import BuildJob, RecipeRun, SourceAsset, SourceColumn
from analystos.evidence.lineage import propagation as prop

ORIGIN = "pii_lineage"
PROPOSER = "rule:pii_lineage"
MAX_RECIPE_RUNS = 200
MAX_BUILD_JOBS = 100
MAX_PROPOSALS = 200


def recorded_edges(session: Session, workspace_id: str) -> list[prop.ColumnEdge]:
    """Column edges of the latest materialized run of each recipe and of recent succeeded build jobs."""
    edges: list[prop.ColumnEdge] = []
    seen_recipes: set[str] = set()
    for run in session.scalars(select(RecipeRun).where(RecipeRun.workspace_id == workspace_id,
                                                        RecipeRun.mode == "materialize",
                                                        RecipeRun.status == "succeeded")
                               .order_by(RecipeRun.created_at.desc()).limit(MAX_RECIPE_RUNS)):
        if run.recipe_id in seen_recipes:
            continue
        seen_recipes.add(run.recipe_id)
        edges += prop.edges_from_recipe(run.lineage or [], run.outputs or {}, f"recipe_run:{run.id}")
    for job in session.scalars(select(BuildJob).where(BuildJob.workspace_id == workspace_id,
                                                       BuildJob.status == "succeeded")
                               .order_by(BuildJob.created_at.desc()).limit(MAX_BUILD_JOBS)):
        edges += prop.edges_from_openlineage(job.openlineage or [], f"build_job:{job.id}")
    return edges


def catalog_index(session: Session, workspace_id: str, assets: set[str]) -> tuple[dict[str, SourceAsset],
                                                                                  dict[prop.Col, SourceColumn]]:
    """Active catalog assets named `schema.table` (either the gateway schema or the crawl key) and their columns."""
    by_name: dict[str, SourceAsset] = {}
    if not assets:
        return {}, {}
    for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id,
                                                        SourceAsset.lifecycle == "active")):
        for name in {prop.norm_asset(f"{a.schema_name}.{a.name}"), prop.norm_asset((a.semantics or {}).get("source_key"))}:
            if name in assets and name not in by_name:
                by_name[name] = a
    cols: dict[prop.Col, SourceColumn] = {}
    ids = {a.id: n for n, a in by_name.items()}
    if ids:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(list(ids)))):
            cols[(ids[c.asset_id], c.name.lower())] = c
    return by_name, cols


def rejected_tags(session: Session, workspace_id: str, subject: str) -> set[str]:
    """Tags a reviewer already rejected for this column: never proposed again, whatever path the lineage takes now."""
    from analystos.db.models import KnowledgeSuggestion

    out: set[str] = set()
    for fields in session.scalars(select(KnowledgeSuggestion.fields).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.subject == subject,
            KnowledgeSuggestion.kind == "column_tag", KnowledgeSuggestion.status == "rejected")):
        value = ((fields or {}).get("tags") or {}).get("value") or []
        out |= {str(t).lower() for t in (value if isinstance(value, list) else [value])}
    return out


def _body(gap: prop.Gap) -> str:
    src = f"{gap.carried_from[0]}.{gap.carried_from[1]}"
    via = " → ".join([f"{gap.path[0].source[0]}.{gap.path[0].source[1]}"] +
                     [f"{e.target[0]}.{e.target[1]}" for e in gap.path]) if gap.path else src
    return (f"Column {gap.target[0]}.{gap.target[1]} receives values of {src}, which is tagged "
            f"{', '.join(gap.missing)}, but it is not tagged itself. Lineage: {via}. Approving adds the tag "
            f"{', '.join(gap.missing)}; no tag is removed.")


def scan(session: Session, workspace_id: str, *, actor: str, extra_edges: list[prop.ColumnEdge] | None = None,
         propose_drafts: bool = True) -> dict[str, Any]:
    """Run the check over the workspace's recorded lineage (plus `extra_edges`). Returns the gaps found and the drafts
    queued; emits one `lineage.pii_gap_detected` event when there is at least one gap."""
    from analystos.events.bus import emit
    from analystos.governance.audit import audit
    from analystos.knowledge.suggestions import field, propose

    edges = recorded_edges(session, workspace_id) + list(extra_edges or [])
    assets = {e.source[0] for e in edges} | {e.target[0] for e in edges}
    by_name, cols = catalog_index(session, workspace_id, assets)
    tags = {key: set(c.tags or []) for key, c in cols.items()}
    gaps = prop.propagate(edges, tags)
    found, uncatalogued, drafts = [], [], []
    for gap in gaps:
        entry = gap.as_dict()
        column = cols.get(gap.target)
        if column is None:
            uncatalogued.append(entry)
            continue
        asset = by_name[gap.target[0]]
        entry.update(asset_id=asset.id, column_name=column.name)
        found.append(entry)
        subject = f"column:{asset.id}:{column.name}"[:200]
        if set(gap.missing) <= rejected_tags(session, workspace_id, subject):
            entry["rejected_before"] = True
            continue
        if not propose_drafts or len(drafts) >= MAX_PROPOSALS:
            continue
        evidence = {"rule": "lineage_propagation", "path": entry["path"], "carried_from": entry["carried_from"]}
        draft = propose(session, workspace_id, kind="column_tag", subject=subject,
                        title=f"Tag {gap.target[0]}.{column.name} as {', '.join(gap.missing)}",
                        fields={"body": field(_body(gap), gap.confidence, source="rule", **evidence),
                                "tags": field(gap.missing, gap.confidence, source="rule", rule="lineage_propagation")},
                        origin=ORIGIN, proposed_by=PROPOSER, batch=f"pii_lineage:{workspace_id}")
        if draft is not None:
            entry["suggestion_id"] = draft.id
            drafts.append(draft.id)
    out = {"edges": len(edges), "gaps": found, "uncatalogued": uncatalogued, "proposed": drafts}
    if found or uncatalogued:
        emit(workspace_id, "lineage.pii_gap_detected",
             {"gaps": len(found), "uncatalogued": len(uncatalogued), "proposed": len(drafts),
              "columns": [f"{g['asset']}.{g['column']}" for g in found[:50]]}, actor=actor, session=session)
        audit(actor, "lineage.pii_gap_detected", workspace_id=workspace_id, decision="review_required",
              details={"gaps": len(found), "uncatalogued": len(uncatalogued), "proposed": drafts[:50]}, session=session)
    return out
