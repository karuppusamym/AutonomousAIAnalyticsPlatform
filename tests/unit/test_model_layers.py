"""S05-T3 layered model configuration: file ⊕ platform ⊕ tenant ⊕ workspace, each lower layer narrowing only
(spec S05 §5, §7, §9: test_layer_can_only_narrow, AT-15, test_jev_never_grants)."""
from __future__ import annotations

import pytest
from tests.fakes import FakeTransport, chat_json

from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.core.errors import (
    Forbidden,
    InvalidInput,
    ModelResidencyBlocked,
    ModelRouteUnavailable,
    PreconditionFailed,
)
from analystos.llm.cache import ResponseCache
from analystos.llm.config import ModelsConfig
from analystos.llm.layers import ModelOverride, narrow_ladder, resolve
from analystos.llm.router import CallContext, ModelRouter

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


def _cfg() -> ModelsConfig:
    return ModelsConfig.model_validate({
        "providers": {
            "openrouter": {"kind": "chat", "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
            "typesafe": {"kind": "decision", "base_url": "https://openrouter.ai/api/alpha/decisions",
                         "api_key_env": "OPENROUTER_API_KEY"},
        },
        "allowlist": ["eu/a", "us/b", "x/c", "eu/d", "typesafe/jev"],
        "models": {"eu/a": {"region": "eu"}, "us/b": {"region": "us"}, "eu/d": {"region": "EU"}},  # x/c: region unknown
        "profiles": {"chat": {"models": ["x/c", "us/b", "eu/a"]}, "other": {"models": ["eu/d"]},
                     "decision": {"provider": "typesafe", "models": ["typesafe/jev"]}},
        "routing": {"planning": "chat", "sql_generation": "chat", "risk_check": "decision"},
    })


def _platform(**llm) -> PlatformSettings:
    return PlatformSettings(llm=LLMSettings(**llm))


def _layers(tenant: dict | None = None, workspace: dict | None = None):
    out = [("tenant", 1 if tenant is not None else 0, ModelOverride.model_validate(tenant) if tenant is not None else None)]
    out.append(("workspace", 1 if workspace is not None else 0,
                ModelOverride.model_validate(workspace) if workspace is not None else None))
    return out


def _router(cfg: ModelsConfig, platform: PlatformSettings, effective) -> tuple[ModelRouter, FakeTransport]:
    transport = FakeTransport(chat=lambda p: chat_json({"ok": 1}, model=p["model"]))
    router = ModelRouter(cfg, transport=transport, api_key_lookup=KEY.get, max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None),
                         layers_provider=lambda ws: effective)
    return router, transport


WS = CallContext(workspace_id="ws1")


# ----------------------------------------------------------------------------- narrowing only
def test_layer_can_only_narrow():
    cfg, platform = _cfg(), _platform(daily_spend_cap_usd=10.0, purpose_modes={"sql_generation": "auto"})
    eff = resolve(cfg, platform, _layers(
        tenant={"allowlist": ["eu/a", "us/b", "x/c"], "budgets": {"per_day_usd": 5, "per_run_usd": 2}},
        workspace={"allowlist": ["eu/a", "eu/d"],  # eu/d is not on the tenant list: no widening
                   "budgets": {"per_day_usd": 8, "per_run_usd": 1},  # raising the day cap is refused
                   "purposes": {"sql_generation": {"mode": "model_first"}},  # platform says auto
                   "profile_models": {"chat": ["eu/a", "eu/d"]}}))

    assert eff.allowlist == ["eu/a"] and eff.layer_allowlist == ["eu/a"]
    assert eff.sources["allowlist"] == "workspace"
    assert eff.budgets == {"per_run_usd": 1, "per_day_usd": 5, "per_user_day_usd": None}
    assert eff.sources["budgets.per_day_usd"] == "tenant" and eff.sources["budgets.per_run_usd"] == "workspace"
    assert eff.purposes["sql_generation"]["mode"] == "auto"  # model_first could not widen auto
    assert eff.profile_models["chat"] == ["eu/a"]
    paths = {(v["layer"], v["path"]) for v in eff.violations}
    assert paths == {("workspace", "allowlist"), ("workspace", "budgets.per_day_usd"),
                     ("workspace", "purposes.sql_generation.mode"), ("workspace", "profile_models.chat")}
    assert eff.excluded["us/b"]["layer"] == "workspace" and eff.excluded["eu/d"]["layer"] == "tenant"

    # The router enforces the same: only eu/a is ever sent, whatever the profile order.
    router, transport = _router(cfg, platform, eff)
    router.complete_json("planning", "s", "u", ctx=WS)
    assert [c["model"] for c in transport.chat_calls] == ["eu/a"]
    assert router.mode("sql_generation", WS) == "auto"


