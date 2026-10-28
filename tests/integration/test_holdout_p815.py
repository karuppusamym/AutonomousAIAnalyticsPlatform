"""P8-15 on the real platform: a governed run reads only the discovery rows until REV locks a verified claim,
then tests it once on the held-out rows; findings carry their strength and are worded by their standing.

The retail orders of `scripts/e2e_full_journey.py` (random order status, planted Marketplace returns) are
staged from Postgres through the real services and analysed on the local orchestrator with no model
provider. Also checks that the partition compiled for Postgres splits the rows exactly as DuckDB does
(the same md5 over the same key text). Skips cleanly when the compose Postgres is unavailable; it uses the
suite's own test databases (tests/conftest.py), never the application database.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

SOURCE_SCHEMA = "p815_shop"
COLUMNS = {"Order ID": "order_id", "Customer ID": "customer_id", "Product ID": "product_id", "Order Date": "order_date",
           "Quantity": "quantity", "Unit Price": "unit_price", "Discount Pct": "discount_pct", "Sales Channel": "sales_channel",
           "Status": "status", "Promised Date": "promised_date", "Delivered Date": "delivered_date", "Returned": "returned"}
DDL = ("order_id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT, order_date DATE, quantity INTEGER, unit_price NUMERIC(10,2), "
       "discount_pct INTEGER, sales_channel TEXT, status TEXT, promised_date DATE, delivered_date DATE, returned TEXT")
OBJECTIVE = "Understand returns, order value and cancellations by channel in our online retail orders."


@pytest.fixture(scope="module")
def retail_pg(control_db):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from retail_fixture import retail_orders

    rows = [tuple(o[c] for c in COLUMNS) for o in retail_orders()]
    engine = create_engine(control_db)
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {SOURCE_SCHEMA}"))
        c.execute(text(f"CREATE TABLE {SOURCE_SCHEMA}.orders ({DDL})"))
        c.exec_driver_sql(f"INSERT INTO {SOURCE_SCHEMA}.orders VALUES ({', '.join(['%s'] * len(COLUMNS))})", rows)
    yield {"url": make_url(control_db), "engine": engine, "rows": len(rows)}
    with engine.begin() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE"))
    engine.dispose()


def _wait(run_id: str, timeout: float = 900) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in ("COMPLETED", "FAILED", "WAITING_USER"):  # nobody answers a question here
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def test_postgres_and_duckdb_split_the_same_rows(retail_pg):
    """Same key text, same md5, same buckets: the partition does not depend on the engine."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from analystos.contracts.analysis import Partition
    from analystos.skills import sqlbuild as sb
    from retail_fixture import retail_duck

    p = Partition(key=["order_id"], basis="declared_key", fraction=0.3)
    held = p.model_copy(update={"side": "holdout", "claim_locked_at": "t"})

    def pg_sql(part):
        return sb.to_sql(sb.exp.select(sb.col("order_id")).from_(sb.table(f"{SOURCE_SCHEMA}.orders")).where(
            sb.partition_predicate(part, "postgres")), "postgres")

    with retail_pg["engine"].connect() as c:
        pg_held = {r[0] for r in c.execute(text(pg_sql(held)))}
        pg_disc = {r[0] for r in c.execute(text(pg_sql(p)))}
    duck = retail_duck()
    dp = Partition(key=["Order ID"], basis="declared_key", fraction=0.3)
    dheld = dp.model_copy(update={"side": "holdout", "claim_locked_at": "t"})
    duck_held = {r["Order ID"] for r in duck(sb.to_sql(sb.exp.select(sb.col("Order ID")).from_(sb.table("retail.orders")).where(
        sb.partition_predicate(dheld, "duckdb")), "duckdb")).records()}
    assert pg_held == duck_held and len(pg_held) + len(pg_disc) == retail_pg["rows"] and not (pg_held & pg_disc)


