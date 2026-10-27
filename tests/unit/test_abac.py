"""S01-T6: ABAC attributes {classification, purpose, region, aggregation_grain, audience} decided on every tool
call (ToolRuntime pre-hook through policy.evaluate) and every query (QueryGateway, before the result cache),
the OPA adapter behind `policy_engine=opa` (fail closed), and `403 policy_denied {rule, reason, remedy}`."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from analystos.contracts.policy import (
    AbacAttributes,
    AbacCondition,
    AbacRule,
    AbacScope,
    DataScope,
    ExecutionIdentity,
    PolicyInput,
    WorkspacePolicyDoc,
)
from analystos.core.config import Settings, get_settings
from analystos.core.errors import PolicyDenied
from analystos.db.models import QueryExecution, Workspace
from analystos.gateway.cache import QueryCache
from analystos.gateway.service import QueryGateway
from analystos.governance import abac

NO_ROWS_FOR_MARKETING = AbacRule(
    id="no_rows_for_marketing", reason="marketing sees aggregates only", remedy="group the query",
    when=[AbacCondition(attribute="purpose", values=["marketing"]),
          AbacCondition(attribute="aggregation_grain", values=["row"])])
EU_ONLY = AbacRule(id="eu_only", when=[AbacCondition(attribute="region", op="not_in", values=["eu"])],
                   reason="EU callers only", remedy="ask from an EU region")


def _inp(point="query", residency=None, **attrs) -> PolicyInput:
    return PolicyInput(point=point, action="query", residency=residency, attributes=AbacAttributes(**attrs))


# ------------------------------------------------------------------------------ attributes
def test_classification_from_tags_takes_the_highest_class():
    assert abac.classify_tags(None) == "internal"
    assert abac.classify_tags(["pii"]) == "confidential"
    assert abac.classify_tags(["PUBLIC"]) == "public"
    assert abac.classify_tags(["public", "pii"]) == "confidential"
    assert abac.classify_tags(["restricted", "public"]) == "restricted"
    assert abac.classify_tags(["dimension"]) == "internal"
    assert abac.purpose_class("monitor.freshness") == "monitor"
    assert abac.purpose_class("federation.leg:qry_1") == "federation"


@pytest.mark.parametrize(("sql", "grain"), [
    ("SELECT priority, COUNT(*) AS n FROM t GROUP BY priority", "aggregate"),
    ("SELECT COUNT(*), AVG(x) FROM t", "aggregate"),
    ("SELECT COUNT(*) + 1 AS n, 'label' AS l FROM t", "aggregate"),
    ("SELECT priority FROM t", "row"),
    ("SELECT * FROM t", "row"),
    ("SELECT 1 FROM t", "row"),
    ("SELECT priority, COUNT(*) OVER () FROM t", "row"),
    ("SELECT (SELECT MAX(x) FROM u) AS m FROM t", "row"),
    ("SELECT COUNT(*) FROM t UNION ALL SELECT COUNT(*) FROM u", "aggregate"),
    ("SELECT COUNT(*) FROM t UNION ALL SELECT x FROM u", "row"),
    ("WITH g AS (SELECT x FROM t) SELECT x, COUNT(*) FROM g GROUP BY x", "aggregate"),
    ("SELECT * FROM (SELECT x, COUNT(*) FROM t GROUP BY x) s", "row"),  # syntactic: the outer SELECT decides
    ("this is not sql (", "row"),
])
def test_aggregation_grain_is_the_outermost_select(sql, grain):
    assert abac.sql_grain(sql, "postgres") == grain


def test_query_classification_counts_unresolved_assets_with_their_highest_column():
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", assets=["s.t", "s.u"],
                      columns={"s.t": ["a", "email"], "s.u": ["b"]},
                      abac=AbacScope(column_classes={"s.t.email": "confidential", "s.u.b": "public"}))
    assert abac.query_classification(scope, ["s.t"], ["s.t.a"]) == "internal"
    assert abac.query_classification(scope, ["s.t"], ["s.t.EMAIL"]) == "confidential"
    assert abac.query_classification(scope, ["s.t"], []) == "confidential"  # COUNT(*): the asset's highest column
    assert abac.query_classification(scope, ["s.u"], ["s.u.b"]) == "public"
    assert abac.query_classification(scope.model_copy(update={"columns": {"s.u": ["b", "c"]}}), ["s.u"], []) == "internal"
    assert abac.scope_classification(scope) == "confidential"
    scope.denied_columns = ["s.t.email"]
    assert abac.scope_classification(scope) == "internal"  # a denied column cannot be touched


# ------------------------------------------------------------------------------ rules
def test_conditions_and_their_validation():
    attrs = AbacAttributes(classification="confidential", purpose="monitor.freshness", region="eu-west-1")
    assert abac.condition_holds(AbacCondition(attribute="region", values=["eu"]), attrs)
    assert not abac.condition_holds(AbacCondition(attribute="region", values=["us"]), attrs)
    assert abac.condition_holds(AbacCondition(attribute="purpose", values=["monitor"]), attrs)
    assert abac.condition_holds(AbacCondition(attribute="classification", op="at_least", values=["internal"]), attrs)
    assert not abac.condition_holds(AbacCondition(attribute="classification", op="at_most", values=["internal"]), attrs)
    unknown = AbacAttributes(region=None)
    assert abac.condition_holds(AbacCondition(attribute="region", op="not_in", values=["eu"]), unknown)  # fail closed
    assert not abac.condition_holds(AbacCondition(attribute="region", values=["eu"]), unknown)
    with pytest.raises(ValidationError):
        AbacCondition(attribute="purpose", op="at_least", values=["x"])
    with pytest.raises(ValidationError):
        AbacCondition(attribute="classification", values=["top-secret"])
    with pytest.raises(ValidationError):
        AbacRule(id="empty", when=[])
    doc = WorkspacePolicyDoc.model_validate({"abac_rules": [NO_ROWS_FOR_MARKETING.model_dump()]})
    assert doc.abac_rules[0].id == "no_rows_for_marketing"


def test_builtin_engine_platform_rules_then_workspace_rules():
    assert abac.builtin_decide(_inp(region="us-east-1", residency="eu"), []).rule == "platform.data_residency"
    assert abac.builtin_decide(_inp(region="eu-west-1", residency="eu"), []).allow
    assert abac.builtin_decide(_inp(region=None, residency="eu"), []).allow  # unknown region: a workspace rule can deny
    restricted = abac.builtin_decide(_inp(point="action", classification="restricted", audience="external"), [])
    assert restricted.rule == "platform.restricted_external" and restricted.remedy
    assert abac.builtin_decide(_inp(classification="restricted", audience="workspace"), []).allow
    denied = abac.builtin_decide(_inp(purpose="marketing.campaign", aggregation_grain="row"), [NO_ROWS_FOR_MARKETING])
    assert (denied.allow, denied.rule, denied.reason, denied.remedy) == (
        False, "no_rows_for_marketing", "marketing sees aggregates only", "group the query")
    assert abac.builtin_decide(_inp(purpose="marketing", aggregation_grain="aggregate"), [NO_ROWS_FOR_MARKETING]).allow
    tools_only = NO_ROWS_FOR_MARKETING.model_copy(update={"applies_to": ["tool"]})
    assert abac.builtin_decide(_inp(purpose="marketing", aggregation_grain="row"), [tools_only]).allow
    assert abac.builtin_decide(_inp(region=None), [EU_ONLY]).rule == "eu_only"


def test_denial_is_403_policy_denied_with_rule_reason_remedy():
    exc = abac.denial(abac.builtin_decide(_inp(region=None), [EU_ONLY]), what="query")
    body = exc.to_dict()
    assert exc.http_status == 403 and body["code"] == "policy_denied"
    assert {k: body["details"][k] for k in ("rule", "reason", "remedy")} == {
        "rule": "eu_only", "reason": "EU callers only", "remedy": "ask from an EU region"}


# ------------------------------------------------------------------------------ OPA adapter
@pytest.fixture()
def opa_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "policy_engine", "opa")
    yield
    abac.use_opa(None)


def _opa(answer=None, exc=None):
    calls = []

    def post(url, body, timeout):
        calls.append((url, body, timeout))
        if exc is not None:
            raise exc
        return answer

    return abac.OpaEngine("http://opa:8181/", "analystos/abac/decision", 0.5, post=post), calls


def test_builtin_is_the_default_and_never_calls_opa():
    engine, calls = _opa({"result": {"allow": False}})
    abac.use_opa(engine)
    try:
        assert get_settings().policy_engine == "builtin"
        assert abac.decide(_inp()).allow and calls == []
    finally:
        abac.use_opa(None)


def test_opa_allow_and_deny_carry_rule_reason_remedy(opa_mode):
    engine, calls = _opa({"result": {"allow": True}})
    abac.use_opa(engine)
    assert abac.decide(_inp(purpose="analysis"), [EU_ONLY.model_copy(update={"applies_to": ["tool"]})]).allow
    url, body, timeout = calls[0]
    assert url == "http://opa:8181/v1/data/analystos/abac/decision" and timeout == 0.5
    assert body["input"]["attributes"] == {"classification": "internal", "purpose": "analysis", "region": None,
                                           "aggregation_grain": "none", "audience": "workspace"}
    assert body["input"]["workspace_rules"][0]["id"] == "eu_only"
    engine, _ = _opa({"result": {"allow": False, "rule": "opa.no_weekend", "reason": "closed", "remedy": "monday"}})
    abac.use_opa(engine)
    d = abac.decide(_inp())
    assert (d.allow, d.rule, d.reason, d.remedy, d.engine) == (False, "opa.no_weekend", "closed", "monday", "opa")
    engine, _ = _opa({"result": True})
    abac.use_opa(engine)
    assert abac.decide(_inp()).allow


@pytest.mark.parametrize("answer", [{}, {"result": None}, {"result": {"allow": "yes"}}, {"result": {}}, [1], None])
def test_opa_malformed_answer_denies(opa_mode, answer):
    engine, _ = _opa(answer)
    abac.use_opa(engine)
    d = abac.decide(_inp())
    assert not d.allow and d.rule == "opa.invalid_decision"


def test_opa_unreachable_denies(opa_mode):
    engine, _ = _opa(exc=TimeoutError("slow"))
    abac.use_opa(engine)
    d = abac.decide(_inp())
    assert not d.allow and d.rule == "opa.unavailable" and d.remedy


def test_opa_cannot_allow_what_the_builtin_engine_denies(opa_mode):
    engine, calls = _opa({"result": {"allow": True}})
    abac.use_opa(engine)
    assert abac.decide(_inp(region="us", residency="eu")).rule == "platform.data_residency"
    assert calls == []


class _OpaHandler(BaseHTTPRequestHandler):
    answer: dict = {}
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body))
        raw = json.dumps(type(self).answer).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


def test_opa_over_http_and_connection_refused(opa_mode, monkeypatch):
    """The default transport (httpx) against a local OPA-shaped server, then a closed port (fail closed)."""
    server = HTTPServer(("127.0.0.1", 0), _OpaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _OpaHandler.answer = {"result": {"allow": False, "rule": "opa.pii", "reason": "r", "remedy": "m"}}
        monkeypatch.setattr(get_settings(), "policy_opa_url", f"http://127.0.0.1:{server.server_port}")
        d = abac.decide(_inp(classification="confidential"))
        assert (d.allow, d.rule) == (False, "opa.pii")
        path, body = _OpaHandler.seen[-1]
        assert path == "/v1/data/analystos/abac/decision" and body["input"]["attributes"]["classification"] == "confidential"
    finally:
        server.shutdown()
        server.server_close()
    monkeypatch.setattr(get_settings(), "policy_opa_url", f"http://127.0.0.1:{server.server_port}")
    assert abac.decide(_inp()).rule == "opa.unavailable"


# ------------------------------------------------------------------------------ gateway: every query
class _FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value.encode()

    def delete(self, key):
        self.store.pop(key, None)


class _Session:
    def __init__(self, sink, source):
        self.sink, self.source = sink, source

    def get(self, model, key):
        if model is Workspace:
            return SimpleNamespace(id=key, status="active", deleted_at=None, tenant_id="tnt_a")
        return self.source if key == self.source.id else None

    def execute(self, stmt):
        return SimpleNamespace(one=lambda: (1, None, 10))

    def add(self, obj):
        self.sink.append(obj)

    def commit(self): ...

    def rollback(self): ...

    def close(self): ...


@pytest.fixture()
def gateway():
    audit: list = []
    source = SimpleNamespace(id="s1", workspace_id="ws_1", kind="servicenow", config={}, secret_ref=None, status="ready",
                             execution_mode="staged", staging_schema="src_s1", last_discovered_at=None)
    cache = QueryCache(Settings(), client=_FakeRedis())
    gw = QueryGateway(Settings(), session_factory=lambda: _Session(audit, source), cache=cache)
    runs: list = []
    gw._run = lambda *a, **k: runs.append(a) or (["priority"], [[1]])  # type: ignore[method-assign]
    return gw, audit, runs, cache


def _scope(**abac_kw) -> DataScope:
    return DataScope(workspace_id="ws_1", user_id="u_1", role="analyst", source_ids=["s1"], assets=["src_s1.incident"],
                     asset_sources={"src_s1.incident": "s1"},
                     columns={"src_s1.incident": ["number", "priority", "caller_email"]},
                     source_dialects={"s1": "postgres"}, max_rows=100, timeout_seconds=10,
                     abac=AbacScope(column_classes={"src_s1.incident.caller_email": "confidential"}, **abac_kw))


def test_gateway_decides_every_query_before_the_cache(gateway):
    gw, audit, runs, _ = gateway
    scope = _scope(rules=[NO_ROWS_FOR_MARKETING])
    ok = gw.execute(scope, "SELECT priority, COUNT(*) AS n FROM incident GROUP BY priority", actor="user:u_1",
                    purpose="marketing.campaign")
    assert not ok.cache_hit and len(runs) == 1
    rows_sql = "SELECT number, priority FROM incident"
    first = gw.execute(scope, rows_sql, actor="user:u_1", purpose="analysis")  # cached for purpose analysis
    assert not first.cache_hit
    with pytest.raises(PolicyDenied) as info:
        gw.execute(scope, rows_sql, actor="user:u_1", purpose="marketing.campaign")
    assert info.value.details["rule"] == "no_rows_for_marketing" and info.value.details["remedy"] == "group the query"
    assert len(runs) == 2  # neither executed nor served from the entry the analysis call cached
    rejected = audit[-1]
    assert isinstance(rejected, QueryExecution) and rejected.status == "rejected"
    assert "no_rows_for_marketing" in rejected.rejected_reason


def test_gateway_classification_and_region_reach_the_decision(gateway):
    gw, _, _, _ = gateway
    confidential_rows = AbacRule(id="no_confidential_rows", when=[
        AbacCondition(attribute="classification", op="at_least", values=["confidential"]),
        AbacCondition(attribute="aggregation_grain", values=["row"])])
    scope = _scope(rules=[confidential_rows])
    assert gw.execute(scope, "SELECT number FROM incident", actor="user:u_1").rows
    assert gw.execute(scope, "SELECT COUNT(DISTINCT caller_email) AS n FROM incident", actor="user:u_1").rows
    with pytest.raises(PolicyDenied, match="no_confidential_rows"):
        gw.execute(scope, "SELECT caller_email FROM incident", actor="user:u_1")
    abroad = _scope(region="us-east-1", residency="eu")
    with pytest.raises(PolicyDenied) as info:
        gw.execute(abroad, "SELECT COUNT(*) AS n FROM incident", actor="user:u_1")
    assert info.value.details["rule"] == "platform.data_residency"


def test_gateway_opa_unreachable_denies_queries(gateway, opa_mode):
    gw, _, runs, _ = gateway
    engine, _ = _opa(exc=ConnectionRefusedError())
    abac.use_opa(engine)
    with pytest.raises(PolicyDenied) as info:
        gw.execute(_scope(), "SELECT COUNT(*) AS n FROM incident", actor="user:u_1")
    assert info.value.details["rule"] == "opa.unavailable" and runs == []


def test_scope_hash_ignores_abac_facts():
    assert _scope().scope_hash() == _scope(region="eu", rules=[EU_ONLY]).scope_hash()
    plain = _scope()
    plain.abac = None
    assert plain.scope_hash() == _scope().scope_hash()


# ------------------------------------------------------------------------------ tool calls (ToolRuntime pre-hook)
@pytest.fixture()
def world(sqlite_db):
    from analystos.db.base import session_scope
    from analystos.db.models import ToolDefinition, User, WorkspaceMember, WorkspacePolicy
    from analystos.tools.registry import BUILTIN_TOOLS

    with session_scope() as s:
        s.add(User(id="usr_1", email="a@x", name="A", password_hash="-", active=True, attributes={"region": "US-East-1"}))
        s.add(Workspace(id="ws_1", name="W", autonomy_level=3, created_by="usr_1", policy_version=1, settings={}))
        s.add(WorkspaceMember(workspace_id="ws_1", user_id="usr_1", role="editor"))
        for t in BUILTIN_TOOLS:
            s.add(ToolDefinition(id=t.tool_id, spec=t.model_dump(), enabled=True))

    def policy(**doc):
        with session_scope() as s:
            ws = s.get(Workspace, "ws_1")
            ws.policy_version += 1
            s.add(WorkspacePolicy(workspace_id="ws_1", version=ws.policy_version,
                                  document=WorkspacePolicyDoc(**doc).model_dump(), created_by="usr_1"))
    return policy


def _runtime(scope=None, purpose="analysis"):
    from analystos.contracts.registry import AgentSpec
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.tools.registry import ToolRuntime

    with session_scope() as s:
        user = s.get(User, "usr_1")
        s.expunge(user)
    agent = AgentSpec.model_validate({"id": "analyst", "name": "a", "description": "", "tools": ["metadata.read"]})
    identity = ExecutionIdentity(user_id="usr_1", workspace_id="ws_1", agent_id="analyst", purpose=purpose, run_id=None)
    return ToolRuntime(user=user, identity=identity, agent=agent, scope=scope)


def test_tool_call_is_decided_with_abac_and_denial_is_recorded(world):
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import ToolExecution

    assert _runtime().authorize("metadata.read").details["abac"]["allow"] is True
    world(abac_rules=[EU_ONLY.model_dump()])
    with pytest.raises(PolicyDenied) as info:
        _runtime().authorize("metadata.read")
    assert info.value.details == {"rule": "eu_only", "reason": "EU callers only", "remedy": "ask from an EU region",
                                  "engine": "builtin"}
    with session_scope() as s:
        row = s.scalars(select(ToolExecution).where(ToolExecution.status == "denied")).one()
    assert row.decision["abac"]["rule"] == "eu_only" and "abac:eu_only" in row.decision["reasons"]


def test_tool_call_uses_the_workspace_residency_and_the_step_scope(world):
    world(data_residency="eu")
    with pytest.raises(PolicyDenied) as info:
        _runtime().authorize("metadata.read")  # the user's region attribute: us-east-1
    assert info.value.details["rule"] == "platform.data_residency"
    world()
    purpose_rule = AbacRule(id="no_confidential_marketing", applies_to=["tool"], when=[
        AbacCondition(attribute="purpose", values=["marketing"]),
        AbacCondition(attribute="classification", op="at_least", values=["confidential"])])
    world(abac_rules=[purpose_rule.model_dump()])
    assert _runtime(purpose="marketing").authorize("metadata.read")  # no scope: internal
    with pytest.raises(PolicyDenied, match="no_confidential_marketing"):
        _runtime(scope=_scope(), purpose="marketing").authorize("metadata.read")
    assert _runtime(scope=_scope(), purpose="analysis").authorize("metadata.read")


def test_publish_to_an_external_audience_is_denied_for_restricted_data(world):
    from analystos.contracts.registry import ToolSpec
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.governance.policy import denied, evaluate

    scope = _scope()
    scope.abac.column_classes["src_s1.incident.number"] = "restricted"
    identity = ExecutionIdentity(user_id="usr_1", workspace_id="ws_1", purpose="analysis")
    tool = ToolSpec(tool_id="superset.publish", name="p", category="bi_publishing", side_effects="external_write",
                    description="d", min_role="editor")
    with session_scope() as s:
        decision = evaluate(s, s.get(User, "usr_1"), identity, "publish", tool=tool, destination="superset", scope=scope)
        internal = evaluate(s, s.get(User, "usr_1"), identity, "tool", tool=ToolSpec(
            tool_id="metadata.read", name="m", category="metadata", description="d"), scope=scope)
    assert decision.decision == "deny" and "abac:platform.restricted_external" in decision.reasons
    assert denied(decision, "publication denied").details["rule"] == "platform.restricted_external"
    assert internal.decision == "allow"
