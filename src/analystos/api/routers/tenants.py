"""Tenants (ADR-009, spec S01): who am I acting for, and platform-admin tenant creation."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from analystos.api.deps import admin_user, current_user
from analystos.core.errors import Conflict
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Tenant, TenantMember, User
from analystos.governance.audit import audit
from analystos.tenancy import current_tenant, system_scope
from analystos.tenancy.resolve import invalidate, user_tenants

router = APIRouter(prefix="/api", tags=["tenants"])


class NewTenant(BaseModel):
    slug: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=200)
    data_residency: str | None = Field(default=None, max_length=20)


def _view(t: Tenant) -> dict:
    return {"id": t.id, "slug": t.slug, "name": t.name, "status": t.status, "data_residency": t.data_residency}


@router.get("/tenants/me")
def my_tenants(user: User = Depends(current_user)):
    """The tenant this request acts for (pick another with the X-Tenant-Id header) and every tenant
    the caller may act for."""
    home, allowed = user_tenants(user.id)
    active = current_tenant() or home
    with system_scope(), session_scope() as s:  # names of the caller's own tenants only
        rows = {t.id: _view(t) for t in s.scalars(select(Tenant).where(Tenant.id.in_(allowed)))}
    return {"current": rows.get(active), "home": home, "tenants": sorted(rows.values(), key=lambda t: t["slug"])}


@router.post("/admin/tenants", status_code=201)
def create_tenant(body: NewTenant, admin: User = Depends(admin_user)):
    """Platform administrators create tenants; the creator becomes the tenant's first admin."""
    tid = new_id("tnt")
    with system_scope(), session_scope() as s:
        if s.scalar(select(Tenant).where(Tenant.slug == body.slug)):
            raise Conflict("tenant slug exists")
        tenant = Tenant(id=tid, slug=body.slug, name=body.name, data_residency=body.data_residency)
        s.add(tenant)
        s.flush()
        s.add(TenantMember(tenant_id=tid, user_id=admin.id, role="tenant_admin"))
        audit(f"user:{admin.id}", "tenant.created", target=tid, decision="allow", session=s)
        view = _view(tenant)
    invalidate(admin.id)
    return view

