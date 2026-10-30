"""ADR-016: every demo workspace gets an approved semantic model with 3-5 key metrics (`analystos demo-seed`).

The pack specs are checked here against the same validators the approval path runs (structure contract, metric
contract, `definition_problems`), and the card questions against the Ask matcher, without services."""
from __future__ import annotations

import pytest

from analystos.contracts.semantic import MetricProposalIn, SemanticDataset, SemanticMetricDef
from analystos.demo import seed
from analystos.semantic import ossie
from analystos.semantic.review import definition_problems


def _spec(path):
    return seed.demo(path)["semantic"]


def _assets(spec) -> dict[str, dict]:
    return {d["table"]: {"name": d["table"], "fq": f"src_demo.{d['table']}", "selected": True} for d in spec["datasets"]}


def _catalog(spec) -> dict:
    datasets = seed.structure_proposal(spec, _assets(spec))
    metrics = {}
    for body in seed.metric_proposals(spec):
        defn = SemanticMetricDef(name=body["name"], expressions=[{"dialect": "ANSI_SQL", "expression": body["expression"]}],
                                 **{k: body[k] for k in ("display_name", "description", "format", "grain", "dataset",
                                                         "dimensions", "ai_context")})
        metrics[body["name"]] = {"version": 1, "definition": defn.model_dump(mode="json")}
    return {"datasets": datasets, "relationships": [], "metrics": metrics, "synonyms": {}}


@pytest.mark.parametrize("path", seed.DEMO_FILES, ids=lambda p: p.name)
def test_each_demo_has_three_to_five_metrics_that_pass_the_approval_checks(path):
    spec = _spec(path)
    assert 3 <= len(spec["metrics"]) <= 5
    datasets = seed.structure_proposal(spec, _assets(spec))
    for d in datasets:
        SemanticDataset.model_validate(d)
        assert d["source"].startswith("src_demo.")
    for body in seed.metric_proposals(spec):
        MetricProposalIn.model_validate(body)
        defn = SemanticMetricDef(name=body["name"], expressions=[{"dialect": "ANSI_SQL", "expression": body["expression"]}],
                                 dataset=body["dataset"], dimensions=body["dimensions"])
        assert ossie.metric_problem(defn) is None, body["name"]
        assert definition_problems(defn, datasets, []) == [], body["name"]
        assert body["dimensions"] and body["display_name"]


def test_the_incident_demo_names_the_metrics_the_pilot_asked_for():
    names = {m["display_name"] for m in _spec(seed.DEMO_FILE)["metrics"]}
    assert names == {"Incident count", "Mean resolution hours", "SLA breach rate", "Reassignment count", "Open backlog"}


def test_a_table_the_workspace_does_not_have_stops_the_seed():
    spec = _spec(seed.DEMO_FILE)
    with pytest.raises(SystemExit, match="not selected"):
        seed.structure_proposal(spec, {})


@pytest.mark.parametrize(("path", "question", "metric", "dims"), [
    (seed.RETAIL_DEMO_FILE, "What is our late delivery rate by sales channel? Put it on a dashboard I can share later.",
     "late_delivery_rate", ["sales_channel"]),
    (seed.DEMO_FILE, "average resolution hours by priority", "mean_resolution_hours", ["priority"]),
    (seed.DEMO_FILE, "average resolution time in hours by priority", "mean_resolution_hours", ["priority"]),
    (seed.DEMO_FILE, "SLA breach rate by assignment group in Q3 2026", "sla_breach_rate", ["assignment_group_name"]),
    (seed.DEMO_FILE, "how many incidents per month", "incident_count", []),
    (seed.PROCESS_DEMO_FILE, "handover rate by transition", "handover_rate", ["transition"]),
])
def test_card_questions_match_an_approved_metric_without_a_model(path, question, metric, dims):
    from analystos.agents.ask_metrics import match

    found = match(None, question, _catalog(_spec(path)))
    assert found.query is not None, (found.unexplained, found.tied)
    assert found.query.metrics == [metric] and found.query.dimensions == dims
