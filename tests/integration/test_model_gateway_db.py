"""S05 on Postgres under strict tenancy: tenant/workspace model overrides through the API (row-level security,
If-Match versions), the model-switch gate's 409 keeping its evaluations, and the router reading a workspace's
layers from a worker context with no bound tenant. Hermetic: suites run through a fake runner."""
from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module")
def tenant_b(control_db):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant, User, Workspace, WorkspaceMember
    from analystos.security.auth import hash_password
    from analystos.tenancy import system_scope, tenant_scope

    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"m-{tb[-6:]}", name="Models tenant"))
    with tenant_scope(tb), session_scope() as s:
        user = User(id=new_id("usr"), email=f"models-{tb}@b.test", name="B admin", password_hash=hash_password(PASSWORD),
                    is_admin=True)
        s.add(user)
        s.flush()
        ws = Workspace(id=new_id("ws"), name="B models", created_by=user.id)
        s.add(ws)
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
        return {"tenant": tb, "email": user.email, "ws": ws.id}


def test_overrides_are_tenant_isolated_versioned_and_gated(tenant_b, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.db.base import session_scope
    from analystos.db.models import ModelConfigOverride, ModelEvaluation
    from analystos.llm.config import load_models_config
    from analystos.services import model_config, model_switch
    from analystos.tenancy import system_scope

    cfg = load_models_config()
    allowed = [m for m in cfg.allowlist if m.split("/", 1)[0] not in cfg.blocked_families]
    coding = [m for m in cfg.profiles["coding"].models if m in allowed]
    assert len(coding) >= 2, "the coding profile needs two allowed models for this test"
    scores = {coding[0]: 1.0, coding[1]: 0.0}
    monkeypatch.setattr(model_switch, "router_runner", lambda **kw: {
        "status": "scored", "results": [{"case": i, "passed": scores.get(kw["model"], 1.0) >= 0.5, "problems": []}
                                        for i in range(len(kw["cases"]))]})
    model_config.invalidate()
    with TestClient(app) as api:
        r = api.post("/api/auth/login", json={"email": tenant_b["email"], "password": PASSWORD})
        b = {"Authorization": f"Bearer {r.json()['access_token']}"}
        a = {"Authorization": "Bearer " + api.post("/api/auth/login", json={"email": "admin@analystos.local",
                                                                             "password": PASSWORD}).json()["access_token"]}
        url = "/api/admin/models/config"
        r = api.put(url, params={"scope": "tenant"}, json={"document": {"budgets": {"per_day_usd": 1.0}}}, headers={**b, "If-Match": '"0"'})
        assert r.status_code == 200, r.text
        # The default tenant cannot see tenant B's workspace (404), nor its tenant override.
        assert api.get("/api/admin/models/effective", params={"workspace": tenant_b["ws"]}, headers=a).status_code == 404
        assert api.get(url, params={"scope": "tenant"}, headers=a).json()["version"] == 0

        # Moving the coding profile to a model that fails its suite is blocked (409); the scores survive.
        doc = {"profile_models": {"coding": [coding[1], coding[0]]}}
        blocked = api.put(url, params={"scope": "workspace", "workspace": tenant_b["ws"]}, json={"document": doc},
                          headers={**b, "If-Match": '"0"'})
        assert blocked.status_code == 409, blocked.text
        gate = blocked.json()["error"]["details"]["model_switch_gate"]
        assert {e["purpose"] for e in gate["evaluations"]} >= {"sql_generation"}
        with system_scope(), session_scope() as s:
            assert s.scalar(select(ModelEvaluation.id).where(ModelEvaluation.model == coding[1])) is not None
            assert s.scalar(select(ModelConfigOverride.id).where(ModelConfigOverride.workspace_id == tenant_b["ws"])) is None
        ok = api.put(url, params={"scope": "workspace", "workspace": tenant_b["ws"]},
                     json={"document": doc, "switch_override_reason": "owner accepts the cheaper model"},
                     headers={**b, "If-Match": '"0"'})
        assert ok.status_code == 200, ok.text
        assert ok.json()["model_switch_gate"]["overridden"] is True

        eff = api.get("/api/admin/models/effective", params={"workspace": tenant_b["ws"]}, headers=b).json()
        assert eff["budgets"]["per_day_usd"] == 1.0 and eff["sources"]["budgets.per_day_usd"] == "tenant"
        assert eff["sources"]["profile_models.coding"] == "workspace"
        assert set(eff["blocked_families"]) >= {"anthropic", "google"}
        assert not [m for m in eff["allowlist"] if m.split("/", 1)[0] in eff["blocked_families"]]

    with system_scope(), session_scope() as s:
        row = s.scalar(select(ModelConfigOverride).where(ModelConfigOverride.workspace_id == tenant_b["ws"]))
        assert row.tenant_id == tenant_b["tenant"]  # the fill trigger / explicit tenant matches the workspace's
    # A worker with no bound tenant still applies the workspace's layers (keyed lookup in system scope).
    model_config.invalidate()
    narrowing = model_config.narrowing_for(tenant_b["ws"])
    assert narrowing.budgets["per_day_usd"] == 1.0 and narrowing.profile_models["coding"][0] == coding[1]
