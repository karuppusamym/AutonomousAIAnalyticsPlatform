"""MCP for any application (Copilot, Claude, Cursor, an in-house app): metadata, stored queries and governed SQL,
plus the client setup command and the one-command local mode. No services."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from analystos import local
from analystos.core.errors import Forbidden, InvalidInput, PolicyDenied
from analystos.mcp import grants as G
from analystos.mcp import server as S
from analystos.mcp import setup_cli


# ------------------------------------------------------------------------------------ grants
def test_assistant_tools_are_grantable_with_the_right_roles():
    for tool in ("catalog.tables", "queries.list", "queries.run", "sql.run"):
        assert tool in G.GRANTABLE and tool in S.TOOL_DEFS and tool in S.TOOL_IMPLS
    assert G.min_role("catalog.tables") == G.min_role("queries.list") == "viewer"
    assert G.min_role("sql.run") == G.min_role("queries.run") == "analyst"  # running SQL needs an analyst grant
    assert all(S.TOOL_DEFS[t]["read_only"] for t in G.ASSISTANT_TOOLS)


def test_setup_defaults_cover_metadata_stored_queries_and_sql():
    assert set(setup_cli.DEFAULT_TOOLS) >= {"catalog.tables", "queries.list", "queries.run", "sql.run", "context.read"}
    assert all(t in G.GRANTABLE for t in setup_cli.DEFAULT_TOOLS)


# ------------------------------------------------------------------------------------ client configs
def test_vscode_config_never_stores_the_token_and_keeps_other_servers():
    existing = {"servers": {"github": {"type": "http", "url": "https://x"}}, "inputs": [{"id": "other"}]}
    cfg = setup_cli.vscode_config(existing, "http://127.0.0.1:8000/mcp")
    assert set(cfg["servers"]) == {"github", "analystos"}
    server = cfg["servers"]["analystos"]
    assert server == {"type": "http", "url": "http://127.0.0.1:8000/mcp",
                      "headers": {"Authorization": "Bearer ${input:analystos-mcp-token}"}}
    token_input = next(i for i in cfg["inputs"] if i["id"] == setup_cli.TOKEN_INPUT)
    assert token_input["password"] is True and {"id": "other"} in cfg["inputs"]
    again = setup_cli.vscode_config(cfg, "http://127.0.0.1:8000/mcp")  # re-running does not duplicate the prompt
    assert sum(i["id"] == setup_cli.TOKEN_INPUT for i in again["inputs"]) == 1


def test_generic_config_is_the_common_mcp_servers_shape():
    cfg = setup_cli.generic_config("http://h:1/mcp", "mcpc_1.secret")
    assert cfg == {"mcpServers": {"analystos": {"type": "http", "url": "http://h:1/mcp",
                                                "headers": {"Authorization": "Bearer mcpc_1.secret"}}}}


# ------------------------------------------------------------------------------------ local mode
def test_local_environment_points_every_database_url_at_the_bundled_server(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYSTOS_DATABASE_URL", "postgresql+psycopg://x:y@elsewhere:5432/other")  # never wins
    monkeypatch.setenv("ANALYSTOS_REDIS_URL", "redis://office:6379/0")  # a user choice wins
    env = local.environment(tmp_path, "127.0.0.1", 61234, api_host="127.0.0.1", api_port=8000,
                            web_dist=Path("/w/dist"), demo=False)
    for key in ("ANALYSTOS_DATABASE_URL", "ANALYSTOS_ANALYTICS_LOADER_URL", "ANALYSTOS_ANALYTICS_READER_URL",
                "ANALYSTOS_ANALYTICS_BUILDER_URL", "ANALYSTOS_ANALYTICS_WRITER_URL"):
        assert "@127.0.0.1:61234/" in env[key], key
    assert env["ANALYSTOS_REDIS_URL"] == "redis://office:6379/0"
    assert env["ANALYSTOS_PROFILE"] == "lite" and env["ANALYSTOS_VECTOR_BACKEND"] == "pgvector"
    assert env["ANALYSTOS_WEB_DIST"] == str(Path("/w/dist")) and "ANALYSTOS_SERVICENOW_MOCK_URL" not in env


def test_local_jwt_secret_is_per_install_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("ANALYSTOS_JWT_SECRET", raising=False)
    first = local.environment(tmp_path, "h", 1, api_host="h", api_port=2, web_dist=None, demo=True)
    second = local.environment(tmp_path, "h", 1, api_host="h", api_port=2, web_dist=None, demo=True)
    assert first["ANALYSTOS_JWT_SECRET"] == second["ANALYSTOS_JWT_SECRET"] and len(first["ANALYSTOS_JWT_SECRET"]) >= 40
    assert first["ANALYSTOS_JWT_SECRET"] != "dev-only-change-me-please-32bytes!!"
    assert first["ANALYSTOS_SERVICENOW_MOCK_URL"].endswith(f":{local.MOCK_PORT}")


# ------------------------------------------------------------------------------------ web on the API port
def test_web_static_serves_the_app_but_never_shadows_api_paths(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from analystos.api import web_static

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path.parent / "secret.txt").write_text("nope", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    assert web_static.mount(app, tmp_path)
    c = TestClient(app)
    assert c.get("/api/health").json() == {"ok": True}
    asset = c.get("/assets/app.js")
    assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
    assert "script-src 'self'" in c.get("/").headers["content-security-policy"]  # the page may load its own code
    assert c.get("/w/ws_1/ask").text == "<html>app</html>"  # client-side route -> app shell
    assert c.get("/api/nope").status_code == 404 and c.get("/api/nope").json()["error"]["code"] == "not_found"
    assert c.get("/mcp-not-here").status_code == 404
    assert "nope" not in c.get("/../secret.txt").text
    assert not web_static.mount(FastAPI(), tmp_path / "missing") and not web_static.mount(FastAPI(), None)


# ------------------------------------------------------------------------------------ governed run
@pytest.fixture
def governed(monkeypatch):
    """`_governed_run` with the session, scope, validator and gateway replaced by recorders."""
    calls = {"audit": [], "executed": [], "validated": []}

    @contextmanager
    def fake_session():
        yield SimpleNamespace(commit=lambda: None)

    scope = SimpleNamespace(max_rows=500)
    monkeypatch.setattr(S, "session_scope", fake_session)
    monkeypatch.setattr(S, "_service_user", lambda s, p: SimpleNamespace(id="usr_svc"))
    monkeypatch.setattr(S, "resolve_scope", lambda s, u, ws: scope)
    monkeypatch.setattr(S, "audit", lambda *a, **k: calls["audit"].append((a, k)))

    def validate(scope_, sql, max_rows):
        calls["validated"].append(sql)
        if "DROP" in sql.upper():
            raise PolicyDenied("only read-only SELECT statements are allowed")
        return SimpleNamespace(dialect="postgres", referenced_assets=["itsm.incident"])

    monkeypatch.setattr("analystos.gateway.validator.validate_sql", validate)

    class Gateway:
        def execute(self, scope_, sql, **kw):
            calls["executed"].append((sql, kw))
            return SimpleNamespace(query_id="qe_1", columns=["n"], rows=[[i] for i in range(kw["max_rows"] + 5)],
                                   row_count=kw["max_rows"] + 5, truncated=False, duration_ms=3,
                                   referenced_assets=["itsm.incident"], handle=None)

    monkeypatch.setattr("analystos.runtime.context.default_gateway", lambda: Gateway())
    principal = SimpleNamespace(client_id="mcpc_1", grants={"ws_1": {"tools": ["sql.run"]}})
    return SimpleNamespace(calls=calls, principal=principal)


def test_sql_run_executes_through_the_gateway_with_the_row_cap(governed):
    out = S._tool_sql_run(governed.principal, "ws_1", {"sql": "SELECT count(*) AS n FROM itsm.incident", "max_rows": 10})
    assert out["executed"] and out["rows_returned"] == 10 and out["truncated"] and out["dialect"] == "postgres"
    (sql, kw), = governed.calls["executed"]
    assert kw["actor"] == "mcp_client:mcpc_1" and kw["purpose"] == "mcp.sql_run" and kw["max_rows"] == 10


def test_sql_run_caps_max_rows_at_the_mcp_limit(governed):
    S._tool_sql_run(governed.principal, "ws_1", {"sql": "SELECT 1 AS n FROM itsm.incident", "max_rows": 10_000})
    assert governed.calls["executed"][0][1]["max_rows"] == S.MAX_ROWS_OUT


def test_rejected_sql_is_audited_raised_and_never_executed(governed):
    with pytest.raises(PolicyDenied):  # a tool error, so the MCP result is recorded as refused, not ok
        S._tool_sql_run(governed.principal, "ws_1", {"sql": "DROP TABLE itsm.incident"})
    assert not governed.calls["executed"]
    (args, kwargs), = governed.calls["audit"]
    assert args[1] == "query.validate_rejected" and kwargs["decision"] == "deny"


def test_sql_outside_the_grant_slice_is_refused_before_the_validator_sees_it(governed, monkeypatch):
    """The validator's messages name known tables and columns: on a slice it must not run for outside tables."""
    governed.principal.grants["ws_1"]["domains"] = ["finance"]
    monkeypatch.setattr("analystos.context.retrieval.slice_assets", lambda s, ws, d, a: frozenset({"fin.ledger"}))
    with pytest.raises(Forbidden) as err:
        S._tool_sql_run(governed.principal, "ws_1", {"sql": "SELECT 1 FROM itsm.incident"})
    assert "itsm" not in err.value.message  # one generic refusal
    assert not governed.calls["validated"] and not governed.calls["executed"]
    inside = S._tool_sql_run(governed.principal, "ws_1", {"sql": "WITH x AS (SELECT * FROM fin.ledger) SELECT * FROM x"})
    assert inside["executed"]  # the CTE name is not a table


