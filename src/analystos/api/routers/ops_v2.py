"""Platform operations API (spec S12): the per-model evaluation matrix and spend (the cost ledger).

  GET /v2/admin/models/evaluations   latest score per profile x model on the golden suite; blocked model
                                     families are never listed as evaluated (S12 §3, FR-055)
  GET /v2/admin/spend?group_by=      spend rolled up by request, run, conversation, user, workspace, tenant, day,
                                     category, item (S12 §4, FR-052)
  GET /v2/admin/spend/export?month=  the month's ledger at full grain, CSV (streamed) or Parquet

Spend is tenant data: a tenant administrator reads their tenant (row-level security bounds the query to it),
a workspace owner one workspace; `scope=platform` (platform administrators) reads every tenant. Evaluations
hold no tenant data and are readable by any administrator.
"""
from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.core.errors import Forbidden, InvalidInput
from analystos.db.models import User
from analystos.ops import cost_ledger, model_matrix

router = APIRouter(prefix="/v2/admin", tags=["admin", "operations"])


def _tenant(user: User) -> str:
    from analystos.tenancy import DEFAULT_TENANT_ID, current_tenant

    return current_tenant() or user.tenant_id or DEFAULT_TENANT_ID


def _spend_access(session: Session, user: User, scope: str, workspace: str | None) -> None:
    from analystos.services.model_config import require_access

    if scope == "platform":
        if not user.is_admin:
            raise Forbidden("scope=platform needs a platform administrator")
        return
    if workspace:
        from analystos.services.model_config import workspace_tenant

        require_access(session, user, workspace_tenant(session, workspace), workspace, what="reading spend")
        return
    require_access(session, user, _tenant(user), None, what="reading spend")


@contextmanager
def _platform_session() -> Iterator[Session]:
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        yield s


def _bound_session(scope: str, tenant: str) -> Session:
    """A session whose transaction has already begun under the right identity (RLS binds the tenant at
    transaction start), for a response body iterated later in another context (StreamingResponse)."""
    from analystos.db import base
    from analystos.tenancy import system_scope, tenant_scope

    s = base.SessionLocal()
    with system_scope() if scope == "platform" else tenant_scope(tenant):
        s.connection()
    return s


@router.get("/models/evaluations")
def model_evaluations(profile: str | None = None, model: str | None = None, mode: Literal["live", "fake"] | None = None,
                      latest: bool = True, limit: int = Query(200, ge=1, le=1000), user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function")):
    from analystos.services.model_config import can_administer

    if not (user.is_admin or can_administer(session, user, _tenant(user), None)):
        raise Forbidden("model evaluations need a platform or tenant administrator")
    # model_matrix_result is a platform table (no tenant column, no RLS): the request session reads it.
    return model_matrix.matrix(session, profile=profile, model=model, mode=mode, latest=latest, limit=limit)


@router.get("/spend")
def spend(group_by: str = "day", since: date | None = None, until: date | None = None, days: int = Query(30, ge=1, le=400),
          workspace: str | None = None, category: list[str] | None = Query(None),
          scope: Literal["tenant", "platform"] = "tenant", limit: int = Query(1000, ge=1, le=cost_ledger.MAX_GROUPS),
          user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Spend per `group_by` (comma-separated dimensions), most expensive first, with per-category totals."""
    _spend_access(session, user, scope, workspace)
    if category and set(category) - {"model", "query", "compute", "storage"}:
        raise InvalidInput("category must be model, query, compute or storage")
    kw = dict(group_by=group_by, since=since, until=until, days=days, workspace_ids=[workspace] if workspace else None,
              categories=category, limit=limit)
    if scope == "platform":
        with _platform_session() as s:  # RLS binds at transaction start: a new session in system scope
            return {**cost_ledger.spend(s, **kw), "scope": "platform"}
    return {**cost_ledger.spend(session, **kw), "scope": "workspace" if workspace else "tenant"}


@router.get("/spend/export")
def spend_export(month: str, format: Literal["csv", "parquet"] = "csv", workspace: str | None = None,  # noqa: A002
                 scope: Literal["tenant", "platform"] = "tenant", user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """One month of the ledger (YYYY-MM): CSV streams batch by batch; Parquet is written to a temporary file."""
    _spend_access(session, user, scope, workspace)
    cost_ledger.month_bounds(month)
    kw = {"workspace_ids": [workspace] if workspace else None}
    name = f"cost-ledger-{month}.{format}"
    if format == "csv":
        s = _bound_session(scope, _tenant(user))  # the response streams after the request session has closed

        def body():
            try:
                yield from cost_ledger.export_csv(s, month, **kw)
            finally:
                s.rollback()
                s.close()
        return StreamingResponse(body(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})
    out = Path(tempfile.mkdtemp(prefix="analystos-spend-"))
    if scope == "platform":
        with _platform_session() as s:
            info = cost_ledger.export_file(s, month, out, "parquet", **kw)
    else:
        info = cost_ledger.export_file(session, month, out, "parquet", **kw)
    return FileResponse(info["path"], media_type="application/vnd.apache.parquet", filename=name)
