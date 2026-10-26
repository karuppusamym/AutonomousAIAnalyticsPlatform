"""P4-05 live analyst workflow, over HTTP with the seeded users (analyst, approver, admin; `analystos seed`):

1. the analyst proposes entities and grain (datasets with primary keys) and a join (measured candidate);
2. both are reviewed (diff before approval) and approved by the approver, never by the analyst (separation
   of duties, through the semantic routes and the approvals inbox alike);
3. the analyst proposes a KPI on that structure; the approver approves v1;
4. an analysis step in a notebook computes the KPI through the governed compiler: its verdict depends on v1;
5. a second version supersedes v1 and voids that verdict; the re-run analysis depends on v2;
6. deprecating the KPI voids the analysis again and the governed query is refused from then on;
7. the analyst hands the semantic model to the admin: a hash-bound offer that only the admin can accept.

Writes a JSON summary when ANALYSTOS_EVIDENCE_OUT is set (the dated evidence file quotes it).
"""
from __future__ import annotations

import json
import os

import pandas as pd
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"


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


def _me(api, headers) -> str:
    r = api.get("/api/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def env(api):
    """A workspace the analyst owns (approver and admin are members) over two staged tables."""
    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, User
    from analystos.services.sources import discover_source, register_source, select_assets

    analyst, approver, admin = (_login(api, f"{u}@analystos.local") for u in ("analyst", "approver", "admin"))
    r = api.post("/api/workspaces", headers=analyst, json={"name": "analyst workflow", "objective": "Governed KPIs"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    assert api.put(f"/api/workspaces/{ws}/policy", headers=analyst,
                   json={"separation_of_duties": True}).status_code == 200
    for email, role in (("approver@analystos.local", "approver"), ("admin@analystos.local", "editor")):
        assert api.post(f"/api/workspaces/{ws}/members", headers=analyst, json={"email": email, "role": role}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"customer_id": [1, 2, 3, 4], "segment": ["Enterprise", "SMB", "SMB", "Enterprise"]}) \
        .to_parquet(folder / "customers.parquet", index=False)
    pd.DataFrame({"order_id": list(range(1, 9)), "customer_id": [1, 1, 2, 3, 3, 3, 4, 4],
                  "amount": [10.0, 20.0, 5.0, 1.0, 2.0, 3.0, 100.0, 50.0]}).to_parquet(folder / "orders.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    select_assets(user, src_id, ["customers", "orders"])
    with session_scope() as s:
        assets = {a.name: f"{a.schema_name}.{a.name}" for a in s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ws))}
    return {"ws": ws, "analyst": analyst, "approver": approver, "admin": admin, "assets": assets,
            "ids": {"analyst": _me(api, analyst), "approver": _me(api, approver), "admin": _me(api, admin)},
            "base": f"/api/workspaces/{ws}/semantic"}


def _field(name: str, dimension: bool = False) -> dict:
    return {"name": name, "expressions": [{"expression": name}], **({"dimension": {"is_time": False}} if dimension else {})}


def _step(api, env, headers, branch: str) -> dict:
    body = {"kind": "query", "title": "Sales by segment",
            "spec": {"semantic_query": {"metrics": ["sales_amount"], "dimensions": ["customers.segment"],
                                        "order": [{"field": "customers.segment"}]}}}
    r = api.post(f"/api/workspaces/{env['ws']}/branches/{branch}/steps", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _deps(step: dict) -> dict:
    return {(d["kind"], d["ref"]): d["version_hash"] for d in (step["verification_record"] or {}).get("dependencies") or []}


def test_the_semantic_approval_workflow_end_to_end(api, env):
    base, ws, analyst, approver, admin = env["base"], env["ws"], env["analyst"], env["approver"], env["admin"]
    orders, customers = env["assets"]["orders"], env["assets"]["customers"]
    ids = env["ids"]
    summary: dict = {"workspace": ws}

    # ---- 1. entities and grain: the analyst proposes; the analyst cannot approve; the approver does
    proposal = {"description": "Shop: one row per order and per customer", "datasets": [
        {"name": "orders", "source": orders, "primary_key": ["order_id"], "description": "Entity: order. Grain: one row per order.",
         "fields": [_field("order_id"), _field("customer_id"), _field("amount")]},
        {"name": "customers", "source": customers, "primary_key": ["customer_id"],
         "description": "Entity: customer. Grain: one row per customer.",
         "fields": [_field("customer_id"), _field("segment", dimension=True)]}]}
    bad = {**proposal, "datasets": [{**proposal["datasets"][0], "primary_key": ["order_no"]}]}
    r = api.post(f"{base}/model/proposals", headers=analyst, json=bad)
    assert r.status_code == 422 and "order_no" in r.text  # a grain on a column the table does not have
    assert api.post(f"{base}/model/proposals", headers=approver, json=proposal).status_code == 403  # approver < editor
    r = api.post(f"{base}/model/proposals", headers=analyst, json=proposal)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert r.json()["status"] == "proposed" and r.json()["origin"] == "user_proposal"
    diff = api.get(f"{base}/model/diff?version={version}", headers=approver).json()
    assert diff["entries"], diff
    assert api.post(f"{base}/model/approve", headers=analyst, json={"version": version}).status_code == 403
    r = api.post(f"{base}/model/approve", headers=approver, json={"version": version, "reason": "grain checked"})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    assert r.json()["decided_by"] == ids["approver"]
    summary["structure"] = {"version": version, "proposed_by": ids["analyst"], "approved_by": ids["approver"]}

    # ---- join: proposed by the analyst, measured through the gateway, accepted by the approver
    r = api.post(f"{base}/relationships/candidates", headers=analyst,
                 json={"from_asset": orders, "from_columns": ["customer_id"], "to_asset": customers, "to_columns": ["customer_id"]})
    assert r.status_code == 200, r.text
    cand = r.json()
    assert cand["status"] == "pending" and cand["cardinality"] == "many_to_one"
    assert api.post(f"{base}/relationships/candidates/{cand['id']}/accept", headers=analyst).status_code == 403
    assert api.post(f"/api/approvals/{cand['approval_id']}/approve", headers=analyst).status_code == 403
    r = api.post(f"{base}/relationships/candidates/{cand['id']}/accept", headers=approver, json={"reason": "FK by design"})
    assert r.status_code == 200 and r.json()["status"] == "accepted", r.text
    model = api.get(base, headers=analyst).json()["model"]
    rel = next(x for x in model["relationships"] if x["name"] == r.json()["relationship_name"])
    assert (rel["from"], rel["to"], rel["cardinality"], rel["validated_by"]) == ("orders", "customers", "many_to_one", ids["approver"])
    assert model["status"] == "approved" and {d["name"]: d["primary_key"] for d in model["datasets"]} == \
        {"orders": ["order_id"], "customers": ["customer_id"]}
    summary["join"] = {"relationship": rel["name"], "cardinality": rel["cardinality"], "structure_version": model["version"]}

    # ---- 2./3. a KPI on the approved structure: proposed by the analyst, approved by the approver
    metric = {"name": "sales_amount", "expression": "SUM(amount)", "dataset": "orders", "display_name": "Sales amount",
              "dimensions": ["customers.segment"]}
    check = api.post(f"{base}/metrics/validate", headers=analyst, json=metric).json()
    assert not check.get("problems"), check
    assert api.post(f"{base}/metrics", headers=analyst, json=metric).status_code == 200
    assert api.post(f"{base}/metrics/sales_amount/approve", headers=analyst).status_code == 403
    r = api.post(f"{base}/metrics/sales_amount/approve", headers=approver, json={"reason": "matches finance"})
    assert r.status_code == 200 and (r.json()["status"], r.json()["version"]) == ("approved", 1), r.text
    assert r.json()["owner_id"] == ids["analyst"] and r.json()["decided_by"] == ids["approver"]

    # ---- 4. an analysis step computes the KPI through the governed compiler; its verdict depends on v1
    nb = api.post(f"/api/workspaces/{ws}/notebooks", headers=analyst, json={"title": "Sales review"})
    assert nb.status_code == 201, nb.text
    branch = api.get(f"/api/workspaces/{ws}/threads/notebook/{nb.json()['id']}", headers=analyst).json()["branch"]["id"]
    step = _step(api, env, analyst, branch)
    assert step["status"] == "ok", step
    assert step["verification_record"]["state"] == "ACTIVE"
    v1_dep = _deps(step)[("semantic", f"{ws}/sales_amount")]
    snapshot = api.get(f"/api/workspaces/{ws}/steps/{step['id']}/why", headers=analyst)
    assert snapshot.status_code == 200, snapshot.text
    summary["analysis_v1"] = {"step": step["id"], "verdict": step["verification_record"]["verdict"],
                              "state": step["verification_record"]["state"]}

    # ---- 5. v2 supersedes v1: the verdict computed on v1 is void; the re-run analysis stands on v2
    metric_v2 = {**metric, "expression": "SUM(amount) * 1.0", "description": "Net sales in EUR"}
    assert api.post(f"{base}/metrics", headers=analyst, json=metric_v2).json()["metric"]["version"] == 2
    r = api.post(f"{base}/metrics/sales_amount/approve", headers=approver, json={"version": 2})
    assert r.status_code == 200 and r.json()["status"] == "approved", r.text
    versions = api.get(f"{base}/metrics/sales_amount", headers=analyst).json()
    assert [(m["version"], m["status"]) for m in versions] == [(1, "deprecated"), (2, "approved")]
    after = api.get(f"/api/workspaces/{ws}/steps/{step['id']}", headers=analyst).json()
    assert (after["verification_record"]["state"], after["verification_record"]["void"]["kind"]) == ("VOID", "semantic"), after
    step2 = _step(api, env, analyst, branch)
    assert step2["status"] == "ok" and step2["verification_record"]["state"] == "ACTIVE"
    assert _deps(step2)[("semantic", f"{ws}/sales_amount")] != v1_dep
    summary["analysis_v2"] = {"step": step2["id"], "v1_step_state": "VOID (semantic)"}

    # ---- 6. deprecation voids the dependents; the governed query is refused from then on
    r = api.post(f"{base}/metrics/sales_amount/deprecate", headers=approver, json={"reason": "replaced by net_sales"})
    assert r.status_code == 200 and {m["status"] for m in r.json()} == {"deprecated"}, r.text
    gone = api.get(f"/api/workspaces/{ws}/steps/{step2['id']}", headers=analyst).json()
    assert (gone["verification_record"]["state"], gone["verification_record"]["void"]["kind"]) == ("VOID", "semantic"), gone
    assert "sales_amount" not in api.get(base, headers=analyst).json()["approved"]
    refused = _step(api, env, analyst, branch)
    assert refused["status"] == "failed" and "approved definition" in (refused["error"] or ""), refused
    summary["deprecation"] = {"voided_step": step2["id"], "rerun_status": refused["status"], "rerun_error": refused["error"]}

    # ---- 7. ownership of the semantic model: offered by the analyst, only the admin (named) can accept
    model = api.get(base, headers=analyst).json()["model"]
    assert model["owner_id"] == ids["analyst"]
    offer = api.post(f"{base}/ownership", headers=analyst,
                     json={"subject": "model", "name": model["name"], "to_owner": ids["admin"], "reason": "team change"})
    assert offer.status_code == 201, offer.text
    offer_id = offer.json()["id"]
    assert offer.json()["payload"]["content_hash"] == model["content_hash"]
    assert api.post(f"/api/approvals/{offer_id}/approve", headers=approver).status_code in (403, 409, 422)
    assert api.post(f"{base}/ownership/{offer_id}/accept", headers=approver).status_code == 403
    r = api.post(f"{base}/ownership/{offer_id}/accept", headers=admin)
    assert r.status_code == 200, r.text
    assert api.get(base, headers=analyst).json()["model"]["owner_id"] == ids["admin"]
    assert api.post(f"{base}/ownership/{offer_id}/accept", headers=admin).status_code == 409  # single use
    summary["ownership"] = {"subject": "model", "from": ids["analyst"], "to": ids["admin"], "approval": offer_id}

    out = os.environ.get("ANALYSTOS_EVIDENCE_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(summary, f, indent=1)
