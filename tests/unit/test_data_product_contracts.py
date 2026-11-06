"""P2-9 data products with a contract and an access request: the signed `contract` block (old manifests still
verify), publish-time validation, contract judging (freshness, rule results) and its monitor, and consumer access
requests as hash-bound approvals that a person decides and code applies after `verify_for_execution`."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from analystos.contracts.quality import DataContract
from analystos.contracts.tool_manifest import ToolExecutionSpec, ToolManifestV2
from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, InvalidInput
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import (
    Approval,
    McpClient,
    McpGrant,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    VerifiedQuery,
    Workspace,
    WorkspaceMember,
)
from analystos.quality import contracts as C
from analystos.quality import monitoring
from analystos.quality.models import QualityRuleRow
from analystos.tenancy import DEFAULT_TENANT_ID, tenant_scope
from analystos.toolreg import access, signing, store
from analystos.toolreg import publish as pub

WS = "ws_dp"


def _manifest(**kw) -> ToolManifestV2:
    base = dict(tool_id="verified_query.x", version="1.0.0", kind="data_product", owner="o", input_schema={"type": "object"},
                side_effect="none", execution=ToolExecutionSpec(via="verified_query"))
    return ToolManifestV2(**{**base, **kw})


def test_contract_block_is_signed_and_absent_blocks_hash_as_before():
    m = _manifest()
    legacy = m.model_dump(mode="json", exclude={"signature", "contract"})
    assert m.signing_payload() == legacy and m.content_hash() == stable_hash(legacy)  # pre-P2-9 manifests verify
    c = DataContract(freshness={"max_age_hours": 24, "assets": ["shop.orders"]}, quality_rules=["qrule_b", "qrule_a"])
    with_c = _manifest(contract=c)
    assert with_c.content_hash() != m.content_hash() and with_c.contract.quality_rules == ["qrule_a", "qrule_b"]
    with pytest.raises(ValueError, match="only a data product"):
        ToolManifestV2(tool_id="playbook.x", version="1.0.0", kind="playbook", owner="o", input_schema={"type": "object"},
                       side_effect="write_internal", execution=ToolExecutionSpec(via="playbook"), contract=c)
    with pytest.raises(ValueError, match="freshness SLA or at least one"):
        DataContract()


@pytest.fixture
def world(sqlite_db):
    from analystos.db import models

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t]
                                                                  for t in ("quality_rule", "quality_rule_result")])
    with tenant_scope(DEFAULT_TENANT_ID):
        with session_scope() as s:
            for uid, admin in (("usr_ow", False), ("usr_ap", False), ("usr_vw", False), ("usr_vw2", False), ("usr_adm", True)):
                s.add(User(id=uid, email=f"{uid}@dp", name=uid, password_hash="x", is_admin=admin, active=True, attributes={}))
            s.add(Workspace(id=WS, name="dp", created_by="usr_ow", settings={}, policy_version=1, tenant_id=DEFAULT_TENANT_ID))
            for uid, role in (("usr_ow", "owner"), ("usr_ap", "approver"), ("usr_vw", "viewer"), ("usr_vw2", "viewer"),
                              ("usr_adm", "owner")):
                s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
            s.add(Source(id="src1", workspace_id=WS, kind="duckdb", name="shop", config={}, status="ready", owners={}))
            s.add(SourceAsset(id="ast1", source_id="src1", workspace_id=WS, schema_name="shop", name="orders",
                              source_name="orders", selected=True, freshness_at=utcnow() - timedelta(hours=3)))
            s.add(SourceColumn(asset_id="ast1", name="amount", ordinal=0, data_type="DOUBLE", tags=[], profile={}, semantics={}))
            s.add(VerifiedQuery(id="vq_1", workspace_id=WS, name="Order totals", description="Totals.",
                                sql_template="SELECT SUM(amount) AS total FROM shop.orders", parameters=[], status="active",
                                created_by="usr_ow"))
            s.add(QualityRuleRow(id="qrule_ok", workspace_id=WS, asset_id="ast1", asset="shop.orders", column="amount",
                                 name="not_null(amount)", type="not_null", dimension="completeness", severity="warning",
                                 threshold=1.0, definition={"type": "not_null", "asset": "shop.orders", "column": "amount"},
                                 content_hash="h1", status="active", origin="owner", created_by="usr_ow"))
            s.add(QualityRuleRow(id="qrule_off", workspace_id=WS, asset_id="ast1", asset="shop.orders", column="amount",
                                 name="range(amount)", type="range", dimension="validity", severity="critical", threshold=1.0,
                                 definition={}, content_hash="h2", status="disabled", origin="owner", created_by="usr_ow"))
        users = {}
        with session_scope() as s:
            for uid in ("usr_ow", "usr_ap", "usr_vw", "usr_vw2", "usr_adm"):
                u = s.get(User, uid)
                s.expunge(u)
                users[uid] = u
        yield SimpleNamespace(users=users)


CONTRACT = {"freshness": {"max_age_hours": 24, "assets": ["shop.orders"]}, "quality_rules": ["qrule_ok"]}


def _publish(user, contract=CONTRACT, **opts):
    with session_scope() as s:
        row, created = pub.publish(s, s.merge(user), WS, "verified_query", "vq_1",
                                   pub.PublishOptions(contract=contract, **opts) if contract else pub.PublishOptions(**opts))
        return row.tool_id, row.version


def test_publish_validates_and_signs_the_contract(world):
    tool_id, version = _publish(world.users["usr_ow"])
    with session_scope() as s:
        row = store.versions(s, WS, tool_id)[-1]
        m = store.verified(s, row)  # the signature covers the contract
        assert m.contract.quality_rules == ["qrule_ok"] and m.contract.freshness.max_age_hours == 24
        tampered = {**row.manifest, "contract": {**row.manifest["contract"], "quality_rules": []}}
        with pytest.raises(signing.ManifestRefused):
            signing.verify(s, DEFAULT_TENANT_ID, tampered, manifest_hash=row.manifest_hash)
    for bad, msg in [({"quality_rules": ["qrule_nope"]}, "not in this workspace"), ({"quality_rules": ["qrule_off"]}, "disabled"),
                     ({"freshness": {"max_age_hours": 1, "assets": ["shop.ghost"]}}, "not in this workspace's catalog")]:
        with pytest.raises(InvalidInput, match=msg):
            _publish(world.users["usr_ow"], contract=bad)
    assert _publish(world.users["usr_ow"], contract={**CONTRACT, "min_pass_rate": 0.9})[1] == "1.1.0"  # a new minor


def test_contract_status_and_monitor_report_breaches(world):
    tool_id, _ = _publish(world.users["usr_ow"])
    with session_scope() as s:
        st = C.contract_status(s, WS, tool_id)
        assert st["ok"] and st["unknown"] == 1  # the rule has not run: unknown, not a breach
        r = s.get(QualityRuleRow, "qrule_ok")
        r.last_status, r.last_pass_rate, r.last_run_at = "failed", 0.97, utcnow()
        s.get(SourceAsset, "ast1").freshness_at = utcnow() - timedelta(hours=60)
    with session_scope() as s:
        st = C.contract_status(s, WS, tool_id)
        kinds = {b["kind"]: b for b in st["breaches"]}
        assert not st["ok"] and set(kinds) == {"freshness", "quality_rule"}
        assert kinds["freshness"]["severity"] == "critical" and kinds["freshness"]["age_hours"] > 59
        monitoring.validate(s, WS, "data_contract", {"tool_id": tool_id})
        result = monitoring.evaluate(s, None, SimpleNamespace(kind="data_contract", workspace_id=WS, config={"tool_id": tool_id}))
        assert result["alert"] and result["severity"] == "critical"
        assert result["new"] == ["freshness:shop.orders", "rule:qrule_ok"]
    _publish(world.users["usr_ow"], contract=None, description="no contract")
    with session_scope() as s, pytest.raises(InvalidInput, match="no contract block"):
        monitoring.validate(s, WS, "data_contract", {"tool_id": tool_id})


def _request(user, tool_id, **kw):
    with session_scope() as s:
        a = access.request_access(s, s.merge(user), WS, tool_id, **kw)
        return a.id


def test_access_request_is_decided_by_a_person_and_applied_by_code(world):
    tool_id, version = _publish(world.users["usr_ow"])
    aid = _request(world.users["usr_vw"], tool_id, reason="monthly report")
    assert _request(world.users["usr_vw"], tool_id) == aid  # the same open request
    with session_scope() as s:
        a = s.get(Approval, aid)
        assert a.status == "pending" and a.action == access.ACTION and a.risk_tier == "medium"
        assert a.payload == {"product": tool_id, "version": version, "manifest_hash": store.versions(s, WS, tool_id)[-1].manifest_hash,
                             "requester": "usr_vw", "role": "analyst", "grant": None}
    with session_scope() as s, pytest.raises(Forbidden):
        access.decide(s, s.merge(world.users["usr_vw"]), aid, approve=True)  # a viewer cannot approve
    with session_scope() as s:
        out = access.decide(s, s.merge(world.users["usr_ap"]), aid, approve=True)
    assert out["status"] == "executed" and out["workspace_role"] == {"user_id": "usr_vw", "from": "viewer", "to": "analyst"}
    with session_scope() as s:
        assert s.get(Approval, aid).status == "executed"  # single use
        with pytest.raises(ApprovalRequired):
            access.apply(s, aid)
    with pytest.raises(Conflict, match="already hold"):
        _request(world.users["usr_vw"], tool_id)


def test_separation_of_duties_and_binding(world):
    tool_id, _ = _publish(world.users["usr_ow"])
    with pytest.raises(Conflict):  # an owner already holds analyst
        _request(world.users["usr_adm"], tool_id, role="analyst")
    own = _request(world.users["usr_ap"], tool_id)  # an approver asking for analyst never approves it themself
    with session_scope() as s, pytest.raises(Forbidden, match="separation of duties"):
        access.decide(s, s.merge(world.users["usr_ap"]), own, approve=True)
    with pytest.raises(InvalidInput, match="viewer, analyst"):
        _request(world.users["usr_vw2"], tool_id, role="editor")
    aid = _request(world.users["usr_vw2"], tool_id)
    with session_scope() as s:  # someone edits the approved payload: verify refuses, nothing is applied
        access.approvals.decide(s, aid, s.merge(world.users["usr_ap"]), approve=True)
        a = s.get(Approval, aid)
        a.payload = {**a.payload, "role": "analyst", "requester": "usr_vw"}
    with session_scope() as s, pytest.raises(ApprovalRequired, match="payload changed"):
        access.apply(s, aid)
    with session_scope() as s:
        assert s.scalar(select(WorkspaceMember.role).where(WorkspaceMember.user_id == "usr_vw2")) == "viewer"


def test_a_new_product_version_invalidates_an_open_request(world):
    tool_id, _ = _publish(world.users["usr_ow"])
    aid = _request(world.users["usr_vw2"], tool_id)
    with session_scope() as s:
        access.approvals.decide(s, aid, s.merge(world.users["usr_ap"]), approve=True)
    _publish(world.users["usr_ow"], contract={**CONTRACT, "min_pass_rate": 0.5})  # 1.1.0 supersedes 1.0.0
    with session_scope() as s, pytest.raises(Conflict, match="no longer the certified one"):
        access.apply(s, aid)


def test_mcp_grant_request_adds_the_product_to_the_clients_grant(world):
    from analystos.mcp import grants as G

    tool_id, _ = _publish(world.users["usr_ow"])
    with session_scope() as s:
        client, _ = G.create_client(s, s.merge(world.users["usr_adm"]), "bi bot")
        cid = client.id
        G.set_grant(s, s.merge(world.users["usr_adm"]), cid, WS, role="viewer", tools=["get_finding_evidence"],
                    quotas={"get_finding_evidence": 5})
    with pytest.raises(Forbidden, match="created an MCP client"):
        _request(world.users["usr_vw"], tool_id, mcp_client_id=cid)
    aid = _request(world.users["usr_adm"], tool_id, mcp_client_id=cid)
    with session_scope() as s:
        out = access.decide(s, s.merge(world.users["usr_ap"]), aid, approve=True)
    assert out["mcp_grant"]["tools"] == ["get_finding_evidence", tool_id] and out["mcp_grant"]["role"] == "analyst"
    with session_scope() as s:
        g = s.scalar(select(McpGrant).where(McpGrant.client_id == cid))
        assert g.quotas == {"get_finding_evidence": 5} and tool_id in g.tools  # nothing else of the grant changed
        assert s.get(McpClient, cid).status == "active"
        listed = access.requests_for(s, WS, tool_id)
        assert listed[0]["approval_id"] == aid and listed[0]["status"] == "executed"
