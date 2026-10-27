"""S01-T7 negative cross-tenant suite on Postgres (RLS, strict tenancy) and Redis, plus S01-T6 ABAC facts read
under RLS: tenant B never gets tenant A's cached query results, model answers or context entries, even with the
same workspace layout, SQL and prompt; a tenant's data residency (read through RLS) denies a query from outside it.

Redis db 3 (the lead stack uses db 0). Query execution is stubbed at `QueryGateway._run`: what is under test is the
control-plane lookup (RLS) and the cache, not the data plane."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.core.errors import NotFound, PolicyDenied

pytestmark = pytest.mark.integration
REDIS_URL = "redis://localhost:6379/3"
SQL = "SELECT priority, COUNT(*) AS n FROM incident GROUP BY priority"


@pytest.fixture(scope="module")
def redis_client():
    import redis

    client = redis.Redis.from_url(REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5)
    try:
        client.ping()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Redis unavailable: {exc}")
    yield client
    client.close()


@pytest.fixture(scope="module")
def tenants(control_db):
    """Tenant A (default) and tenant B (data residency eu), each with an admin, a workspace, a ready source and
    a selected `incident` asset with the same schema name, so every cache input except the tenant is equal."""
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Source, SourceAsset, SourceColumn, Tenant, User, Workspace, WorkspaceMember
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    tb = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"cache-b-{tb[-6:]}", name="Cache tenant B", data_residency="eu"))
    out = {"a": DEFAULT_TENANT_ID, "b": tb}
    for key, tenant in (("a", DEFAULT_TENANT_ID), ("b", tb)):
        with tenant_scope(tenant), session_scope() as s:
            user = User(id=new_id("usr"), email=f"cache-{key}-{tb}@t.test", name=key, password_hash="-",
                        is_admin=False, active=True, attributes={"region": "us-east-1" if key == "a" else "eu-west-1"})
            s.add(user)
            s.flush()
            ws = Workspace(id=new_id("ws"), name=f"cache {key}", created_by=user.id, autonomy_level=3)
            s.add(ws)
            s.flush()
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
            src = Source(id=new_id("src"), workspace_id=ws.id, kind="servicenow", name="sn", status="ready",
                         execution_mode="staged", staging_schema="src_cache")
            s.add(src)
            s.flush()
            asset = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name="src_cache",
                                name="incident", source_name="incident", selected=True)
            s.add(asset)
            s.flush()
            for i, (name, tags) in enumerate((("priority", []), ("caller_email", ["pii"]))):
                s.add(SourceColumn(asset_id=asset.id, name=name, ordinal=i, data_type="text", tags=tags))
            out[f"user_{key}"], out[f"ws_{key}"], out[f"src_{key}"] = user.id, ws.id, src.id
    return out


def _scope(tenant: str, user_id: str, ws_id: str):
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.governance.policy import resolve_scope
    from analystos.tenancy import tenant_scope

    with tenant_scope(tenant), session_scope() as s:
        return resolve_scope(s, s.get(User, user_id), ws_id)


def _gateway(redis_client, rows: list):
    from analystos.core.config import get_settings
    from analystos.gateway.cache import QueryCache
    from analystos.gateway.service import QueryGateway

    settings = get_settings()
    gw = QueryGateway(settings, cache=QueryCache(settings, client=redis_client, ttl_seconds=120))
    gw.executions = []  # type: ignore[attr-defined]
    gw._run = lambda *a, **k: gw.executions.append(a) or (["priority", "n"], rows)  # type: ignore[method-assign]
    return gw


def _keys(redis_client, pattern: str) -> list[str]:
    return [k.decode() for k in redis_client.scan_iter(match=pattern, count=500)]


@pytest.fixture()
def clean_redis(redis_client, tenants):
    yield
    for tenant in (tenants["a"], tenants["b"]):
        for k in _keys(redis_client, f"*tenant:{tenant}:*"):
            redis_client.delete(k)


def test_query_results_never_cross_tenants(tenants, redis_client, clean_redis):
    from analystos.tenancy import tenant_scope

    scope_a = _scope(tenants["a"], tenants["user_a"], tenants["ws_a"])
    scope_b = _scope(tenants["b"], tenants["user_b"], tenants["ws_b"])
    gw_a, gw_b = _gateway(redis_client, [[1, 111]]), _gateway(redis_client, [[1, 222]])
    with tenant_scope(tenants["a"]):
        assert not gw_a.execute(scope_a, SQL, actor="user:a").cache_hit
        hit = gw_a.execute(scope_a, SQL, actor="user:a")
    assert hit.cache_hit and hit.rows == [[1, 111]] and len(gw_a.executions) == 1
    [key_a] = _keys(redis_client, f"analystos:qcache:tenant:{tenants['a']}:*")
    assert f":policy:{scope_a.policy_version}:" in key_a and f":scope:{scope_a.scope_hash()}:" in key_a
    with tenant_scope(tenants["b"]):
        own = gw_b.execute(scope_b, SQL, actor="user:b")
        assert not own.cache_hit and own.rows == [[1, 222]]
        # B presenting A's scope: RLS hides A's workspace and source, so nothing runs and no entry is read.
        with pytest.raises(NotFound):
            gw_b.execute(scope_a, SQL, actor="user:b")
    assert len(gw_b.executions) == 1
    assert _keys(redis_client, f"analystos:qcache:tenant:{tenants['a']}:*") == [key_a]


def test_model_answers_never_cross_tenants(tenants, redis_client, clean_redis, pinned_models_config):
    from tests.fakes import FakeTransport

    from analystos.contracts.platform import LLMSettings, PlatformSettings
    from analystos.llm.cache import ResponseCache
    from analystos.llm.router import ModelRouter
    from analystos.runtime.context import workspace_call_ctx
    from analystos.tenancy import tenant_scope

    calls: list = []

    def answer(payload):
        calls.append(payload["model"])
        return {"model": payload["model"], "choices": [{"message": {"content": '{"plan":"tenant A only"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0001}}

    class Sink:
        def record(self, **kw):
            pass

        def check_budget(self, ctx, purpose):
            return None

    platform = PlatformSettings(llm=LLMSettings(cache_enabled=True))
    router = ModelRouter(transport=FakeTransport(chat=answer), sink=Sink(), max_retries=0,
                         api_key_lookup={"OPENROUTER_API_KEY": "sk-test-000000000000"}.get,
                         settings_provider=lambda: platform, cache=ResponseCache(None, client=redis_client))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "plan the same objective"}]
    with tenant_scope(tenants["a"]):
        ctx_a = workspace_call_ctx(tenants["ws_a"], knowledge_version="kv")
        assert ctx_a.tenant_id == tenants["a"] and ctx_a.policy_version == 1  # read under RLS
        router.complete("planning", messages, ctx=ctx_a, json_output=True)
        assert router.complete("planning", messages, ctx=ctx_a, json_output=True).cached
    with tenant_scope(tenants["b"]):
        # B naming A's workspace id: A's workspace is invisible under RLS, the key carries tenant B.
        forged = workspace_call_ctx(tenants["ws_a"], knowledge_version="kv")
        assert forged.tenant_id is None
        assert not router.complete("planning", messages, ctx=forged, json_output=True).cached
        own = workspace_call_ctx(tenants["ws_b"], knowledge_version="kv")
        assert not router.complete("planning", messages, ctx=own, json_output=True).cached
    assert len(calls) == 3
    assert len(_keys(redis_client, f"aos:llm:tenant:{tenants['a']}:*")) == 1
    assert len(_keys(redis_client, f"aos:llm:tenant:{tenants['b']}:*")) == 2


def test_context_entries_never_cross_tenants(tenants, redis_client, clean_redis):
    from types import SimpleNamespace

    from analystos.context import cache as context_cache
    from analystos.db.base import session_scope
    from analystos.db.models import Workspace
    from analystos.llm.cache import ResponseCache
    from analystos.tenancy import tenant_scope

    def ctx(tenant, user, ws_id):
        with tenant_scope(tenant), session_scope() as s:
            ws = s.get(Workspace, ws_id)
            ws = SimpleNamespace(id=ws_id, tenant_id=ws.tenant_id, policy_version=ws.policy_version) if ws else \
                SimpleNamespace(id=ws_id, tenant_id=None, policy_version=None)
        return SimpleNamespace(workspace=ws, scope=_scope(tenant, user, ws_id) if ws.tenant_id else None)

    context_cache.use(ResponseCache(None, client=redis_client))
    try:
        material = {"knowledge": "kv", "query": "sla"}
        with tenant_scope(tenants["a"]):
            key_a = context_cache.key("retrieval", tenants["ws_a"], material,
                                      **context_cache.scope_of(ctx(tenants["a"], tenants["user_a"], tenants["ws_a"])))
            context_cache.put(key_a, [{"text": "A's glossary"}])
            assert context_cache.get("retrieval", "planning", key_a) == [{"text": "A's glossary"}]
        assert key_a.startswith(f"aos:ctx:tenant:{tenants['a']}:scope:")
        with tenant_scope(tenants["b"]):
            forged = ctx(tenants["b"], tenants["user_b"], tenants["ws_a"])  # A's workspace id, seen from B
            key = context_cache.key("retrieval", tenants["ws_a"], material, **context_cache.scope_of(forged))
            assert key != key_a and context_cache.get("retrieval", "planning", key) is None
        with tenant_scope(tenants["a"]):
            assert context_cache.clear_workspace(tenants["ws_a"]) == 1
            assert context_cache.get("retrieval", "planning", key_a) is None
    finally:
        context_cache.use(None)


def test_tenant_residency_read_under_rls_denies_queries_from_outside(tenants, redis_client, clean_redis):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution, User, WorkspaceMember
    from analystos.tenancy import tenant_scope

    with tenant_scope(tenants["b"]), session_scope() as s:  # a tenant B member working from the US
        traveller_id = new_id("usr")
        traveller = User(id=traveller_id, email=f"us-{tenants['b']}@t.test", name="us", password_hash="-",
                         active=True, attributes={"region": "us-east-1"})
        s.add(traveller)
        s.flush()
        s.add(WorkspaceMember(workspace_id=tenants["ws_b"], user_id=traveller_id, role="analyst"))
    scope_b = _scope(tenants["b"], traveller_id, tenants["ws_b"])
    assert scope_b.abac.residency == "eu" and scope_b.abac.region == "us-east-1"
    assert "src_cache.incident.caller_email" in scope_b.denied_columns  # pii without clearance: denied
    assert scope_b.abac.column_classes == {}
    gw = _gateway(redis_client, [[1, 1]])
    with tenant_scope(tenants["b"]):
        with pytest.raises(PolicyDenied) as info:
            gw.execute(scope_b, SQL, actor="user:b")
        assert info.value.details["rule"] == "platform.data_residency" and info.value.details["remedy"]
        with session_scope() as s:
            rejected = s.scalars(select(QueryExecution).where(QueryExecution.workspace_id == tenants["ws_b"],
                                                             QueryExecution.status == "rejected")).all()
    assert gw.executions == [] and rejected and "platform.data_residency" in rejected[-1].rejected_reason
    with tenant_scope(tenants["a"]), session_scope() as s:  # A cannot see B's audit row
        assert not s.scalars(select(QueryExecution).where(QueryExecution.workspace_id == tenants["ws_b"])).all()
    scope_a = _scope(tenants["a"], tenants["user_a"], tenants["ws_a"])
    assert scope_a.abac.residency is None and scope_a.abac.region == "us-east-1"  # no residency in tenant A: allowed
