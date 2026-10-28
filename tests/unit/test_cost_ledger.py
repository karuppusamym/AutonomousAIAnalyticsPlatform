"""S12 §4 cost ledger (FR-052): priced lines from model calls, gateway statements, compute tasks and artifact
writes, rolled up by every dimension; blocked model families stay in the totals but are flagged; monthly
CSV/Parquet export streams; the spend API is tenant-bound. No services (SQLite)."""
from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta

import pytest

DAY1 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
DAY2 = datetime(2026, 10, 4, 9, 0, tzinfo=UTC)


@pytest.fixture
def ledger(sqlite_db):
    from analystos.db.base import session_scope
    from analystos.db.models import (
        AnalysisRun,
        Artifact,
        AskThread,
        AskTurn,
        ModelCall,
        QueryExecution,
        User,
        WorkerTask,
        Workspace,
    )

    with session_scope() as s:
        s.add(User(id="usr_a", email="a@x", name="A", password_hash="-", is_admin=False, attributes={}, tenant_id="tnt_a"))
        s.add(Workspace(id="ws_a", name="A", created_by="usr_a", settings={}, tenant_id="tnt_a"))
        s.add(Workspace(id="ws_b", name="B", created_by="usr_a", settings={}, tenant_id="tnt_b"))
        s.add(AnalysisRun(id="run_1", workspace_id="ws_a", objective="o", requested_by="usr_a", created_at=DAY1))
        s.add(AskThread(id="thr_1", workspace_id="ws_a", user_id="usr_a", title="t"))
        s.add(AskTurn(id="trn_1", thread_id="thr_1", workspace_id="ws_a", user_id="usr_a", question="q"))

        def call(id_, model, cost, *, ws="ws_a", run=None, task=None, status="ok", at=DAY1):
            s.add(ModelCall(id=id_, workspace_id=ws, run_id=run, task_id=task, purpose="sql_generation", profile="coding",
                            provider="openrouter", model=model, status=status, input_tokens=1000, output_tokens=100,
                            cost_usd=cost, created_at=at))

        call(1, "qwen/qwen3-32b", 0.01, task="trn_1")
        call(2, "qwen/qwen3-32b", 0.02, run="run_1", at=DAY2)
        call(3, "anthropic/claude-sonnet-5", 0.50, run="run_1")  # recorded before the family was blocked
        call(4, "qwen/qwen3-32b", 9.99, status="skipped")  # not billed: never in the ledger
        call(5, "qwen/qwen3-32b", 0.04, ws="ws_b")
        s.add(QueryExecution(id="q1", workspace_id="ws_a", run_id="run_1", actor="agent:x", sql="select 1", status="ok",
                             scan_bytes=2 * 10**12, credits=0.5, created_at=DAY1))
        s.add(QueryExecution(id="q2", workspace_id="ws_a", task_id="trn_1", actor="user:usr_a", sql="select 2", status="ok",
                             cache_hit=True, scan_bytes=10**12, created_at=DAY1))  # a cache hit costs nothing
        s.add(WorkerTask(id="wt1", workspace_id="ws_a", run_id="run_1", pool="compute", capability_id="c", capability_version="1",
                         capability_hash="h", kind="k", idempotency_key="i", envelope_hash="e", usage={"cpu_seconds": 7200},
                         created_at=DAY2))
        s.add(Artifact(id="art1", workspace_id="ws_a", run_id="run_1", type="dashboard", name="d", content={"x": "y" * 1000},
                       content_hash="h", created_at=DAY2))
    return sqlite_db


def _spend(**kw):
    from analystos.db.base import session_scope
    from analystos.ops import cost_ledger

    with session_scope() as s:
        return cost_ledger.spend(s, since=datetime(2026, 10, 1, tzinfo=UTC), until=datetime(2026, 11, 1, tzinfo=UTC), **kw)


def test_every_category_is_priced_and_rolled_up(ledger):
    from analystos.ops.cost_ledger import load_rates

    rates = load_rates()
    out = _spend(group_by="category")
    by = {g["category"]: g for g in out["groups"]}
    assert by["model"]["cost_usd"] == pytest.approx(0.01 + 0.02 + 0.50 + 0.04) and by["model"]["tokens"] == 4 * 1100
    assert by["query"]["cost_usd"] == pytest.approx(2 * rates.scan_usd_per_tb + 0.5 * rates.credit_usd)
    assert by["query"]["lines"] == 1 and by["query"]["scan_bytes"] == 2 * 10**12
    assert by["compute"]["cost_usd"] == pytest.approx(2 * rates.cpu_usd_per_hour) and by["compute"]["cpu_seconds"] == 7200
    assert by["storage"]["storage_bytes"] > 1000 and by["storage"]["cost_usd"] > 0
    assert out["totals"]["cost_usd"] == pytest.approx(sum(g["cost_usd"] for g in out["groups"]))
    assert out["rates_version"] == rates.version and out["groups"][0]["category"] == "query"  # most expensive first


def test_rollups_by_request_run_conversation_user_workspace_tenant_and_day(ledger):
    tenants = {g["tenant"]: g["cost_usd"] for g in _spend(group_by="tenant")["groups"]}
    assert set(tenants) == {"tnt_a", "tnt_b"} and tenants["tnt_b"] == pytest.approx(0.04)
    days = {g["day"]: g for g in _spend(group_by="day", categories=["model"])["groups"]}
    assert set(days) == {"2026-10-03", "2026-10-04"} and days["2026-10-04"]["cost_usd"] == pytest.approx(0.02)
    conv = {g["conversation"]: g["cost_usd"] for g in _spend(group_by="conversation", categories=["model"])["groups"]}
    assert conv["thr_1"] == pytest.approx(0.01)
    req = {g["request"]: g for g in _spend(group_by="request", categories=["model"])["groups"]}
    assert req["trn_1"]["cost_usd"] == pytest.approx(0.01)
    users = {g["user"]: g["cost_usd"] for g in _spend(group_by="user,workspace")["groups"] if g["workspace"] == "ws_a"}
    assert users["usr_a"] > 0.5  # the run's requester and the turn's user
    runs = {g["run"]: g["cost_usd"] for g in _spend(group_by="run")["groups"]}
    assert runs["run_1"] > 0.52
    ws = _spend(group_by="workspace", workspace_ids=["ws_b"])["groups"]
    assert [g["workspace"] for g in ws] == ["ws_b"]