def test_a_layer_cannot_revive_a_platform_disabled_model_and_routing_fails_closed():
    cfg, platform = _cfg(), _platform(disabled_models=["eu/a", "us/b", "x/c"])
    eff = resolve(cfg, platform, _layers(tenant={"allowlist": ["eu/a"]}))
    assert eff.allowlist == [] and eff.violations[0]["path"] == "allowlist"
    assert eff.purposes["planning"]["model_unavailable"] == {"purpose": "planning", "reason": "allowlist"}
    assert eff.purposes["planning"]["rules_fallback"] is True
    router, transport = _router(cfg, platform, eff)
    with pytest.raises(ModelRouteUnavailable, match="no allowed model for purpose 'planning'"):
        router.complete_json("planning", "s", "u", ctx=WS)
    assert not router.available("planning", WS) and transport.chat_calls == []


def test_a_layer_may_turn_a_purpose_off_and_drop_rungs_but_not_add_a_model_rung():
    cfg, platform = _cfg(), _platform()
    eff = resolve(cfg, platform, _layers(tenant={"purposes": {"planning": {"mode": "rules_only"}}},
                                         workspace={"purposes": {"planning": {"ladder": ["cache", "llm_large", "rules"]}}}))
    assert eff.purposes["planning"]["mode"] == "off"  # the workspace cannot bring the model back
    assert any(v["path"] == "purposes.planning.ladder" for v in eff.violations)
    router, _ = _router(cfg, platform, eff)
    assert router.mode("planning", WS) == "off" and router.mode("planning") == "always"  # platform-level call: unchanged
    assert narrow_ladder(["cache", "rules", "llm_small"], ["llm_large", "rules"]) == (["rules"], "adds model rung(s) llm_large")


def test_overrides_cannot_declare_providers_or_profiles():
    with pytest.raises(ValueError, match="providers"):
        ModelOverride.model_validate({"providers": {"onprem": {"type": "openai_compatible", "base_url": "http://x"}}})
    with pytest.raises(ValueError, match="profile_models"):
        ModelOverride.model_validate({"models": {"profiles": {"p": {"models": ["eu/a"]}}}})
    assert ModelOverride.model_validate({"models": {"residency": "EU"}}).residency == ["eu"]  # the spec's wrapper


# ----------------------------------------------------------------------------- residency (AT-15)
def test_residency_excludes_unknown_and_out_of_policy_regions():
    """AT-15: under residency `eu` a model whose region is unknown (x/c) or another region (us/b) is never
    called; a wider workspace residency does not reopen us; no remaining model -> visible residency error."""
    cfg, platform = _cfg(), _platform()
    eff = resolve(cfg, platform, _layers(tenant={"residency": "eu"}, workspace={"residency": ["eu", "us"]}))
    assert eff.regions == ["eu"] and eff.sources["residency"] == "workspace"
    assert [v["path"] for v in eff.violations] == ["residency"]
    assert eff.excluded["x/c"]["reason"].startswith("region unknown") and "us" in eff.excluded["us/b"]["reason"]
    assert eff.purposes["planning"]["models"] == ["eu/a"]
    router, transport = _router(cfg, platform, eff)
    router.complete_json("planning", "s", "u", ctx=WS)
    assert [c["model"] for c in transport.chat_calls] == ["eu/a"]

    only_us = resolve(cfg, platform, _layers(tenant={"residency": "eu"}, workspace={"profile_models": {"chat": ["us/b", "x/c"]}}))
    assert only_us.purposes["planning"]["model_unavailable"]["reason"] == "residency"
    router, transport = _router(cfg, platform, only_us)
    with pytest.raises(ModelResidencyBlocked, match="unknown regions fail closed"):
        router.complete_json("planning", "s", "u", ctx=WS)
    assert transport.chat_calls == []


def test_layer_residency_intersects_the_workspace_policy_residency():
    cfg, platform = _cfg(), _platform()
    eff = resolve(cfg, platform, _layers(tenant={"residency": ["eu", "us"]}))
    router, transport = _router(cfg, platform, eff)
    ctx = CallContext(workspace_id="ws1", data_residency="us")
    router.complete_json("planning", "s", "u", ctx=ctx)
    assert [c["model"] for c in transport.chat_calls] == ["us/b"]


