"""S02-T7 multi-source and S02-T8 REST on the compose Postgres, through the product flows (register, discover,
select/stage, ingest, resolve_scope, gateway):

* a PostgreSQL pushdown source joined with a CSV file source: each leg runs through the gateway on its own
  source and comes back as a Parquet ResultHandle, DuckDB joins the handles, lineage records both legs, and a
  workspace whose policy says `data_movement: none` is refused;
* a declarative REST source served by a local HTTP stub is staged and read through the gateway.

Skips cleanly without Postgres."""
from __future__ import annotations

import json
import random
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from analystos.core.errors import PolicyDenied

pytestmark = pytest.mark.integration
SRC_DB = "analystos_test_s02_src"
SEGMENTS = ("enterprise", "smb", "mid")


def _admin_url() -> str:
    import os

    return os.environ.get("ANALYSTOS_TEST_ADMIN_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos")


@pytest.fixture(scope="module")
def world(control_db, tmp_path_factory):
    import polars as pl

    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.file_ingest import IngestSpec, ingest_file
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    rng = random.Random(7)
    segments = {cid: rng.choice(SEGMENTS) for cid in range(1, 61)}
    tickets = [(i, rng.randint(1, 60), round(rng.uniform(1, 9), 2)) for i in range(1, 401)]
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SRC_DB} WITH (FORCE)"))
        c.execute(text(f"CREATE DATABASE {SRC_DB}"))
    src_url = make_url(_admin_url()).set(database=SRC_DB)
    src = create_engine(src_url)
    with src.begin() as c:
        c.execute(text("CREATE SCHEMA s02ops"))
        c.execute(text("CREATE TABLE s02ops.tickets (id int PRIMARY KEY, customer_id int NOT NULL, hours numeric(6,2))"))
        c.execute(text("INSERT INTO s02ops.tickets VALUES " + ", ".join(f"({i}, {c_}, {h})" for i, c_, h in tickets)))
    src.dispose()

    import os

    os.environ["ANALYSTOS_SOURCE_SECRET_S02_SRC"] = src_url.password or ""
    get_settings.cache_clear()
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws = create_workspace(s, owner, name=f"s02 multi {new_id('t')}", objective="", autonomy_level=3)
        s.flush()
        pg = register_source(s, owner, ws.id, kind="postgres", name="ops tickets",
                             config={"host": src_url.host, "port": src_url.port or 5432, "database": SRC_DB,
                                     "username": src_url.username, "schemas": ["s02ops"]},
                             secret_ref="env:ANALYSTOS_SOURCE_SECRET_S02_SRC")
        csv = register_source(s, owner, ws.id, kind="csv", name="crm export", config={"path": ws.id}, secret_ref=None)
        s.flush()
        ids = {"ws": ws.id, "pg": pg.id, "csv": csv.id}
        s.expunge(owner)
    folder = Path(get_settings().upload_dir) / ids["ws"]
    folder.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"customer_id": list(segments), "segment": list(segments.values())}).write_csv(folder / "customers.csv")
    discover_source(owner, ids["pg"], ids["ws"])
    select_assets(owner, ids["pg"], ["tickets"], ids["ws"])
    ingest_file(owner, ids["csv"], IngestSpec(path="customers.csv", table="customers"), ids["ws"])
    expected: dict[str, list] = {}
    for _, cid, hours in tickets:
        seg = segments[cid]
        n, total = expected.get(seg, [0, 0.0])
        expected[seg] = [n + 1, round(total + hours, 2)]
    yield {**ids, "owner": owner, "expected": expected, "store": tmp_path_factory.mktemp("results")}
    shutil.rmtree(folder, ignore_errors=True)
    from analystos.gateway.engines import dispose_all

    dispose_all()
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {SRC_DB} WITH (FORCE)"))
    admin.dispose()


def _gateway(world):
    from analystos.core.config import get_settings
    from analystos.gateway.service import QueryGateway

    return QueryGateway(get_settings().model_copy(update={"result_store_dir": world["store"]}))


def _scope(world):
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope

    with session_scope() as s:
        return resolve_scope(s, world["owner"], world["ws"])


def _join_sql(scope) -> str:
    csv_table = next(a for a, sid in scope.asset_sources.items() if a.endswith(".customers"))
    return (f"SELECT c.segment, COUNT(*) AS n, SUM(t.hours) AS hours FROM s02ops.tickets t "
            f"JOIN {csv_table} c ON c.customer_id = t.customer_id GROUP BY c.segment ORDER BY c.segment")


