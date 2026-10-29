"""S06-T6 (spec §3.7, FR-058): the playbook editor, the simulator and playbook signing.

* Editor: YAML is parsed safely and validated against the playbook contract and the capability registry,
  listing every problem; drafts stay versioned definitions (one draft per key, If-Match revisions).
* Simulator: a dry run against fixture workspaces shows the steps, budget and approval gates a playbook would
  hit, with the engine's skip, enablement, budget and replan rules, and never executes (golden test below).
* Signing: a draft publishes (= is enabled) only after a passing simulation and an approved, hash-bound
  sign-off by someone else; publish signs it with the tenant key; a change voids the sign-off; the runtime
  refuses an unsigned, changed or forged version.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from sqlalchemy import func, select
from tests.unit.playbook_signoff import SIGNER, publish_signed, sign_off, signer

from analystos.autonomy import playbook_editor as editor
from analystos.autonomy import playbook_signing as signing
from analystos.autonomy import simulator as sim
from analystos.capabilities import registry as reg
from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
from analystos.core.errors import ApprovalRequired, Conflict, Forbidden, InvalidInput
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Definition, Schedule, User, Workspace, WorkspaceMember
from analystos.governance import approvals
from analystos.services import definitions as defs
from analystos.services import pins
from analystos.services import runs as runs_svc
from analystos.tenancy import DEFAULT_TENANT_ID
from analystos.toolreg import signing as keys

WS = "ws_pb"
KEY = "playbook.weekly_review"
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "playbooks" / "simulation_golden.yaml"


@pytest.fixture
def world(sqlite_db):
    with session_scope() as s:
        for uid in ("usr_author", "usr_owner", "usr_view"):
            s.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", is_admin=False, active=True, attributes={},
                       tenant_id=DEFAULT_TENANT_ID))
        s.add(Workspace(id=WS, name="pb", description="", objective="Review the weekly SLA", autonomy_level=2, status="active",
                        settings={}, policy_version=1, created_by="usr_owner", tenant_id=DEFAULT_TENANT_ID))
        s.flush()
        for uid, role in (("usr_author", "editor"), ("usr_owner", "owner"), ("usr_view", "viewer")):
            s.add(WorkspaceMember(workspace_id=WS, user_id=uid, role=role))
        signer(s, WS)
    return {u: _user(u) for u in ("usr_author", "usr_owner", "usr_view", SIGNER)}


def _user(uid: str) -> User:
    with session_scope() as s:
        u = s.get(User, uid)
        s.expunge(u)
    return u


def _spec(key: str = KEY, **over) -> dict:
    base = reg.current().get("playbook.investigate").model_dump(mode="json", exclude={"source"})
    base.update({"id": key, "version": "1.0.0", "certification": {"status": "draft"}, **over})
    return base


def _yaml(spec: dict | None = None) -> str:
    return editor.to_yaml(spec or _spec())


def _draft(s, user: User, spec: dict | None = None) -> Definition:
    return defs.create_draft(s, s.merge(user), WS, DefinitionDraftIn(kind="playbook", key=(spec or _spec())["id"],
                                                                     spec=spec or _spec()))


# ------------------------------------------------------------------------------------ editor
def test_yaml_is_parsed_safely_and_every_problem_is_listed(world):
    with pytest.raises(InvalidInput, match="line 2"):
        editor.parse_yaml("id: playbook.x\n  bad: [indent\n")
    with pytest.raises(InvalidInput, match="exactly one mapping"):
        editor.parse_yaml("- a\n- b\n")
    with pytest.raises(InvalidInput):  # no python tags: a playbook is data
        editor.parse_yaml("!!python/object/apply:os.system ['echo hi']")
    bad = _spec()
    bad["spec"]["steps"][1]["use"] = "agent.nope"
    bad["spec"]["steps"][2]["behaviour"] = "no_such_behaviour"
    with session_scope() as s:
        out = editor.check(s, WS, _yaml(bad))
        assert out["ok"] is False and len(out["problems"]) >= 2
        assert any("agent.nope, which is not a registered agent" in p for p in out["problems"])
        assert any("no behaviour no_such_behaviour" in p for p in out["problems"])
        shape = _spec()
        shape["spec"]["steps"][3]["after"] = ["later_step"]
        assert "unknown step later_step" in " ".join(editor.check(s, WS, _yaml(shape))["problems"])
        good = editor.check(s, WS, _yaml())
    assert good["ok"] and good["key"] == KEY and good["approval_gates"] == ["plan_approval", "publish_request", "publish"]
    assert good["diff"] == {"against": None, "changes": []}  # a new key: nothing published or installed to compare
    assert editor.parse_yaml(editor.to_yaml(good["manifest"])) == good["manifest"]  # round trip


def test_drafts_are_versioned_and_diffed_against_the_installed_playbook(world):
    with session_scope() as s:
        out = editor.check(s, WS, _yaml(_spec("playbook.investigate", version="1.2.0")))
    # the key is a built-in: a workspace cannot redefine it, but the editor shows what would change
    assert out["ok"] is True and out["diff"]["against"] == "installed"
    assert {c["path"] for c in out["diff"]["changes"]} == {"version", "certification.status", "certification.evidence"}
    with session_scope() as s:
        row = _draft(s, world["usr_author"])
        with pytest.raises(Conflict, match="already has a draft"):
            _draft(s, world["usr_author"])
        with pytest.raises(Forbidden):  # a viewer cannot author
            defs.update_draft(s, s.merge(world["usr_view"]), row, DefinitionPatch(spec=_spec()), row.revision)
        assert (row.version, row.status, row.revision) == (1, "draft", 1)


def test_editor_options_list_agents_checks_fixtures_and_a_valid_template(world):
    with session_scope() as s:
        opts = editor.options(s, WS)
        assert {"supervised", "autonomous", "scheduled", "rejected", "tight_budget", "redirect"} <= \
            {f["name"] for f in opts["fixtures"]}
        sup = next(a for a in opts["agents"] if a["id"] == "agent.supervisor")
        assert "plan_approved" in sup["behaviours"] and sup["enabled"]
        assert "statistics" in opts["checks"] and "user_redirect" in opts["replan_reasons"]
        assert "steps" in opts["schema"]["playbook"]["properties"]
        assert editor.check(s, WS, opts["template"])["ok"]


# ------------------------------------------------------------------------------------ simulator
def _golden_view(report: dict) -> dict:
    return {"outcome": report["outcome"], "reason": report["reason"],
            "timeline": [f"{e['key']}:{e['status']}" for e in report["timeline"]],
            "used": {k: report["budget"]["used"][k] for k in ("steps", "retries", "replans")},
            "approvals": [f"{a['gate']}:{a['status']}" for a in report["approvals"]]}


def test_simulation_golden_investigate_v1():
    """The playbook simulation golden test (spec §5): investigate.v1 against every built-in fixture."""
    m = reg.current().get("playbook.investigate")
    got = {name: _golden_view(sim.simulate(m, name)) for name in sim.fixtures()}
    expected = yaml.safe_load(GOLDEN.read_text(encoding="utf-8"))
    assert got == expected


def test_simulation_never_executes(world, monkeypatch):
    from analystos.agents import dispatch
    from analystos.gateway import service as gateway
    from analystos.llm import router

    def boom(*_a, **_k):
        raise AssertionError("a simulation must not execute anything")

    monkeypatch.setattr(gateway.QueryGateway, "execute", boom)
    monkeypatch.setattr(router.ModelRouter, "__init__", boom)  # no router is even built
    monkeypatch.setattr(dispatch, "dispatch", boom)
    with session_scope() as s:
        reports = sim.simulate_all(_spec(), session=s, workspace_id=WS, workspace_source=True)
        assert {r["outcome"] for r in reports} <= set(sim.PASSING)
        side = [e for r in reports for e in r["timeline"] if e["status"] == "side_effect"]
        assert side and all("not performed" in e["detail"] for e in side)
        s.flush()
        for model in (Approval, AnalysisRun, Definition):
            assert s.scalar(select(func.count()).select_from(model)) == 0  # nothing requested, started or saved


def test_simulation_applies_workspace_enablement_and_certification(world):
    kpi = reg.current().get("playbook.kpi_review")  # a tested (not certified) template, off until enabled
    off = sim.simulate(kpi, "supervised")
    assert off["outcome"] == "refused" and "disabled for this workspace" in off["reason"]
    on = sim.simulate(kpi, {"base": "supervised", "enable": ["playbook.kpi_review"]})
    assert on["outcome"] == "completed"
    auto = sim.simulate(kpi, {"base": "autonomous", "enable": ["playbook.kpi_review"]})
    assert auto["outcome"] == "refused" and "not certified" in auto["reason"]
    optional = sim.simulate(_spec(), {"disable": ["agent.data_quality"]}, workspace_source=True)
    quality = next(e for e in optional["timeline"] if e["key"] == "quality")
    assert optional["outcome"] == "completed" and quality["status"] == "skipped" and "disabled" in quality["detail"]
    required = sim.simulate(_spec(), {"disable": ["agent.profiler"]}, workspace_source=True)
    assert required["outcome"] == "refused" and "step profile" in required["reason"]


def test_simulation_budget_gates_and_replans(world):
    tight = sim.simulate(_spec(), "tight_budget", workspace_source=True)
    assert tight["outcome"] == "completed_with_limitations" and tight["budget"]["stopped_by"] == "max_steps"
    assert tight["budget"]["remaining"]["max_steps"] == 0
    assert any(e["status"] == "not_reached" for e in tight["timeline"])
    no_publish = sim.simulate(_spec(), {"approvals": {"publish_request": "reject"}}, workspace_source=True)
    publish = next(e for e in no_publish["timeline"] if e["key"] == "publish")
    assert no_publish["outcome"] == "completed" and publish["status"] == "skipped"
    late = sim.simulate(_spec(), {"replans": [{"reason_code": "user_redirect", "after": "visualize"}]}, workspace_source=True)
    plan_apr, *rest = late["approvals"]
    assert plan_apr["gate"] == "plan_approval" and plan_apr["status"] == "invalidated"  # bound to the old plan hash
    assert [a["status"] for a in rest] == ["executed"]  # the publication gate asks again for plan v2
    assert late["replans"][0]["approvals_invalidated"] == [plan_apr["id"]] and late["plan_version"] == 2
    assert late["budget"]["used"]["retries"] > 0  # a reset step runs again as a retry, as the run budget counts it
    refused = sim.simulate(_spec(), {"limits": {"max_replans": 0}, "replans": [{"reason_code": "user_redirect"}]},
                           workspace_source=True)
    assert refused["replans"][0]["status"] == "refused" and refused["plan_version"] == 1
    failing = sim.simulate(_spec(), {"fail": ["profile"]}, workspace_source=True)
    assert failing["outcome"] == "failed" and "profile" in failing["reason"]
    tolerated = sim.simulate(_spec(), {"fail": ["test:*"]}, workspace_source=True)
    assert tolerated["outcome"] == "completed"  # expanded tests failing do not block insights (pattern dependency)
    with pytest.raises(InvalidInput, match="reason_code"):
        sim.fixture({"replans": [{"reason_code": "because"}]})


def test_simulation_warns_about_an_ungated_side_effect(world):
    spec = _spec()
    spec["spec"]["steps"] = [s for s in spec["spec"]["steps"] if s["key"] != "publish_request"]
    for s in spec["spec"]["steps"]:
        s["after"] = [d for d in s.get("after", []) if d != "publish_request"]
    report = sim.simulate(spec, "supervised", workspace_source=True)
    assert any("publish has no approval_gate" in w for w in report["warnings"])
    assert next(e for e in report["timeline"] if e["key"] == "publish")["status"] == "skipped"


# ------------------------------------------------------------------------------------ signing
def test_a_playbook_publishes_only_simulated_and_signed_off_by_someone_else(world):
    author = world["usr_owner"]  # an owner may approve in general, but never their own playbook
    with session_scope() as s:
        row = _draft(s, author)
        with pytest.raises(Conflict, match="must pass its test"):
            defs.publish(s, s.merge(author), row, row.revision)
        with pytest.raises(Conflict, match="must pass its simulation"):
            signing.request_signature(s, s.merge(author), row, row.revision)
        defs.test(s, s.merge(author), row, row.revision, arguments={"fixtures": ["supervised", "autonomous"]})
        assert row.status == "tested" and [x["fixture"] for x in row.test_evidence["simulations"]] == ["supervised", "autonomous"]
        with pytest.raises(Conflict, match="unsigned: request a signature"):
            defs.publish(s, s.merge(author), row, row.revision)
        approval = signing.request_signature(s, s.merge(author), row, row.revision)
        assert approval.action == signing.ACTION and approval.payload["content_hash"] == row.content_hash
        assert signing.request_signature(s, s.merge(author), row, row.revision).id == approval.id  # asking again: same
        with pytest.raises(ApprovalRequired, match="pending"):
            defs.publish(s, s.merge(author), row, row.revision)
        with pytest.raises(Forbidden, match="separation of duties"):
            approvals.decide(s, approval.id, s.merge(author), approve=True)
        with pytest.raises(Forbidden, match="cannot approve"):
            approvals.decide(s, approval.id, s.merge(world["usr_view"]), approve=True)
        approvals.decide(s, approval.id, s.merge(world[SIGNER]), approve=True)
        signing.apply_decision(s, approval)
        assert defs.out(row)["signature"]["state"] == "approved"
        defs.publish(s, s.merge(author), row, row.revision)
        assert row.status == "published" and row.signature["state"] == "signed"
        assert row.signature["payload"]["approval_id"] == approval.id and row.signature["key_id"] == "tenant-signing-1"
        assert s.get(Approval, approval.id).status == "executed"  # single use
        signing.verify_definition(s, row)
        caps = runs_svc._capabilities(s, WS, playbook=None, definition={"id": row.id}, pins=None, trigger="api")
        assert caps["playbook"] == KEY and caps["definition"]["status"] == "published"


def test_a_changed_playbook_voids_its_sign_off_and_needs_re_approval(world):
    author = world["usr_author"]
    with session_scope() as s:
        row = _draft(s, author)
        sign_off(s, s.merge(author), row)
        old = row.signature["approval_id"]
        changed = _spec(summary="Investigate the weekly SLA breaches end to end")
        defs.update_draft(s, s.merge(author), row, DefinitionPatch(spec=changed), row.revision)
        assert row.status == "draft" and s.get(Approval, old).status == "invalidated"
        assert defs.out(row)["signature"]["state"] in ("invalidated", "stale")
        defs.test(s, s.merge(author), row, row.revision)
        with pytest.raises(ApprovalRequired):  # the old sign-off covered other content
            defs.publish(s, s.merge(author), row, row.revision)
        approval = signing.request_signature(s, s.merge(author), row, row.revision)
        assert approval.id != old
        # a change while the new sign-off is pending: approving it is refused, the draft must be signed again
        approval.payload = {**approval.payload, "content_hash": "0" * 64}
        approval.payload_hash = approvals.stable_hash(approval.payload)
        approvals.decide(s, approval.id, s.merge(world[SIGNER]), approve=True)
        with pytest.raises(Conflict, match="changed while its signature was pending"):
            signing.apply_decision(s, approval)


def test_runtime_refuses_unsigned_changed_or_forged_playbooks(world):
    author = world["usr_author"]
    with session_scope() as s:
        row = publish_signed(s, s.merge(author), _draft(s, author))
        ref = defs.ref_of(row)
        sch = Schedule(id="sch_pb", workspace_id=WS, name="weekly", kind="reanalysis", cron="0 7 * * 1", timezone="UTC",
                       config={}, enabled=True, owner_id="usr_author", revision=1,
                       pins={"revision": 1, "playbook": row.key, "definition": ref.model_dump(), "manifests": {},
                             "methods": [], "semantic": {"model": None, "metrics": {}}, "analyses": []}, pin_status={})
        s.add(sch)
        s.flush()
        assert pins.refresh(s, sch).state == "current"
        good_spec, good_sig = copy.deepcopy(row.spec), copy.deepcopy(row.signature)

        row.spec = {**row.spec, "summary": "rewritten in the database"}  # tampered content
        with pytest.raises(keys.ManifestRefused, match="changed after it was signed"):
            defs.require_runnable(s, WS, ref, trigger="api")
        status = pins.refresh(s, sch)
        assert status.state == "blocked" and any(i.state == "rejected" for i in status.items)

        row.spec = good_spec
        row.signature = {**good_sig, "value": good_sig["value"][::-1]}  # forged signature
        with pytest.raises(keys.ManifestRefused, match="invalid signature"):
            defs.require_runnable(s, WS, ref, trigger="api")
        row.signature = {**good_sig, "payload": {**good_sig["payload"], "tenant": "tnt_other"}}
        with pytest.raises(keys.ManifestRefused, match="made for something else"):
            defs.require_runnable(s, WS, ref, trigger="api")
        row.signature = None
        with pytest.raises(keys.ManifestRefused, match="unsigned"):
            defs.require_runnable(s, WS, ref, trigger="api")

        row.signature = good_sig
        defs.require_runnable(s, WS, ref, trigger="api")
        keys.rotate(s, DEFAULT_TENANT_ID, actor="test", revoke_previous=True)  # a revoked key signs nothing any more
        with pytest.raises(keys.ManifestRefused, match="unknown or revoked key"):
            defs.require_runnable(s, WS, ref, trigger="api")


def test_a_failing_simulation_keeps_the_draft_a_draft(world):
    with session_scope() as s:
        s.add(Workspace(id="ws_other", name="o", description="", objective="x", autonomy_level=2, status="active",
                        settings={}, policy_version=1, created_by="usr_owner", tenant_id=DEFAULT_TENANT_ID))
        row = _draft(s, world["usr_author"])
        from analystos.db.models import WorkspaceCapability

        s.add(WorkspaceCapability(workspace_id=WS, capability_id="agent.profiler", enabled=False, updated_by="usr_owner"))
        s.flush()
        with pytest.raises(InvalidInput, match="simulation did not pass: supervised: refused") as exc:
            defs.test(s, s.merge(world["usr_author"]), row, row.revision)
        assert exc.value.details["reports"][0]["outcome"] == "refused"
        assert row.status == "draft" and row.test_evidence is None
        with pytest.raises(InvalidInput, match="published through its simulation"):
            defs.publish_frozen(s, WS, "playbook", KEY, _spec(), created_by="usr_author", published_by="usr_author")


def test_promotion_carries_the_signature_and_verifies_it_in_the_target(world):
    with session_scope() as s:
        for ws, env in (("ws_pb_dev", "dev"), ("ws_pb_test", "test")):
            s.add(Workspace(id=ws, name=ws, description="", objective="x", autonomy_level=2, status="active",
                            settings={"environment": env}, policy_version=1, created_by="usr_owner",
                            tenant_id=DEFAULT_TENANT_ID))
            s.add(WorkspaceMember(workspace_id=ws, user_id="usr_author", role="editor"))
        s.flush()
        signer(s, "ws_pb_dev")
        author = s.merge(world["usr_author"])
        row = defs.create_draft(s, author, "ws_pb_dev", DefinitionDraftIn(kind="playbook", key=KEY, spec=_spec()))
        publish_signed(s, author, row)
        new = defs.promote(s, author, row, "ws_pb_test")
        assert new.signature == row.signature and new.content_hash == row.content_hash
        signing.verify_definition(s, new)
        unsigned = Definition(id="defn_unsigned", workspace_id="ws_pb_dev", kind="playbook", key="playbook.other", version=1,
                              status="published", spec=_spec("playbook.other"),
                              content_hash=defs.content_hash("playbook", _spec("playbook.other")), revision=1,
                              created_by="usr_author")
        s.add(unsigned)
        s.flush()
        with pytest.raises(keys.ManifestRefused, match="unsigned"):
            defs.promote(s, author, unsigned, "ws_pb_test")


# ------------------------------------------------------------------------------------ API
@pytest.fixture
def client(world, sqlite_db, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api import deps
    from analystos.api.app import app
    from analystos.db import base, models
    from analystos.tenancy import resolve

    engine = base.SessionLocal().get_bind()
    for table in ("tenant_member", "tenant"):
        if table in models.Base.metadata.tables:
            models.Base.metadata.tables[table].create(engine, checkfirst=True)
    monkeypatch.setattr(deps, "SessionLocal", base.SessionLocal)
    resolve.invalidate()
    return TestClient(app)


def _hdr(uid: str, **extra) -> dict:
    from analystos.security.auth import issue_token

    return {"Authorization": f"Bearer {issue_token(uid, f'{uid}@x')}", **extra}


def test_editor_api_drafts_simulates_signs_and_publishes(client, world):
    base = f"/api/workspaces/{WS}/playbook-editor"
    assert client.get(base, headers=_hdr("usr_view")).status_code == 200
    bad = client.post(f"{base}/validate", json={"yaml": "id: [x"}, headers=_hdr("usr_view")).json()
    assert bad["ok"] is False and "line" in bad["problems"][0]
    assert client.post(f"{base}/drafts", json={"yaml": _yaml()}, headers=_hdr("usr_view")).status_code == 403
    r = client.post(f"{base}/drafts", json={"yaml": _yaml(), "title": "Weekly review"}, headers=_hdr("usr_author"))
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["key"] == KEY and d["signature"] == {"state": "unsigned"} and "kind: Playbook" in d["yaml"]
    did, etag = d["id"], r.headers["ETag"]
    dry = client.post(f"{base}/simulate", json={"definition_id": did, "fixture": "tight_budget"}, headers=_hdr("usr_author"))
    assert dry.status_code == 200 and dry.json()["outcome"] == "completed_with_limitations"
    assert client.post(f"{base}/simulate", json={"yaml": _yaml(), "fixture": {"base": "rejected"}},
                       headers=_hdr("usr_author")).json()["outcome"] == "rejected"
    assert client.post(f"{base}/drafts/{did}/simulate", json={}, headers=_hdr("usr_author")).status_code == 428
    t = client.post(f"{base}/drafts/{did}/simulate", json={"fixtures": ["supervised"]},
                    headers=_hdr("usr_author", **{"If-Match": etag}))
    assert t.status_code == 200 and t.json()["status"] == "tested"
    sig = client.post(f"{base}/drafts/{did}/request-signature", headers=_hdr("usr_author", **{"If-Match": t.headers["ETag"]}))
    assert sig.status_code == 201, sig.text
    apr, etag = sig.json()["approval"]["id"], sig.headers["ETag"]
    assert client.post(f"/api/approvals/{apr}/approve", headers=_hdr("usr_author")).status_code == 403
    assert client.post(f"/api/approvals/{apr}/approve", headers=_hdr(SIGNER)).status_code == 200
    got = client.get(f"{base}/drafts/{did}", headers=_hdr("usr_view")).json()
    assert got["signature"]["state"] == "approved"
    pub = client.post(f"/api/workspaces/{WS}/definitions/{did}/publish", headers=_hdr("usr_author", **{"If-Match": etag}))
    assert pub.status_code == 200, pub.text
    assert pub.json()["status"] == "published" and pub.json()["signature"]["state"] == "signed"
    nxt = client.post(f"{base}/drafts", json={"yaml": _yaml(_spec(version="1.1.0"))}, headers=_hdr("usr_author")).json()
    assert nxt["version"] == 2
    view = client.get(f"{base}/drafts/{nxt['id']}", headers=_hdr("usr_view")).json()
    assert view["diff"]["against"] == "v1" and view["diff"]["changes"] == [{"path": "version", "from": "1.0.0", "to": "1.1.0"}]
    renamed = client.put(f"{base}/drafts/{nxt['id']}", json={"yaml": _yaml(_spec("playbook.renamed"))},
                         headers=_hdr("usr_author", **{"If-Match": f'"{nxt["revision"]}"'}))
    assert renamed.status_code == 422 and "must stay" in renamed.json()["error"]["message"]
