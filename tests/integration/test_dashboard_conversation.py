"""CONV-01/02/03 live: conversational dashboards over the compose Postgres with the seeded users (`analystos seed`).

A workspace over a staged incidents table gets an approved semantic model (proposed by the analyst, approved by the
approver) with two metrics. Then, over HTTP:

1. CONV-02: "Build an incident operations dashboard" -> a draft (version 1) of KPIs, charts and a filter control, every
   tile computed through the semantic compiler and the gateway; the numbers equal pandas over the same file.
2. CONV-01: "add resolution time by priority" in the builder chat -> a metric-bound chart with real data as version 2;
   "add weather by city" -> a clarifying reply, nothing changed.
3. CONV-03: an Ask answer -> "Add to dashboard" as a new draft and into the existing one (version 3).
4. The draft's publish request is an approval bound to its bundle (the metric charts compile for publishing).

No model is called: the provider key is blanked for the module, so anything the rules cannot ground is refused.
Writes a JSON summary when ANALYSTOS_EVIDENCE_OUT is set (the dated evidence file quotes it).
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


def _incidents() -> pd.DataFrame:
    start = datetime(2026, 7, 1)
    rows = []
    for i in range(60):
        rows.append({"number": f"INC{i:05}", "priority": ["1 - Critical", "2 - High", "3 - Moderate"][i % 3],
                     "category": ["Network", "Database", "Software", "Hardware"][i % 4],
                     "opened_at": start + timedelta(days=i * 2), "resolution_hours": float(2 + (i % 7) * 3 + (i % 3))})
    return pd.DataFrame(rows)


def _field(name: str, *, dimension: bool = False, time: bool = False) -> dict:
    return {"name": name, "expressions": [{"expression": name}],
            **({"dimension": {"is_time": time}} if dimension or time else {})}


@pytest.fixture(scope="module")
def env(api):
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.services.sources import discover_source, register_source, select_assets

    analyst, approver = _login(api, "analyst@analystos.local"), _login(api, "approver@analystos.local")
    r = api.post("/api/workspaces", headers=analyst, json={"name": "conversational dashboards", "objective": "Incident operations"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.post(f"/api/workspaces/{ws}/members", headers=analyst,
                    json={"email": "approver@analystos.local", "role": "approver"}).status_code == 200
    folder = get_settings().upload_dir / ws / "itsm"
    folder.mkdir(parents=True, exist_ok=True)
    frame = _incidents()
    frame.to_parquet(folder / "incidents.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="itsm", config={"path": f"{ws}/itsm"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    select_assets(user, src_id, ["incidents"])
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws, SourceAsset.name == "incidents"))
        fq = f"{asset.schema_name}.{asset.name}"
    base = f"/api/workspaces/{ws}/semantic"
    model = {"description": "Incidents: one row per incident", "datasets": [
        {"name": "incidents", "source": fq, "primary_key": ["number"], "description": "Entity: incident. Grain: one row per incident.",
         "fields": [_field("number"), _field("priority", dimension=True), _field("category", dimension=True),
                    _field("opened_at", time=True), _field("resolution_hours")]}]}
    r = api.post(f"{base}/model/proposals", headers=analyst, json=model)
    assert r.status_code == 201, r.text
    r = api.post(f"{base}/model/approve", headers=approver, json={"version": r.json()["version"], "reason": "grain checked"})
    assert r.status_code == 200, r.text
    for metric in ({"name": "resolution_time", "expression": "AVG(resolution_hours)", "dataset": "incidents",
                    "display_name": "Resolution time", "description": "Average hours to resolve an incident",
                    "dimensions": ["incidents.priority", "incidents.category", "incidents.opened_at"]},
                   {"name": "incident_count", "expression": "COUNT(number)", "dataset": "incidents", "display_name": "Incident count",
                    "dimensions": ["incidents.priority", "incidents.category", "incidents.opened_at"]}):
        assert api.post(f"{base}/metrics", headers=analyst, json=metric).status_code == 200
        r = api.post(f"{base}/metrics/{metric['name']}/approve", headers=approver, json={"reason": "ok"})
        assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    return {"ws": ws, "analyst": analyst, "approver": approver, "frame": frame, "fq": fq}


def _rows(chart: dict) -> dict:
    return {r[0]: r[1] for r in chart["preview"]["rows"]}


def test_prompt_question_and_answer_become_governed_draft_dashboards(api, env):
    ws, analyst, frame = env["ws"], env["analyst"], env["frame"]
    summary: dict = {"workspace": ws}
    by_priority = frame.groupby("priority")["resolution_hours"].mean().to_dict()

    # ---- CONV-02: a whole draft from one request
    r = api.post(f"/api/v2/workspaces/{ws}/dashboards/draft", headers=analyst, json={"prompt": "Build an incident operations dashboard"})
    assert r.status_code == 201, r.text
    draft = r.json()
    doc = draft["dashboard"]["document"]
    assert draft["dashboard"]["version"] == 1 and draft["plan"]["grounded_on"] == "semantic"
    assert 2 <= len(doc["kpis"]) <= 4 and 3 <= len(doc["charts"]) <= 6 and 1 <= len(doc["filters"]) <= 2, draft
    assert all(t["governance"] == "governed" for t in draft["built"]), draft["built"]
    kpis = {k["metric"]["name"]: k["value"] for k in doc["kpis"]}
    assert kpis["incident_count"] == len(frame)
    assert kpis["resolution_time"] == pytest.approx(frame["resolution_hours"].mean())
    by_pri = next(c for c in doc["charts"] if c["dataset_ref"].get("dimensions") == ["incidents.priority"])
    assert by_pri["type"] == "bar" and all(c["provenance"]["query_ids"] for c in doc["charts"])
    assert all(f["value"] is None for f in doc["filters"])  # controls, not fixed values
    summary["draft"] = {"id": draft["dashboard"]["id"], "kpis": kpis, "charts": [(c["title"], c["type"], c["dataset_ref"]["kind"])
                                                                                  for c in doc["charts"]],
                        "filters": [f["field"] for f in doc["filters"]], "failed": draft["failed"]}
    dash_id = draft["dashboard"]["id"]
    history = api.get(f"/api/v2/dashboards/{dash_id}/versions", headers=analyst).json()
    assert history[-1]["diff"][0]["origin"]["kind"] == "prompt"

    # a request that matches nothing the workspace has reviewed: a clarifying reply, no dashboard
    r = api.post(f"/api/v2/workspaces/{ws}/dashboards/draft", headers=analyst, json={"prompt": "a weather forecast dashboard"})
    assert r.status_code == 200 and r.json()["status"] == "clarify", r.text

    # ---- CONV-01: a chart from a question in the builder chat, on an empty dashboard
    empty = {"id": "ops_chat", "title": "Ops chat", "layout": {"items": []}, "charts": [], "kpis": [], "text_blocks": [], "filters": []}
    r = api.post(f"/api/v2/workspaces/{ws}/dashboards", headers=analyst, json={"document": empty})
    assert r.status_code == 201, r.text
    chat_id = r.json()["id"]
    r = api.post(f"/api/v2/dashboards/{chat_id}/messages", headers=analyst, json={"text": "add resolution time by priority", "base_version": 1})
    assert r.status_code == 200 and r.json()["status"] == "applied", r.text
    turn = r.json()
    chart = turn["dashboard"]["document"]["charts"][0]
    assert turn["proposal"]["answered_by"] == "rules" and turn["dashboard"]["version"] == 2
    assert chart["dataset_ref"] == {"kind": "metric", "name": "resolution_time", "version": 1,
                                    "dimensions": ["incidents.priority"], "sensitivity": "internal"}
    assert chart["type"] == "bar" and chart["provenance"]["sources"] == [env["fq"]]
    assert {k: pytest.approx(v) for k, v in _rows(chart).items()} == by_priority
    assert turn["applied"][0]["result"]["answered_by"] == "semantic"
    r = api.post(f"/api/v2/dashboards/{chat_id}/messages", headers=analyst, json={"text": "add weather by city"})
    refused = r.json()
    assert refused["status"] == "refused" and refused["clarify"]["clarify"], refused
    assert refused["dashboard"]["version"] == 2 and "Resolution time by priority" in refused["clarify"].get("examples", [])
    summary["chat"] = {"dashboard": chat_id, "chart": chart["id"], "rows": chart["preview"]["rows"],
                       "refused": refused["refused"][0]["reason"]}

    # ---- CONV-03: an Ask answer into a new draft, then into the prompt-built draft
    thread = api.post(f"/api/workspaces/{ws}/ask/threads", headers=analyst, json={"title": "resolution"}).json()
    r = api.post(f"/api/ask/threads/{thread['id']}/turns", headers=analyst, json={"question": "resolution time by category"})
    assert r.status_code == 200 and r.json()["status"] == "answered", r.text
    turn_id = r.json()["id"]
    r = api.post(f"/api/ask/turns/{turn_id}/promote", headers=analyst, json={"target": "draft", "title": "Resolution by category"})
    assert r.status_code == 200 and r.json()["status"] == "created", r.text
    new_draft = api.get(f"/api/v2/dashboards/{r.json()['id']}", headers=analyst).json()
    added = new_draft["document"]["charts"][0]
    assert added["type"] == "bar" and added["dataset_ref"]["kind"] == "metric" and added["provenance"]["turn_id"] == turn_id
    r = api.post(f"/api/ask/turns/{turn_id}/promote", headers=analyst, json={"target": "draft", "dashboard_id": dash_id})
    assert r.status_code == 200 and (r.json()["status"], r.json()["version"]) == ("added", 2), r.text
    summary["ask_to_draft"] = {"turn": turn_id, "new_draft": new_draft["id"], "existing": dash_id, "chart_type": added["type"]}

    # ---- the publish request is a hash-bound approval over the draft's bundle
    r = api.post(f"/api/v2/dashboards/{dash_id}/publish-request", headers=analyst, json={"destination": "preview"})
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "pending" and r.json()["payload_hash"]
    summary["publish_request"] = {"approval_id": r.json()["approval_id"], "risk_tier": r.json()["risk_tier"]}

    if os.environ.get("ANALYSTOS_EVIDENCE_OUT"):
        with open(os.environ["ANALYSTOS_EVIDENCE_OUT"], "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