def test_unparseable_sql_on_a_slice_gets_the_same_refusal(governed, monkeypatch):
    governed.principal.grants["ws_1"]["assets"] = ["fin.ledger"]
    monkeypatch.setattr("analystos.context.retrieval.slice_assets", lambda s, ws, d, a: frozenset({"fin.ledger"}))
    with pytest.raises(Forbidden):
        S._tool_sql_run(governed.principal, "ws_1", {"sql": "SELECT FROM WHERE ((("})


def test_sql_run_needs_sql_and_an_integer_row_count():
    with pytest.raises(InvalidInput):
        S._tool_sql_run(SimpleNamespace(client_id="c", grants={}), "ws_1", {"sql": " "})
    with pytest.raises(InvalidInput):
        S._max_rows({"max_rows": "lots"})


def test_a_sliced_grant_accepts_the_assistant_tools_in_check_grant():
    principal = SimpleNamespace(grants={"ws_1": {"tools": ["sql.run", "catalog.tables"], "domains": ["finance"]}})
    assert G.check_grant(principal, "ws_1", "sql.run")["domains"] == ["finance"]
    principal.grants["ws_1"]["tools"].append("ask")
    with pytest.raises(Forbidden):
        G.check_grant(principal, "ws_1", "ask")  # not slice-aware: refused on a slice