def test_blocked_families_stay_in_totals_but_are_flagged(ledger):
    out = _spend(group_by="category,item", categories=["model"])
    items = {g["item"]: g for g in out["groups"]}
    assert items["anthropic/claude-sonnet-5"]["blocked"] is True and items["qwen/qwen3-32b"]["blocked"] is False
    assert out["blocked_model_usd"] == pytest.approx(0.50) and set(out["blocked_families"]) >= {"anthropic", "google"}
    assert out["totals"]["cost_usd"] == pytest.approx(0.57)  # real spend is never hidden


def test_bad_parameters_are_refused(ledger):
    from analystos.core.errors import InvalidInput

    with pytest.raises(InvalidInput, match="unknown: model"):
        _spend(group_by="model")
    from analystos.ops.cost_ledger import month_bounds

    with pytest.raises(InvalidInput):
        month_bounds("2026-13")
    assert month_bounds("2026-12")[1] == datetime(2027, 1, 1, tzinfo=UTC)


def test_group_limit_truncates(ledger):
    out = _spend(group_by="category", limit=2)
    assert len(out["groups"]) == 2 and out["truncated"] is True


def test_monthly_export_csv_and_parquet(ledger, tmp_path):
    import pyarrow.parquet as pq

    from analystos.db.base import session_scope
    from analystos.ops import cost_ledger

    with session_scope() as s:
        info = cost_ledger.export_file(s, "2026-10", tmp_path, "csv")
        rows = list(csv.DictReader((tmp_path / "cost-ledger-2026-10.csv").open(encoding="utf-8")))
        assert info["rows"] == len(rows) > 0 and set(rows[0]) == set(cost_ledger.EXPORT_COLUMNS)
        pinfo = cost_ledger.export_file(s, "2026-10", tmp_path, "parquet")
        table = pq.read_table(pinfo["path"])
        assert table.num_rows == info["rows"] and pinfo["cost_usd"] == pytest.approx(info["cost_usd"])
        assert cost_ledger.export_file(s, "2026-09", tmp_path, "csv")["rows"] == 0
        assert list(cost_ledger.export_rows(s, "2026-10", batch=2))[0].__len__() <= 2  # streamed in batches


def test_scope_cost_reads_all_categories_of_a_run(ledger):
    from analystos.db.base import session_scope
    from analystos.ops.cost_ledger import scope_cost

    with session_scope() as s:
        total = scope_cost(s, run_id="run_1", since=DAY1 - timedelta(days=1))
    assert total > 0.52 + 2 * 5.0  # model + the statement's scan + compute + storage


def test_spend_api_is_tenant_bound(ledger, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api import deps
    from analystos.api.app import app
    from analystos.db import base, models
    from analystos.db.base import session_scope
    from analystos.db.models import TenantMember, User
    from analystos.security.auth import issue_token
    from analystos.tenancy import resolve

    models.Base.metadata.tables["tenant_member"].create(ledger.kw["bind"])
    with session_scope() as s:
        s.add(User(id="usr_root", email="root@x", name="R", password_hash="-", is_admin=True, attributes={}, tenant_id="tnt_a"))
        s.add(User(id="usr_ta", email="ta@x", name="T", password_hash="-", is_admin=False, attributes={}, tenant_id="tnt_a"))
        s.add(TenantMember(tenant_id="tnt_a", user_id="usr_ta", role="tenant_admin"))
    monkeypatch.setattr(deps, "SessionLocal", base.SessionLocal)
    resolve.invalidate()
    client = TestClient(app)

    def hdr(uid, email):
        return {"Authorization": f"Bearer {issue_token(uid, email)}"}

    params = {"group_by": "category", "since": "2026-10-01", "until": "2026-11-01"}
    assert client.get("/v2/admin/spend", params=params, headers=hdr("usr_a", "a@x")).status_code == 403
    r = client.get("/v2/admin/spend", params=params, headers=hdr("usr_ta", "ta@x"))
    assert r.status_code == 200 and r.json()["scope"] == "tenant"
    assert client.get("/v2/admin/spend", params={**params, "scope": "platform"}, headers=hdr("usr_ta", "ta@x")).status_code == 403
    p = client.get("/v2/admin/spend", params={**params, "scope": "platform"}, headers=hdr("usr_root", "root@x"))
    assert p.status_code == 200 and p.json()["scope"] == "platform"
    assert client.get("/v2/admin/spend", params={**params, "group_by": "nope"}, headers=hdr("usr_root", "root@x")).status_code == 422
    csv_r = client.get("/v2/admin/spend/export", params={"month": "2026-10"}, headers=hdr("usr_ta", "ta@x"))
    assert csv_r.status_code == 200 and csv_r.text.splitlines()[0].startswith("day,tenant,workspace")
    pq_r = client.get("/v2/admin/spend/export", params={"month": "2026-10", "format": "parquet"}, headers=hdr("usr_ta", "ta@x"))
    assert pq_r.status_code == 200 and pq_r.content[:4] == b"PAR1"
