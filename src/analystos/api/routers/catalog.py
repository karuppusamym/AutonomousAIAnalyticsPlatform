"""Data catalog: source kinds, metadata crawls, curated asset metadata."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row
from analystos.context.lookup import guidelines_of
from analystos.core.errors import InvalidInput
from analystos.core.ids import utcnow
from analystos.db.models import CrawlRun, Source, SourceAsset, SourceColumn, User
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role
from analystos.services import crawler

router = APIRouter(prefix="/api", tags=["catalog"])


def public_profile(profile: dict | None, tags: list[str] | None, semantics: dict | None = None) -> dict | None:
    """Only aggregate completeness and cardinality leave the server for a sensitive column (tagged, or classified
    as personal data): the profiling sanitizer, which also strips value distributions stored by older crawlers."""
    from analystos.skills.profiling import column_is_sensitive, sanitize_column_profile

    if not profile:
        return None
    if column_is_sensitive(tags, semantics):
        return sanitize_column_profile(profile, sensitive=True)
    return profile


@router.get("/source-kinds")
def source_kinds(user: User = Depends(current_user)):
    """Every database/file/API kind the platform can connect, with the fields a form needs,
    whether its driver is installed and enabled by the administrator, and whether it is certified
    (a dated live-run evidence file exists; connectors/certification.py)."""
    from analystos.connectors import certification, kinds
    from analystos.services.platform_settings import get as platform

    enabled = set(platform().sources.enabled_kinds)
    certs = certification.statuses()
    return [{**k.model_dump(include={"kind", "label", "category", "required", "optional", "default_port", "docs"}),
             "secret_field": k.secret.field if k.secret else None,
             "execution_mode": kinds.execution_mode_for(k.kind), "dialect": kinds.dialect_for(k.kind),
             "driver_installed": kinds.driver_available(k.kind),
             "install_hint": (f"pip install 'analystos[{k.driver.extra}]'" if k.driver.extra else
                              ("pip install " + " ".join(k.driver.packages) if k.driver.packages else None)),
             "enabled": not enabled or k.kind in enabled,
             "certified": certs[k.kind]["status"] == "certified", "certification": certs[k.kind]}
            for k in kinds.list_kinds()]


class CrawlIn(BaseModel):
    mode: str | None = None  # full | incremental (default: admin crawl.default_mode)
    include: list[str] | None = None
    exclude: list[str] | None = None
    profile: bool | None = None
    enrich: bool | None = None


@router.post("/workspaces/{workspace_id}/sources/{source_id}/crawl")
def start_crawl(workspace_id: str, source_id: str, body: CrawlIn, background: BackgroundTasks,
                user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    load_in_workspace(session, Source, source_id, workspace_id, user=user, label="source")
    run = crawler.start_crawl(session, user, source_id, **body.model_dump())
    view = crawler.crawl_view(run)
    session.commit()  # the background task reads the crawl_run row in its own session: it must be visible first
    from analystos.workflows.orchestrator import start_crawl_job

    if start_crawl_job(run.id, user.id) is None:  # local orchestrator, or Temporal unreachable
        background.add_task(_run_quietly, run.id, user.id)
    return view


def _run_quietly(crawl_id: str, user_id: str) -> None:
    try:
        crawler.run_crawl(crawl_id, user_id)
    except Exception:  # recorded on the crawl_run by run_crawl; logged here so nothing fails silently
        logging.getLogger(__name__).exception("background crawl %s failed", crawl_id)


# ------------------------------------------------------------------------ crawler sources (P4-K06)
class QueryHistoryIn(BaseModel):
    source_ids: list[str] | None = None


class SupersetCrawlIn(BaseModel):
    include: list[str] | None = None  # glob patterns on dataset/chart/dashboard names


@router.post("/workspaces/{workspace_id}/knowledge/crawl/query-history")
def crawl_query_history(workspace_id: str, body: QueryHistoryIn, user: User = Depends(current_user),
                        session: Session = Depends(db, scope="function")):
    """Mine the workspace's governed query audit for join paths, columns, filters and groupings
    (structure only, never values) into the knowledge pack."""
    from analystos.services import knowledge_ingest

    return knowledge_ingest.query_history(session, session.merge(user), workspace_id, source_ids=body.source_ids)


@router.post("/workspaces/{workspace_id}/knowledge/crawl/dbt-manifest")
async def crawl_dbt_manifest(workspace_id: str, file: UploadFile = File(...), user: User = Depends(current_user),
                             session: Session = Depends(db, scope="function")):
    """Ingest a dbt manifest.json (v12+): model and source documents, tests, lineage, catalog descriptions."""
    from analystos.services import knowledge_ingest

    data = await file.read(knowledge_ingest.MAX_MANIFEST_BYTES + 1)
    return knowledge_ingest.ingest_dbt_manifest(session, session.merge(user), workspace_id, data)


@router.post("/workspaces/{workspace_id}/knowledge/crawl/superset")
def crawl_superset(workspace_id: str, body: SupersetCrawlIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Read Superset datasets, charts and dashboards (GET only) into the knowledge pack."""
    from analystos.services import knowledge_ingest

    return knowledge_ingest.superset_metadata(session, session.merge(user), workspace_id, include=body.include)


