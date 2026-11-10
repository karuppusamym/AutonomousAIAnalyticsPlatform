"""A knowledge review batch is scored against golden questions before it is approved (review P0-1)."""
from types import SimpleNamespace

import pytest

from analystos.core.errors import Conflict
from analystos.knowledge import effect


def case(cid, passed, cols=()):
    return {"id": cid, "question": f"q {cid}", "passed": passed, "expected": {"expected_columns": list(cols)},
            "actual_columns": list(cols), "served_query": None}


def test_compare_orders_newly_failing_first_and_counts_delta():
    before = [case("a", True), case("b", False), case("c", True), case("d", False)]
    after = [case("a", False), case("b", True), case("c", True), case("d", False)]
    out = effect.compare(before, after)
    assert [c["change"] for c in out["cases"]] == ["newly_failing", "fixed", "still_failing", "still_passing"]
    assert out["baseline"] == {"passed": 2, "total": 4, "rate": 0.5}
    assert out["projected"] == {"passed": 2, "total": 4, "rate": 0.5}
    assert out["delta_points"] == 0.0
    assert out["counts"] == {"newly_failing": 1, "fixed": 1, "still_failing": 1, "still_passing": 1}


def test_compare_ignores_cases_missing_after():
    out = effect.compare([case("a", False), case("b", False)], [case("a", True)])
    assert [c["id"] for c in out["cases"]] == ["a"]
    assert out["delta_points"] == 100.0


class FakeSavepoint:
    def __init__(self, log):
        self.log = log

    def rollback(self):
        self.log.append("rollback")


class FakeSession:
    def __init__(self):
        self.log = []

    def begin_nested(self):
        self.log.append("savepoint")
        return FakeSavepoint(self.log)

    def flush(self):
        self.log.append("flush")


@pytest.fixture
def harness(monkeypatch):
    """Golden scores depend on whether the batch is applied in the session at that moment."""
    state = {"applied": False, "reviews": 0, "audits": []}
    session = FakeSession()

    def run_golden(_session, _user, _ws, **_kw):
        if state.get("no_cases"):
            return []
        good = state["applied"]
        return [case("fixes", good), case("breaks", not good if state.get("breaking") else True)]

    def review(_session, _ws, _user, decisions):
        state["reviews"] += 1
        state["applied"] = True
        return {"revision": 7, "approved": [{"id": d["id"]} for d in decisions], "rejected": [], "errors": []}

    real_rollback = FakeSavepoint.rollback

    def rollback(self):
        state["applied"] = False
        real_rollback(self)

    monkeypatch.setattr(FakeSavepoint, "rollback", rollback)
    monkeypatch.setattr("analystos.services.context_grounding.run_golden", run_golden)
    monkeypatch.setattr("analystos.knowledge.suggestions.review", review)
    monkeypatch.setattr("analystos.governance.audit.audit", lambda *a, **k: state["audits"].append((a, k)))
    return SimpleNamespace(state=state, session=session, user=SimpleNamespace(id="u1"))


def test_preview_applies_in_a_savepoint_and_rolls_back(harness):
    out = effect.preview(harness.session, "ws", harness.user, [{"id": "s1", "action": "approve"}])
    assert harness.session.log == ["savepoint", "flush", "rollback"]
    assert harness.state["applied"] is False
    assert out["evaluated"] and out["counts"]["fixed"] == 1 and out["delta_points"] == 50.0
    assert out["fingerprint"]


def test_preview_without_golden_questions_says_so(harness):
    harness.state["no_cases"] = True
    out = effect.preview(harness.session, "ws", harness.user, [{"id": "s1", "action": "approve"}])
    assert out == {**out, "evaluated": False, "reason": "no_golden_questions"}
    assert harness.session.log == []


def test_regressing_batch_is_refused_without_a_reason(harness):
    harness.state["breaking"] = True
    with pytest.raises(Conflict) as err:
        effect.review_checked(harness.session, "ws", harness.user, [{"id": "s1", "action": "approve"}])
    assert err.value.details["code"] == "golden_regression"
    assert err.value.details["effect"]["counts"]["newly_failing"] == 1
    assert harness.state["reviews"] == 1  # only the previewed (rolled back) apply ran
    assert harness.state["applied"] is False


def test_regressing_batch_with_reason_applies_and_audits_the_override(harness):
    harness.state["breaking"] = True
    out = effect.review_checked(harness.session, "ws", harness.user, [{"id": "s1", "action": "approve"}],
                                override_reason="The old mapping was wrong; the golden case is being rewritten")
    assert harness.state["reviews"] == 2 and harness.state["applied"] is True
    assert out["revision"] == 7 and out["effect"]["counts"]["newly_failing"] == 1
    (args, kwargs), = harness.state["audits"]
    assert args[1] == "knowledge.review.golden_effect" and kwargs["decision"] == "override"
    assert kwargs["reasons"] == ["The old mapping was wrong; the golden case is being rewritten"]
    assert kwargs["details"]["newly_failing"] == ["breaks"]


def test_improving_batch_applies_without_a_reason(harness):
    out = effect.review_checked(harness.session, "ws", harness.user, [{"id": "s1", "action": "approve"}])
    assert out["effect"]["counts"] == {"newly_failing": 0, "fixed": 1, "still_failing": 0, "still_passing": 1}
    (_, kwargs), = harness.state["audits"]
    assert kwargs["decision"] == "allow" and kwargs["details"]["fixed"] == ["fixes"]
