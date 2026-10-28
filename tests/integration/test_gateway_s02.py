"""S02 on the compose Postgres: result handles + TTL purge (T3), the scan budget stop (T4, AT-04), session
identity reaching the source's own row security (T5), a source outage (AT-12), cancelling a statement in
flight, refusals audited with reason codes and redacted SQL, the QueryRequest protocol (T1), and the
secret_ref policy at registration. Skips cleanly without Postgres."""
from __future__ import annotations

import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.core.errors import (  # noqa: E402
    BudgetExceeded,
    Forbidden,
    NotFound,
    RunCancelled,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id  # noqa: E402
from dataplane_fixtures import *  # noqa: E402,F403

pytestmark = pytest.mark.integration
RLS_ROLE = "aostest_s02_rls"
RLS_PASSWORD = "rls-test-only"  # a throwaway login created and dropped by this module, not a real credential
ROWS = 2000


@pytest.fixture(scope="module")
def shop(dp_control_url, dp_session_factory, dp_workspace):
    """`s02shop.events` (2000 rows, half per owner) under row-level security keyed on app.user, read by a
    non-owner login, registered as a Postgres pushdown source with catalogued columns (`note` tagged pii)."""
    from analystos.db.models import Source, SourceAsset, SourceColumn

    url = make_url(dp_control_url)
    admin = create_engine(dp_control_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS s02shop CASCADE"))
        conn.execute(text(f"DROP ROLE IF EXISTS {RLS_ROLE}"))
        conn.execute(text(f"CREATE ROLE {RLS_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{RLS_PASSWORD}'"))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{url.database}" TO {RLS_ROLE}'))
        conn.execute(text("CREATE SCHEMA s02shop"))
        conn.execute(text("CREATE TABLE s02shop.events (id int PRIMARY KEY, owner text NOT NULL, amount numeric(10,2), "
                          "note text)"))
        conn.execute(text(f"INSERT INTO s02shop.events SELECT g, CASE WHEN g % 2 = 0 THEN 'u_alice' ELSE 'u_bob' END, "
                          f"g * 1.25, repeat('n', 40) FROM generate_series(1, {ROWS}) g"))
        conn.execute(text("ALTER TABLE s02shop.events ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("CREATE POLICY own_rows ON s02shop.events FOR SELECT "
                          "USING (owner = current_setting('app.user', true))"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA s02shop TO {RLS_ROLE}"))
        conn.execute(text(f"GRANT SELECT ON s02shop.events TO {RLS_ROLE}"))
        conn.execute(text("ANALYZE s02shop.events"))
    sid = new_id("pg")
    ws = dp_workspace["workspace_id"]
    config = {"host": url.host, "port": url.port or 5432, "database": url.database, "username": RLS_ROLE,
              "schemas": ["s02shop"]}
    with dp_session_factory() as s:
        s.add(Source(id=sid, workspace_id=ws, kind="postgres", name="s02 shop", config=config,
                     secret_ref="env:ANALYSTOS_SOURCE_SECRET_S02_RLS", status="ready", execution_mode="pushdown"))
        s.flush()
        aid = new_id("ast")
        s.add(SourceAsset(id=aid, source_id=sid, workspace_id=ws, schema_name="s02shop", name="events",
                          source_name="events", selected=True, row_count=ROWS))
        s.flush()
        for i, (col, typ) in enumerate((("id", "integer"), ("owner", "text"), ("amount", "numeric"), ("note", "text"))):
            s.add(SourceColumn(asset_id=aid, name=col, ordinal=i, data_type=typ, tags=["pii"] if col == "note" else []))
        s.commit()
    yield SimpleNamespace(source_id=sid, config=config, workspace_id=ws)
    with admin.connect() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS s02shop CASCADE"))
        conn.execute(text(f'REVOKE CONNECT ON DATABASE "{url.database}" FROM {RLS_ROLE}'))
        from analystos.gateway.engines import dispose_all

        dispose_all()  # pooled connections of the RLS login must close before it can be dropped
        conn.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{RLS_ROLE}'"))
        conn.execute(text(f"DROP ROLE IF EXISTS {RLS_ROLE}"))
    admin.dispose()


@pytest.fixture()
def env(shop, dp_settings, dp_session_factory, tmp_path, monkeypatch):
    from analystos.gateway.service import QueryGateway

    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_S02_RLS", RLS_PASSWORD)
    settings = dp_settings.model_copy(update={"result_store_dir": tmp_path / "results"})
    gw = QueryGateway(settings, session_factory=dp_session_factory)
    return SimpleNamespace(gw=gw, settings=settings, sf=dp_session_factory, shop=shop)


def _scope(shop, user: str = "u_alice", **over):
    from analystos.contracts.policy import DataScope

    sid = shop.source_id
    base = dict(workspace_id=shop.workspace_id, user_id=user, role="analyst", source_ids=[sid],
                assets=["s02shop.events"], asset_sources={"s02shop.events": sid},
                columns={"s02shop.events": ["id", "owner", "amount", "note"]}, source_dialects={sid: "postgres"},
                max_rows=50_000, timeout_seconds=20)
    base.update(over)
    return DataScope(**base)


def _audit(sf, query_id: str):
    from analystos.db.models import QueryExecution

    with sf() as s:
        return s.get(QueryExecution, query_id)


# ---------------------------------------------------------------------------------------------- T5
def test_session_identity_reaches_the_sources_row_security(env) -> None:
    """The source's own RLS policy (owner = current_setting('app.user')) filters by the end user (S02-T5)."""
    for user in ("u_alice", "u_bob"):
        res = env.gw.execute(_scope(env.shop, user), "SELECT owner, COUNT(*) AS n FROM events GROUP BY owner",
                             actor=f"user:{user}", use_cache=False)
        assert res.rows == [[user, ROWS // 2]], user
    nobody = env.gw.execute(_scope(env.shop, "u_carol"), "SELECT COUNT(*) AS n FROM events", actor="user:u_carol",
                            use_cache=False)
    assert nobody.rows == [[0]]


# ---------------------------------------------------------------------------------------------- T3
def test_large_results_become_handles_small_ones_stay_inline(env) -> None:
    from analystos.db.models import ResultHandle
    from analystos.gateway.handles import open_dataset, path_for, read_rows

    small = env.gw.execute(_scope(env.shop), "SELECT COUNT(*) AS n FROM events", actor="user:u_alice",
                           result_mode="auto", use_cache=False)
    assert small.handle is None and small.inline == [[ROWS // 2]]

    big = env.gw.execute(_scope(env.shop), "SELECT id, amount, note FROM events ORDER BY id", actor="user:u_alice",
                         result_mode="auto", use_cache=False)
    h = big.handle
    assert h is not None and big.rows == [] and big.row_count == ROWS // 2 and not big.truncated
    assert h.uri.endswith(f"/results/{big.query_id}.parquet") and h.uri.startswith("tenant/")
    assert h.classification_max == "pii"  # `note` is tagged pii
    assert [c["name"] for c in h.result_schema] == ["id", "amount", "note"]
    assert path_for(env.settings, h.uri).stat().st_size == h.bytes
    with env.sf() as s:
        cols, rows = read_rows(h, workspace_id=env.shop.workspace_id, limit=3, settings=env.settings, session=s)
        assert cols == ["id", "amount", "note"] and rows[0][:2] == [2, 2.5]
        assert open_dataset(h, workspace_id=env.shop.workspace_id, settings=env.settings, session=s).count_rows() == ROWS // 2
        with pytest.raises(NotFound):  # another workspace cannot open it
            open_dataset(h, workspace_id="ws_other", settings=env.settings, session=s)
        row = s.get(ResultHandle, big.query_id)
        assert row.sha256 == h.sha256 and row.row_count == ROWS // 2
    audit = _audit(env.sf, big.query_id)
    assert audit.result_uri == h.uri and audit.result_hash == h.sha256 and len(audit.result_preview) == 20


def test_tampered_or_expired_handles_do_not_open_and_ttl_purge_deletes(env) -> None:
    from analystos.core.ids import utcnow
    from analystos.db.models import ResultHandle
    from analystos.gateway.handles import open_dataset, path_for, purge_expired_results

    res = env.gw.execute(_scope(env.shop), "SELECT id, note FROM events", actor="user:u_alice", result_mode="handle",
                         use_cache=False)
    path = path_for(env.settings, res.handle.uri)
    original = path.read_bytes()
    path.write_bytes(original[:-8] + b"tampered")
    with env.sf() as s, pytest.raises(Forbidden, match="sha256"):
        open_dataset(res.handle, workspace_id=env.shop.workspace_id, settings=env.settings, session=s)
    path.write_bytes(original)

    with env.sf() as s:
        s.get(ResultHandle, res.query_id).expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
    assert purge_expired_results(env.settings, session_factory=env.sf) >= 1
    assert not path.exists()
    with env.sf() as s:
        assert s.get(ResultHandle, res.query_id).purged_at is not None
        with pytest.raises(NotFound):
            open_dataset(res.handle, workspace_id=env.shop.workspace_id, settings=env.settings, session=s)


# ---------------------------------------------------------------------------------------------- T4 / AT-04
def test_scan_budget_stops_the_statement_before_it_runs(env, dp_session_factory, dp_workspace) -> None:
    """AT-04: the estimate (EXPLAIN) exceeds the budget, the statement is refused and audited, nothing runs."""
    from analystos.db.models import QueryExecution, Workspace, WorkspacePolicy

    est = env.gw.estimate(_scope(env.shop), "SELECT id, note FROM events", actor="user:u_alice")
    assert est.basis == "explain" and est.bytes and est.bytes > 1000

    executed: list = []
    real_run = env.gw._run
    env.gw._run = lambda *a, **k: (executed.append(k.get("sql")), real_run(*a, **k))[1]  # type: ignore[method-assign]
    with pytest.raises(BudgetExceeded, match="scan budget"):
        env.gw.execute(_scope(env.shop), "SELECT id, note FROM events WHERE note <> 'secret-literal'",
                       actor="user:u_alice", max_scan_bytes=1000, use_cache=False)
    # only the estimate ran: the plan, then the size of the relation it scans whole
    assert executed and all(s and (s.startswith("EXPLAIN") or "pg_relation_size" in s) for s in executed)
    with dp_session_factory() as s:
        row = s.scalars(select(QueryExecution).where(QueryExecution.reason_code == "budget.max_scan_bytes")
                        .order_by(QueryExecution.created_at.desc())).first()
    assert row.status == "rejected" and row.cost_estimate["budget"] == 1000 and row.cost_estimate["bytes"] > 1000
    assert "secret-literal" not in row.sql and row.sql_hash

    env.gw._run = real_run  # type: ignore[method-assign]
    platform = env.gw.__class__(env.settings.model_copy(update={"query_max_scan_bytes": 1000}),
                                session_factory=dp_session_factory)
    with pytest.raises(BudgetExceeded):
        platform.execute(_scope(env.shop), "SELECT id FROM events", actor="user:u_alice", use_cache=False)

    with dp_session_factory() as s:  # the workspace policy's own budget
        ws = s.get(Workspace, env.shop.workspace_id)
        old_version = ws.policy_version
        version = (s.scalar(select(WorkspacePolicy.version).where(WorkspacePolicy.workspace_id == ws.id)
                            .order_by(WorkspacePolicy.version.desc()).limit(1)) or 0) + 1
        s.add(WorkspacePolicy(workspace_id=ws.id, version=version, document={"max_scan_bytes": 1000},
                              created_by=dp_workspace["user_id"]))
        ws.policy_version = version
        s.commit()
    try:
        with pytest.raises(BudgetExceeded):
            env.gw.execute(_scope(env.shop), "SELECT id FROM events", actor="user:u_alice", use_cache=False)
        ok = env.gw.execute(_scope(env.shop), "SELECT COUNT(*) AS n FROM events WHERE id = 2", actor="user:u_alice",
                            use_cache=False)  # an index lookup fits the budget
        assert ok.rows == [[1]] and ok.estimate is not None and ok.estimate.bytes <= 1000
    finally:
        with dp_session_factory() as s:
            s.get(Workspace, env.shop.workspace_id).policy_version = old_version
            s.query(WorkspacePolicy).filter(WorkspacePolicy.workspace_id == env.shop.workspace_id,
                                            WorkspacePolicy.version == version).delete()
            s.commit()


# ---------------------------------------------------------------------------------------------- AT-12 / cancel
def test_source_outage_is_retryable_and_audited(env, dp_session_factory) -> None:
    """AT-12: an unreachable source is UpstreamUnavailable (retryable), audited as an error."""
    from analystos.db.models import QueryExecution, Source

    with dp_session_factory() as s:
        down = Source(id=new_id("pg"), workspace_id=env.shop.workspace_id, kind="postgres", name="down",
                      config=dict(env.shop.config, host="127.0.0.1", port=1, connect_timeout=2),
                      secret_ref="env:ANALYSTOS_SOURCE_SECRET_S02_RLS", status="ready", execution_mode="pushdown")
        s.add(down)
        s.commit()
        sid = down.id
    scope = _scope(env.shop, source_ids=[sid], asset_sources={"s02shop.events": sid}, source_dialects={sid: "postgres"})
    with pytest.raises(UpstreamUnavailable) as err:
        env.gw.execute(scope, "SELECT COUNT(*) AS n FROM events", actor="user:u_alice", use_cache=False)
    assert err.value.retryable
    with dp_session_factory() as s:
        row = s.scalars(select(QueryExecution).where(QueryExecution.source_id == sid)).one()
    assert row.status == "error" and row.reason_code == "upstream_unavailable"


def test_cancel_interrupts_a_statement_in_flight(env) -> None:
    from analystos.connectors.postgres import PostgresConnector
    from analystos.engines.sql import run_postgres

    url = PostgresConnector(env.shop.config, "env:ANALYSTOS_SOURCE_SECRET_S02_RLS").sqlalchemy_url()
    qid = new_id("qry")
    outcome: dict = {}

    def run() -> None:
        try:
            run_postgres(url, "SELECT pg_sleep(30)", 1, 60, query_id=qid)
            outcome["done"] = True
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    started = time.monotonic()
    thread.start()
    for _ in range(50):
        time.sleep(0.1)
        if env.gw.cancel(qid):
            break
    thread.join(15)
    assert isinstance(outcome.get("error"), RunCancelled) and time.monotonic() - started < 10
    assert env.gw.cancel(qid) is False  # nothing left running under that id


def test_validator_refusal_keeps_a_reason_code_and_redacted_sql(env) -> None:
    with pytest.raises(SQLRejected):
        env.gw.execute(_scope(env.shop), "SELECT * FROM s02shop.payroll WHERE ssn = '123-45-6789'", actor="user:u_alice")
    from analystos.db.models import QueryExecution

    with env.sf() as s:
        row = s.scalars(select(QueryExecution).where(QueryExecution.status == "rejected",
                                                     QueryExecution.sql_hash.is_not(None))
                        .order_by(QueryExecution.created_at.desc())).first()
    assert row.reason_code.startswith("validator") and "123-45-6789" not in row.sql and "payroll" in row.sql


# ---------------------------------------------------------------------------------------------- T1 protocol
def test_query_request_through_the_in_process_service(env) -> None:
    from analystos.gateway.protocol import InProcessQueryService, QueryBudget, QueryRequest

    svc = InProcessQueryService(env.gw, session_factory=env.sf)
    req = QueryRequest(workspace=env.shop.workspace_id, principal="agent:test", scope=_scope(env.shop),
                       sql="SELECT COUNT(*) AS n FROM events WHERE amount > :floor AND owner = :who",
                       params={"floor": 100, "who": "u_alice' OR '1'='1"}, budget=QueryBudget(max_rows=10))
    res = svc.execute(req)
    assert res.rows == [[0]]  # the injection attempt is one literal value, not SQL
    big = svc.execute(req.model_copy(update={"sql": "SELECT id FROM events", "params": {}, "result_mode": "auto",
                                             "budget": QueryBudget()}))
    assert big.handle is not None and big.row_count == ROWS // 2
    assert svc.estimate(req.model_copy(update={"sql": "SELECT id FROM events", "params": {}})).basis == "explain"


# ---------------------------------------------------------------------------------------------- secret refs
def test_editor_cannot_register_a_platform_env_ref(control_db) -> None:
    """The security finding through the service the API calls: refused at registration, nothing stored."""
    from analystos.db.base import session_scope
    from analystos.db.models import Source, User
    from analystos.services.sources import register_source
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        editor = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        ws = create_workspace(s, owner, name=f"s02-secret {new_id('t')}", objective="", autonomy_level=3)
        add_member(s, owner, ws.id, editor.email, "editor")
        ws_id = ws.id
    for ref in ("env:OPENROUTER_API_KEY", "env:ANALYSTOS_JWT_SECRET", f"file:{Path(__file__).resolve()}"):
        with session_scope() as s, pytest.raises(Forbidden):
            editor = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
            register_source(s, editor, ws_id, kind="postgres", name="exfil",
                            config={"host": "attacker.example", "database": "d", "username": "u"}, secret_ref=ref)
    with session_scope() as s:
        assert not s.scalars(select(Source).where(Source.workspace_id == ws_id)).all()