@router.post("/workspaces/{workspace_id}/knowledge/documents")
async def upload_knowledge_document(workspace_id: str, file: UploadFile = File(...), user: User = Depends(current_user),
                                    session: Session = Depends(db, scope="function")):
    """Upload a Markdown, text or PDF document; it becomes draft knowledge sections for review."""
    from analystos.knowledge.documents import MAX_UPLOAD_BYTES
    from analystos.services import knowledge_ingest

    data = await file.read(MAX_UPLOAD_BYTES + 1)
    return knowledge_ingest.upload_document(session, session.merge(user), workspace_id, file.filename or "document", data)


# ------------------------------------------------------------------------ knowledge sources (P2-10)
class KnowledgeSourceIn(BaseModel):
    kind: str = Field(pattern="^(github|confluence)$")
    name: str = Field(min_length=1, max_length=200)
    config: dict = Field(default_factory=dict)  # location filter only; credentials go in secret_ref
    secret_ref: str | None = Field(default=None, max_length=200)
    enabled: bool = True


class KnowledgeSourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    config: dict | None = None
    secret_ref: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


@router.get("/workspaces/{workspace_id}/knowledge/sources")
def list_knowledge_sources(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Read-only GitHub / Confluence knowledge sources of the workspace and their last sync."""
    from analystos.services import knowledge_sources

    return knowledge_sources.list_sources(session, session.merge(user), workspace_id)


@router.post("/workspaces/{workspace_id}/knowledge/sources")
def create_knowledge_source(workspace_id: str, body: KnowledgeSourceIn, user: User = Depends(current_user),
                            session: Session = Depends(db, scope="function")):
    from analystos.services import knowledge_sources

    return knowledge_sources.create_source(session, session.merge(user), workspace_id, **body.model_dump())


@router.patch("/workspaces/{workspace_id}/knowledge/sources/{source_id}")
def update_knowledge_source(workspace_id: str, source_id: str, body: KnowledgeSourcePatch, user: User = Depends(current_user),
                            session: Session = Depends(db, scope="function")):
    from analystos.services import knowledge_sources

    return knowledge_sources.update_source(session, session.merge(user), workspace_id, source_id,
                                           body.model_dump(exclude_unset=True))


@router.delete("/workspaces/{workspace_id}/knowledge/sources/{source_id}")
def delete_knowledge_source(workspace_id: str, source_id: str, user: User = Depends(current_user),
                            session: Session = Depends(db, scope="function")):
    from analystos.services import knowledge_sources

    return knowledge_sources.delete_source(session, session.merge(user), workspace_id, source_id)


@router.post("/workspaces/{workspace_id}/knowledge/sources/{source_id}/sync")
def sync_knowledge_source(workspace_id: str, source_id: str, user: User = Depends(current_user),
                          session: Session = Depends(db, scope="function")):
    """Sync now: fetch the source and merge the files into the pack as untrusted drafts (a person reviews them)."""
    from analystos.db.models import KnowledgeSource
    from analystos.services import knowledge_sources

    load_in_workspace(session, KnowledgeSource, source_id, workspace_id, user=user, minimum="editor", label="knowledge source")
    session.commit()  # the sync works in its own short transactions
    return knowledge_sources.sync_source(source_id, actor=f"user:{user.id}")


@router.get("/workspaces/{workspace_id}/crawls")
def list_crawls(workspace_id: str, source_id: str | None = None, limit: int = 50, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    stmt = select(CrawlRun).where(CrawlRun.workspace_id == workspace_id)
    if source_id:
        stmt = stmt.where(CrawlRun.source_id == source_id)
    return [crawler.crawl_view(r) for r in session.scalars(stmt.order_by(CrawlRun.started_at.desc()).limit(min(limit, 200)))]


@router.get("/crawls/{crawl_id}")
def get_crawl(crawl_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    r = load_in_workspace(session, CrawlRun, crawl_id, user=user, label="crawl")
    return crawler.crawl_view(r)


@router.get("/crawls/{crawl_id}/impact")
def crawl_impact(crawl_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """What reads the columns this crawl saw removed, retyped or deprecated (P1-5): downstream columns along
    recorded lineage, metrics, verified queries, dashboards, monitors and schedules. Read-only."""
    from analystos.services import change_impact

    load_in_workspace(session, CrawlRun, crawl_id, user=user, label="crawl")
    return change_impact.crawl_impact(session, user, crawl_id)


class PauseIn(BaseModel):
    schedule_ids: list[str] = Field(default_factory=list, max_length=200)


@router.post("/crawls/{crawl_id}/impact/pause")
def request_schedule_pause(crawl_id: str, body: PauseIn, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    """Request approval to pause impacted schedules. Nothing changes until the approval is decided and executed."""
    from analystos.services import change_impact

    load_in_workspace(session, CrawlRun, crawl_id, user=user, minimum="editor", label="crawl")
    return change_impact.request_pause(session, session.merge(user), crawl_id, body.schedule_ids)


@router.post("/approvals/{approval_id}/execute-schedule-pause")
def execute_schedule_pause(approval_id: str, user: User = Depends(current_user),
                           session: Session = Depends(db, scope="function")):
    """Pause exactly the approved schedules after `verify_for_execution` re-checks the hash binding."""
    from analystos.db.models import Approval
    from analystos.services import change_impact

    load_in_workspace(session, Approval, approval_id, user=user, minimum="editor", label="approval")
    return change_impact.execute_pause(session, session.merge(user), approval_id)


class PiiLineageIn(BaseModel):
    dbt_manifest: dict | None = None  # optional: a dbt manifest.json whose column lineage joins the recorded one
    sql: list[str] = Field(default_factory=list, max_length=50)  # optional CREATE/INSERT ... SELECT statements
    dialect: str = "postgres"
    propose: bool = True


@router.post("/workspaces/{workspace_id}/lineage/pii-scan")
def pii_scan(workspace_id: str, body: PiiLineageIn, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    """Find untagged columns that receive values of pii / restricted / sensitive columns along column lineage and
    queue review drafts that add the tag (P1-5). Never removes or applies a tag."""
    from analystos.evidence.lineage import propagation as prop
    from analystos.governance.policy import resolve_scope
    from analystos.services import pii_lineage

    supplied = bool(body.dbt_manifest or body.sql)
    # drafting is an editor's action; caller-supplied lineage is analyst work; reading the recorded result is a viewer's
    minimum = "editor" if body.propose else "analyst" if supplied else "viewer"
    scope = resolve_scope(session, user, workspace_id, minimum_role=minimum)
    extra = prop.edges_from_dbt_manifest(body.dbt_manifest, body.dialect) if body.dbt_manifest else []
    if body.sql:
        # unqualified columns resolve against what this caller may read (as POST /recipes/lineage does)
        catalog = {asset: [c for c in cols if f"{asset}.{c}" not in scope.denied_columns]
                   for asset, cols in scope.columns.items()}
        for stmt in body.sql:
            extra += prop.edges_from_sql(stmt, body.dialect, catalog=catalog or None)
    # only gaps whose every catalogued column this caller may read are returned or drafted
    return pii_lineage.scan(session, workspace_id, actor=f"user:{user.id}", extra_edges=extra,
                            propose_drafts=body.propose, scope=scope)


@router.get("/workspaces/{workspace_id}/catalog")
def catalog(workspace_id: str, q: str = "", domain: str | None = None, role: str | None = None, include_deprecated: bool = False,
            user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Searchable catalog built by the crawler (names, business names, descriptions, role/domain). Three queries
    whatever the size: assets, all their columns, their relationships (plus the brief). A column's unit and alias
    show the brief's reviewed value when a person set one (`unit_origin: brief`), else the crawler's rule."""
    from collections import defaultdict

    from analystos.db.models import Relationship
    from analystos.quality.service import asset_trust
    from analystos.semantic.suggest import _time_column
    from analystos.services import brief as brief_svc

    require_role(session, user, workspace_id, "viewer")
    stmt = select(SourceAsset).where(SourceAsset.workspace_id == workspace_id)
    if not include_deprecated:
        stmt = stmt.where(SourceAsset.lifecycle == "active")
    needle = q.strip().lower()
    assets = [a for a in session.scalars(stmt.order_by(SourceAsset.schema_name, SourceAsset.name))
              if not (domain and (a.semantics or {}).get("domain") != domain or role and (a.semantics or {}).get("role") != role)]
    ids = [a.id for a in assets]
    cols_by: dict[str, list[SourceColumn]] = defaultdict(list)
    rels: dict[str, dict[str, int]] = defaultdict(lambda: {"outgoing": 0, "incoming": 0, "validated": 0})
    if ids:
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(ids))
                                 .order_by(SourceColumn.asset_id, SourceColumn.ordinal)):
            cols_by[c.asset_id].append(c)
        for r in session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id)):
            rels[r.from_asset_id]["outgoing"] += 1
            rels[r.to_asset_id]["incoming"] += 1
            for side in (r.from_asset_id, r.to_asset_id):
                rels[side]["validated"] += int(bool(r.validated))
    brief = brief_svc.assertions_of(brief_svc.head(session, workspace_id))
    curated = {(a.field, a.subject): a.value for a in brief if a.effective and a.subject
               and (a.group, a.field) in (("time_measures", "unit"), ("time_measures", "currency"), ("domain", "alias"))}
    event_time = {a.subject: a.value for a in brief if a.effective and a.group == "time_measures" and a.field == "event_time"}
    trust = asset_trust(session, workspace_id) if assets else {}  # P1-7: declared rules' latest results, one query
    out = []
    for a in assets:
        sem = a.semantics or {}
        fq = f"{a.schema_name}.{a.name}"
        cols = cols_by[a.id]
        hay = " ".join([a.name, a.business_name or "", a.description or "", *(c.name for c in cols),
                        *(c.business_name or "" for c in cols)]).lower()
        if needle and needle not in hay:
            continue
        out.append({"id": a.id, "fq": fq, "source_id": a.source_id, "name": a.name,
                    "business_name": a.business_name, "business_name_origin": a.business_name_origin,
                    "description": a.description, "description_origin": a.description_origin,
                    "reviewed": a.reviewed, "selected": a.selected, "lifecycle": a.lifecycle, "row_count": a.row_count,
                    "role": sem.get("role"), "domain": sem.get("domain"), "grain": sem.get("grain"),
                    "confidence": sem.get("confidence"), "last_crawled_at": a.last_crawled_at,
                    "entity": sem.get("entity"), "profile_meta": (a.stats or {}).get("profile_meta"),
                    "time_column": event_time.get(fq) or _time_column(cols),
                    "relationships": dict(rels[a.id]) if a.id in rels else {"outgoing": 0, "incoming": 0, "validated": 0},
                    "model_description_draft": sem.get("model_description_draft"),
                    "guidelines": guidelines_of(a),
                    "renamed_to": sem.get("renamed_to"), "renamed_from": sem.get("renamed_from"),
                    "snapshot": a.snapshot or None,  # staged population: rows staged vs origin, truncated, sampling
                    "quality": trust.get(fq),  # declared-rule trust signal (P1-7): level, pass counts, by dimension
                    "columns": [_column_view(c, fq, curated, profiled=bool(a.stats)) for c in cols]})
    return out


