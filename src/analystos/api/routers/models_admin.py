"""Model gateway administration (spec S05 §6): effective layered configuration, providers with health,
a provider test, and versioned tenant / workspace overrides with If-Match and rollback.

Platform administrators see and test providers. Tenant administrators (and workspace owners, for their
workspace) read the effective configuration and write overrides, which can only narrow (llm.layers).
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.http import expected_revision, set_etag
from analystos.core.errors import InvalidInput
from analystos.db.models import User
from analystos.services import model_config

router = APIRouter(prefix="/api/admin/models", tags=["admin", "models"])

Scope = Literal["tenant", "workspace"]


class OverrideIn(BaseModel):
    document: dict[str, Any]
    note: str = ""


class RollbackIn(BaseModel):
    version: int
    scope: Scope = "tenant"
    workspace: str | None = None


def _tenant_for(session: Session, user: User, scope: str, workspace: str | None) -> tuple[str, str | None]:
    """(tenant id, workspace id) the request addresses. A workspace names its own tenant; otherwise the
    request's bound tenant (TenantMiddleware), else the caller's home tenant."""
    from analystos.tenancy import DEFAULT_TENANT_ID, current_tenant

    if scope == "workspace" or workspace:
        if not workspace:
            raise InvalidInput("scope=workspace needs ?workspace=<id>")
        tenant = model_config.workspace_tenant(session, workspace)
        return tenant, workspace
    return current_tenant() or user.tenant_id or DEFAULT_TENANT_ID, None


def _require_reader(session: Session, user: User, tenant: str, workspace: str | None) -> None:
    model_config.require_access(session, user, tenant, workspace, what="reading the model configuration")


@router.get("/effective")
def effective(response: Response, workspace: str | None = None, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """The resolved configuration (file ⊕ platform ⊕ tenant ⊕ workspace) with the source layer of each
    value, excluded models with the reason, and per purpose the models that may serve it."""
    tenant, ws = _tenant_for(session, user, "workspace" if workspace else "tenant", workspace)
    _require_reader(session, user, tenant, ws)
    resolved = model_config.effective(session, tenant_id=tenant, workspace_id=ws)
    scope = "workspace" if ws else "tenant"
    version = model_config.current_version(session, scope=scope, tenant_id=tenant, workspace_id=ws)
    set_etag(response, version)
    return {"tenant_id": tenant, "workspace_id": ws, "scope": scope, "version": version, **resolved.view()}


@router.get("/config")
def get_override(response: Response, scope: Scope = "tenant", workspace: str | None = None,
                 user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """The scope's own override document (its latest version) and its history."""
    tenant, ws = _tenant_for(session, user, scope, workspace)
    _require_reader(session, user, tenant, ws)
    items = model_config.history(session, scope=scope, tenant_id=tenant, workspace_id=ws)
    version = items[0]["version"] if items else 0
    set_etag(response, version)
    return {"scope": scope, "tenant_id": tenant, "workspace_id": ws, "version": version,
            "document": items[0]["document"] if items else {}, "history": items}


@router.put("/config")
def put_override(body: OverrideIn, response: Response, scope: Scope = Query("tenant"), workspace: str | None = None,
                 if_match: str | None = Header(default=None), user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """Replace the scope's override as a new version. If-Match is required ("0" before the first version)."""
    tenant, ws = _tenant_for(session, user, scope, workspace)
    out = model_config.put(session, session.merge(user), scope=scope, tenant_id=tenant, workspace_id=ws,
                           document=body.document, expected_version=expected_revision(if_match, required=True),
                           note=body.note)
    set_etag(response, out["version"])
    return out


@router.post("/config/rollback")
def rollback(body: RollbackIn, response: Response, user: User = Depends(current_user),
             session: Session = Depends(db, scope="function")):
    tenant, ws = _tenant_for(session, user, body.scope, body.workspace)
    out = model_config.rollback(session, session.merge(user), scope=body.scope, tenant_id=tenant, workspace_id=ws,
                                version=body.version)
    set_etag(response, out["version"])
    return out


@router.get("/providers")
def providers(_: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    """Every configured provider: type, region, egress, declared capabilities (keys are referenced by
    environment variable name, never shown), the profiles that use it, and health (key present, last
    success/error, cooldown, today's spend, 24 h calls / error rate / latency p50 and p95)."""
    from analystos.services.model_health import providers_view

    return providers_view(session)


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: str, _: User = Depends(admin_user)):
    """One live, billable ping with a fixed prompt through the router (allowlists, spend caps and the
    model_call record all apply)."""
    from analystos.services.model_health import test_provider as run_test

    return run_test(provider_id)
