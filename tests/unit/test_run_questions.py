from copy import deepcopy

import pytest

from analystos.services.run_questions import answer_saved_question


@pytest.fixture
def findings():
    return [{"id": "i1", "code": "F1", "title": "Resolution time", "finding": "Median is 52.6 hours versus 33.5 hours.",
             "status": "verified", "verified": True, "badge": "verified", "caveats": ["Synthetic sample only"],
             "checks": [{"check": "reproducible_rerun", "passed": True, "detail": "Identical hash"}]},
            {"id": "i2", "code": "F2", "title": "Old claim", "finding": "Do not promote this claim.",
             "status": "verified", "verified": True, "badge": "void", "void_reason": "The method changed", "caveats": []}]


def test_summary_only_promotes_currently_verified_findings(findings):
    result = answer_saved_question("Summarize the findings", findings)
    assert result["references"] == [{"id": "i1", "code": "F1", "title": "Resolution time"}]
    assert "52.6" in " ".join(result["paragraphs"])
    assert "Do not promote" not in " ".join(result["paragraphs"])


def test_verification_explains_void_records_without_revalidating(findings):
    before = deepcopy(findings)
    result = answer_saved_question("Which findings need checking?", findings)
    assert result["topic"] == "verification"
    assert result["references"][0]["id"] == "i2"
    assert "The method changed" in " ".join(result["paragraphs"])
    assert findings == before


def test_finding_answer_has_recorded_checks_and_caveats(findings):
    result = answer_saved_question("How was F1 checked?", findings)
    text = " ".join(result["paragraphs"])
    assert "reproducible rerun — passed" in text
    assert "Synthetic sample only" in text
    assert "do not establish causation" in text


def test_limitations_do_not_invent_missing_data_or_checks():
    result = answer_saved_question("What are the limitations?", [])
    assert "does not mean" in result["paragraphs"][0]
    assert result["references"] == []


@pytest.mark.parametrize("question", ["Publish the findings", "Delete F1", "Forecast next month", "Compare revenue to last year"])
def test_new_analysis_and_actions_are_not_claimed_as_completed(findings, question):
    result = answer_saved_question(question, findings)
    assert result["topic"] == "unsupported"
    assert result["references"] == []


@pytest.mark.parametrize("badge,status", [("legacy", "verified"), ("failed", "verified"), ("pending", "verified"),
                                          ("superseded", "verified"), ("verified", "rejected")])
def test_old_or_excluded_verdicts_are_not_summarized_as_verified(findings, badge, status):
    findings[0].update(badge=badge, status=status)
    result = answer_saved_question("Summarize the findings", findings)
    assert result["references"] == []
    assert "no currently verified conclusion" in " ".join(result["paragraphs"])
