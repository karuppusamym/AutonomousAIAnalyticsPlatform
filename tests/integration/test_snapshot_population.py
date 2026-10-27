"""P4-C12 acceptance: a staged snapshot says which population it is, and findings respect it.

A Postgres table (the seeded retail orders, 24,000 rows, planted effects) is registered as a
*staged* Postgres source twice, through the real services (register -> crawl -> select/stage ->
governed run on the local orchestrator, no model provider):

1. with a per-source cap below the table size and no declared sampling: the snapshot is recorded as
   truncated, every finding carries the population caveat and fails ``representative_population``;
2. with ``TABLESAMPLE BERNOULLI`` declared (native, seeded): the snapshot is a recorded random
   sample, findings carry the sample caveat and the check passes with the method recorded.

Skips cleanly when the compose Postgres is unavailable.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

SOURCE_SCHEMA = "c12_shop"
ORDER_COLUMNS = ("order_id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER, order_date DATE, channel TEXT, "
                 "sales_region TEXT, customer_segment TEXT, quantity INTEGER, discount_pct NUMERIC(4,2), "
                 "net_amount NUMERIC(12,2), shipping_days INTEGER, returned BOOLEAN, payment_method TEXT")
OBJECTIVE = ("Understand what drives product returns, order value and shipping delays in our online retail orders, "
             "and which channels, regions and customer segments need attention.")


@pytest.fixture(scope="module")
def pg_orders(control_db, tmp_path_factory):
    """The retail orders table in its own schema of the throwaway control database."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from e2e_increment3 import build_shop_db

    db = tmp_path_factory.mktemp("c12") / "shop.db"
    build_shop_db(db)
    con = sqlite3.connect(db)
    rows = [(*r[:11], bool(r[11]), r[12]) for r in con.execute("SELECT * FROM orders ORDER BY order_id")]
    con.close()
    engine = create_engine(control_db)
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SOURCE_SCHEMA}"))
        c.execute(text(f"CREATE TABLE {SOURCE_SCHEMA}.orders ({ORDER_COLUMNS})"))
        c.exec_driver_sql(f"INSERT INTO {SOURCE_SCHEMA}.orders VALUES ({', '.join(['%s'] * 13)})", rows)
        c.execute(text(f"ANALYZE {SOURCE_SCHEMA}.orders"))
    engine.dispose()
    yield {"url": make_url(control_db), "rows": len(rows)}


def _wait(run_id: str, statuses: set[str], timeout: float = 900) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in statuses:
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def _stage_and_run(pg_orders, monkeypatch, name: str, extra_config: dict) -> dict:
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Insight, RunEvent, SourceAsset, User
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    url = pg_orders["url"]
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_C12_PG_PASSWORD", url.password or "")
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=name, objective=OBJECTIVE)
        s.flush()
        src = register_source(s, admin, ws.id, kind="postgres", name=name, secret_ref="env:ANALYSTOS_SOURCE_SECRET_C12_PG_PASSWORD",
                              config={"host": url.host, "port": url.port or 5432, "database": url.database,
                                      "username": url.username, "schemas": [SOURCE_SCHEMA], "execution_mode": "staged",
                                      **extra_config})
        s.flush()
        assert src.execution_mode == "staged"
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    staged = select_assets(admin, src_id, ["orders"])
    run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(run.id, {"COMPLETED", "FAILED"}) == "COMPLETED"
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.source_id == src_id, SourceAsset.name == "orders"))
        event = s.scalar(select(RunEvent).where(RunEvent.workspace_id == ws_id, RunEvent.type == "metadata.collected",
                                                RunEvent.run_id.is_(None)))
        insights = [{"code": i.code, "status": i.status, "caveats": list(i.caveats or []),
                     "checks": {c["check"]: c for c in (i.verification or {}).get("evaluate", [])}}
                    for i in s.scalars(select(Insight).where(Insight.run_id == run.id))]
        return {"loaded": staged["loaded"][0], "snapshot": dict(asset.snapshot), "row_count": asset.row_count,
                "event": dict(event.payload), "insights": insights}


def test_truncated_snapshot_is_flagged_and_fails_verification(control_db, pg_orders, monkeypatch):
    out = _stage_and_run(pg_orders, monkeypatch, "c12 truncated", {"max_rows": 5000})
    snap = out["snapshot"]
    assert (snap["rows_staged"], snap["source_total_rows"], snap["total_rows_basis"]) == (5000, pg_orders["rows"], "count")
    assert snap["truncated"] is True and snap["sampling_method"] == "undeclared" and snap["representative"] is False
    assert out["row_count"] == 5000
    assert out["loaded"]["truncated"] is True and out["loaded"]["snapshot"]["rows_staged"] == 5000  # loader result
    assert out["event"]["loaded"][0]["snapshot"]["truncated"] is True  # metadata.collected
    assert out["insights"], "the planted effects should still produce findings on 5,000 rows"
    for ins in out["insights"]:
        assert any("no declared sampling strategy" in c and "5,000 rows of 24,000 rows" in c for c in ins["caveats"]), ins
        check = ins["checks"]["representative_population"]
        assert check["passed"] is False and check["method"] == "undeclared" and check["truncated"] is True
        assert ins["status"] == "failed_verification"


def test_declared_tablesample_passes_with_method_recorded(control_db, pg_orders, monkeypatch):
    out = _stage_and_run(pg_orders, monkeypatch, "c12 tablesample",
                         {"max_rows": 20_000, "sampling": {"method": "tablesample", "percent": 50, "seed": 7}})
    snap = out["snapshot"]
    assert snap["sampling_method"] == "tablesample" and snap["truncated"] is False and snap["representative"] is True
    assert snap["sampling"] == {"method": "tablesample", "percent": 50.0, "seed": 7, "sampler": "bernoulli",
                                "native": True, "repeatable": True}
    assert 10_000 < snap["rows_staged"] < 14_000 and snap["source_total_rows"] == pg_orders["rows"]
    assert out["insights"]
    for ins in out["insights"]:
        assert any(c.startswith(f"Computed on a {snap['rows_staged']:,}-row 50% random sample (bernoulli) of 24,000 rows")
                   for c in ins["caveats"]), ins
        check = ins["checks"]["representative_population"]
        assert check["passed"] is True and check["method"] == "tablesample"
        assert check["sampling"] == {"percent": 50.0, "sampler": "bernoulli", "seed": 7, "native": True}
    assert any(i["status"] == "verified" for i in out["insights"])
