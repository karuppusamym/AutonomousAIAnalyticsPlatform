"""S07-T7: a void (metric version, data snapshot, policy, context ...) reaches the published artifacts that present
the finding: a banner in the same transaction and one Desk task per artifact; re-verification lifts the banner.
SQLite control plane; the Desk task itself (Postgres tables) is stubbed here and covered by
tests/integration/test_s07_evidence.py."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from tests.unit.test_verification_p701 import RUN, WS, _insight
from tests.unit.test_verification_p701 import db as p701_db  # noqa: F401 (a fixture)

from analystos.evidence import banners
from analystos.evidence import verification as V


@pytest.fixture
def db(p701_db, monkeypatch):  # noqa: F811
    calls: list[dict] = []

    def desk_task(session, art, findings, reason, request_id, added):
        calls.append({"artifact": art.id, "reason": reason, "request_id": request_id, "added": [f["code"] for f in added]})
        return request_id or f"rq_{art.id}"

    monkeypatch.setattr(banners, "_desk_task", desk_task)
    p701_db.desk_calls = calls
    return p701_db


def _publish(s, ins, *, published: bool = True):
    """A chart presenting the finding inside a dashboard, both published; plus a draft chart of the same finding."""
    from analystos.artifacts.registry import link
    from analystos.db.models import Artifact

    status = "published" if published else "draft"
    s.add(Artifact(id="art_chart", workspace_id=WS, run_id=RUN, type="chart", name="sla_by_priority", status=status,
                   content={"insight_codes": [ins.code]}, content_hash="c"))
    s.add(Artifact(id="art_dash", workspace_id=WS, run_id=RUN, type="dashboard", name="exec", status=status,
                   content={"charts": ["sla_by_priority"]}, content_hash="d"))
    s.add(Artifact(id="art_draft", workspace_id=WS, run_id=RUN, type="chart", name="draft_chart", status="draft",
                   content={"insight_codes": [ins.code]}, content_hash="e"))
    s.flush()
    link(s, WS, ("insight", ins.id), "visualized_by", ("chart", "art_chart"), run_id=RUN)
    link(s, WS, ("insight", ins.id), "visualized_by", ("chart", "art_draft"), run_id=RUN)
    link(s, WS, ("chart", "art_chart"), "included_in", ("dashboard", "art_dash"), run_id=RUN)
    s.flush()


def _events(s, type_):
    from analystos.db.models import RunEvent

    return [e.payload for e in s.scalars(select(RunEvent).where(RunEvent.type == type_).order_by(RunEvent.id))]


def test_void_on_metric_version_change(db, monkeypatch):
    """The real approval path voids the verdict; the published chart and its dashboard show the banner."""
    from analystos.core.ids import utcnow
    from analystos.db.models import Approval, Artifact, SemanticMetric
    from analystos.governance import approvals
    from analystos.knowledge import learning
    from analystos.semantic import service as semantic

    with db() as s:
        ins, rec = _insight(s)
        _publish(s, ins)
        s.add(SemanticMetric(id="smet_2", workspace_id=WS, name="missed_sla_rate", version=2, status="proposed",
                             definition={"name": "missed_sla_rate", "expressions": [{"dialect": "ANSI_SQL", "expression": "AVG(y)"}]},
                             expression="AVG(y)", normalized_expression="avg(y)", proposed_by="u_owner", proposed_via="user",
                             content_hash="m2", approval_id="apr_2"))
        s.add(Approval(id="apr_2", workspace_id=WS, action=semantic.APPROVAL_ACTION, payload={}, payload_hash="p",
                       policy_version=0, requested_by="u_owner", status="approved", decided_by="u_approver",
                       risk_tier="medium", expires_at=utcnow() + timedelta(hours=1)))
        s.flush()
        monkeypatch.setattr(approvals, "verify_for_execution", lambda *a, **k: None)
        monkeypatch.setattr(learning, "draft_from_metric", lambda *a, **k: None)
        semantic.apply_decision(s, s.get(Approval, "apr_2"))
        assert V.state_of(V.latest(s, "insight", [ins.id])[ins.id])["state"] == "VOID"
        chart, dash, draft = (s.get(Artifact, i) for i in ("art_chart", "art_dash", "art_draft"))
        for art in (chart, dash):
            assert art.banner["state"] == "void" and art.banner["request_id"] == f"rq_{art.id}"
            assert art.banner["reason"] == "I-1 is void: metric missed_sla_rate v2 approved"
            [f] = art.banner["findings"]
            assert (f["insight_id"], f["record_id"], f["kind"]) == (ins.id, rec.id, "semantic")
            assert banners.banner_of(art)["message"].startswith("Needs re-verification: I-1 is void")
        assert draft.banner is None  # only published artifacts carry a banner
        assert {c["artifact"] for c in db.desk_calls} == {"art_chart", "art_dash"}
        assert {e["artifact_id"] for e in _events(s, "publication.void_banner")} == {"art_chart", "art_dash"}


def test_a_second_void_joins_the_open_task_and_reverification_lifts_the_banner(db):
    from tests.unit.test_verification_p701 import SPEC

    from analystos.db.models import Artifact
    from analystos.registries.hypotheses import spec_hash

    with db() as s:
        ins, _ = _insight(s)
        _publish(s, ins)
        V.void_dependents(s, "semantic", f"{WS}/missed_sla_rate", "v2", "metric missed_sla_rate v2 approved")
        first = dict(s.get(Artifact, "art_chart").banner)
        # a second, different finding on the same chart voids later: same Desk task, both listed
        ins2, _ = _insight(s, n=2)
        from analystos.artifacts.registry import link

        link(s, WS, ("insight", ins2.id), "visualized_by", ("chart", "art_chart"), run_id=RUN)
        s.flush()
        V.void_dependents(s, "query", ins2.hypothesis_id, "changed", "step H-2 edited")
        chart = s.get(Artifact, "art_chart")
        assert chart.banner["request_id"] == first["request_id"]
        assert [f["code"] for f in chart.banner["findings"]] == ["I-1", "I-2"]
        last = next(c for c in reversed(db.desk_calls) if c["artifact"] == "art_chart")
        assert last == {"artifact": "art_chart", "reason": chart.banner["reason"], "request_id": first["request_id"],
                        "added": ["I-2"]}
        assert [f["code"] for f in s.get(Artifact, "art_dash").banner["findings"]] == ["I-1", "I-2"]  # holds the chart
        # the same void again changes nothing (no second event, no second task)
        n_events = len(_events(s, "publication.void_banner"))
        banners.raise_for_voids(s, [first["findings"][0]["record_id"]])
        assert len(_events(s, "publication.void_banner")) == n_events
        # re-verify I-1: still banner (I-2 is void); re-verify I-2: cleared
        for i in (ins, ins2):
            deps = V.insight_dependencies(s, workspace_id=WS, run_id=RUN, hypothesis_id=i.hypothesis_id, spec=SPEC,
                                          entry=None, narrative_source="template")
            V.record_verdict(s, workspace_id=WS, run_id=RUN, subject_type="insight", subject_id=i.id, verdict="verified",
                             checks=[], verifier="rev.v2", dependencies=deps, question_hash=spec_hash(SPEC))
            if i is ins:
                assert [f["code"] for f in s.get(Artifact, "art_chart").banner["findings"]] == ["I-2"]
        assert s.get(Artifact, "art_chart").banner is None and s.get(Artifact, "art_dash").banner is None
        assert {e["artifact_id"] for e in _events(s, "publication.banner_cleared")} == {"art_chart", "art_dash"}


def test_the_nightly_sweep_raises_the_banner_for_a_missed_event(db):
    from analystos.db.models import Artifact, SourceAsset

    with db() as s:
        ins, _ = _insight(s)
        _publish(s, ins)
        asset = s.get(SourceAsset, "asset_p701")
        asset.snapshot = {"content_fingerprint": "c3", "rows_staged": 1, "load_id": "l3", "sampling_method": "full"}  # no event
        s.flush()
        out = V.sweep(s, actor="test")
        assert out["late_voids"] == 1
        banner = s.get(Artifact, "art_dash").banner
        assert banner["findings"][0]["kind"] == "data" and "nightly sweep" in banner["reason"]


def test_a_banner_failure_never_undoes_the_void(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("desk down")

    monkeypatch.setattr(banners, "raise_for_voids", boom)
    with db() as s:
        ins, rec = _insight(s)
        _publish(s, ins)
        assert V.void_dependents(s, "semantic", f"{WS}/missed_sla_rate", "v2", "metric v2") == [rec.id]
        assert V.state_of(V.latest(s, "insight", [ins.id])[ins.id])["state"] == "VOID"


def test_unpublished_artifacts_get_no_banner(db):
    from analystos.db.models import Artifact

    with db() as s:
        ins, _ = _insight(s)
        _publish(s, ins, published=False)
        V.void_dependents(s, "semantic", f"{WS}/missed_sla_rate", "v2", "metric v2")
        assert all(s.get(Artifact, a).banner is None for a in ("art_chart", "art_dash", "art_draft"))
        assert db.desk_calls == []
