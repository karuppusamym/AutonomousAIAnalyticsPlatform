"""S01-T7 negative cross-tenant suite (no services): every tenant-data cache — the query result cache, the model
response cache (L0), the shared context cache (retrieval) and the compiled-context cache — keys its entries
`tenant:{t}:scope:{scope_hash}:policy:{policy_version}`, so tenant B never gets an entry tenant A wrote, even
with identical inputs. A caller bound to a tenant other than the workspace's gets no cache at all, and entries
written with no tenant bound are never served to a bound tenant."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from tests.fakes import FakeTransport

from analystos.contracts.platform import LLMSettings, PlatformSettings
from analystos.contracts.policy import DataScope
from analystos.core.config import Settings
from analystos.db.models import Workspace
from analystos.gateway.cache import QueryCache
from analystos.gateway.service import QueryGateway
from analystos.llm.cache import ResponseCache
from analystos.llm.router import CallContext, ModelRouter
from analystos.tenancy import tenant_scope
from analystos.tenancy.cache_keys import cache_tenant, parse, prefix, scoped_prefix

A, B = "tnt_a", "tnt_b"


class FakeRedis:
    """Enough of redis-py for the three stores (strings, setex, hashes, scan)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode() if isinstance(value, str) else value

    def setex(self, key, ttl, value):
        self.set(key, value)

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)

    def ping(self):
        return True

    def hincrbyfloat(self, key, field, amount):
        pass

    def expire(self, key, ttl):
        pass

    def hgetall(self, key):
        return {}

    def scan_iter(self, match="*", count=None):
        import fnmatch

        return [k for k in list(self.store) if fnmatch.fnmatchcase(k, match)]


# ------------------------------------------------------------------------------ the prefix
def test_prefix_format_and_tenant_resolution():
    assert prefix(tenant=A, scope_hash="abc", policy_version=3) == "tenant:tnt_a:scope:abc:policy:3"
    assert prefix(tenant=None, scope_hash=None, policy_version=None) == "tenant:-:scope:-:policy:-"
    assert prefix(tenant="x:y", scope_hash="s", policy_version=1) == "tenant:x_y:scope:s:policy:1"  # no separator leak
    assert parse(["tenant", "tnt_a", "scope", "abc", "policy", "3"]) == {"tenant": A, "scope": "abc", "policy": "3"}
    assert cache_tenant() == "-" and cache_tenant(A) == A
    with tenant_scope(B):
        assert cache_tenant() == B and cache_tenant(B) == B
        assert cache_tenant(A) is None  # bound to B, the data is A's: no cache
        assert scoped_prefix(owner_tenant=A, scope_hash="s", policy_version=1) is None


# ------------------------------------------------------------------------------ query result cache
def test_query_cache_keys_never_collide_across_tenants():
    redis = FakeRedis()
    cache = QueryCache(Settings(query_cache_ttl_seconds=60), client=redis)
    args = ("fingerprint", "scopehash", "v1", 100)
    with tenant_scope(A):
        key_a = cache.key(*args, tenant=A, policy_version=1)
        assert cache.set(key_a, {"rows": [["a-secret"]], "columns": ["x"]})
    with tenant_scope(B):
        key_b = cache.key(*args, tenant=B, policy_version=1)
        assert key_b != key_a and cache.get(key_b) is None
        forged = cache.key(*args, tenant=A, policy_version=1)  # B naming A as the owner
        assert forged is None and cache.get(forged) is None and cache.set(forged, {"rows": []}) is False
    assert key_a.startswith("analystos:qcache:tenant:tnt_a:scope:scopehash:policy:1:")
    unbound = cache.key(*args)
    with tenant_scope(A):
        assert cache.key(*args) != unbound  # an unbound entry is not tenant A's
    assert cache.key(*args, tenant=A, policy_version=2) != key_a  # a policy change misses


class _Session:
    def __init__(self, tenant, source):
        self.tenant, self.source = tenant, source

    def get(self, model, key):
        if model is Workspace:
            return SimpleNamespace(id=key, status="active", deleted_at=None, tenant_id=self.tenant)
        return self.source if key == self.source.id else None

    def execute(self, stmt):
        return SimpleNamespace(one=lambda: (1, None, 10))

    def add(self, obj): ...

    def commit(self): ...

    def rollback(self): ...

    def close(self): ...