# ------------------------------------------------------------------------------------ stored queries
def test_parameter_binding_is_one_pass_so_a_value_cannot_capture_another():
    from analystos.registries.verified_queries import render

    params = [{"name": "a", "type": "string"}, {"name": "b", "type": "string"}]
    sql = render("SELECT * FROM t WHERE x = {{a}} AND y = {{b}}", params, {"a": "{{b}}", "b": "z' OR '1'='1"}, "postgres")
    assert sql == "SELECT * FROM t WHERE x = '{{b}}' AND y = 'z'' OR ''1''=''1'"
    with pytest.raises(InvalidInput):
        render("SELECT {{missing}}", params, {"a": "1", "b": "2"}, "postgres")


def test_stored_query_values_are_shown_only_with_the_values_flag():
    vq = SimpleNamespace(id="vq_1", name="n", description="", patterns=[], source_id="src", dialect="postgres", hits=0,
                         parameters=[{"name": "region", "type": "enum", "values": ["North", "South"], "default": "North"}])
    assert "values" not in S._query_view(vq, with_values=False)["parameters"][0]
    assert "default" not in S._query_view(vq, with_values=False)["parameters"][0]  # a default is a data value too
    assert S._query_view(vq, with_values=True)["parameters"][0]["values"] == ["North", "South"]


def test_a_stored_query_reading_a_denied_column_is_not_offered():
    tpl = "SELECT state, COUNT(*) FROM itsm.incident WHERE caller_email = {{email}} GROUP BY state"
    assert S._reads_denied(tpl, "postgres", ["itsm.incident"], {"itsm.incident.caller_email"})
    assert not S._reads_denied(tpl, "postgres", ["itsm.incident"], {"itsm.incident.salary"})
    assert not S._reads_denied(tpl, "postgres", ["itsm.incident"], set())


# ------------------------------------------------------------------------------------ local hardening
def test_trust_rules_become_scram_and_comments_stay():
    text = "# TYPE DATABASE USER ADDRESS METHOD\n# host all all 0.0.0.0/0 trust\nhost all all 127.0.0.1/32 trust\n" \
           "host all all ::1/128   trust\nlocal all all peer\n"
    out = local.hardened_hba(text)
    assert "host all all 127.0.0.1/32 scram-sha-256" in out and "host all all ::1/128   scram-sha-256" in out
    assert "# host all all 0.0.0.0/0 trust" in out and "local all all peer" in out
    assert local.hardened_hba(out) == out  # idempotent


def test_generated_passwords_are_per_install_private_and_used_in_every_url(tmp_path):
    pw = local.passwords(tmp_path)
    assert set(pw) == set(local.ROLES) and len(set(pw.values())) == len(local.ROLES)
    assert local.passwords(tmp_path) == pw  # stable across starts
    env = local.environment(tmp_path, "127.0.0.1", 5, api_host="127.0.0.1", api_port=8000, web_dist=None, demo=False)
    assert "analystos:analystos@" not in env["ANALYSTOS_DATABASE_URL"] and ":loader@" not in env["ANALYSTOS_ANALYTICS_LOADER_URL"]


def test_a_local_run_listens_on_loopback_unless_shared_without_dev_defaults():
    local.check_bind("127.0.0.1", False, "dev", "ChangeMe123!", "ChangeMe123!")
    with pytest.raises(SystemExit):
        local.check_bind("0.0.0.0", False, "production", "s3cret-pw", "ChangeMe123!")
    with pytest.raises(SystemExit):
        local.check_bind("0.0.0.0", True, "dev", "s3cret-pw", "ChangeMe123!")
    with pytest.raises(SystemExit):
        local.check_bind("0.0.0.0", True, "production", "ChangeMe123!", "ChangeMe123!")
    local.check_bind("0.0.0.0", True, "production", "s3cret-pw", "ChangeMe123!")


def test_sliced_setup_keeps_only_slice_aware_default_tools():
    allowed = (*G.CONTEXT_TOOLS, G.CONTEXT_READ, *G.ASSISTANT_TOOLS)
    assert [t for t in setup_cli.DEFAULT_TOOLS if t not in allowed] == ["validate_sql", "ask"]