def test_postgres_plus_csv_join_runs_over_handles_with_lineage(world) -> None:
    from analystos.db.base import session_scope
    from analystos.db.models import LineageEdge, QueryExecution, ResultHandle

    scope = _scope(world)
    assert set(scope.asset_sources.values()) == {world["pg"], world["csv"]}
    gw = _gateway(world)
    res = gw.execute(scope, _join_sql(scope), actor="agent:s02", federated=True, use_cache=False, run_id=None)
    got = {seg: [n, round(h, 2)] for seg, n, h in res.rows}
    assert got == world["expected"]

    with session_scope() as s:
        legs = list(s.scalars(select(QueryExecution).where(QueryExecution.purpose == f"federation.leg:{res.query_id}")))
        assert {q.source_id for q in legs} == {world["pg"], world["csv"]}
        assert all(q.status == "ok" and q.result_uri and q.result_preview == [] for q in legs)
        handles = {h.id: h for h in s.scalars(select(ResultHandle).where(ResultHandle.id.in_([q.id for q in legs])))}
        assert set(handles) == {q.id for q in legs}
        edges = list(s.scalars(select(LineageEdge).where(LineageEdge.workspace_id == world["ws"])))
    derived = {e.to_id for e in edges if e.from_type == "query" and e.from_id == res.query_id
               and e.relation == "derived_from" and e.to_type == "result_handle"}
    assert derived == set(handles)
    from_source = {(e.from_id, e.to_id) for e in edges if e.relation == "from_source"}
    assert from_source == {(q.id, q.source_id) for q in legs}
    reads = {e.to_id for e in edges if e.relation == "reads" and e.from_type == "result_handle"}
    assert "s02ops.tickets" in reads and any(r.endswith(".customers") for r in reads)


def test_data_movement_none_refuses_a_multi_source_statement(world) -> None:
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution, Workspace
    from analystos.governance.policy import load_policy, save_policy

    with session_scope() as s:
        ws = s.get(Workspace, world["ws"])
        before = load_policy(s, ws)
        save_policy(s, ws, before.model_copy(update={"data_movement": "none"}), world["owner"].id)
    try:
        scope = _scope(world)
        with pytest.raises(PolicyDenied, match="data_movement"):
            _gateway(world).execute(scope, _join_sql(scope), actor="agent:s02", federated=True, use_cache=False)
        with session_scope() as s:
            row = s.scalars(select(QueryExecution).where(QueryExecution.workspace_id == world["ws"],
                                                         QueryExecution.reason_code == "policy.data_movement")).first()
        assert row is not None and row.status == "rejected"
        one = _gateway(world).run_sql_for(scope, actor="agent:s02", source_id=world["pg"])  # one source stays fine
        assert one("SELECT COUNT(*) AS n FROM s02ops.tickets", use_cache=False).rows == [[400]]
    finally:
        with session_scope() as s:
            save_policy(s, s.get(Workspace, world["ws"]), before, world["owner"].id)


# ---------------------------------------------------------------------------------------------- REST (T8)
CUSTOMERS = [{"id": i, "profile": {"name": f"c{i}", "tier": "gold" if i % 3 == 0 else "basic"}, "spend": str(i * 10)}
             for i in range(1, 8)]


class _Api(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        _Api.seen.append({"path": parts.path, "query": query, "auth": self.headers.get("X-Api-Key")})
        if self.headers.get("X-Api-Key") != "rest-test-key":
            self.send_response(401)
            self.end_headers()
            return
        offset, size = int(query.get("offset", 0)), int(query.get("limit", 3))
        body = json.dumps({"results": CUSTOMERS[offset:offset + size]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        _Api.seen.append({"path": self.path, "method": "POST"})
        self.send_response(405)
        self.end_headers()

    def log_message(self, *_: object) -> None:
        return


def test_rest_source_is_staged_and_read_through_the_gateway(world, monkeypatch) -> None:
    from analystos.db.base import session_scope
    from analystos.services.sources import discover_source, register_source, select_assets

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_S02_REST", "rest-test-key")
    try:
        config = {
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/api",
            "auth": {"type": "header", "header": "X-Api-Key"},
            "rate_limit": {"requests_per_second": 50},
            "endpoints": [{
                "name": "customers", "path": "/customers",
                "pagination": {"type": "offset", "offset_param": "offset", "size_param": "limit", "size": 3},
                "records_path": "results",
                "columns": [{"name": "id", "path": "id", "type": "integer", "key": True},
                            {"name": "name", "path": "profile.name"}, {"name": "tier", "path": "profile.tier"},
                            {"name": "spend", "path": "spend", "type": "numeric"}],
            }],
        }
        with session_scope() as s:
            rest = register_source(s, world["owner"], world["ws"], kind="rest", name="billing api", config=config,
                                   secret_ref="env:ANALYSTOS_SOURCE_SECRET_S02_REST")
            s.flush()
            rest_id = rest.id
            assert rest.execution_mode == "staged"
        discover_source(world["owner"], rest_id, world["ws"])
        select_assets(world["owner"], rest_id, ["customers"], world["ws"])
        scope = _scope(world)
        table = next(a for a, sid in scope.asset_sources.items() if sid == rest_id)
        runner = _gateway(world).run_sql_for(scope, actor="agent:s02", source_id=rest_id)
        res = runner(f"SELECT tier, COUNT(*) AS n, SUM(spend) AS spend FROM {table} GROUP BY tier ORDER BY tier",
                     use_cache=False)
        assert res.rows == [["basic", 5, 190.0], ["gold", 2, 90.0]]
        gets = [r for r in _Api.seen if r.get("path") == "/api/customers"]
        assert gets and all(r["auth"] == "rest-test-key" for r in gets)
        assert [r["query"]["offset"] for r in gets[-3:]] == ["0", "3", "6"]  # the short page ends it
        assert not [r for r in _Api.seen if r.get("method") == "POST"]
    finally:
        server.shutdown()