def _gateway(redis, owner_tenant, rows):
    source = SimpleNamespace(id="s1", workspace_id="ws_1", kind="servicenow", config={}, secret_ref=None, status="ready",
                             execution_mode="staged", staging_schema="src_s1", last_discovered_at=None)
    gw = QueryGateway(Settings(), session_factory=lambda: _Session(owner_tenant, source),
                      cache=QueryCache(Settings(), client=redis))
    gw._run = lambda *a, **k: (["priority"], rows)  # type: ignore[method-assign]
    return gw


@pytest.fixture()
def scope() -> DataScope:
    return DataScope(workspace_id="ws_1", user_id="u_1", role="analyst", source_ids=["s1"], assets=["src_s1.incident"],
                     asset_sources={"src_s1.incident": "s1"}, columns={"src_s1.incident": ["number", "priority"]},
                     source_dialects={"s1": "postgres"}, max_rows=100, timeout_seconds=10)


SQL = "SELECT priority, COUNT(*) AS n FROM incident GROUP BY priority"


def test_gateway_tenant_b_never_reads_tenant_a_results(scope):
    """Same workspace id, scope, SQL and source version in two tenants (the worst case): no shared entry."""
    redis = FakeRedis()
    with tenant_scope(A):
        gw_a = _gateway(redis, A, [["a-rows"]])
        assert not gw_a.execute(scope, SQL, actor="user:u_1").cache_hit
        assert gw_a.execute(scope, SQL, actor="user:u_1").cache_hit
    with tenant_scope(B):
        gw_b = _gateway(redis, B, [["b-rows"]])
        first = gw_b.execute(scope, SQL, actor="user:u_1")
        assert not first.cache_hit and first.rows == [["b-rows"]]
    assert sorted(k.split(":")[3] for k in redis.store) == [A, B]


def test_gateway_bound_to_another_tenant_than_the_workspace_uses_no_cache(scope):
    redis = FakeRedis()
    with tenant_scope(A):
        _gateway(redis, A, [["a-rows"]]).execute(scope, SQL, actor="user:u_1")
    with tenant_scope(B):  # under RLS B could not even load A's workspace; here the cache must not help either
        gw = _gateway(redis, A, [["fresh"]])
        res = gw.execute(scope, SQL, actor="user:u_1")
        again = gw.execute(scope, SQL, actor="user:u_1")
    assert not res.cache_hit and not again.cache_hit and res.rows == [["fresh"]]
    assert len(redis.store) == 1  # nothing written under the mismatch


def test_gateway_scope_and_policy_version_are_part_of_the_key(scope):
    redis = FakeRedis()
    with tenant_scope(A):
        gw = _gateway(redis, A, [["x"]])
        gw.execute(scope, SQL, actor="user:u_1")
        other_user = scope.model_copy(update={"user_id": "u_2"})
        assert not gw.execute(other_user, SQL, actor="user:u_2").cache_hit
        bumped = scope.model_copy(update={"policy_version": 2})
        assert not gw.execute(bumped, SQL, actor="user:u_1").cache_hit
    policies = {k.split(":")[7] for k in redis.store}
    assert policies == {"1", "2"}


# ------------------------------------------------------------------------------ model response cache (L0)
def _router(redis, transport):
    platform = PlatformSettings(llm=LLMSettings(cache_enabled=True))
    return ModelRouter(transport=transport, sink=_Sink(), api_key_lookup={"OPENROUTER_API_KEY": "sk-test-000000000000"}.get,
                       max_retries=0, settings_provider=lambda: platform, cache=ResponseCache(None, client=redis))


class _Sink:
    def record(self, **kw):
        pass

    def check_budget(self, ctx, purpose):
        return None