def _column_view(c: SourceColumn, fq: str, curated: dict, *, profiled: bool) -> dict:
    sem = c.semantics or {}
    ref = f"{fq}.{c.name}"
    unit = curated.get(("unit", ref)) or curated.get(("currency", ref))
    return {"name": c.name, "data_type": c.data_type, "business_name": c.business_name,
            "business_name_origin": c.business_name_origin, "description": c.description,
            "description_origin": c.description_origin, "tags": c.tags, "tags_origin": c.tags_origin,
            "semantic_type": c.semantic_type, "is_key": c.is_key,
            "profile": public_profile(c.profile, c.tags, sem) if profiled else None,
            "references": (c.profile or {}).get("references"),
            "role": sem.get("semantic_role"), "unit": unit or sem.get("unit"),
            "unit_origin": "brief" if unit else ("rule" if sem.get("unit") else None),
            "alias": curated.get(("alias", ref)), "pii": sem.get("pii"), "glossary": sem.get("glossary"),
            "polymorphic_reference": sem.get("polymorphic_reference")}


class AssetMetadataIn(BaseModel):
    business_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    reviewed: bool | None = None
    # Notes for agents reading this table ("prefer closed_at for resolution time"), served by context.lookup.
    # Human-curated only: an empty string clears them.
    guidelines: str | None = Field(default=None, max_length=4000)


