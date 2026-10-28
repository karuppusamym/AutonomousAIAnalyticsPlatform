"""P4-03 acceptance on the real platform: typed evidence, data-version manifest, snapshot-change handling,
discovery vs confirmation, and the legacy badge migration (alembic up / down / up).

The seeded retail orders (planted effects) are staged from Postgres through the real services and
analysed by governed runs on the local orchestrator with no model provider:

1. run 1: every finding carries a complete evidence bundle bound to the run's data manifest (content
   version of the staged snapshot), typed facts that bind its wording, and is a *discovery*
   (``exploratory``), never ``confirmed``;
2. re-staging identical data keeps the version: nothing goes stale;
3. the origin table changes and is re-staged: run 1's findings are marked stale (``insight.stale``);
4. run 2 re-tests run 1's verified claims (carried forward, pre-registered) on the new snapshot: those
   that replicate are ``confirmed`` by ``fresh_snapshot_replication``; its own new hypotheses stay
   exploratory.

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

from analystos.db import vectors

pytestmark = pytest.mark.integration

SOURCE_SCHEMA = "p403_shop"
ORDER_COLUMNS = ("order_id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER, order_date DATE, channel TEXT, "
                 "sales_region TEXT, customer_segment TEXT, quantity INTEGER, discount_pct NUMERIC(4,2), "
                 "net_amount NUMERIC(12,2), shipping_days INTEGER, returned BOOLEAN, payment_method TEXT")
OBJECTIVE = "Understand what drives product returns, order value and shipping delays in our online retail orders."


@pytest.fixture(scope="module")
def pg_orders(control_db, tmp_path_factory):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from e2e_increment3 import build_shop_db

    db = tmp_path_factory.mktemp("p403") / "shop.db"
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
    yield {"url": make_url(control_db), "engine": engine, "rows": len(rows)}
    engine.dispose()


@pytest.fixture
def without_holdout(control_db):
    """This test pins the fresh-snapshot rule. Held-out confirmation (P8-15, `analysis.holdout_fraction`) is
    switched off for it, so that rule is the only one that can confirm; tests/integration/test_holdout_p815.py
    covers the held-out rule."""
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services import platform_settings as ps

    before = ps.get().analysis.holdout_fraction
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ps.update(s, admin, {"analysis": {"holdout_fraction": 0}}, note="P4-03 test: snapshot rule only")
    yield
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ps.update(s, admin, {"analysis": {"holdout_fraction": before}}, note="P4-03 test done")


def _wait(run_id: str, timeout: float = 900) -> str:
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun

    started = time.time()
    while time.time() - started < timeout:
        with session_scope() as s:
            status = s.get(AnalysisRun, run_id).status
        if status in ("COMPLETED", "FAILED"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"run {run_id} did not finish")


def _insights(run_id: str) -> list[dict]:
    from analystos.db.base import session_scope
    from analystos.db.models import Hypothesis, Insight

    with session_scope() as s:
        return [{"id": i.id, "code": i.code, "status": i.status, "validation": i.validation, "bundle": dict(i.evidence_bundle),
                 "data_version": i.data_version, "stale_since": i.stale_since, "origin": h.origin, "title": i.title,
                 "finding": i.finding}
                for i, h in s.execute(select(Insight, Hypothesis).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                                      .where(Insight.run_id == run_id))]


def test_evidence_manifest_staleness_and_confirmation(control_db, pg_orders, without_holdout, monkeypatch):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunEvent, User
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    url = pg_orders["url"]
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_P403_PG_PASSWORD", url.password or "")
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="p403 evidence", objective=OBJECTIVE)
        s.flush()
        src = register_source(s, admin, ws.id, kind="postgres", name="p403 shop", secret_ref="env:ANALYSTOS_SOURCE_SECRET_P403_PG_PASSWORD",
                              config={"host": url.host, "port": url.port or 5432, "database": url.database,
                                      "username": url.username, "schemas": [SOURCE_SCHEMA], "execution_mode": "staged"})
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    first = select_assets(admin, src_id, ["orders"])["loaded"][0]
    assert first["snapshot"]["content_fingerprint"] and first["snapshot"]["load_id"]

    # ---- run 1: discoveries with complete, typed evidence bound to the manifest
    run1 = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(run1.id) == "COMPLETED"
    with session_scope() as s:
        manifest = dict(s.get(AnalysisRun, run1.id).data_manifest)
    entry = manifest["entries"][0]
    assert entry["mode"] == "staged" and entry["version_basis"] == "content" and entry["immutable"] is True
    assert entry["rows"] == pg_orders["rows"] and entry["load_id"] == first["snapshot"]["load_id"]
    found = _insights(run1.id)
    verified = [i for i in found if i["status"] == "verified"]
    assert len(verified) >= 3, [(i["code"], i["status"], i["validation"]) for i in found]
    for i in found:
        b = i["bundle"]
        assert i["data_version"] == manifest["version"] and b["version"] == "evidence.v1"
        assert b["validation"]["label"] == "discovery" and i["validation"] != "confirmed"
        checks = {c["check"]: c["outcome"] for c in b["validation"]["checks"]}
        assert {"fact_binding", "data_version_stable", "evidence_complete", "reproducible_rerun"} <= set(checks)
        assert b["review_score"]["calibrated"] is False
    for i in verified:
        b = i["bundle"]
        assert i["validation"] in ("exploratory", "replicated") and not b["validation"]["missing_evidence"]
        assert b["claim"]["binding"]["ok"] and b["claim"]["facts"] and all(m["fact_id"] for m in b["claim"]["binding"]["mentions"])
        assert b["data"]["entry"]["version"] == entry["version"] and b["data"]["queries"][0]["result_hash"]
        assert b["method"]["effect"]["value"] is not None and b["method"]["sample_sizes"]["n"] > 0
        assert b["method"]["uncertainty"] is not None and b["freshness"]["state"] == "current"

    # ---- identical re-stage: same content version, nothing stale
    same = select_assets(admin, src_id, ["orders"])["loaded"][0]
    assert same["snapshot"]["content_fingerprint"] == first["snapshot"]["content_fingerprint"]
    assert same["snapshot"]["load_id"] != first["snapshot"]["load_id"]
    assert all(i["stale_since"] is None for i in _insights(run1.id))

    # ---- the origin changes: re-stage marks run 1's findings stale
    with pg_orders["engine"].begin() as c:
        c.execute(text(f"DELETE FROM {SOURCE_SCHEMA}.orders WHERE order_id % 9 = 0"))
    select_assets(admin, src_id, ["orders"])
    after = _insights(run1.id)
    assert all(i["stale_since"] is not None for i in after)
    assert all(i["bundle"]["freshness"]["state"] == "stale" and "orders" in i["bundle"]["freshness"]["reason"] for i in after)
    with session_scope() as s:
        stale_events = list(s.scalars(select(RunEvent).where(RunEvent.workspace_id == ws_id, RunEvent.type == "insight.stale")))
    assert len(stale_events) == len(after)
    # P7-01: `stale` is the data case of VOID; every run-1 verdict's record says so, with the cause
    from analystos.evidence.verification import latest

    with session_scope() as s:
        records = latest(s, "insight", [i["id"] for i in after])
    assert len(records) == len(after)
    assert all((r.state, r.void_kind) == ("VOID", "data") and "orders" in r.void_reason for r in records.values())

    # ---- run 2 re-tests run 1's verified claims (pre-registered) on the new snapshot
    run2 = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip", "previous_run_id": run1.id})
    assert _wait(run2.id) == "COMPLETED"
    with session_scope() as s:
        manifest2 = dict(s.get(AnalysisRun, run2.id).data_manifest)
    assert manifest2["version"] != manifest["version"]
    second = _insights(run2.id)
    carried = [i for i in second if i["origin"] == "carried" and i["status"] == "verified"]
    fresh = [i for i in second if i["origin"] != "carried"]
    assert carried, [(i["code"], i["origin"], i["status"]) for i in second]
    confirmed = [i for i in carried if i["validation"] == "confirmed"]
    assert confirmed, [(i["code"], i["validation"], i["bundle"]["validation"]["confirmation"]) for i in carried]
    for i in confirmed:
        conf = i["bundle"]["validation"]["confirmation"]
        assert conf["rule"] == "fresh_snapshot_replication" and conf["passed"] and i["bundle"]["validation"]["label"] == "confirmation"
    assert all(i["validation"] != "confirmed" for i in fresh)
    assert all(i["stale_since"] is None for i in second)
    with session_scope() as s:  # run 2's verdicts are bound to the new snapshot: live
        records = latest(s, "insight", [i["id"] for i in second if i["status"] == "verified"])
    assert records and all(r.state == "ACTIVE" for r in records.values())
    count = lambda rows: {s: sum(1 for i in rows if i["validation"] == s) for s in sorted({i["validation"] for i in rows})}  # noqa: E731
    print(f"\nP4-03 run1: {len(found)} findings, {len(verified)} verified, states {count(found)}; "
          f"{len(after)} marked stale after the origin changed ({entry['rows']} -> {manifest2['entries'][0]['rows']} rows); "
          f"run2: {len(second)} findings, {len(carried)} carried+verified, {len(confirmed)} confirmed, states {count(second)}")


def test_migration_0030_maps_legacy_badges_and_reverts(control_db):
    """Alembic up (0024 -> 0030) maps existing badges; down removes the columns; up again re-maps."""
    import json

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig30"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    rows = {"i_ok": ("verified", True, 0.82, {"evaluate": [{"check": "sample_size", "passed": True, "detail": "n=900"}],
                                              "verify": {"reproducible": True}}),
            "i_fail": ("failed_verification", False, 0.3, {"evaluate": [{"check": "second_method", "passed": False}]}),
            "i_rej": ("rejected", False, 0.1, {}),
            "i_draft": ("draft", False, 0.0, {})}
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0024")
        with engine.begin() as c:
            for iid, (status, verified, conf, ver) in rows.items():
                c.execute(text("INSERT INTO insight (id, workspace_id, run_id, code, title, finding, confidence, population_size, "
                               "business_impact, caveats, evidence, verified, verification, status, narrative_source) VALUES "
                               "(:id, 'ws', 'run', 'I-1', 't', 'f', :conf, 10, '{}', '[]', '[]', :v, CAST(:ver AS jsonb), :st, "
                               "'template')"), {"id": iid, "conf": conf, "v": verified, "ver": json.dumps(ver), "st": status})

        def mapped() -> dict:
            with engine.connect() as c:
                return {r.id: (r.validation, r.evidence_bundle) for r in
                        c.execute(text("SELECT id, validation, evidence_bundle FROM insight"))}

        for _ in range(2):  # up, down, up
            command.upgrade(cfg, "0030")
            got = mapped()
            assert {k: v[0] for k, v in got.items()} == {"i_ok": "legacy", "i_fail": "inconclusive", "i_rej": "invalid",
                                                         "i_draft": "legacy"}
            ok = got["i_ok"][1]
            assert ok["legacy"] == {"status": "verified", "verified": True, "confidence": 0.82, "verifier_version": "rev.v1",
                                    "equivalent": "exploratory"}
            assert ok["validation"]["label"] == "legacy" and ok["review_score"]["calibrated"] is False
            assert ok["validation"]["checks"] == [{"check": "sample_size", "outcome": "pass", "reason": "n=900"}]
            cols = {c["name"] for c in inspect(engine).get_columns("insight")}
            assert {"evidence_bundle", "validation", "data_version", "stale_since"} <= cols
            assert "data_manifest" in {c["name"] for c in inspect(engine).get_columns("analysis_run")}
            with engine.connect() as c:  # the verified/confidence values themselves are untouched
                assert c.execute(text("SELECT verified, confidence FROM insight WHERE id = 'i_ok'")).one() == (True, 0.82)
            command.downgrade(cfg, "0024")
            cols = {c["name"] for c in inspect(engine).get_columns("insight")}
            assert not {"evidence_bundle", "validation", "data_version", "stale_since"} & cols
            assert "data_manifest" not in {c["name"] for c in inspect(engine).get_columns("analysis_run")}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