@pytest.mark.usefixtures("pinned_models_config")
def test_model_response_cache_is_per_tenant():
    redis = FakeRedis()
    calls: list = []

    def answer(payload):
        calls.append(payload["model"])
        return {"model": payload["model"], "choices": [{"message": {"content": '{"answer":"tenant-a"}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.0001}}

    router = _router(redis, FakeTransport(chat=answer))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "same question"}]

    def ask(tenant, scope_hash="h"):
        ctx = CallContext(workspace_id="ws_1", tenant_id=tenant, scope_hash=scope_hash, policy_version=1)
        return router.complete("planning", messages, ctx=ctx, json_output=True)

    with tenant_scope(A):
        assert not ask(A).cached and ask(A).cached
    assert len(calls) == 1
    with tenant_scope(B):
        assert not ask(B).cached
        assert not ask(A).cached  # B claiming A's workspace tenant: no cache at all
    assert len(calls) == 3
    tenants = sorted(k.split(":")[3] for k in redis.store)
    assert tenants == [A, B] and all(k.startswith("aos:llm:tenant:") for k in redis.store)
    with tenant_scope(A):  # another caller scope in the same tenant does not reuse it either
        assert not ask(A, "other").cached


def test_response_cache_key_none_is_never_stored():
    cache = ResponseCache(None)
    with tenant_scope(B):
        key = ResponseCache.key("p", "m", {"x": 1}, "ws_1", tenant=A)
    assert key is None
    cache.set(key, {"text": "a"}, 60)
    assert cache.get(key) is None and not cache._local


# ------------------------------------------------------------------------------ context caches
@pytest.fixture()
def ctx_store():
    from analystos.context import cache as context_cache

    store = ResponseCache(None, client=FakeRedis())
    context_cache.use(store)
    yield context_cache, store
    context_cache.use(None)


def _ctx(tenant, scope_hash="h", version=1):
    return SimpleNamespace(workspace=SimpleNamespace(id="ws_1", tenant_id=tenant, policy_version=version),
                           scope=SimpleNamespace(scope_hash=lambda: scope_hash, policy_version=version))


def test_context_retrieval_entries_are_per_tenant_and_scope(ctx_store):
    context_cache, store = ctx_store
    material = {"knowledge": "kv1", "query": "sla breaches"}
    with tenant_scope(A):
        key_a = context_cache.key("retrieval", "ws_1", material, **context_cache.scope_of(_ctx(A)))
        context_cache.put(key_a, [{"text": "tenant A glossary"}])
        assert context_cache.get("retrieval", "p", key_a) == [{"text": "tenant A glossary"}]
    with tenant_scope(B):
        key_b = context_cache.key("retrieval", "ws_1", material, **context_cache.scope_of(_ctx(B)))
        assert key_b != key_a and context_cache.get("retrieval", "p", key_b) is None
        assert context_cache.key("retrieval", "ws_1", material, **context_cache.scope_of(_ctx(A))) is None
        assert not context_cache.peek(key_b)
    with tenant_scope(A):
        narrower = context_cache.key("retrieval", "ws_1", material, **context_cache.scope_of(_ctx(A, "narrow")))
        assert narrower != key_a and context_cache.get("retrieval", "p", narrower) is None
    assert context_cache.workspace_of(key_a) == "ws_1"
    assert context_cache.workspace_stats("ws_1")["total_entries"] == 1
    assert context_cache.clear_workspace("ws_1") == 1 and context_cache.get("retrieval", "p", key_a) is None


def test_compiled_context_cache_is_per_tenant(ctx_store):
    from analystos.agents import common
    from analystos.context.compiler import CompiledContext

    cache = common.CompiledContextCache()
    compiled = CompiledContext(purpose="sql_generation", header={}, body={"catalog": "tenant A tables"})
    with tenant_scope(A):
        scope_a = common.context_cache.scope_of(_ctx(A))
        cache.put("digest", "sql_generation", compiled, workspace_id="ws_1", scope=scope_a)
        assert cache.get("digest", "sql_generation", workspace_id="ws_1", scope=scope_a) is not None
    with tenant_scope(B):
        assert cache.get("digest", "sql_generation", workspace_id="ws_1",
                         scope=common.context_cache.scope_of(_ctx(B))) is None
        assert cache.get("digest", "sql_generation", workspace_id="ws_1", scope=scope_a) is None  # forged owner
        cache.put("digest", "sql_generation", compiled, workspace_id="ws_1", scope=scope_a)
    _, store = ctx_store
    assert [k.split(":")[3] for k in store._redis.store if ":compiled:" in k] == [A]


def test_unbound_entries_are_never_served_to_a_bound_tenant(ctx_store):
    context_cache, _ = ctx_store
    unbound = context_cache.key("retrieval", "ws_1", {"q": 1})
    context_cache.put(unbound, ["written with no tenant bound"])
    for tenant in (A, B):
        with tenant_scope(tenant):
            assert context_cache.get("retrieval", "p", context_cache.key("retrieval", "ws_1", {"q": 1})) is None
