"""P6-01/P6-02/P6-03 on the compose Postgres with the ServiceNow mock (in-process).

* P6-02: a table staged by watermark equals a reference full load after updates, inserts and a delete
  reconcile; a crash between the merge and the watermark commit re-reads the window; overlapping
  refreshes do not double count; rows changing while the window is paged are not skipped (fixed cursor).
  An incremental recipe (through a PipelineSpec) equals its full rebuild, also after a retry.
* P6-01: a cross-source pipeline dry run: per-source scope and snapshot manifest, compiled SQL,
  cardinality/fan-out/unmatched checks and the reconciled virtual output; a failing reconciliation blocks.
* P6-03: build -> test -> approve -> materialize -> fail -> recover through the managed writer: no approval,
  a pending approval and a moved rollback pointer are refused; an invalid output never replaces the good
  version; a crash before promotion resumes from its checkpoint; rollback; freshness alert; the writer
  cannot write (or read) a source schema. Skips cleanly without the stack.
"""
from __future__ import annotations

import copy
import os
import socket
import threading
import time
from datetime import timedelta

import psycopg
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.engine import make_url

from analystos.db import vectors

pytestmark = pytest.mark.integration
TABLE = "change_request"
DEST = "aos_pipe_mart"
PASSWORD = "ChangeMe123!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg(url: str) -> str:
    return make_url(url).render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://")


