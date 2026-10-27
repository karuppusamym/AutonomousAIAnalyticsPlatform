"""P7-01 / P7-08 acceptance on the real platform: a governed run's verdicts carry dependency fingerprints,
each change path voids exactly the verdicts that depended on it (through its real service call), the
nightly sweep catches a change whose event was missed, consumers refuse or relabel VOID, and "why this
number?" resolves every reported number with each link's current state. Plus migration 0032 up/down/up.

Seeded retail orders (planted effects) are staged from Postgres and analysed on the local orchestrator
with no model provider. Skips cleanly when the compose Postgres is unavailable.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from analystos.db import vectors

pytestmark = pytest.mark.integration

SOURCE_SCHEMA = "p701_shop"
ORDER_COLUMNS = ("order_id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER, order_date DATE, channel TEXT, "
                 "sales_region TEXT, customer_segment TEXT, quantity INTEGER, discount_pct NUMERIC(4,2), "
                 "net_amount NUMERIC(12,2), shipping_days INTEGER, returned BOOLEAN, payment_method TEXT")
OBJECTIVE = "Understand what drives product returns, order value and shipping delays in our online retail orders."
TERM_PATH = "glossary/shipping-delay.md"


@pytest.fixture(scope="module")
def pg_orders(control_db, tmp_path_factory):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from e2e_increment3 import build_shop_db

    db = tmp_path_factory.mktemp("p701") / "shop.db"
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


def _records(run_id: str) -> dict[str, dict]:
    """insight code -> its newest verification record (as plain values)."""
    from analystos.db.base import session_scope
    from analystos.db.models import Hypothesis, Insight
    from analystos.evidence.verification import latest

    with session_scope() as s:
        rows = list(s.execute(select(Insight, Hypothesis).join(Hypothesis, Insight.hypothesis_id == Hypothesis.id)
                              .where(Insight.run_id == run_id)))
        recs = latest(s, "insight", [i.id for i, _ in rows])
        return {i.code: {"insight_id": i.id, "status": i.status, "hypothesis_id": h.id, "spec": dict(h.spec),
                         "record_id": recs[i.id].id if i.id in recs else None,
                         "state": recs[i.id].state if i.id in recs else None,
                         "void_kind": recs[i.id].void_kind if i.id in recs else None,
                         "void_detail": recs[i.id].void_detail if i.id in recs else None,
                         "deps": {(d["kind"], d["ref"]) for d in recs[i.id].dependencies} if i.id in recs else set()}
                for i, h in rows}


def _active(run_id: str) -> dict[str, dict]:
    return {c: r for c, r in _records(run_id).items() if r["status"] == "verified" and r["state"] == "ACTIVE"}


def test_verdicts_void_when_a_dependency_changes(control_db, pg_orders, monkeypatch):
    from analystos import methods
    from analystos.contracts.semantic import DialectExpression, SemanticMetricDef
    from analystos.core.config import get_settings
    from analystos.core.errors import PolicyDenied
    from analystos.db.base import session_scope
    from analystos.db.models import RunEvent, SourceAsset, User, VerificationSweep
    from analystos.evidence import verification as V
    from analystos.evidence.why import explain_run
    from analystos.knowledge import okf, store, studio
    from analystos.semantic import service as semantic
    from analystos.services.reports import build_report_data
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    url = pg_orders["url"]
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_P701_PG_PASSWORD", url.password or "")
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="p701 verification", objective=OBJECTIVE)
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, admin, ws.id, kind="postgres", name="p701 shop", secret_ref="env:ANALYSTOS_SOURCE_SECRET_P701_PG_PASSWORD",
                              config={"host": url.host, "port": url.port or 5432, "database": url.database,
                                      "username": url.username, "schemas": [SOURCE_SCHEMA], "execution_mode": "staged"})
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    discover_source(admin, src_id)
    select_assets(admin, src_id, ["orders"])
    with session_scope() as s:  # an approved KPI on the returns outcome, and a glossary term on shipping days
        asset = s.scalar(select(SourceAsset).where(SourceAsset.source_id == src_id, SourceAsset.name == "orders"))
        fq = f"{asset.schema_name}.orders"
        semantic.propose_metric(s, ws_id, SemanticMetricDef(
            name="return_rate", expressions=[DialectExpression(expression='AVG(CASE WHEN "returned" THEN 1.0 ELSE 0.0 END)')],
            source_columns=[f"{fq}.returned"], display_name="Return rate"), proposed_by=admin.id, via="user")
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        assert semantic.decide_metric(s, ws_id, "return_rate", approver, approve=True).status == "approved"
        store.commit(s, store.workspace_pack(s, ws_id), {TERM_PATH: okf.render_document(
            {"type": "Glossary Term", "title": "Shipping delay", "analystos": {"kind": "term", "mapped_columns": [f"{fq}.shipping_days"]}},
            "# Definition\n\nShipping delay: the shipping days between an online retail order and its delivery.").encode()},
            author="human:test", reason="glossary", origin="user", merge=True)

    run = create_run(admin, ws_id, objective=None, origin={"type": "user", "publish": "skip"})
    assert _wait(run.id) == "COMPLETED"
    recs = _records(run.id)
    verified = {c: r for c, r in recs.items() if r["status"] == "verified"}
    assert len(verified) >= 3, [(c, r["status"]) for c, r in recs.items()]
    assert all(r["record_id"] for r in recs.values())  # every REV verdict has a record, failed ones too
    for r in verified.values():
        assert r["state"] == "ACTIVE"
        assert {"query", "data", "method", "policy"} <= {k for k, _ in r["deps"]}
    with session_scope() as s:
        doc_id = store.workspace_pack(s, ws_id).id
        from analystos.knowledge.index import doc_id as make_doc_id

        term_ref = make_doc_id(doc_id, TERM_PATH)
    on_returns = {c for c, r in verified.items() if ("semantic", f"{ws_id}/return_rate") in r["deps"]}
    cites_term = {c for c, r in verified.items() if ("context", term_ref) in r["deps"]}
    assert on_returns, "a finding about returns depends on the approved return_rate KPI"
    assert cites_term, "a finding on shipping days cites the glossary term"

    # ---- P7-08: every number of the report resolves, every link ok
    with session_scope() as s:
        why = explain_run(s, run.id)
    assert why["numbers"] > 0 and why["by_state"] == {"ok": why["numbers"]}, why["by_state"]

    # ---- editing a step's SQL/spec voids that verdict only (P7-04 will call the same public hook)
    edited = sorted(set(verified) - on_returns - cites_term)[0]
    from analystos.db.models import Hypothesis

    with session_scope() as s:
        h = s.get(Hypothesis, verified[edited]["hypothesis_id"])
        h.spec = {**h.spec, "min_group_size": h.spec.get("min_group_size", 30), "filters": [
            {"column": "channel", "op": "!=", "value": "__none__"}]}
        voided = V.dependency_changed(s, "query", h.id, f"step {h.code} edited", event="step.edited")
        # exactly the edited finding's verdict, plus its copy on the recorded Data Thread claim step (same dependencies)
        from analystos.db.models import VerificationRecord

        subjects = {r.id: r.subject_type for r in s.scalars(select(VerificationRecord).where(VerificationRecord.id.in_(voided)))}
    assert verified[edited]["record_id"] in voided
    assert [rid for rid in voided if subjects[rid] != "step"] == [verified[edited]["record_id"]], subjects
    assert _records(run.id)[edited]["void_kind"] == "query"

    # ---- an edit to the cited glossary section (knowledge studio -> revision -> index) voids its citers
    with session_scope() as s:
        pack = store.workspace_pack(s, ws_id)
        doc = studio.read(s, pack, TERM_PATH)
        studio.save(s, pack, s.get(User, admin.id), path=TERM_PATH, frontmatter=doc["frontmatter"],
                    body="# Definition\n\nShipping delay: business days from order to delivery, excluding holidays.",
                    base_sha256=doc["sha256"])
    after = _records(run.id)
    for c in cites_term - {edited}:
        assert (after[c]["state"], after[c]["void_kind"]) == ("VOID", "context"), (c, after[c])
    assert all(after[c]["state"] == "ACTIVE" for c in set(verified) - cites_term - {edited})

    # ---- a method version bump nobody announced: the nightly sweep catches it and counts a late void
    # (the method is chosen so that a verdict on returns stays live for the metric step below)
    live = _active(run.id)
    live_returns = set(live) & on_returns
    method = next(m for m in sorted({r["spec"]["method"] for r in live.values()})
                  if live_returns - {c for c, r in live.items() if r["spec"]["method"] == m})
    reg = methods.current()
    bumped = methods.registry.MethodRegistry(manifests={
        n: (m.model_copy(update={"version": "99.0.0"}) if n == method else m) for n, m in reg.manifests.items()})
    methods.reset(bumped)
    try:
        with session_scope() as s:
            sweep = V.sweep(s, actor="test")
        by_method = {c for c, r in live.items() if r["spec"]["method"] == method}
        # (other workspaces in the shared test database may hold live verdicts on the same method: >=)
        assert sweep["late_voids"] >= len(by_method) and sweep["by_kind"].get("method", 0) >= len(by_method)
        assert {r["record_id"] for c, r in live.items() if c in by_method} <= set(sweep["voided"])
        after = _records(run.id)
        for c in by_method:
            assert (after[c]["void_kind"], after[c]["void_detail"]["late"]) == ("method", True)
        with session_scope() as s:
            assert s.get(VerificationSweep, sweep["sweep_id"]).late_voids == sweep["late_voids"]
    finally:
        methods.reset(None)

    # ---- approving a new version of the KPI a verdict measured voids it
    with session_scope() as s:
        semantic.propose_metric(s, ws_id, SemanticMetricDef(
            name="return_rate", expressions=[DialectExpression(expression='AVG(CASE WHEN "returned" THEN 100.0 ELSE 0.0 END)')],
            source_columns=[f"{fq}.returned"], display_name="Return rate (%)"), proposed_by=admin.id, via="user")
        approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        assert semantic.decide_metric(s, ws_id, "return_rate", approver, approve=True).version == 2
    after = _records(run.id)
    metric_voided = live_returns - by_method
    assert metric_voided
    for c in metric_voided:
        assert (after[c]["state"], after[c]["void_kind"]) == ("VOID", "semantic"), (c, after[c])

    # ---- a snapshot change (real re-stage) voids whatever is still live as VOID(data); a verdict already void
    # keeps its first cause (the P4-03 re-stage path itself is asserted in test_typed_evidence_p403)
    before = _records(run.id)
    remaining = _active(run.id)
    with pg_orders["engine"].begin() as c:
        c.execute(text(f"DELETE FROM {SOURCE_SCHEMA}.orders WHERE order_id % 9 = 0"))
    select_assets(admin, src_id, ["orders"])
    after = _records(run.id)
    for c in remaining:
        assert (after[c]["state"], after[c]["void_kind"]) == ("VOID", "data"), (c, after[c])
    assert all(r["state"] == "VOID" for c, r in after.items() if r["status"] == "verified")
    assert all(after[c]["void_detail"] == before[c]["void_detail"] for c in set(verified) - set(remaining))

    # ---- consumers: publish gate and reports refuse to present VOID; the UI sees each void with its cause
    with session_scope() as s:
        with pytest.raises(PolicyDenied, match="void"):
            V.gate_publish(s, run.id, list(verified))
        data = build_report_data(s, run.id)
        assert data.insights and all(not i.verified and i.void_reason for i in data.insights)
        events = list(s.scalars(select(RunEvent).where(RunEvent.workspace_id == ws_id, RunEvent.type == "verification.voided")))
        assert len(events) >= len(verified) and all(e.payload["reason"] and e.payload["kind"] for e in events)
        why = explain_run(s, run.id)
    assert "ok" not in why["by_state"]
    for f in why["findings"]:
        for n in f["numbers"]:
            verdict = n["links"][-1]
            assert verdict["link"] == "verdict" and verdict["state"] == "void" and verdict["reason"]
    print(f"\nP7-01: {len(verified)} verified verdicts; voided by step edit 1, glossary {len(cites_term - {edited})}, "
          f"sweep {sweep['late_voids']} (late, method {method}), metric {len(metric_voided)}, snapshot {len(remaining)}; "
          f"{why['numbers']} reported numbers resolved")


def test_migration_0032_maps_existing_findings_and_reverts(control_db):
    """Alembic up (0030 -> 0032) maps stale -> VOID(data), verified with a manifest entry -> ACTIVE(data),
    legacy verified -> LEGACY; down drops the tables; up again re-maps."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    from analystos.core.config import REPO_ROOT
    from analystos.evidence.verification import Dependency, fingerprint

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig32"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    entry = {"asset": "shop.orders", "source_id": "src_1", "mode": "staged", "version": "v1"}
    rows = {"i_active": ({"version": "evidence.v1", "data": {"entry": entry}}, None),
            "i_stale": ({"version": "evidence.v1", "data": {"entry": entry}, "freshness": {"reason": "snapshot changed"}},
                        "2026-09-26T10:00:00+00:00"),
            "i_legacy": ({"version": "evidence.legacy", "data": {}}, None)}
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0030")
        with engine.begin() as c:
            for iid, (bundle, stale) in rows.items():
                c.execute(text("INSERT INTO insight (id, workspace_id, run_id, code, title, finding, confidence, population_size, "
                               "business_impact, caveats, evidence, verified, verification, status, narrative_source, "
                               "evidence_bundle, stale_since) VALUES (:id, 'ws', 'run', 'I-1', 't', 'f', 0.8, 10, '{}', '[]', "
                               "'[]', true, '{}', 'verified', 'template', CAST(:b AS jsonb), :stale)"),
                          {"id": iid, "b": json.dumps(bundle), "stale": stale})
            c.execute(text("INSERT INTO insight (id, workspace_id, run_id, code, title, finding, confidence, population_size, "
                           "business_impact, caveats, evidence, verified, verification, status, narrative_source) VALUES "
                           "('i_failed', 'ws', 'run', 'I-2', 't', 'f', 0.2, 10, '{}', '[]', '[]', false, '{}', "
                           "'failed_verification', 'template')"))
        for _ in range(2):  # up, down, up
            command.upgrade(cfg, "0032")
            with engine.connect() as c:
                got = {r.subject_id: r for r in c.execute(text("SELECT * FROM verification_record"))}
                deps = list(c.execute(text("SELECT record_id, kind, ref, version_hash FROM verification_dependency")))
            assert {k: v.state for k, v in got.items()} == {"i_active": "ACTIVE", "i_stale": "VOID", "i_legacy": "LEGACY"}
            assert (got["i_stale"].void_kind, got["i_stale"].void_reason) == ("data", "snapshot changed")
            assert got["i_active"].fingerprint == fingerprint([Dependency("data", "src_1/shop.orders", "v1")])
            assert got["i_legacy"].dependencies == [] and got["i_legacy"].verifier == "migration.0032"
            assert {(d.kind, d.ref, d.version_hash) for d in deps} == {("data", "src_1/shop.orders", "v1")}
            assert len(deps) == 2  # active + stale
            assert "ix_verification_dependency_kind_ref" in {i["name"] for i in inspect(engine).get_indexes("verification_dependency")}
            command.downgrade(cfg, "0030")
            assert not {"verification_record", "verification_dependency", "verification_sweep"} & set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
