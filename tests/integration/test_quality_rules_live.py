"""P1-7 and P2-9 on the real stack: rules declared on a staged table run through the gateway on Postgres (RLS
on), the catalog shows the trust signal, rules export as an OKF pack; a verified query published with a contract
shows its state, and a consumer's access request is decided by an approver and applied by code."""
from __future__ import annotations

import io
import zipfile

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


def _login(api, email):
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def env(api):
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.security.auth import hash_password
    from analystos.services.sources import discover_source, register_source

    consumer_email = f"consumer-{new_id('u')}@quality.test"  # not an admin: an admin acts as owner everywhere
    with session_scope() as s:
        s.add(User(id=new_id("usr"), email=consumer_email, name="Consumer", password_hash=hash_password(PASSWORD),
                   is_admin=False, active=True, attributes={}))
    owner = _login(api, "analyst@analystos.local")
    approver = _login(api, "approver@analystos.local")
    consumer = _login(api, consumer_email)
    r = api.post("/api/workspaces", headers=owner, json={"name": "quality rules", "objective": "Trust the orders table"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    for email, role in (("approver@analystos.local", "approver"), (consumer_email, "viewer")):
        assert api.post(f"/api/workspaces/{ws}/members", headers=owner, json={"email": email, "role": role}).status_code == 200
    folder = get_settings().upload_dir / ws / "shop"
    folder.mkdir(parents=True, exist_ok=True)
    n = 200
    pd.DataFrame({"order_id": list(range(1, n + 1)), "amount": [10.0 + i for i in range(n)],
                  "channel": (["web", "shop", "phone", "app"] * (n // 4))[:n - 2] + [None, "fax"]}
                 ).to_parquet(folder / "orders.parquet", index=False)
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        src = register_source(s, user, ws, kind="csv", name="shop", config={"path": f"{ws}/shop"}, secret_ref=None)
        s.flush()
        src_id = src.id
        s.expunge(user)
    discover_source(user, src_id)
    r = api.put(f"/api/workspaces/{ws}/sources/{src_id}/selection", headers=owner, json={"assets": ["orders"]})
    assert r.status_code == 200, r.text
    catalog = api.get(f"/api/workspaces/{ws}/catalog", headers=owner).json()
    asset = next(a for a in catalog if a["name"] == "orders")
    return {"ws": ws, "src": src_id, "owner": owner, "approver": approver, "consumer": consumer, "fq": asset["fq"],
            "asset_id": asset["id"]}


def test_declared_rules_run_through_the_gateway_and_show_as_trust(api, env):
    base = f"/api/workspaces/{env['ws']}/quality-rules"
    rules = [{"type": "not_null", "asset": env["fq"], "column": "order_id"},
             {"type": "unique", "asset": env["fq"], "column": "order_id", "severity": "critical"},
             {"type": "accepted_values", "asset": env["fq"], "column": "channel", "values": ["web", "shop", "phone", "app"]},
             {"type": "statistic_range", "asset": env["fq"], "column": "amount", "statistic": "mean", "min": 50, "max": 150}]
    ids = []
    for rule in rules:
        r = api.post(base, headers=env["owner"], json=rule)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    assert api.post(base, headers=env["approver"], json=rules[0]).status_code == 403  # not an editor
    r = api.post(f"{base}:run", headers=env["owner"], json={})
    assert r.status_code == 200, r.text
    by_id = {o["rule_id"]: o for o in r.json()["results"]}
    assert [by_id[i]["status"] for i in ids] == ["passed", "passed", "failed", "passed"]
    assert by_id[ids[2]]["pass_rate"] == pytest.approx(199 / 200) and all(o["query_id"] for o in by_id.values())
    trust = next(a for a in api.get(f"/api/workspaces/{env['ws']}/catalog", headers=env["owner"]).json()
                 if a["id"] == env["asset_id"])["quality"]
    assert trust["level"] == "at_risk" and trust["passing"] == 3 and trust["failing"] == 1
    pack = api.get(f"{base}/export", headers=env["approver"])
    assert pack.status_code == 200 and pack.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(pack.content)).namelist()
    assert "index.md" in names and sum(n.startswith("quality-rules/") for n in names) == 4
    env["rules"] = ids


def test_product_contract_and_access_request(api, env):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User, VerifiedQuery

    if "rules" not in env:
        pytest.skip("needs the declared rules")
    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        vq = VerifiedQuery(id=new_id("vq"), workspace_id=env["ws"], name="Order count", description="Orders.",
                           sql_template=f"SELECT COUNT(*) AS n FROM {env['fq']}", parameters=[], source_id=env["src"],
                           status="active", created_by=owner.id)
        s.add(vq)
        vq_id = vq.id
    contract = {"freshness": {"max_age_hours": 24, "assets": [env["fq"]]}, "quality_rules": env["rules"][:2]}
    r = api.post("/api/v2/tools:publish", headers=env["owner"], json={
        "workspace_id": env["ws"], "source_type": "verified_query", "ref": vq_id, "options": {"contract": contract}})
    assert r.status_code == 201, r.text
    tool_id = r.json()["tool_id"]
    st = api.get(f"/api/v2/tools/{tool_id}/contract", headers=env["consumer"], params={"workspace_id": env["ws"]}).json()
    assert st["has_contract"] and st["ok"], st
    r = api.post(f"/api/v2/tools/{tool_id}/access-requests", headers=env["consumer"],
                 json={"workspace_id": env["ws"], "reason": "weekly review"})
    assert r.status_code == 201, r.text
    aid = r.json()["approval_id"]
    assert api.post(f"/api/v2/access-requests/{aid}:decide", headers=env["consumer"], json={"approve": True}).status_code == 403
    r = api.post(f"/api/v2/access-requests/{aid}:decide", headers=env["approver"], json={"approve": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed" and r.json()["workspace_role"]["to"] == "analyst"