@router.patch("/assets/{asset_id}/metadata")
def curate_asset(asset_id: str, body: AssetMetadataIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """A person's curation wins over every crawler/model description from now on."""
    from analystos.core.errors import PolicyDenied

    a = load_in_workspace(session, SourceAsset, asset_id, user=user, minimum="editor", label="asset")
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise InvalidInput("nothing to change")
    if "guidelines" in patch:
        if (user.attributes or {}).get("service_account"):  # an MCP client or other service identity
            raise PolicyDenied("asset guidelines are written by a person, not by a service account")
        text = patch["guidelines"].strip()
        sem = {k: v for k, v in (a.semantics or {}).items() if k != "guidelines"}
        a.semantics = {**sem, "guidelines": {"text": text, "by": user.id, "at": utcnow().isoformat()}} if text else sem
    if "business_name" in patch:
        a.business_name, a.business_name_origin = patch["business_name"].strip() or None, "user"
    if "description" in patch:
        a.description, a.description_origin = patch["description"].strip() or None, "user"
    if "reviewed" in patch:
        a.reviewed = patch["reviewed"]
    audit(f"user:{user.id}", "asset.curated", workspace_id=a.workspace_id, target=f"{a.schema_name}.{a.name}",
          details=patch, session=session)
    return {**row(a), "guidelines": guidelines_of(a)}
