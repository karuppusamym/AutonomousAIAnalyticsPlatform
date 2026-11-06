from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.serialize import row, rows
from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput
from analystos.db.models import (
    AnalysisRun,
    Artifact,
    AuditEvent,
    Insight,
    Relationship,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    WorkspaceMember,
)
from analystos.events.bus import list_events
from analystos.governance.policy import get_workspace, load_policy, require_role
from analystos.services import file_ingest, workspace_inventory, workspace_modes
from analystos.services import sources as source_svc
from analystos.services import workspaces as ws_svc

router = APIRouter(prefix="/api", tags=["workspaces"])
# Files and file databases (sqlite/duckdb kinds read them in place, inside the upload directory only).
UPLOAD_EXTENSIONS = (".csv", ".tsv", ".json", ".ndjson", ".jsonl", ".parquet", ".xlsx", ".db", ".sqlite", ".sqlite3",
                     ".duckdb")


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)  # the column's width: a longer name was a 500 (self-check 2026-10-30)
    description: str = ""
    objective: str = ""
    autonomy_level: int = 3
    policy: dict | None = None
    work_modes: list[str] | None = None


class WorkspacePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    objective: str | None = None
    autonomy_level: int | None = None
    settings: dict | None = None
    status: str | None = None  # active | disabled; owner only


class WorkModesIn(BaseModel):
    modes: list[str]


class MemberIn(BaseModel):
    email: str
    role: str


class SourceIn(BaseModel):
    kind: str
    name: str
    config: dict = {}
    secret_ref: str | None = None


class SourceUpdateIn(BaseModel):
    """Only the fields sent change. The kind cannot change here (add a new source instead). Send
    `clear_secret_ref: true` to remove a secret reference; `secret_ref` on its own only sets one."""

    name: str | None = None
    config: dict | None = None
    secret_ref: str | None = None
    clear_secret_ref: bool = False


class Selection(BaseModel):
    assets: list[str]


class TagsIn(BaseModel):
    tags: list[str]


class ColumnMetadataIn(BaseModel):
    """Only the fields sent change; an empty string clears one (and the crawler leaves it cleared)."""

    business_name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)


_COUNTED_ARTIFACTS = ("query", "dataset", "metric", "chart", "dashboard")
_IN_CHUNK = 500


def _summaries(session: Session, workspaces: list) -> list[dict]:
    """Workspaces with their counts, in four grouped queries per chunk of workspaces rather than eight
    per workspace (P4-S05: at 1,000 workspaces the per-workspace counts made the list take ~6 s alone
    and time out under concurrent load)."""
    counts: dict[str, dict[str, int]] = {ws.id: {k: 0 for k in (*_COUNTED_ARTIFACTS, "runs", "verified_insights", "sources")}
                                         for ws in workspaces}
    ids = list(counts)
    for i in range(0, len(ids), _IN_CHUNK):
        chunk = ids[i:i + _IN_CHUNK]
        for ws_id, kind, n in session.execute(select(Artifact.workspace_id, Artifact.type, func.count()).where(
                Artifact.workspace_id.in_(chunk), Artifact.type.in_(_COUNTED_ARTIFACTS)).group_by(Artifact.workspace_id, Artifact.type)):
            counts[ws_id][kind] = n
        for key, model, extra in (("runs", AnalysisRun, None), ("verified_insights", Insight, Insight.status == "verified"),
                                  ("sources", Source, None)):
            stmt = select(model.workspace_id, func.count()).where(model.workspace_id.in_(chunk))
            if extra is not None:
                stmt = stmt.where(extra)
            for ws_id, n in session.execute(stmt.group_by(model.workspace_id)):
                counts[ws_id][key] = n
    return [{**row(ws), "counts": counts[ws.id]} for ws in workspaces]


def _summary(session: Session, ws) -> dict:
    return _summaries(session, [ws])[0]