def test_a_governed_run_locks_before_it_reads_the_held_out_rows(control_db, retail_pg, monkeypatch):
    from analystos.contracts.analysis import Partition
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Experiment, Hypothesis, Insight, QueryExecution, User
    from analystos.evidence.strength import UNCONFIRMED_NOTE, WEAK_NOTE
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace
    from analystos.skills import sqlbuild as sb

    url = retail_pg["url"]
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_P815_PG_PASSWORD", url.password or "")
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="p815 holdout", objective=OBJECTIVE)
        s.flush()
        src = register_source(s, admin, ws.id, kind="postgres", name="p815 shop", secret_ref="env:ANALYSTOS_SOURCE_SECRET_P815_PG_PASSWORD",
                              config={"host": url.host, "port": url.port or 5432, "database": url.database,
                                      "username": url.username, "schemas": [SOURCE_SCHEMA], "execution_mode": "staged"})
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["orders"])
    # The objective uses the sales pack's "order value" and "channel": context grounding waits for the owner to map
    # them to this table, as a workspace owner would before an unattended run.
    from evaluation.analytical import _record_definitions

    _record_definitions(admin, ws_id, "orders", [
        {"term": "Average order value (AOV)", "columns": ["quantity", "unit_price", "discount_pct"],
         "definition": "Order value is quantity times unit_price less discount_pct percent; AOV is its mean per order.",
         "examples": "2 units at 50 with a 10 percent discount is an order value of 90."},
        {"term": "Sales channel", "columns": ["sales_channel"],
         "definition": "The channel recorded on the order (sales_channel).", "examples": "web, store, marketplace"}])
    run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(run.id) == "COMPLETED"

    with session_scope() as s:
        exps = list(s.scalars(select(Experiment).where(Experiment.run_id == run.id)))
        hyps = {h.id: h for h in s.scalars(select(Hypothesis).where(Hypothesis.run_id == run.id))}
        insights = list(s.scalars(select(Insight).where(Insight.run_id == run.id)))
        queries = {q.id: q for q in s.scalars(select(QueryExecution).where(QueryExecution.run_id == run.id))}
        summary = dict(s.get(AnalysisRun, run.id).summary or {})
        rows = [(e.role, e.hypothesis_id, dict(e.result or {}), list(e.query_ids or []), e.id) for e in exps]
        found = [{"code": i.code, "status": i.status, "validation": i.validation, "finding": i.finding,
                  "bundle": dict(i.evidence_bundle or {}), "hypothesis_id": i.hypothesis_id} for i in insights]
        qsql = {qid: (q.sql, q.created_at) for qid, q in queries.items()}
        statuses = {h.id: h.status for h in hyps.values()}

    primaries = [r for r in rows if r[0] == "primary"]
    holdouts = [r for r in rows if r[0] == "holdout"]
    assert primaries
    parts = [(r[2].get("details") or {}).get("partition") or {} for r in primaries]
    assert all(p.get("applied") and p["fraction"] == 0.3 for p in parts), parts[:2]
    key = parts[0]["key"]
    disc = Partition(key=key, basis=parts[0]["basis"], fraction=0.3)
    held_pred = sb.to_sql(sb.partition_predicate(disc.model_copy(update={"side": "holdout", "claim_locked_at": "t"}), "postgres"),
                          "postgres")
    disc_pred = sb.to_sql(sb.partition_predicate(disc, "postgres"), "postgres")
    holdout_qids = {q for r in holdouts for q in r[3]}
    for qid, (sql, _) in qsql.items():  # nothing but the held-out tests ever read the held-out rows
        assert (held_pred in sql) == (qid in holdout_qids), (qid, sql[:200])
    for r in primaries:
        assert all(disc_pred in qsql[q][0] for q in r[3])
        if statuses.get(r[1]) == "supported":
            assert r[2].get("strength", {}).get("label") in ("weak", "moderate", "strong")

    verified = [i for i in found if i["status"] == "verified"]
    assert verified, [(i["code"], i["status"]) for i in found]
    assert len(holdouts) == len({r[1] for r in holdouts}) == len(verified)  # one held-out test per verified claim
    for i in verified:
        v = i["bundle"]["validation"]
        h = v["holdout"]
        assert h["evaluated"] and h["claim_locked_at"] < h["partition_accessed_at"], h
        assert h["experiment_id"] in {r[4] for r in holdouts}
        exp_qids = next(r[3] for r in holdouts if r[4] == h["experiment_id"])
        locked_at = datetime.fromisoformat(h["claim_locked_at"])
        assert exp_qids and all(qsql[q][1] is None or qsql[q][1] >= locked_at - timedelta(seconds=1) for q in exp_qids)
        assert v["strength"]["label"] in ("weak", "moderate", "strong")
        confirmed = i["validation"] == "confirmed"
        assert confirmed == bool(h["confirmed"]) == (v["confirmation"]["rule"] == "holdout_partition")
        assert (UNCONFIRMED_NOTE in i["finding"]) == (not confirmed)
        assert (WEAK_NOTE in i["finding"]) == (v["strength"]["label"] == "weak")
    if any(i["validation"] != "confirmed" for i in verified):
        assert "Not yet confirmed on separate data" in summary["summary_markdown"]
    print("\nP8-15 run:", "; ".join(f"{i['code']} {i['validation']}/{i['bundle']['validation']['strength']['label']} "
                                    f"({(i['bundle'].get('claim') or {}).get('title')})" for i in verified))