# ----------------------------------------------------------------------------- JEV never grants
def test_jev_never_grants():
    """Configuration cannot wire a purpose onto the decision model (or a decision purpose off it), and every
    decision purpose in models.yaml has an authority that ranks, chooses, routes or escalates — none grants."""
    from analystos.decisions.config import AUTHORITY_CLASSES
    from analystos.llm.config import load_models_config

    cfg, platform = _cfg(), _platform()
    eff = resolve(cfg, platform, _layers(workspace={"purposes": {"planning": {"profile": "decision"},
                                                                 "risk_check": {"profile": "chat"}}}))
    assert eff.routing == {} and {v["path"] for v in eff.violations} == {"purposes.planning.profile",
                                                                         "purposes.risk_check.profile"}
    assert not any(word in a for a in AUTHORITY_CLASSES for word in ("grant", "approve", "authorize", "allow"))
    load_models_config.cache_clear()
    try:
        real = load_models_config()
    finally:
        load_models_config.cache_clear()
    authorities = {spec["authority"] for spec in real.decisions.get("purposes", {}).values()}
    assert authorities <= set(AUTHORITY_CLASSES)


# ----------------------------------------------------------------------------- persistence (versions, If-Match)
@pytest.fixture
def store(sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.db.base import session_scope
    from analystos.db.models import User, Workspace
    from analystos.services import model_config

    models.Base.metadata.tables["tenant_member"].create(sqlite_db.kw["bind"])
    cfg = _cfg()
    platform = _platform()
    monkeypatch.setattr(model_config, "_upper", lambda config=None, plat=None: (cfg, platform))
    from analystos.services import model_switch

    # The model-switch gate (S05-T5) scores every changed primary model: here every model passes every case.
    monkeypatch.setattr(model_switch, "router_runner", lambda **kw: {
        "status": "scored", "results": [{"case": i, "passed": True, "problems": []} for i in range(len(kw["cases"]))]})
    with session_scope() as s:
        s.add(User(id="usr_admin", email="admin@x", name="A", password_hash="-", is_admin=True, attributes={},
                   tenant_id="tnt_a"))
        s.add(User(id="usr_plain", email="plain@x", name="P", password_hash="-", is_admin=False, attributes={}))
        s.add(Workspace(id="ws1", name="W", created_by="usr_admin", settings={}, tenant_id="tnt_a"))
    model_config.invalidate()
    yield model_config
    model_config.invalidate()


def test_overrides_are_versioned_checked_and_rolled_back(store):
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent, User

    with session_scope() as s:
        admin = s.get(User, "usr_admin")
        out = store.put(s, admin, scope="tenant", tenant_id="tnt_a", workspace_id=None,
                        document={"allowlist": ["eu/a", "us/b"]}, expected_version=0)
        assert out["version"] == 1
        with pytest.raises(PreconditionFailed):  # stale If-Match
            store.put(s, admin, scope="tenant", tenant_id="tnt_a", workspace_id=None,
                      document={"allowlist": ["eu/a"]}, expected_version=0)
        with pytest.raises(InvalidInput, match="widen"):  # x/c is not on the tenant list
            store.put(s, admin, scope="workspace", tenant_id="tnt_a", workspace_id="ws1",
                      document={"allowlist": ["eu/a", "x/c"]}, expected_version=0)
        store.put(s, admin, scope="workspace", tenant_id="tnt_a", workspace_id="ws1",
                  document={"allowlist": ["eu/a"]}, expected_version=0)
        store.put(s, admin, scope="workspace", tenant_id="tnt_a", workspace_id="ws1",
                  document={"allowlist": ["us/b"]}, expected_version=1)
        eff = store.effective(s, tenant_id="tnt_a", workspace_id="ws1")
        assert eff.allowlist == ["us/b"] and eff.layers[-1] == {"layer": "workspace", "version": 2, "present": True}
        back = store.rollback(s, admin, scope="workspace", tenant_id="tnt_a", workspace_id="ws1", version=1)
        assert {k: back[k] for k in ("scope", "version", "rolled_back_to")} == {"scope": "workspace", "version": 3,
                                                                               "rolled_back_to": 1}
        assert back["model_switch_gate"]["gated"] is True  # us/b -> eu/a is a primary-model change
        assert store.effective(s, tenant_id="tnt_a", workspace_id="ws1").allowlist == ["eu/a"]
        # Narrowing the tenant afterwards makes the old workspace version unrestorable as-is.
        store.put(s, admin, scope="tenant", tenant_id="tnt_a", workspace_id=None, document={"allowlist": ["us/b"]},
                  expected_version=None)
        with pytest.raises(InvalidInput):
            store.rollback(s, admin, scope="workspace", tenant_id="tnt_a", workspace_id="ws1", version=1)
        assert [h["version"] for h in store.history(s, scope="workspace", tenant_id="tnt_a", workspace_id="ws1")] == [3, 2, 1]
        actions = [a for (a,) in s.query(AuditEvent.action).all()]
        assert actions.count("models.config.updated") == 4 and actions.count("models.config.rolled_back") == 1
        with pytest.raises(Forbidden):
            store.put(s, s.get(User, "usr_plain"), scope="tenant", tenant_id="tnt_a", workspace_id=None,
                      document={}, expected_version=None)


def test_the_router_reads_the_workspace_layers_from_the_store(store):
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        store.put(s, s.get(User, "usr_admin"), scope="tenant", tenant_id="tnt_a", workspace_id=None,
                  document={"residency": "eu"}, expected_version=0)
    narrowing = store.narrowing_for("ws1")
    assert narrowing.regions == ["eu"]
    assert store.narrowing_for("ws_unknown").regions is None  # no such workspace: file + platform only


def test_admin_models_api(store, monkeypatch):
    """The S05 §6 routes: effective view with sources, If-Match versions, rollback, providers with health."""
    from fastapi.testclient import TestClient

    from analystos.api import deps
    from analystos.api.app import app
    from analystos.db import base
    from analystos.security.auth import issue_token
    from analystos.tenancy import resolve

    monkeypatch.setattr(deps, "SessionLocal", base.SessionLocal)  # bound at import: an earlier test may have imported it
    resolve.invalidate()
    client = TestClient(app)
    admin = {"Authorization": f"Bearer {issue_token('usr_admin', 'admin@x')}"}
    plain = {"Authorization": f"Bearer {issue_token('usr_plain', 'plain@x')}"}
    url = "/api/admin/models/config"

    assert client.put(url, params={"scope": "tenant"}, json={"document": {}}, headers=admin).status_code == 428
    r = client.put(url, params={"scope": "tenant"}, json={"document": {"allowlist": ["eu/a", "us/b"], "residency": "eu"}},
                   headers={**admin, "If-Match": '"0"'})
    assert r.status_code == 200 and r.headers["ETag"] == '"1"' and r.json()["version"] == 1
    stale = client.put(url, params={"scope": "tenant"}, json={"document": {}}, headers={**admin, "If-Match": '"0"'})
    assert stale.status_code == 412
    wider = client.put(url, params={"scope": "workspace", "workspace": "ws1"}, json={"document": {"allowlist": ["x/c"]}},
                       headers={**admin, "If-Match": '"0"'})
    assert wider.status_code == 422 and wider.json()["error"]["details"]["violations"][0]["path"] == "allowlist"
    ok = client.put(url, params={"scope": "workspace", "workspace": "ws1"},
                    json={"document": {"budgets": {"per_run_usd": 1.5}}}, headers={**admin, "If-Match": '"0"'})
    assert ok.status_code == 200

    eff = client.get("/api/admin/models/effective", params={"workspace": "ws1"}, headers=admin)
    assert eff.status_code == 200 and eff.headers["ETag"] == '"1"'
    view = eff.json()
    assert view["tenant_id"] == "tnt_a" and view["allowlist"] == ["eu/a", "us/b"] and view["residency"] == ["eu"]
    assert view["sources"]["allowlist"] == "tenant" and view["sources"]["budgets.per_run_usd"] == "workspace"
    assert view["purposes"]["planning"]["models"] == ["eu/a"]
    assert client.get("/api/admin/models/effective", headers=plain).status_code == 403
    assert client.get("/api/admin/models/effective", params={"workspace": "ws1"}, headers=plain).status_code == 404

    back = client.post(f"{url}/rollback", json={"version": 1, "scope": "tenant"}, headers=admin)
    assert back.status_code == 200 and back.json()["version"] == 2
    history = client.get(url, params={"scope": "tenant"}, headers=admin).json()
    assert [h["version"] for h in history["history"]] == [2, 1] and history["document"]["residency"] == ["eu"]

    providers = client.get("/api/admin/models/providers", headers=admin)
    assert providers.status_code == 200
    entry = next(p for p in providers.json()["providers"] if p["provider"] == "openrouter")
    assert {"key_present", "stats_24h", "capabilities", "profiles"} <= set(entry) and "api_key" not in entry
    assert client.get("/api/admin/models/providers", headers=plain).status_code == 403
    assert client.post("/api/admin/models/providers/nope/test", headers=admin).status_code == 404
