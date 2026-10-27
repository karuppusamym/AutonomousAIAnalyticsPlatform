"""AT-05 / AT-13 (spec S01, ADR-009) on Postgres: row-level security isolates tenants for every
tenant-owned table, the fill trigger derives tenant_id from the parent row, a tenant cannot write into
another tenant's workspace, the API answers 404 across tenants, and audit_event is append-only."""
from __future__ import annotations

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def two_tenants(control_db):
    """Tenant B with its own admin and workspace next to the seeded default tenant."""
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Tenant, User, Workspace
    from analystos.security.auth import hash_password
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"b-{tb[-6:]}", name="Tenant B"))
    with tenant_scope(tb), session_scope() as s:
        user = User(id=new_id("usr"), email=f"admin-{tb}@b.test", name="B admin",
                    password_hash=hash_password(PASSWORD), is_admin=True)
        s.add(user)
        s.flush()
        ws_b = Workspace(id=new_id("ws"), name="B workspace", created_by=user.id)
        s.add(ws_b)
        s.flush()
        run_b = AnalysisRun(id=new_id("run"), workspace_id=ws_b.id, objective="b only", requested_by=user.id)
        s.add(run_b)
        ids = {"tenant_b": tb, "user_b": user.id, "email_b": user.email, "ws_b": ws_b.id, "run_b": run_b.id}
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        admin_a = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws_a = Workspace(id=new_id("ws"), name="A workspace", created_by=admin_a.id)
        s.add(ws_a)
        s.flush()
        from analystos.db.models import WorkspaceMember

        s.add(WorkspaceMember(workspace_id=ws_a.id, user_id=admin_a.id, role="owner"))
        ids["ws_a"] = ws_a.id
    return ids


def test_every_tenant_table_has_rls_policy_and_trigger(control_db):
    from analystos.db.base import get_engine
    from analystos.db.models import Base
    from analystos.tenancy.ddl import rls_status
    from analystos.tenancy.schema import tenant_sources

    tables = list(tenant_sources(Base.metadata))
    with get_engine().connect() as c:
        status = rls_status(c, tables)
    bad = {t: s for t, s in status.items() if not all(s.values())}
    assert set(status) == set(tables) and bad == {}


def test_trigger_derives_tenant_from_workspace_and_parent(two_tenants):
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        run_tenant = s.execute(text("SELECT tenant_id FROM analysis_run WHERE id = :r"), {"r": two_tenants["run_b"]}).scalar()
        ws_tenant = s.execute(text("SELECT tenant_id FROM workspace WHERE id = :w"), {"w": two_tenants["ws_b"]}).scalar()
    assert run_tenant == ws_tenant == two_tenants["tenant_b"]


def test_a_tenant_sees_only_its_own_rows(two_tenants):
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, User, Workspace
    from analystos.tenancy import DEFAULT_TENANT_ID, tenant_scope

    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        ws = set(s.scalars(select(Workspace.id)))
        runs = set(s.scalars(select(AnalysisRun.id)))
        users = set(s.scalars(select(User.email)))
    assert two_tenants["ws_a"] in ws and two_tenants["ws_b"] not in ws
    assert two_tenants["run_b"] not in runs
    assert two_tenants["email_b"] not in users
    with tenant_scope(two_tenants["tenant_b"]), session_scope() as s:
        assert set(s.scalars(select(Workspace.id))) == {two_tenants["ws_b"]}


def test_a_tenant_cannot_write_into_another_tenants_workspace(two_tenants):
    from sqlalchemy.exc import ProgrammingError

    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun
    from analystos.tenancy import DEFAULT_TENANT_ID, tenant_scope

    with pytest.raises(ProgrammingError, match="row-level security"), tenant_scope(DEFAULT_TENANT_ID),             session_scope() as s:
        s.add(AnalysisRun(id=new_id("run"), workspace_id=two_tenants["ws_b"], objective="cross-tenant",
                          requested_by=two_tenants["user_b"]))


def test_strict_mode_unbound_session_sees_nothing(two_tenants, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Workspace

    monkeypatch.setattr(get_settings(), "tenancy_mode", "strict")
    with session_scope() as s:
        assert list(s.scalars(select(Workspace.id))) == []


def test_api_is_isolated_across_tenants(two_tenants):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as api:
        def login(email):
            r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        a, b = login("admin@analystos.local"), login(two_tenants["email_b"])
        assert api.get(f"/api/workspaces/{two_tenants['ws_b']}", headers=a).status_code == 404
        assert api.get(f"/api/workspaces/{two_tenants['ws_a']}", headers=b).status_code == 404
        listed_a = {w["id"] for w in api.get("/api/workspaces", headers=a).json()}
        assert two_tenants["ws_b"] not in listed_a and two_tenants["ws_a"] in listed_a
        me = api.get("/api/tenants/me", headers=b).json()
        assert me["current"]["id"] == two_tenants["tenant_b"]
        assert api.get("/api/tenants/me", headers={**a, "X-Tenant-Id": two_tenants["tenant_b"]}).status_code == 403


def test_audit_event_is_append_only(control_db):
    from sqlalchemy.exc import DBAPIError

    from analystos.db.base import session_scope
    from analystos.governance.audit import audit
    from analystos.tenancy import system_scope

    audit("test", "tenancy.audit_probe", decision="allow")
    for stmt in ("UPDATE audit_event SET actor = 'x' WHERE action = 'tenancy.audit_probe'",
                 "DELETE FROM audit_event WHERE action = 'tenancy.audit_probe'"):
        with pytest.raises(DBAPIError, match="append-only"), system_scope(), session_scope() as s:
            s.execute(text(stmt))