def _superuser() -> str:
    """The analytics database as the test cluster's superuser (reads what any role wrote)."""
    admin, analytics = make_url(os.environ["ANALYSTOS_DATABASE_URL"]), make_url(os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"])
    return _pg(analytics.set(username=admin.username, password=admin.password).render_as_string(hide_password=False))


def _records() -> list[dict]:
    from analystos.connectors.servicenow_mock import _records as recs

    return recs()[TABLE]


@pytest.fixture(scope="module")
def world(control_db):
    os.environ["ANALYSTOS_SOURCE_SECRET_SERVICENOW"] = "admin"
    from analystos.connectors.servicenow_mock import app

    original = copy.deepcopy(_records())
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import Source, User
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace

    get_settings.cache_clear()
    base = {"instance_url": f"http://127.0.0.1:{port}", "username": "admin", "tables": [TABLE, "sys_user_group"],
            "page_size": 250}
    inc = {TABLE: {"watermark": "sys_updated_on", "key": ["sys_id"], "late_window": "10m", "deletes": "reconcile"}}
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name="pipelines", objective="Prepare change data for reporting")
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        a = register_source(s, admin, ws.id, kind="servicenow", name="SN incremental", config={**base, "incremental": inc},
                            secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        b = register_source(s, admin, ws.id, kind="servicenow", name="SN reference", config=dict(base),
                            secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ids = {"ws": ws.id, "a": a.id, "b": b.id}
        s.expunge(admin)
    for key in ("a", "b"):
        discover_source(admin, ids[key])
    select_assets(admin, ids["a"], [TABLE])
    select_assets(admin, ids["b"], [TABLE, "sys_user_group"])
    with session_scope() as s:
        ids["schema_a"] = s.get(Source, ids["a"]).staging_schema
        ids["schema_b"] = s.get(Source, ids["b"]).staging_schema
    yield {**ids, "admin": admin, "original": original}
    _records()[:] = original
    server.should_exit = True


# ------------------------------------------------------------------------------------ helpers
def _fp(source_id: str, table: str = TABLE) -> tuple[str, int]:
    from analystos.core.config import get_settings
    from analystos.staging.loader import StagingLoader

    info = StagingLoader(get_settings()).table_info(source_id, table)
    return info["content_fingerprint"], info["row_count"]


def _state(source_id: str, table: str = TABLE) -> dict | None:
    from analystos.core.config import get_settings
    from analystos.staging.loader import StagingLoader

    return StagingLoader(get_settings()).read_state(source_id, table)


def _refresh(world, key: str = "a") -> dict:
    from analystos.services.sources import select_assets

    return select_assets(world["admin"], world[key], [TABLE] + (["sys_user_group"] if key == "b" else []))


def _touch(rows: list[dict], n: int, *, start: int, minute: int, field: str = "risk", value: str = "4") -> list[str]:
    stamp = f"2031-01-01 00:{minute:02d}:00"
    touched = []
    for r in rows[start:start + n]:
        r["sys_updated_on"], r[field] = stamp, value
        touched.append(r["sys_id"])
    return touched


def _same_as_reference(world) -> None:
    _refresh(world, "b")
    assert _fp(world["a"]) == _fp(world["b"])


# ------------------------------------------------------------------------------------ P6-02 staging
def test_watermark_refresh_equals_a_full_reload_through_updates_inserts_deletes_crash_and_overlap(world, monkeypatch):
    from analystos.core.errors import InvalidInput
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset
    from analystos.staging import loader as loader_mod

    rows = _records()
    first = _state(world["a"])
    assert first and first["watermark"] and first["last_window"]["mode"] == "auto"
    _same_as_reference(world)

    # updates and inserts: a window, merged on the key
    _touch(rows, 30, start=100, minute=1)
    for i in range(5):
        new = dict(rows[i], sys_id=f"feed{i:028d}", number=f"CHGNEW{i}", sys_updated_on="2031-01-01 00:02:00")
        rows.append(new)
    deleted = [rows.pop(500)["sys_id"] for _ in range(3)]
    out = _refresh(world)["loaded"][0]
    run = out["snapshot"]["incremental"]
    assert run["load_mode"] == "merge" and 35 <= run["rows_fetched"] < 200 and run["high_watermark"].startswith("2031-01-01T00:02")
    assert _state(world["a"])["watermark"].startswith("2031-01-01T00:02")
    fp_a, n_a = _fp(world["a"])
    _refresh(world, "b")
    assert n_a == _fp(world["b"])[1] + 3  # a watermark cannot see the 3 deletes...
    from analystos.services.sources import refresh_asset

    rec = refresh_asset(world["admin"], world["a"], TABLE, world["ws"], mode="reconcile")
    assert rec["incremental"]["reconcile"]["deleted_rows"] == 3 and set(deleted)  # ...the full reconcile does
    assert _fp(world["a"]) == _fp(world["b"])

    # a crash after the merge, before COMMIT: neither the rows nor the watermark move; the retry re-reads the window
    before_fp, before_state = _fp(world["a"]), _state(world["a"])
    _touch(rows, 10, start=1000, minute=3)
    real = loader_mod._write_state

    def crash(*a, **k):
        real(*a, **k)
        raise RuntimeError("process killed between the merge and COMMIT")
    monkeypatch.setattr(loader_mod, "_write_state", crash)
    with pytest.raises(RuntimeError):
        _refresh(world)
    monkeypatch.setattr(loader_mod, "_write_state", real)
    assert _fp(world["a"]) == before_fp and _state(world["a"]) == before_state
    out = _refresh(world)["loaded"][0]["snapshot"]["incremental"]
    assert out["since"] < before_state["watermark"] and out["rows_fetched"] >= 10  # the same window again
    _same_as_reference(world)

    # overlapping refreshes (two schedules firing together): serialized per table, merged idempotently
    _touch(rows, 20, start=1500, minute=4, value="1")
    errors: list[Exception] = []

    def go():
        try:
            _refresh(world)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _same_as_reference(world)
    with psycopg.connect(_pg(os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"])) as conn:
        n, distinct = conn.execute(f'SELECT count(*), count(DISTINCT sys_id) FROM "{world["schema_a"]}".{TABLE}').fetchone()
    assert n == distinct == len(rows)

    # the fixed cursor: rows updated while the window is paged are not skipped (they re-enter the next window)
    from analystos.connectors.servicenow import ServiceNowConnector

    real_table = ServiceNowConnector._table
    moved = iter(range(1800, 2400))

    def concurrent_writer(self, table, params):
        out = real_table(self, table, params)
        if table == TABLE and "sys_updated_on>=" in str(params.get("sysparm_query")):
            for _ in range(3):
                rows[next(moved)]["sys_updated_on"] = "2031-01-01 00:05:00"
        return out
    _touch(rows, 40, start=1700, minute=4, value="2")
    monkeypatch.setattr(ServiceNowConnector, "_table", concurrent_writer)
    _refresh(world)
    monkeypatch.setattr(ServiceNowConnector, "_table", real_table)
    _refresh(world)
    _same_as_reference(world)

    # the reconcile refuses a key read that looks like an outage, and changes nothing
    saved = list(rows)
    rows[:] = rows[:10]
    with pytest.raises(InvalidInput, match="looks partial"):
        refresh_asset(world["admin"], world["a"], TABLE, world["ws"], mode="reconcile")
    rows[:] = saved
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.source_id == world["a"], SourceAsset.name == TABLE))
        assert asset.snapshot["incremental"]["watermark"] and asset.snapshot["content_fingerprint"]


# ------------------------------------------------------------------------------------ recipes and pipelines
def _recipe(world, name: str, *, gates: list | None = None) -> dict:
    schema = world["schema_a"]
    cols = [{"name": "sys_id", "type": "text"}, {"name": "number", "type": "text"}, {"name": "risk", "type": "bigint"},
            {"name": "state", "type": "bigint"}, {"name": "sys_updated_on", "type": "timestamp"}]
    out_cols = [*cols[:3], {"name": "high_risk", "type": "boolean"}, cols[4]]
    return {"kind": "Recipe", "name": name, "nodes": [
        {"op": "source", "id": "src", "asset": f"{schema}.{TABLE}", "schema": cols},
        {"op": "derive", "id": "flagged", "input": "src", "columns": [
            {"name": "high_risk", "expr": "risk >= 3", "type": "boolean"}]},
        {"op": "select", "id": "picked", "input": "flagged", "columns": ["sys_id", "number", "risk", "high_risk",
                                                                          "sys_updated_on"]},
        {"op": "output", "id": "out", "input": "picked", "name": "clean", "keys": ["sys_id"], "grain": ["sys_id"],
         "schema": out_cols, "gates": gates or []}]}


def _save_publish(world, spec: dict) -> str:
    from analystos.db.base import session_scope
    from analystos.services.recipes import publish_recipe, save_recipe

    with session_scope() as s:
        rid = save_recipe(s, world["admin"], world["ws"], spec).id
    with session_scope() as s:
        publish_recipe(s, world["admin"], rid, world["ws"])
    return rid


def _pipeline(world, spec: dict, publish: bool = True) -> str:
    from analystos.db.base import session_scope
    from analystos.services.pipelines import publish_pipeline, save_pipeline

    with session_scope() as s:
        pid = save_pipeline(s, world["admin"], world["ws"], spec).id
    if publish:
        with session_scope() as s:
            publish_pipeline(s, world["admin"], pid, world["ws"])
    return pid


def _output_fp(world, recipe: str) -> tuple[str, int]:
    from analystos.db.base import session_scope
    from analystos.services.recipes import ensure_output_source, output_table

    with session_scope() as s:
        src = ensure_output_source(s, world["ws"]).id
    return _fp(src, output_table(recipe, "clean"))


def test_incremental_pipeline_run_equals_its_full_rebuild_and_a_retry(world):
    from analystos.services.pipelines import run_pipeline
    from analystos.services.recipes import run_recipe

    _save_publish(world, _recipe(world, "cr_clean"))
    ref_id = _save_publish(world, _recipe(world, "cr_clean_ref"))
    out_schema = _recipe(world, "x")["nodes"][-1]["schema"]
    pid = _pipeline(world, {"type": "pipeline", "name": "cr_incremental", "recipes": [{"name": "cr_clean"}],
                            "output": {"output": "clean", "keys": ["sys_id"], "grain": ["sys_id"], "schema": out_schema},
                            "incremental": {"watermark": "sys_updated_on", "key": ["sys_id"], "late_window": "10m",
                                            "deletes": "reconcile"}})
    first = run_pipeline(world["admin"], pid, world["ws"])
    assert first["status"] == "succeeded"
    rows = _records()
    _touch(rows, 25, start=2200, minute=10, value="5")
    rows.append(dict(rows[0], sys_id="beef" + "0" * 28, number="CHGBEEF", sys_updated_on="2031-01-01 00:11:00"))
    _refresh(world)
    second = run_pipeline(world["admin"], pid, world["ws"])
    from analystos.db.base import session_scope
    from analystos.db.models import RecipeRun

    with session_scope() as s:
        inc = s.get(RecipeRun, second["recipe_run_ids"][0]).plan["incremental"]
    assert inc["load_mode"] == "merge" and inc["since"] < inc["until"]
    run_recipe(world["admin"], ref_id, world["ws"])  # the reference: a full rebuild of the same nodes
    assert _output_fp(world, "cr_clean") == _output_fp(world, "cr_clean_ref")
    retry = run_pipeline(world["admin"], pid, world["ws"])  # a retry / overlapping firing
    assert retry["status"] == "succeeded" and _output_fp(world, "cr_clean") == _output_fp(world, "cr_clean_ref")
    backfill = run_pipeline(world["admin"], pid, world["ws"], mode="backfill",
                            window=("2025-01-01T00:00:00", "2031-12-31T00:00:00"))
    assert backfill["status"] == "succeeded" and _output_fp(world, "cr_clean") == _output_fp(world, "cr_clean_ref")
    # late_rows: a windowed run cannot see behind its window (None, with why); the dry run measures it
    assert second["reconciliation"]["late_rows"] is None and "dry run" in second["reconciliation"]["late_rows_reason"]
    from analystos.services.pipelines import dry_run

    dry = dry_run(world["admin"], pid, world["ws"])
    assert dry["status"] == "succeeded", dry["error"]
    rec = dry["reconciliation"]
    assert rec["late_rows"] == 0 and rec["late_rows_reason"] is None, rec  # the output equals its full rebuild
    assert rec["late"]["cutoff"] and rec["late"]["candidate_rows_behind_window"] > 0 and rec["late"]["committed_rows_read"] > 0


def _cross_recipe(world, name: str) -> dict:
    a, b = world["schema_a"], world["schema_b"]
    return {"kind": "Recipe", "name": name, "nodes": [
        {"op": "source", "id": "changes", "asset": f"{a}.{TABLE}",
         "schema": [{"name": "sys_id", "type": "text"}, {"name": "assignment_group", "type": "text"},
                    {"name": "risk", "type": "bigint"}]},
        {"op": "source", "id": "groups", "asset": f"{b}.sys_user_group",
         "schema": [{"name": "sys_id", "type": "text"}, {"name": "name", "type": "text"}]},
        {"op": "rename", "id": "g", "input": "groups", "mapping": {"sys_id": "group_id", "name": "group_name"}},
        {"op": "filter", "id": "risky", "input": "changes", "predicate": "risk >= 2"},
        {"op": "join", "id": "with_group", "left": "risky", "right": "g", "how": "left",
         "on": [{"left": "assignment_group", "right": "group_id"}], "expected_cardinality": "many_to_one"},
        {"op": "output", "id": "out", "input": "with_group", "name": "clean", "keys": ["sys_id"],
         "schema": [{"name": "sys_id", "type": "text"}, {"name": "assignment_group", "type": "text"},
                    {"name": "risk", "type": "bigint"}, {"name": "group_name", "type": "text"}]}]}


def test_cross_source_dry_run_manifest_checks_and_reconciliation(world):
    from analystos.services.pipelines import dry_run

    _save_publish(world, _cross_recipe(world, "cr_groups"))
    out_schema = _cross_recipe(world, "x")["nodes"][-1]["schema"]
    spec = {"type": "pipeline", "name": "cr_groups", "recipes": [{"name": "cr_groups"}],
            "inputs": [{"asset": f"{world['schema_a']}.{TABLE}", "max_age_hours": 1}],
            "output": {"output": "clean", "keys": ["sys_id"], "schema": out_schema},
            "joins": [{"node": "with_group", "expected_cardinality": "many_to_one", "max_unmatched_pct": 5}],
            "checks": [{"name": "risk_kept", "func": "sum", "input_node": "risky", "input_column": "risk",
                        "output_column": "risk", "tolerance_pct": 0},
                       {"name": "rows_kept", "func": "count", "input_node": "risky"}]}
    view = dry_run(world["admin"], _pipeline(world, spec), world["ws"])
    assert view["status"] == "succeeded", view["error"]
    m = view["manifest"]
    assert m["cross_source"] is True and m["engine"] == "duckdb" and set(m["sources"]) == {world["a"], world["b"]}
    for entry in m["sources"].values():
        assert "reader role" in entry["read_identity"] and entry["assets"]
        for asset in entry["assets"].values():
            assert asset["snapshot"] and asset["content_fingerprint"] and asset["columns_read"]
    checks = {c["check"]: c for c in view["checks"]}
    assert checks["join_cardinality"]["observed"] in ("many_to_one", "one_to_one") and checks["fanout"]["ok"]
    assert checks["unmatched_rows"]["ok"] and checks["input_freshness"]["ok"]
    rec = view["reconciliation"]
    assert rec["inputs"]["changes"] == len(_records()) and rec["output_rows"] == view["candidate"]["row_count"]
    assert all(a["ok"] for a in rec["aggregates"]) and rec["rejected_rows"] == 0
    assert rec["late_rows"] is None and "no incremental watermark" in rec["late_rows_reason"]  # not measured, not 0
    assert "JOIN" in view["sql"]["output"].upper() and "with_group" in view["sql"]["preflight"]
    assert view["candidate"]["snapshot"] and view["approval_id"] is None  # no destination: nothing to approve

    # the output silently loses a business measure (reconciled against the unfiltered input): blocked
    spec2 = {**spec, "name": "cr_groups_bad",
             "checks": [{"name": "risk_all", "func": "sum", "input_node": "changes", "input_column": "risk",
                         "output_column": "risk", "tolerance_pct": 0.1}]}
    bad = dry_run(world["admin"], _pipeline(world, spec2), world["ws"])
    assert bad["status"] == "blocked" and "aggregate_reconciliation" in bad["error"]


def test_build_test_approve_materialize_fail_and_recover(world, monkeypatch):
    from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, InvalidInput
    from analystos.db.base import session_scope
    from analystos.db.models import Materialization, Notification, RunEvent, User
    from analystos.governance.approvals import decide
    from analystos.pipelines.writer import ManagedWriter, writer_role_for
    from analystos.services import pipelines as svc

    admin = world["admin"]
    with session_scope() as s, pytest.raises(Forbidden):  # a source schema is never a destination
        svc.designate_destination(s, s.merge(admin), world["ws"], world["schema_a"])
    with session_scope() as s:
        svc.designate_destination(s, s.merge(admin), world["ws"], DEST, tables=["cr_mart"])
    gates = [{"type": "range", "column": "risk", "min": 0, "max": 5, "severity": "fail"}]
    _save_publish(world, _recipe(world, "cr_mart_src", gates=gates))
    out_schema = _recipe(world, "x")["nodes"][-1]["schema"]
    spec = {"type": "pipeline", "name": "cr_mart", "recipes": [{"name": "cr_mart_src"}],
            "output": {"output": "clean", "keys": ["sys_id"], "grain": ["sys_id"], "schema": out_schema},
            "freshness": {"max_age_hours": 24}, "destination": {"schema": DEST, "table": "cr_mart"}}
    with session_scope() as s:  # the table allowlist is enforced at dry run
        bad = svc.save_pipeline(s, s.merge(admin), world["ws"], {**spec, "name": "cr_other",
                                                                 "destination": {"schema": DEST, "table": "other"}}).id
    with pytest.raises(Forbidden, match="allowlist"):
        svc.dry_run(admin, bad, world["ws"])
    pid = _pipeline(world, spec)

    def approve(approval_id: str) -> None:
        with session_scope() as s:
            decide(s, approval_id, s.scalar(select(User).where(User.email == "approver@analystos.local")), approve=True)

    def count() -> int:
        with psycopg.connect(_superuser()) as conn:
            return conn.execute(f"SELECT count(*) FROM {DEST}.cr_mart").fetchone()[0]

    # build + test: the dry run asks for an approval bound to the candidate
    d1 = svc.dry_run(admin, pid, world["ws"])
    assert d1["status"] == "awaiting_approval" and d1["approval_id"]
    with pytest.raises(ApprovalRequired):
        svc.materialize(admin, d1["id"], None, world["ws"])
    with pytest.raises(ApprovalRequired, match="pending"):
        svc.materialize(admin, d1["id"], d1["approval_id"], world["ws"])
    approve(d1["approval_id"])
    v1 = svc.materialize(admin, d1["id"], d1["approval_id"], world["ws"])
    assert v1["status"] == "promoted" and v1["version"] == 1 and count() == d1["candidate"]["row_count"]
    again = svc.materialize(admin, d1["id"], d1["approval_id"], world["ws"])  # idempotent: no second version
    assert again["id"] == v1["id"]
    # the workspace reader role reads the destination (default privileges)
    from analystos.core.config import get_settings
    from analystos.staging.roles import role_for

    with psycopg.connect(_pg(os.environ["ANALYSTOS_ANALYTICS_READER_URL"])) as conn:
        conn.execute(f'SET ROLE "{role_for(get_settings(), world["ws"])}"')
        assert conn.execute(f"SELECT count(*) FROM {DEST}.cr_mart").fetchone()[0] == count()

    # fail: an invalid output (a fail gate) is blocked at the dry run; nothing is approved, v1 stays
    rows = _records()
    saved = copy.deepcopy(rows[:5])
    for r in rows[:5]:
        r["risk"], r["sys_updated_on"] = "99", "2031-01-02 00:00:00"
    _refresh(world)
    blocked = svc.dry_run(admin, pid, world["ws"])
    assert blocked["status"] == "blocked" and blocked["approval_id"] is None and "gate" in blocked["error"]
    with pytest.raises(Conflict, match="blocked"):
        svc.materialize(admin, blocked["id"], None, world["ws"])
    assert count() == d1["candidate"]["row_count"]
    for r, old in zip(rows[:5], saved, strict=True):
        r.update(old, sys_updated_on="2031-01-02 00:01:00")
    _refresh(world)

    # fail: a crash after staging, before promotion -> the good version stays; the retry resumes, no restage
    d2 = svc.dry_run(admin, pid, world["ws"])
    approve(d2["approval_id"])
    real_promote = ManagedWriter.promote
    monkeypatch.setattr(ManagedWriter, "promote", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worker killed")))
    with pytest.raises(RuntimeError):
        svc.materialize(admin, d2["id"], d2["approval_id"], world["ws"])
    with session_scope() as s:
        failed = s.scalar(select(Materialization).where(Materialization.pipeline_run_id == d2["id"]))
        assert failed.status == "failed" and failed.checkpoint["staged"] and failed.checkpoint["failed_stage"] == "promote"
        assert s.scalar(select(RunEvent).where(RunEvent.workspace_id == world["ws"],
                                               RunEvent.type == "pipeline.materialization.failed")) is not None
        assert s.scalar(select(Notification).where(Notification.workspace_id == world["ws"],
                                                   Notification.kind == "pipeline")) is not None
    assert count() == d1["candidate"]["row_count"]
    monkeypatch.setattr(ManagedWriter, "promote", real_promote)
    monkeypatch.setattr(ManagedWriter, "stage", lambda *a, **k: (_ for _ in ()).throw(AssertionError("restaged")))
    v2 = svc.materialize(admin, d2["id"], d2["approval_id"], world["ws"])
    monkeypatch.undo()
    assert v2["status"] == "promoted" and v2["version"] == 2 and v2["previous_id"] == v1["id"]

    # an approval bound to a rollback pointer that moved is refused
    d3 = svc.dry_run(admin, pid, world["ws"])
    approve(d3["approval_id"])
    back = svc.rollback(admin, v2["id"], world["ws"])  # recover: back to v1
    assert back["id"] == v1["id"] and back["status"] == "promoted" and count() == d1["candidate"]["row_count"]
    with pytest.raises(ApprovalRequired, match="payload changed"):
        svc.materialize(admin, d3["id"], d3["approval_id"], world["ws"])

    # freshness alert, once per version
    from analystos.core.ids import utcnow

    alerts = svc.check_freshness(utcnow() + timedelta(hours=48))
    assert [a["destination"] for a in alerts] == [f"{DEST}.cr_mart"]
    assert svc.check_freshness(utcnow() + timedelta(hours=49)) == []

    # source mutation stays denied: the writer role holds nothing on a source schema
    settings = get_settings()
    with psycopg.connect(_pg(settings.analytics_writer_url)) as conn:
        conn.execute(f'SET ROLE "{writer_role_for(settings, world["ws"])}"')
        for stmt in (f'INSERT INTO "{world["schema_a"]}".{TABLE} (sys_id) VALUES (\'x\')',
                     f'CREATE TABLE "{world["schema_a"]}".evil (x int)',
                     f'SELECT count(*) FROM "{world["schema_a"]}".{TABLE}'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
            conn.rollback()
            conn.execute(f'SET ROLE "{writer_role_for(settings, world["ws"])}"')
    # the login alone (NOINHERIT) cannot write either
    with psycopg.connect(_pg(settings.analytics_writer_url)) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"CREATE TABLE {DEST}.sneaky (x int)")
    with pytest.raises(InvalidInput):
        svc.run_pipeline(admin, pid, world["ws"], mode="sideways")


def test_pipeline_api_routes(world):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.core.config import get_settings

    with TestClient(app) as api:
        r = api.post("/api/auth/login", json={"email": get_settings().bootstrap_admin_email, "password": PASSWORD})
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}
        ws = world["ws"]
        listed = api.get(f"/api/workspaces/{ws}/pipelines", headers=h)
        assert listed.status_code == 200 and {p["name"] for p in listed.json()} >= {"cr_mart", "cr_groups"}
        pid = next(p["id"] for p in listed.json() if p["name"] == "cr_groups")
        assert api.get(f"/api/workspaces/{ws}/pipelines/{pid}", headers=h).status_code == 200
        runs = api.get(f"/api/workspaces/{ws}/pipeline-runs", headers=h, params={"pipeline": "cr_groups"}).json()
        assert runs and api.get(f"/api/workspaces/{ws}/pipeline-runs/{runs[0]['id']}", headers=h).json()["manifest"]
        assert api.get(f"/api/workspaces/{ws}/writer-destinations", headers=h).json()[0]["schema_name"] == DEST
        assert len(api.get(f"/api/workspaces/{ws}/materializations", headers=h).json()) >= 2
        bad = api.post(f"/api/workspaces/{ws}/pipelines/validate", headers=h,
                       json={"spec": {"type": "pipeline", "name": "x", "recipes": [{"name": "nope"}],
                                      "output": {"output": "o", "schema": [{"name": "a", "type": "text"}]}}})
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "pipeline_invalid"
        refresh = api.post(f"/api/workspaces/{ws}/sources/{world['a']}/refresh", headers=h,
                           json={"asset": TABLE, "mode": "replay", "since": "2031-01-01T00:00:00",
                                 "until": "2031-01-02T00:00:00"})
        assert refresh.status_code == 200 and refresh.json()["incremental"]["load_mode"] == "merge"
        other = api.post(f"/api/workspaces/{ws}/sources/{world['b']}/refresh", headers=h, json={"asset": TABLE, "mode": "full"})
        assert other.status_code == 422  # no incremental block declared


def test_migration_0039_up_down_up_matches_the_models(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT
    from analystos.db.models import Materialization, Pipeline, PipelineRun, WriterDestination

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig39"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    models = [Pipeline, PipelineRun, WriterDestination, Materialization]
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0039")
        for m in models:
            cols = {c["name"] for c in inspect(engine).get_columns(m.__tablename__)}
            assert cols == {c.name for c in m.__table__.columns}, m.__tablename__
        command.downgrade(cfg, "0035")
        assert not {m.__tablename__ for m in models} & set(inspect(engine).get_table_names())
        command.upgrade(cfg, "0039")
        assert {m.__tablename__ for m in models} <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