@router.post("/workspaces")
def create(body: WorkspaceIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    ws = ws_svc.create_workspace(session, session.merge(user), **body.model_dump(exclude={"work_modes"}))
    if body.work_modes is not None:
        workspace_modes.apply(session, user, ws.id, body.work_modes)
    session.flush()
    return row(ws)


@router.get("/workspaces")
def list_(user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    summaries = _summaries(session, ws_svc.list_workspaces(session, user))
    roles = {} if user.is_admin or not summaries else dict(session.execute(
        select(WorkspaceMember.workspace_id, WorkspaceMember.role).where(
            WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id.in_([w["id"] for w in summaries]))).all())
    return [{**w, "role": "owner" if user.is_admin else roles.get(w["id"])} for w in summaries]


@router.get("/workspaces/{workspace_id}")
def get(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    role = require_role(session, user, workspace_id, "viewer")
    ws = get_workspace(session, workspace_id)
    return {**_summary(session, ws), "role": role, "policy": load_policy(session, ws).model_dump(),
            "members": [{"user_id": m.user_id, "role": m.role, "email": u.email, "name": u.name} for m, u in session.execute(
                select(WorkspaceMember, User).join(User, User.id == WorkspaceMember.user_id)
                .where(WorkspaceMember.workspace_id == workspace_id)).all()]}


@router.patch("/workspaces/{workspace_id}")
def patch(workspace_id: str, body: WorkspacePatch, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return row(ws_svc.update_workspace(session, user, workspace_id, body.model_dump()))


@router.get("/workspaces/{workspace_id}/work-modes")
def get_work_modes(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    role = require_role(session, user, workspace_id, "viewer")
    ws = get_workspace(session, workspace_id)
    current = workspace_modes.current(ws)
    if role == "owner" or user.is_admin:
        return workspace_modes.preview(session, user, workspace_id, current)
    return {"workspace_id": workspace_id, "current": current, "selected": current, "capabilities": [],
            "note": "Modes configure work shortcuts and playbook enablement; data grants and roles remain separate."}


@router.post("/workspaces/{workspace_id}/work-modes/preview")
def preview_work_modes(workspace_id: str, body: WorkModesIn, user: User = Depends(current_user),
                       session: Session = Depends(db, scope="function")):
    return workspace_modes.preview(session, user, workspace_id, body.modes)


@router.put("/workspaces/{workspace_id}/work-modes")
def put_work_modes(workspace_id: str, body: WorkModesIn, user: User = Depends(current_user),
                   session: Session = Depends(db, scope="function")):
    return workspace_modes.apply(session, user, workspace_id, body.modes)


@router.delete("/workspaces/{workspace_id}")
def delete(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Archive and disable. This route does not erase stored or published data."""
    ws_svc.delete_workspace(session, user, workspace_id)
    return {"deleted": True, "archived": True, "purged": False}


@router.get("/workspaces/{workspace_id}/inventory")
def get_inventory(workspace_id: str, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    """Read-only inventory of retained records and registered resource identifiers; owner only."""
    return workspace_inventory.inventory(session, user, workspace_id)


@router.put("/workspaces/{workspace_id}/policy")
def put_policy(workspace_id: str, body: dict, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return {"policy_version": ws_svc.set_policy(session, user, workspace_id, body)}


@router.post("/workspaces/{workspace_id}/members")
def add_member(workspace_id: str, body: MemberIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    m = ws_svc.add_member(session, user, workspace_id, body.email, body.role)
    return {"user_id": m.user_id, "role": m.role}


@router.delete("/workspaces/{workspace_id}/members/{user_id}")
def remove_member(workspace_id: str, user_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    ws_svc.remove_member(session, user, workspace_id, user_id)
    return {"removed": True}


# --------------------------------------------------------------------------------------- sources
@router.post("/workspaces/{workspace_id}/sources")
def add_source(workspace_id: str, body: SourceIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    src = source_svc.register_source(session, user, workspace_id, **body.model_dump())
    session.flush()
    return row(src)


@router.get("/workspaces/{workspace_id}/sources")
def list_sources(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(session.scalars(select(Source).where(Source.workspace_id == workspace_id).order_by(Source.created_at)))


@router.patch("/workspaces/{workspace_id}/sources/{source_id}")
def edit_source(workspace_id: str, source_id: str, body: SourceUpdateIn, user: User = Depends(current_user),
                session: Session = Depends(db, scope="function")):
    """Rename a source or correct its connection (host, port, tables, other config, secret reference). The
    kind cannot change; add a new source for a different connector. Changing the connection marks the
    source `registered` again so the catalog shows it has not been checked against it yet."""
    kwargs: dict = {}
    if body.name is not None:
        kwargs["name"] = body.name
    if body.config is not None:
        kwargs["config"] = body.config
    if body.clear_secret_ref:
        kwargs["clear_secret_ref"] = True
    elif body.secret_ref is not None:
        kwargs["secret_ref"] = body.secret_ref
    src = source_svc.update_source(session, user, workspace_id, source_id, **kwargs)
    session.flush()
    return row(src)


@router.post("/workspaces/{workspace_id}/sources/{source_id}/discover")
def discover(workspace_id: str, source_id: str, user: User = Depends(current_user)):
    return source_svc.discover_source(user, source_id, workspace_id)


@router.put("/workspaces/{workspace_id}/sources/{source_id}/selection")
def select_assets(workspace_id: str, source_id: str, body: Selection, background: BackgroundTasks,
                  user: User = Depends(current_user)):
    # newly selected tables are profiled by a background crawl (the same mechanism as POST .../crawl)
    return source_svc.select_assets(user, source_id, body.assets, workspace_id, schedule=background.add_task)


@router.post("/workspaces/{workspace_id}/sources/{source_id}/ingest")
def ingest(workspace_id: str, source_id: str, body: file_ingest.IngestSpec, user: User = Depends(current_user)):
    """Map and load an uploaded file (CSV, JSON, Excel, Parquet) into a file source: replace, append or merge (P6-06)."""
    return file_ingest.ingest_file(user, source_id, body, workspace_id)


@router.post("/workspaces/{workspace_id}/uploads")
async def upload(workspace_id: str, file: UploadFile = File(...), user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "editor")
    name = (file.filename or "upload.csv").replace("/", "_").replace("..", "_")
    if not name.lower().endswith(UPLOAD_EXTENSIONS):
        raise InvalidInput(f"only {', '.join(UPLOAD_EXTENSIONS)} uploads")
    target = get_settings().upload_dir / workspace_id
    target.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise InvalidInput("file larger than 200 MB")
    (target / name).write_bytes(data)
    return {"path": str(target / name), "bytes": len(data)}


@router.get("/workspaces/{workspace_id}/assets")
def assets(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    out = []
    for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id).order_by(SourceAsset.name)):
        cols = session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
        out.append({**row(a), "fq": f"{a.schema_name}.{a.name}", "columns": rows(cols)})
    return out


@router.put("/assets/{asset_id}/columns/{column}/tags")
def tag(asset_id: str, column: str, body: TagsIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return row(source_svc.tag_column(session, user, asset_id, column, body.tags))


@router.patch("/assets/{asset_id}/columns/{column}/metadata")
def curate_column(asset_id: str, column: str, body: ColumnMetadataIn, user: User = Depends(current_user),
                  session: Session = Depends(db, scope="function")):
    """A person's business name and description for a column; the crawler never overwrites them."""
    return row(source_svc.curate_column(session, user, asset_id, column, body.model_dump(exclude_unset=True)))


@router.get("/workspaces/{workspace_id}/relationships")
def relationships(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Every recorded join with where it came from (`origin`), its measured confidence and, for a validated one, the
    join as a runnable value-free statement (`join_sql`) in the source's dialect."""
    from analystos.db.models import Source
    from analystos.governance.policy import source_dialect
    from analystos.skills.relationships import join_sql

    require_role(session, user, workspace_id, "viewer")
    assets = {a.id: a for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id))}
    names = {i: f"{a.schema_name}.{a.name}" for i, a in assets.items()}
    dialects = {s.id: source_dialect(s.kind, s.execution_mode)
                for s in session.scalars(select(Source).where(Source.workspace_id == workspace_id))}

    def sql_of(r: Relationship) -> str | None:
        ev = r.evidence or {}
        f, t = names.get(r.from_asset_id), names.get(r.to_asset_id)
        fc, tc = list(ev.get("from_columns") or [r.from_column]), list(ev.get("to_columns") or [r.to_column])
        if not (r.validated and f and t) or ev.get("rejected") or len(fc) != len(tc):
            return None
        try:
            return join_sql(f, fc, t, tc, dialects.get(assets[r.from_asset_id].source_id, "postgres"))
        except Exception:  # noqa: BLE001 - an unusual name or dialect: the row is still listed, without SQL
            return None

    rels = session.scalars(select(Relationship).where(Relationship.workspace_id == workspace_id).order_by(Relationship.id))
    return [{**row(r), "from_asset": names.get(r.from_asset_id), "to_asset": names.get(r.to_asset_id), "join_sql": sql_of(r)}
            for r in rels]


@router.get("/workspaces/{workspace_id}/activity")
def activity(workspace_id: str, after_id: int = 0, limit: int = 200, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "viewer")
    return rows(list_events(session, workspace_id=workspace_id, after_id=after_id, limit=min(limit, 1000)))


@router.get("/workspaces/{workspace_id}/audit")
def audit_log(workspace_id: str, limit: int = 200, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    require_role(session, user, workspace_id, "owner")
    return rows(session.scalars(select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
                                .order_by(AuditEvent.id.desc()).limit(min(limit, 2000))))
