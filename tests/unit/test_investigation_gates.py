"""S07-T5 (v1 §7.2): the investigation workflow checks as playbook gates. Each check is deterministic over
what the run persisted; `warn` records issues and limitations, `block` fails the step. SQLite, no services."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from tests.unit.test_verification_p701 import RUN, WS, _insight
from tests.unit.test_verification_p701 import db as p701_db  # noqa: F401 (a fixture)

from analystos.capabilities import registry
from analystos.capabilities.playbook import Step, parse
from analystos.evidence import investigation as inv

NOW = datetime(2026, 10, 27, tzinfo=UTC)


@pytest.fixture
def db(p701_db):  # noqa: F811
    from analystos.db import models

    models.Base.metadata.create_all(p701_db.kw["bind"], tables=[models.Base.metadata.tables["evidence_citation_set"]])
    return p701_db


def _run(s):
    from analystos.db.models import AnalysisRun

    return s.get(AnalysisRun, RUN)


def _profile(s, name: str, content: dict) -> None:
    from analystos.db.models import Artifact

    s.add(Artifact(id=f"art_{name}", workspace_id=WS, run_id=RUN, type="profile", name=f"Profile {name}", content=content,
                   content_hash=name))
    s.flush()


def test_investigate_declares_the_gates_in_the_analysts_order():
    pb = parse(registry.load(packs_dir=None, entry_points=False).get("playbook.investigate"))
    assert pb.manifest.version == "1.1.0"
    order = [(s.key, g.name, g.on_fail) for s in pb.body.steps for g in s.checks]
    assert order == [("profile", "baseline", "warn"), ("hypotheses", "hypotheses", "warn"),
                     ("insights", "segmentation", "warn"), ("insights", "statistics", "block"),
                     ("verify", "alternatives", "warn"), ("verify", "conclusions", "block")]
    assert [g for g, _ in sorted({(g.name, 0) for s in pb.body.steps for g in s.checks},
                                 key=lambda x: inv.CHECK_NAMES.index(x[0]))] == list(inv.CHECK_NAMES)
    plan = pb.build_plan("x", autonomy_level=3)
    assert all("checks" not in step for step in plan["steps"])  # gates are not in the plan hash


def test_an_unknown_gate_is_refused():
    with pytest.raises(ValidationError, match="unknown investigation check 'vibes'"):
        Step(key="x", title="x", use="agent.x", checks=["vibes"])
    assert Step(key="x", title="x", use="agent.x", checks=[{"name": "baseline", "on_fail": "block"}]).checks[0].on_fail == "block"


def test_baseline_reads_window_time_zone_denominator_freshness_and_completeness(db):
    with db() as s:
        _insight(s)
        _profile(s, "shop.orders", {"row_count": 20000, "columns": [
            {"name": "opened_at", "data_type": "timestamp", "semantic_type": "datetime", "type_family": "datetime",
             "min": "2025-01-01T00:00:00", "max": "2026-08-01T00:00:00", "null_rate": 0.0,
             "monthly_counts": [{"month": "2026-06", "count": 900}, {"month": "2026-07", "count": 1000},
                                {"month": "2026-08", "count": 100}]},
            {"name": "region", "data_type": "text", "semantic_type": "categorical", "null_rate": 0.35}]})
        _profile(s, "shop.empty", {"row_count": 0, "columns": []})
        r = inv.baseline(s, _run(s), now=NOW)
        assert r.checked == 2 and r.issues == ["shop.empty: no rows to form a denominator"]
        text = " | ".join(r.limitations)
        assert "window 2025-01-01 to 2026-08-01" in text and "no time zone; read as UTC" in text
        assert "latest value is 87 days old" in text and "latest month has under half" in text
        assert "incomplete columns: region (35% empty)" in text


def test_hypotheses_need_a_testable_spec_and_a_mechanism(db):
    from analystos.db.models import Hypothesis

    with db() as s:
        _insight(s)
        s.add(Hypothesis(id="hyp_bad", workspace_id=WS, run_id=RUN, code="H-9", statement="Something about vibes",
                         spec={"method": "astrology", "asset": "shop.orders"}, status="proposed"))
        s.flush()
        r = inv.hypotheses(s, _run(s))
        assert any(i.startswith("H-9: no executable test") for i in r.issues)
        assert "H-1: no mechanism stated (why the claim might hold)" in r.limitations


def test_statistics_needs_multiple_testing_correction_and_blocks(db):
    from analystos.db.models import Experiment, RunTask

    with db.begin() as s:
        _insight(s)
        exp = s.get(Experiment, "exp_1")
        exp.result = {**exp.result, "p_adjusted": None}
        s.flush()
        r = inv.statistics(s, _run(s))
        assert r.issues == ["H-1: p-value without multiple-testing correction"]
        s.add(RunTask(id="rt_insights", run_id=RUN, key="insights", agent_id="insight", title="t",
                      status="RUNNING", depends_on=[]))
    gates = Step(key="insights", title="t", use="agent.insight", checks=["segmentation", {"name": "statistics", "on_fail": "block"}]).checks
    with pytest.raises(inv.InvestigationGateFailed, match="statistics: H-1: p-value without multiple-testing correction"):
        inv.run_gates(RUN, "insights", gates)
    with db.begin() as s:
        exp = s.get(Experiment, "exp_1")
        exp.result = {**exp.result, "p_adjusted": 1e-12, "bh_family_size": 1}
    results = inv.run_gates(RUN, "insights", gates)
    assert [(r["check"], r["passed"]) for r in results] == [("segmentation", True), ("statistics", True)]
    with db.begin() as s:
        from sqlalchemy import select

        from analystos.db.models import RunEvent

        types = [e.type for e in s.scalars(select(RunEvent).where(RunEvent.run_id == RUN))]
        assert types.count("investigation.check") == 4


def test_segmentation_flags_immaterial_groups_and_undisclosed_mix(db):
    from analystos.db.models import Experiment

    with db() as s:
        _insight(s)
        exp = s.get(Experiment, "exp_1")
        exp.result = {**exp.result, "groups": [{"segment": "P1", "n": 2000}, {"segment": "P9", "n": 4}]}
        s.flush()
        r = inv.segmentation(s, _run(s))
        assert r.issues == ["H-1: groups below the materiality floor of 30 rows: P9"]
        assert any("cover 10% of the population" in x for x in r.limitations)
        assert any("mix effects were not separated" in x for x in r.limitations)


def test_conclusions_need_a_label_a_verdict_and_licensed_wording(db):
    with db() as s:
        ins, _ = _insight(s)
        ins.claim_label = "supported_association"
        s.flush()
        assert inv.conclusions(s, _run(s)).passed
        ins.finding = "Priority P1 drives SLA misses."
        s.flush()
        r = inv.conclusions(s, _run(s))
        assert len(r.issues) == 1 and "causal language" in r.issues[0]
        ins.status = "draft"
        s.flush()
        assert "I-1: concluded without a REV verdict" in inv.conclusions(s, _run(s)).issues


def test_alternatives_are_only_considered_on_validated_data(db):
    with db() as s:
        ins, _ = _insight(s)
        ins.verification = {"evaluate": [{"check": "data_version_stable", "passed": False}]}
        s.flush()
        r = inv.alternatives(s, _run(s))
        assert r.limitations == ["I-1: alternatives were not tested: the data it rests on is not validated"]
        ins.verification = {"evaluate": [{"check": "data_version_stable", "passed": True}]}
        s.flush()
        assert any("traffic mix" in x for x in inv.alternatives(s, _run(s)).limitations)


def test_gate_results_become_evidence_limitations(db):
    from analystos.db.models import RunTask
    from analystos.evidence import api_v2

    with db() as s:
        ins, _ = _insight(s)
        s.add(RunTask(id="rt_profile", run_id=RUN, key="profile", agent_id="profiler", title="t",
                      status="COMPLETED", depends_on=[],
                      output={"checks": [{"check": "baseline", "passed": True, "issues": [],
                                          "limitations": ["shop.orders: no time column, so the analysis window is the whole table"]}]}))
        s.flush()
        ev = api_v2.build(s, [ins], _run(s))[0]
        assert "baseline: shop.orders: no time column, so the analysis window is the whole table" in ev.limitations
