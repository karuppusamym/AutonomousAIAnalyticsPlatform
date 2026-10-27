"""Tenants (ADR-009, spec S01): who am I acting for, platform-admin tenant creation, and the tenant's
tamper-evident audit log (S01-T5)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from analystos.api.deps import admin_user, current_user
from analystos.core.errors import Conflict, Forbidden, NotFound
from analystos.core.ids import canonical_json, new_id
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



# ------------------------------------------------------------------------------------- audit (S01-T5)
AUDIT_ROLES = ("tenant_admin", "tenant_auditor")


def _audit_reader(tenant_id: str, user: User) -> str:
    """Tenant admins and auditors of the tenant this request acts for (or platform admins). Another tenant's
    id answers 404, never 403: pick the tenant with X-Tenant-Id."""
    if tenant_id != current_tenant():
        raise NotFound("tenant not found")
    if user.is_admin:
        return tenant_id
    with session_scope() as s:
        role = s.scalar(select(TenantMember.role).where(TenantMember.tenant_id == tenant_id, TenantMember.user_id == user.id))
    if role not in AUDIT_ROLES:
        raise Forbidden("reading the audit log needs the tenant_admin or tenant_auditor role",
                        details={"rule": "audit_reader", "remedy": "ask a tenant admin for the tenant_auditor role"})
    return tenant_id


@router.get("/tenants/{tenant_id}/audit")
def audit_log(tenant_id: str, after_seq: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=10_000),
              action: str | None = Query(None, max_length=80), user: User = Depends(current_user)) -> Response:
    """Sealed audit events with their hash links, as JSON lines in chain order. Continue from the
    `X-Next-After-Seq` header. Each line verifies offline: sha256(prev_hash + "\n" + canonical event) = hash."""
    from analystos.governance.audit_chain import page

    _audit_reader(tenant_id, user)
    items, cursor = page(tenant_id, after_seq=after_seq, limit=limit, action=action)
    audit(f"user:{user.id}", "audit.read", target=tenant_id, decision="allow",
          details={"after_seq": after_seq, "returned": len(items), "action": action})
    body = "".join(canonical_json(i) + "\n" for i in items)
    return Response(content=body, media_type="application/x-ndjson", headers={"X-Next-After-Seq": str(cursor)})


@router.post("/tenants/{tenant_id}/audit/verify")
def audit_verify(tenant_id: str, user: User = Depends(current_user)) -> dict:
    """Seal what is pending and recompute the tenant's whole chain: `{ok, checked, head, first_bad, ...}`."""
    from analystos.governance.audit_chain import verify_and_record

    _audit_reader(tenant_id, user)
    return verify_and_record(tenant_id, f"user:{user.id}")
