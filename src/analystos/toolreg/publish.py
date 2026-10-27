"""Publish-as-tool (S10-T2, FR-046): a certified metric, verified query, dataset or playbook becomes a typed,
signed tool.

The input schema is derived from what the source allows (a metric's declared dimensions and time
dimensions, a verified query's typed parameters, a dataset's declared columns, a playbook's objective),
with `additionalProperties: false`: a caller supplies names and literal values, never SQL. The execution
reference binds the exact source version (metric version, template hash, artifact content hash, definition
version); a source that changed after publishing makes the tool stale (refused at invoke, shown in health).
Re-publishing unchanged content is idempotent; a new source version is a new major version.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.contracts.semantic import FilterOp, TimeGrain
from analystos.contracts.tool_manifest import ToolExecutionSpec, ToolExposure, ToolLimits, ToolManifestV2
from analystos.core.errors import Conflict, InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import stable_hash
from analystos.db.models import Artifact, RegisteredTool, User, VerifiedQuery
from analystos.governance.policy import require_role
from analystos.skills.catalog import screen_text
from analystos.toolreg import store

SOURCE_TYPES = ("metric", "verified_query", "dataset", "playbook")
FAMILY = {"metric": "metric", "verified_query": "verified_query", "dataset": "dataset", "playbook": "playbook"}
PUBLISH_ROLE = "editor"
METRIC_MAX_ROWS = 5000  # SemanticQuery.limit ceiling
FILTER_OPS = list(FilterOp.__args__)  # type: ignore[attr-defined]
GRAINS = list(TimeGrain.__args__)  # type: ignore[attr-defined]
DATASET_OPS = ["=", "!=", "<", "<=", ">", ">=", "in", "is_null", "is_not_null"]


class PublishOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,79}$")  # tool id is <family>.<name>
    title: str | None = Field(default=None, max_length=300)
    description: str | None = Field(default=None, max_length=2000)
    owner: str | None = Field(default=None, max_length=120)
    dimensions: list[str] | None = None  # metric: the allowed subset of its dimensions
    columns: list[str] | None = None  # dataset: the allowed subset of its columns
    limits: ToolLimits | None = None
    exposure: ToolExposure | None = None


def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", (text or "").lower())).strip("_")[:80] or "tool"


def _desc(text: str | None) -> str:
    return screen_text(text or "", max_chars=2000)  # data shown to callers and models, never instructions


def _limits(opts: PublishOptions, *, max_rows_cap: int | None = None) -> ToolLimits:
    lim = opts.limits or ToolLimits()
    if max_rows_cap is not None and lim.max_rows > max_rows_cap:
        lim = lim.model_copy(update={"max_rows": max_rows_cap})
    return lim


def _exposure(opts: PublishOptions, tool_id: str) -> ToolExposure:
    exp = opts.exposure or ToolExposure()
    family = tool_id.split(".", 1)[0]
    return exp.model_copy(update={"grants_required": exp.grants_required or [tool_id, f"{family}.*"]})


def _subset(wanted: list[str] | None, allowed: list[str], what: str) -> list[str]:
    if wanted is None:
        return list(allowed)
    unknown = sorted(set(wanted) - set(allowed))
    if unknown:
        raise InvalidInput(f"{what} {', '.join(unknown)} are not allowed by the source (allowed: {', '.join(allowed) or 'none'})")
    return [a for a in allowed if a in wanted]


def _enum_array(values: list[str], max_items: int) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "enum": values}, "uniqueItems": True, "maxItems": max_items}


ROWS_OUTPUT = {"type": "object", "properties": {
    "columns": {"type": "array", "items": {"type": "string"}}, "rows": {"type": "array"}, "row_count": {"type": "integer"},
    "truncated": {"type": "boolean"}, "governed": {"const": True}, "query_id": {"type": "string"},
    "sql_hash": {"type": "string"}, "result_hash": {"type": "string"}, "freshness": {"type": "object"}}}


# ------------------------------------------------------------------------------------ sources
def _metric(session: Session, ws: str, ref: str, opts: PublishOptions) -> tuple[ToolManifestV2, dict[str, Any], int]:
    from analystos.semantic.compiler import _is_time, load_catalog
    from analystos.semantic.service import approved_metrics, metric_rows

    approved = approved_metrics(session, ws)
    row = approved.get(ref)
    if row is None:
        if metric_rows(session, ws, name=ref):
            raise PolicyDenied(f"metric {ref} has no approved version: only a certified metric can be published as a tool")
        raise NotFound(f"metric {ref} not found")
    catalog = load_catalog(session, ws)
    if catalog is None:
        raise PolicyDenied("publishing a metric needs an approved semantic model in this workspace")
    defn = dict(row.definition or {})
    declared = [d for d in defn.get("dimensions") or [] if isinstance(d, str)]
    times = [d for d in declared if _is_time(catalog, defn, d)]
    dims = _subset(opts.dimensions, [d for d in declared if d not in times], "dimensions")
    fields = dims + times
    lim = _limits(opts, max_rows_cap=METRIC_MAX_ROWS)
    props: dict[str, Any] = {}
    if dims:
        props["dimensions"] = _enum_array(dims, min(10, len(dims)))
    if fields:
        props["filters"] = {"type": "array", "maxItems": 20, "items": {
            "type": "object", "additionalProperties": False, "required": ["field", "op"],
            "properties": {"field": {"type": "string", "enum": fields}, "op": {"type": "string", "enum": FILTER_OPS},
                           "value": {"type": ["string", "number", "boolean", "array", "null"],
                                     "items": {"type": ["string", "number", "boolean"]}, "maxItems": 200}}}}
    if times:
        props["grain"] = {"type": "string", "enum": GRAINS}
        props["time_dimension"] = {"type": "string", "enum": times}
        props["period"] = {"type": "object", "additionalProperties": False, "properties": {
            "start": {"type": "string", "format": "date"}, "end": {"type": "string", "format": "date"}}}
    props["limit"] = {"type": "integer", "minimum": 1, "maximum": lim.max_rows}
    tool_id = f"metric.{opts.name or _slug(row.name)}"
    m = ToolManifestV2(
        tool_id=tool_id, version="0.0.0", kind="data_product", owner=opts.owner or (row.owner_id or "workspace"),
        title=opts.title or row.display_name or row.name,
        description=_desc(opts.description or defn.get("description") or f"Certified metric {row.name}"),
        input_schema={"type": "object", "additionalProperties": False, "properties": props},
        output_schema={**ROWS_OUTPUT, "properties": {**ROWS_OUTPUT["properties"], "metric_version": {"type": "integer"}}},
        side_effect="none", permissions=["dataset.read"], data_classification_max="internal_aggregate",
        execution=ToolExecutionSpec(via="semantic_compiler", ref={
            "metric": row.name, "metric_version": row.version, "metric_id": row.id, "definition_hash": row.content_hash,
            "dimensions": dims, "time_dimensions": times}),
        limits=lim, exposure=_exposure(opts, tool_id))
    source = {"type": "metric", "id": row.id, "name": row.name, "version": row.version, "content_hash": row.content_hash}
    return m, source, row.version


VQ_TYPES = {"number": {"type": "number"}, "date": {"type": "string", "format": "date"},
            "string": {"type": "string", "maxLength": 200}}


def _verified_query(session: Session, ws: str, ref: str, opts: PublishOptions) -> tuple[ToolManifestV2, dict[str, Any], None]:
    vq = session.scalar(select(VerifiedQuery).where(VerifiedQuery.workspace_id == ws,
                                                    or_(VerifiedQuery.id == ref, VerifiedQuery.name == ref)))
    if vq is None:
        raise NotFound(f"verified query {ref} not found")
    if vq.status != "active":
        raise PolicyDenied(f"verified query {vq.name} is {vq.status}: only an active verified query can be published")
    props: dict[str, Any] = {}
    for p in vq.parameters or []:
        kind = p.get("type") or "string"
        if kind == "enum" and p.get("values"):
            props[p["name"]] = {"type": "string", "enum": [str(v) for v in p["values"]][:500]}
        else:
            props[p["name"]] = dict(VQ_TYPES.get(kind, VQ_TYPES["string"]))
        if p.get("description"):
            props[p["name"]]["description"] = _desc(p["description"])[:300]
    tool_id = f"verified_query.{opts.name or _slug(vq.name)}"
    template_hash = stable_hash(vq.sql_template)
    m = ToolManifestV2(
        tool_id=tool_id, version="0.0.0", kind="data_product", owner=opts.owner or vq.created_by,
        title=opts.title or vq.name, description=_desc(opts.description or vq.description or f"Verified query {vq.name}"),
        input_schema={"type": "object", "additionalProperties": False, "properties": props, "required": sorted(props)},
        output_schema=ROWS_OUTPUT, side_effect="none", permissions=["dataset.read"], data_classification_max="internal",
        execution=ToolExecutionSpec(via="verified_query", source=vq.source_id, ref={
            "verified_query_id": vq.id, "template_hash": template_hash}),
        limits=_limits(opts), exposure=_exposure(opts, tool_id))
    return m, {"type": "verified_query", "id": vq.id, "name": vq.name, "content_hash": template_hash}, None


def _dataset(session: Session, ws: str, ref: str, opts: PublishOptions) -> tuple[ToolManifestV2, dict[str, Any], int]:
    from analystos.contracts.bi import DatasetDef

    a = session.scalar(select(Artifact).where(Artifact.workspace_id == ws, Artifact.type == "dataset",
                                              or_(Artifact.id == ref, Artifact.name == ref))
                       .order_by(Artifact.version.desc()).limit(1))
    if a is None:
        raise NotFound(f"dataset {ref} not found")
    if a.status != "published":
        raise PolicyDenied(f"dataset {a.name} is {a.status}: only a published dataset can be published as a tool")
    d = DatasetDef.model_validate(a.content)
    declared = [str(c["name"]) for c in d.columns if isinstance(c, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                                                                                      str(c.get("name") or ""))]
    if not declared:
        raise InvalidInput(f"dataset {a.name} declares no columns: a dataset tool exposes declared columns only")
    cols = _subset(opts.columns, declared, "columns")
    lim = _limits(opts)
    props = {"columns": _enum_array(cols, len(cols)),
             "filters": {"type": "array", "maxItems": 20, "items": {
                 "type": "object", "additionalProperties": False, "required": ["column", "op"],
                 "properties": {"column": {"type": "string", "enum": cols}, "op": {"type": "string", "enum": DATASET_OPS},
                                "value": {"type": ["string", "number", "boolean", "array", "null"],
                                          "items": {"type": ["string", "number", "boolean"]}, "maxItems": 200}}}},
             "order_by": {"type": "array", "maxItems": 5, "items": {
                 "type": "object", "additionalProperties": False, "required": ["column"],
                 "properties": {"column": {"type": "string", "enum": cols},
                                "direction": {"type": "string", "enum": ["asc", "desc"]}}}},
             "limit": {"type": "integer", "minimum": 1, "maximum": lim.max_rows}}
    tool_id = f"dataset.{opts.name or _slug(a.name)}"
    m = ToolManifestV2(
        tool_id=tool_id, version="0.0.0", kind="data_product", owner=opts.owner or (a.creator_user or "workspace"),
        title=opts.title or a.name, description=_desc(opts.description or d.description or f"Published dataset {a.name}"),
        input_schema={"type": "object", "additionalProperties": False, "properties": props},
        output_schema=ROWS_OUTPUT, side_effect="none", permissions=["dataset.read"], data_classification_max="internal",
        execution=ToolExecutionSpec(via="dataset", ref={"artifact_id": a.id, "artifact_version": a.version,
                                                        "content_hash": a.content_hash, "columns": cols}),
        limits=lim, exposure=_exposure(opts, tool_id))
    return m, {"type": "dataset", "id": a.id, "name": a.name, "version": a.version, "content_hash": a.content_hash}, a.version


PLAYBOOK_INPUT = {"type": "object", "additionalProperties": False, "required": ["objective"], "properties": {
    "objective": {"type": "string", "minLength": 10, "maxLength": 4000},
    "source_ids": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 50}}}
PLAYBOOK_OUTPUT = {"type": "object", "properties": {"run_id": {"type": "string"}, "status": {"type": "string"},
                                                    "workflow_id": {"type": ["string", "null"]}}}


def _playbook(session: Session, ws: str, ref: str, opts: PublishOptions) -> tuple[ToolManifestV2, dict[str, Any], int]:
    from analystos.capabilities import registry as cap_registry
    from analystos.services.definitions import latest_published

    key = ref.removeprefix("playbook.")
    d = latest_published(session, ws, "playbook", key)
    if d is not None:
        exec_ref = {"definition_key": d.key, "definition_version": d.version, "definition_id": d.id,
                    "content_hash": d.content_hash}
        source = {"type": "definition", "id": d.id, "name": d.key, "version": d.version, "content_hash": d.content_hash}
        title, summary, version, owner = d.title or d.key, (d.spec or {}).get("summary") or "", d.version, d.created_by
    else:
        cap = cap_registry.current().manifests.get(f"playbook.{key}")
        if cap is None or cap.kind != "Playbook":
            raise NotFound(f"playbook {key} not found (no published workspace definition or built-in playbook)")
        if cap.certification.status != "certified":
            raise PolicyDenied(f"playbook {cap.ref} is {cap.certification.status}: only a certified playbook can be published")
        exec_ref = {"capability": cap.id, "capability_version": cap.version}
        source = {"type": "capability", "id": cap.id, "name": cap.id, "version": cap.version}
        title, summary, version, owner = cap.id, cap.summary, int(cap.version.split(".")[0]), "platform"
    tool_id = f"playbook.{opts.name or _slug(key)}"
    m = ToolManifestV2(
        tool_id=tool_id, version="0.0.0", kind="playbook", owner=opts.owner or owner, title=opts.title or title,
        description=_desc(opts.description or summary or f"Run playbook {key}"), input_schema=PLAYBOOK_INPUT,
        output_schema=PLAYBOOK_OUTPUT, side_effect="write_internal", permissions=["run.start"],
        data_classification_max="internal", execution=ToolExecutionSpec(via="playbook", ref=exec_ref),
        limits=_limits(opts), exposure=_exposure(opts, tool_id))
    return m, source, version


def _identity(source: dict[str, Any]) -> tuple[Any, Any]:
    """What a tool id is bound to across versions: a metric or playbook by name (each version is a new row),
    a verified query or dataset by id."""
    kind = source.get("type")
    return kind, source.get("name") if kind in ("metric", "definition", "capability") else source.get("id")


BUILDERS = {"metric": _metric, "verified_query": _verified_query, "dataset": _dataset, "playbook": _playbook}


def publish(session: Session, user: User, workspace_id: str, source_type: str, ref: str,
            options: PublishOptions | None = None) -> tuple[RegisteredTool, bool]:
    """(row, created). A certified source becomes a signed, certified tool version."""
    if source_type not in BUILDERS:
        raise InvalidInput(f"source type must be one of {', '.join(SOURCE_TYPES)}")
    require_role(session, user, workspace_id, PUBLISH_ROLE)
    candidate, source, source_version = BUILDERS[source_type](session, workspace_id, str(ref or "").strip(), options or PublishOptions())
    for other in store.versions(session, workspace_id, candidate.tool_id):
        if _identity(other.source or {}) != _identity(source):
            raise Conflict(f"tool id {candidate.tool_id} is already published from another {source_type}; pass a name")
    version, existing = store.next_version(session, workspace_id, candidate, source_version)
    if existing is not None:
        return existing, False
    manifest = candidate.model_copy(update={"version": version})
    row = store.register(session, workspace_id, ToolManifestV2.model_validate(manifest.model_dump()), status="certified",
                         source=source, actor=f"user:{user.id}")
    return row, True
