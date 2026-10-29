"""CONV-06 live: cross-filtering and drill-down over HTTP against the compose Postgres, every re-query governed.

A workspace over a staged orders table gets an approved semantic model with two metrics; the analyst builds a
dashboard in the builder chat (a KPI, two metric charts and a monthly trend, each a governed question). Then:

1. `GET explore` names what a click on each chart filters by and drills into.
2. A click on "1 - Critical" filters the other tiles (the KPI and a metric chart through the semantic compiler) and
   leaves the trend alone ("not filtered"); the numbers equal pandas over the same file. No version is created.
3. A viewer with a row filter (region IN their regions) sees the same click restricted to EMEA; the row-level
   detail never lists the PII column; a value outside the vocabulary and an unbound field are refused (422).
4. A month drills to its days, a priority drills to the metric's next dimension.
5. "Apply as dashboard filter" is the typed op: a new version (editors only; the viewer gets 403).
6. Every statement is a gateway execution with the viewer as actor and a `dashboard.*` purpose.

No model is called (the provider key is blanked for the module).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


@pytest.fixture(scope="module", autouse=True)
def no_models():
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OPENROUTER_API_KEY", "")
        yield


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _orders() -> pd.DataFrame:
    start = datetime(2026, 6, 2)
    return pd.DataFrame([{"number": f"ORD{i:05}", "priority": ["1 - Critical", "2 - High", "3 - Moderate"][i % 3],
                          "category": ["Network", "Billing", "Shipping"][i % 3 if i % 2 else (i // 2) % 3],
                          "region": ["EMEA", "APAC", "AMER"][(i // 3) % 3], "opened_at": start + timedelta(days=i * 2),
                          "resolution_hours": float(2 + (i % 7) * 3), "customer_email": f"buyer{i}@example.com"}
                         for i in range(72)])


def _field(name: str, *, dimension: bool = False, time: bool = False) -> dict:
    return {"name": name, "expressions": [{"expression": name}],
            **({"dimension": {"is_time": time}} if dimension or time else {})}


@pytest.fixture(scope="module")
def env(api):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, SourceColumn, User
    from analystos.services.sources import discover_source, register_source, select_assets

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "dashboard exploration", "objective": "Order operations"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    frame = _orders()
    frame.to_parquet(folder / "orders.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    select_assets(user, src_id, ["orders"])
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.name == "orders"))
        fq = f"{asset.schema_name}.{asset.name}"
        email = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset.id, SourceColumn.name == "customer_email"))
        email.tags = sorted({*(email.tags or []), "pii"})  # nobody here has PII clearance: denied to every reader
    base = f"/api/workspaces/{ws}/semantic"
    model = {"description": "Orders: one row per order", "datasets": [
        {"name": "orders", "source": fq, "primary_key": ["number"], "description": "Entity: order. Grain: one row per order.",
         "fields": [_field("number"), _field("priority", dimension=True), _field("category", dimension=True),
                    _field("region", dimension=True), _field("opened_at", time=True), _field("resolution_hours")]}]}
    r = api.post(f"{base}/model/proposals", headers=analyst, json=model)
    assert r.status_code == 201, r.text
    r = api.post(f"{base}/model/approve", headers=approver, json={"version": r.json()["version"], "reason": "grain checked"})
    assert r.status_code == 200, r.text
    dims = ["orders.priority", "orders.category", "orders.region", "orders.opened_at"]
    for metric in ({"name": "order_count", "expression": "COUNT(number)", "dataset": "orders", "display_name": "Order count",
                    "dimensions": dims},
                   {"name": "resolution_time", "expression": "AVG(resolution_hours)", "dataset": "orders",
                    "display_name": "Resolution time", "description": "Average hours to resolve an order", "dimensions": dims}):
        assert api.post(f"{base}/metrics", headers=analyst, json=metric).status_code == 200
        r = api.post(f"{base}/metrics/{metric['name']}/approve", headers=approver, json={"reason": "ok"})
        assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    empty = {"id": "order_ops", "title": "Order operations", "layout": {"items": []}, "charts": [], "kpis": [], "text_blocks": [],
             "filters": []}
    r = api.post(f"/api/v2/workspaces/{ws}/dashboards", headers=analyst, json={"document": empty})
    assert r.status_code == 201, r.text
    dash = r.json()["id"]
    for text in ("add order count", "add order count by priority", "add resolution time by category", "add monthly order count"):
        r = api.post(f"/api/v2/dashboards/{dash}/messages", headers=analyst, json={"text": text})
        assert r.status_code == 200 and r.json()["status"] == "applied", (text, r.text)
    doc = api.get(f"/api/v2/dashboards/{dash}", headers=analyst).json()
    return {"ws": ws, "analyst": analyst, "approver": approver, "frame": frame, "fq": fq, "dash": dash, "doc": doc}


def _by(doc: dict) -> dict:
    charts = doc["document"]["charts"]
    pick = lambda f: next(c for c in charts if f(c))  # noqa: E731
    return {"priority": pick(lambda c: c["dataset_ref"].get("dimensions") == ["orders.priority"]),
            "category": pick(lambda c: c["dataset_ref"].get("dimensions") == ["orders.category"]),
            "trend": pick(lambda c: c["type"] == "line"), "kpi": doc["document"]["kpis"][0]}


def test_cross_filter_and_drill_are_governed_requeries(api, env):
    from analystos.db.base import session_scope
    from analystos.db.models import QueryExecution, User, Workspace
    from analystos.governance.policy import load_policy, save_policy

    ws, analyst, approver, frame, dash = env["ws"], env["analyst"], env["approver"], env["frame"], env["dash"]
    tiles = _by(env["doc"])
    version = env["doc"]["version"]
    summary: dict = {"workspace": ws, "dashboard": dash, "version": version}

    # ---- 1. what each chart offers
    r = api.get(f"/api/v2/dashboards/{dash}/explore", headers=analyst)
    assert r.status_code == 200, r.text
    offers = {c["id"]: c for c in r.json()["charts"]}
    assert offers[tiles["trend"]["id"]]["drill"]["kind"] == "time"
    assert [lv["grain"] for lv in offers[tiles["trend"]["id"]]["drill"]["levels"]] == ["month", "day"]
    assert offers[tiles["priority"]["id"]]["drill"]["kind"] == "dimension"
    assert offers[tiles["priority"]["id"]]["filter_fields"] == ["orders.priority"]
    summary["explore"] = {k: v["drill"]["kind"] for k, v in offers.items()}

    # ---- 2. a click filters the other tiles; the trend has no priority field and is left alone
    click = {"field": "orders.priority", "value": "1 - Critical", "source": tiles["priority"]["id"]}
    r = api.post(f"/api/v2/dashboards/{dash}/view", headers=analyst, json={"filters": [click], "base_version": version})
    assert r.status_code == 200, r.text
    view = {t["id"]: t for t in r.json()["tiles"]}
    crit = frame[frame.priority == "1 - Critical"]
    assert view[tiles["kpi"]["id"]]["status"] == "filtered" and view[tiles["kpi"]["id"]]["value"] == len(crit)
    by_cat = view[tiles["category"]["id"]]
    assert by_cat["status"] == "filtered"
    assert {row[0]: pytest.approx(row[1]) for row in by_cat["preview"]["rows"]} == crit.groupby("category")["resolution_hours"].mean().to_dict()
    assert view[tiles["priority"]["id"]]["status"] == "source"
    assert view[tiles["trend"]["id"]]["status"] == "not_filtered" and "Priority" in view[tiles["trend"]["id"]]["reason"]
    assert api.get(f"/api/v2/dashboards/{dash}", headers=analyst).json()["version"] == version  # a view is not a version
    r = api.post(f"/api/v2/dashboards/{dash}/view", headers=analyst, json={"filters": [click], "base_version": version + 5})
    assert r.status_code == 409
    summary["analyst_view"] = {k: {kk: v.get(kk) for kk in ("status", "value", "reason")} for k, v in view.items()}

    # ---- 3. a viewer with a row filter: the same click, restricted to their region; PII never listed
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "viewer"}).status_code == 200
    with session_scope() as s:
        w = s.get(Workspace, ws)
        policy = load_policy(s, w)
        policy.row_filters = [{"id": "by_region", "assets": [env["fq"]], "predicate": "region IN {{user.regions}}"}]
        save_policy(s, w, type(policy).model_validate(policy.model_dump()), s.scalar(select(User.id).where(User.email == "analyst@analystos.local")))
        viewer = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        viewer.attributes = {**(viewer.attributes or {}), "regions": ["EMEA"]}
        viewer_id = viewer.id
    try:
        r = api.post(f"/api/v2/dashboards/{dash}/view", headers=approver, json={"filters": [click]})
        assert r.status_code == 200, r.text
        seen = {t["id"]: t for t in r.json()["tiles"]}
        emea = crit[crit.region == "EMEA"]
        assert seen[tiles["kpi"]["id"]]["value"] == len(emea)
        assert {row[0]: pytest.approx(row[1]) for row in seen[tiles["category"]["id"]]["preview"]["rows"]} == \
            emea.groupby("category")["resolution_hours"].mean().to_dict()
        r = api.post(f"/api/v2/dashboards/{dash}/drill", headers=approver, json={
            "chart_id": tiles["priority"]["id"], "path": [{"field": "orders.priority", "value": "2 - High"}], "detail": True})
        assert r.status_code == 200, r.text
        detail = r.json()["detail"]
        assert "customer_email" not in detail["columns"] and detail["rows"], detail
        region, priority = detail["columns"].index("region"), detail["columns"].index("priority")
        assert {(row[region], row[priority]) for row in detail["rows"]} == {("EMEA", "2 - High")}
        assert len(detail["rows"]) == len(frame[(frame.region == "EMEA") & (frame.priority == "2 - High")])
        for bad in ({"field": "orders.priority", "value": "1' OR '1'='1"}, {"field": "customer_email", "value": "x"},
                    {"field": "orders.opened_at", "value": "2026-07-01"}):
            r = api.post(f"/api/v2/dashboards/{dash}/view", headers=approver, json={"filters": [bad]})
            assert r.status_code == 422, (bad, r.text)
        summary["viewer"] = {"kpi": seen[tiles["kpi"]["id"]]["value"], "detail_columns": detail["columns"],
                             "detail_rows": len(detail["rows"])}
    finally:
        with session_scope() as s:
            w = s.get(Workspace, ws)
            policy = load_policy(s, w)
            policy.row_filters = []
            save_policy(s, w, policy, viewer_id)

    # ---- 4. drill: a month to its days; a priority to the metric's next dimension
    trend = tiles["trend"]
    month = trend["preview"]["rows"][1][0]
    r = api.post(f"/api/v2/dashboards/{dash}/drill", headers=analyst, json={
        "chart_id": trend["id"], "path": [{"field": trend["encoding"]["x"], "value": month}]})
    assert r.status_code == 200, r.text
    days = r.json()
    start = pd.Timestamp(str(month)[:10])
    in_month = frame[(frame.opened_at >= start) & (frame.opened_at < start + pd.offsets.MonthBegin(1))]
    assert days["level"]["grain"] == "day" and sum(row[1] for row in days["preview"]["rows"]) == len(in_month)
    assert all(str(row[0])[:7] == str(month)[:7] for row in days["preview"]["rows"])
    r = api.post(f"/api/v2/dashboards/{dash}/drill", headers=analyst, json={
        "chart_id": tiles["priority"]["id"], "path": [{"field": "orders.priority", "value": "1 - Critical"}]})
    assert r.status_code == 200, r.text
    deeper = r.json()
    assert deeper["level"]["field"] == "orders.category"
    assert {row[0]: row[1] for row in deeper["preview"]["rows"]} == crit.groupby("category").size().to_dict()
    summary["drill"] = {"month": str(month), "days": len(days["preview"]["rows"]), "by_category": deeper["preview"]["rows"]}

    # ---- 5. "Apply as dashboard filter": the typed op, a new version; viewers cannot
    apply = api.post(f"/api/v2/dashboards/{dash}/view", headers=analyst, json={"filters": [click]}).json()["apply"][0]
    assert apply["op"] == "dashboard.add_filter" and apply["scope"] == [tiles["priority"]["id"]]
    r = api.post(f"/api/v2/dashboards/{dash}/ops", headers=approver, json={"ops": [apply]})
    assert r.status_code == 403, r.text
    r = api.post(f"/api/v2/dashboards/{dash}/ops", headers=analyst, json={"ops": [apply], "base_version": version})
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    assert r.json()["dashboard"]["version"] == version + 1
    saved = api.post(f"/api/v2/dashboards/{dash}/view", headers=analyst, json={"refresh_saved": True}).json()
    pri = next(t for t in saved["tiles"] if t["id"] == tiles["priority"]["id"])
    assert pri["status"] == "saved_filter" and [row[0] for row in pri["preview"]["rows"]] == ["1 - Critical"]

    # ---- 6. every statement was a gateway execution as the person who clicked
    with session_scope() as s:
        rows = list(s.scalars(select(QueryExecution).where(QueryExecution.workspace_id == ws,
                                                           QueryExecution.purpose.like("dashboard.%"))))
    by_purpose: dict[str, set[str]] = {}
    for q in rows:
        by_purpose.setdefault(q.purpose, set()).add(q.actor)
    assert set(by_purpose) == {"dashboard.cross_filter", "dashboard.drill", "dashboard.detail"}
    assert f"user:{viewer_id}" in by_purpose["dashboard.cross_filter"] and by_purpose["dashboard.detail"] == {f"user:{viewer_id}"}
    assert all(q.status == "ok" for q in rows)
    summary["gateway"] = {p: sorted(a) for p, a in by_purpose.items()} | {"statements": len(rows)}

    if os.environ.get("ANALYSTOS_EVIDENCE_OUT"):
        with open(os.environ["ANALYSTOS_EVIDENCE_OUT"], "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
