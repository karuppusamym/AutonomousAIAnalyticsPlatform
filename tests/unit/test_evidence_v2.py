"""S07-T4 (v1 §7.3): a finding as the Evidence v2 object, read from the Insight, its experiment, its
verification record, the run's manifest and binding. SQLite control plane, no services."""
from __future__ import annotations

import pytest

from analystos.contracts.evidence import EvidenceV2
from analystos.evidence import api_v2
from tests.unit.test_verification_p701 import ASSET, RUN, WS, _insight
from tests.unit.test_verification_p701 import db as p701_db  # noqa: F401 (a fixture)


@pytest.fixture
def db(p701_db):  # noqa: F811
    from analystos.db import models

    models.Base.metadata.create_all(p701_db.kw["bind"], tables=[models.Base.metadata.tables["evidence_citation_set"]])
    return p701_db

FIELDS = {"evidence_id", "run_id", "claim_id", "label", "description", "dataset_versions", "metric_version",
          "query_execution_ids", "method", "effect", "sample", "limitations", "review_status", "fingerprint", "void_reason"}


def test_the_contract_has_every_v1_field():
    props = EvidenceV2.model_json_schema()["properties"]
    assert FIELDS <= set(props)
    assert set(props["effect"]["$ref"].split("/")[-1:]) == {"EvidenceEffect"}
    assert set(EvidenceV2.model_json_schema()["$defs"]["EvidenceEffect"]["properties"]) == {"value", "unit", "ci"}
    assert set(EvidenceV2.model_json_schema()["$defs"]["EvidenceSample"]["properties"]) == {"n", "coverage"}


def test_a_verified_finding_as_evidence(db):
    from analystos.db.models import AnalysisRun

    with db() as s:
        ins, rec = _insight(s)
        ins.claim_label, ins.caveats = "supported_association", ["Association in historical data; not proof of causation."]
        s.flush()
        run = s.get(AnalysisRun, RUN)
        [ev] = api_v2.build(s, [ins], run)
        assert ev.evidence_id == ins.id and ev.run_id == RUN and ev.code == "I-1"
        assert ev.label == "supported_association" and ev.description == ins.finding
        assert ev.review_status == "verified" and ev.fingerprint == rec.fingerprint and ev.void_reason is None
        assert ev.verification_record_id == rec.id
        assert ev.metric_version and ev.metric_version["metric"] == "missed_sla_rate"
        [ds] = ev.dataset_versions
        assert ds.asset == ASSET and ds.version and ds.mode == "staged"
        assert ev.query_execution_ids == ["q_1"]
        assert (ev.method.name, ev.method.test, ev.method.claim_kind) == ("rate_by_segment", "chi_square_independence",
                                                                          "association")
        assert ev.method.version  # from the method registry when the run bound none
        assert (ev.effect.value, ev.effect.unit, ev.effect.ci) == (0.21, "cramers_v", None)
        assert ev.sample.n == 20000
        assert "Association in historical data; not proof of causation." in ev.limitations
        assert ev.claim_id.startswith("clm_")
        # the claim id is the claim's identity: the same test and top group give the same id, wording does not count
        again = api_v2.build(s, [ins], run)[0]
        assert again.claim_id == ev.claim_id
        ins.finding = "Reworded."
        assert api_v2.build(s, [ins], run)[0].claim_id == ev.claim_id


def test_a_void_finding_carries_its_reason(db):
    from analystos.db.models import AnalysisRun
    from analystos.evidence.verification import void_dependents

    with db() as s:
        ins, _ = _insight(s)
        void_dependents(s, "semantic", f"{WS}/missed_sla_rate", "v2", "metric missed_sla_rate v2 approved")
        ev = api_v2.build(s, [ins], s.get(AnalysisRun, RUN))[0]
        assert ev.review_status == "void" and ev.void_reason == "metric missed_sla_rate v2 approved"


def test_a_runs_evidence_is_read_in_a_fixed_number_of_statements(db):
    from sqlalchemy import event

    from analystos.db.models import AnalysisRun

    with db() as s:
        found = [_insight(s, n=n)[0] for n in range(1, 6)]
        run = s.get(AnalysisRun, RUN)
        s.flush()
        statements: list[str] = []
        engine = s.get_bind()
        listener = lambda *a, **k: statements.append(a[2])  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            out = api_v2.build(s, found, run)
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        assert len(out) == 5 and len(statements) <= 6, statements
