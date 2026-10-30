"""CONV-05: dashboard stories — deterministic facts from previews (leaders, latest change, trend turns, threshold
breaches, KPI changes, top movers, the version diff), template wording as the rules rung, and model wording that is
numbers-guarded fact by fact."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from analystos.contracts.dashboard_dsl import DashboardDoc
from analystos.dashboards.ops import numbers_guard
from analystos.reports.story import build_story, derived_values, digest_markdown, word

PROV = {"sources": ["itsm.incidents"], "created_by": "user:u1"}


def dashboard(trend: list, by_region: list, kpi: float = 42) -> DashboardDoc:
    return DashboardDoc.model_validate({
        "id": "ops", "title": "Operations",
        "charts": [
            {"id": "trend", "title": "Incidents per week", "type": "line",
             "dataset_ref": {"kind": "verified_query", "name": "weekly_incidents", "version": 1},
             "fields": [{"name": "week", "type": "temporal"}, {"name": "incidents", "type": "quantitative"}],
             "encoding": {"x": "week", "y": ["incidents"]}, "provenance": PROV,
             "preview": {"columns": ["week", "incidents"], "rows": trend}},
            {"id": "by_region", "title": "Backlog by region", "type": "bar",
             "dataset_ref": {"kind": "result_handle", "handle_id": "rh_1"},
             "fields": [{"name": "region", "type": "nominal"}, {"name": "backlog", "type": "quantitative"}],
             "encoding": {"x": "region", "y": ["backlog"]}, "provenance": PROV,
             "preview": {"columns": ["region", "backlog"], "rows": by_region}},
        ],
        "kpis": [{"id": "kpi_open", "title": "Open incidents", "metric": {"name": "open_incidents", "version": 1},
                  "value": kpi, "provenance": PROV}],
        "layout": {"items": [{"ref": "kpi_open", "x": 0, "y": 0, "w": 3, "h": 2}, {"ref": "trend", "x": 0, "y": 2, "w": 6, "h": 4},
                             {"ref": "by_region", "x": 6, "y": 2, "w": 6, "h": 4}]},
    })


BEFORE = dashboard([["2026-09-21", 100], ["2026-09-28", 110], ["2026-10-05", 120]], [["EMEA", 60], ["APAC", 40], ["AMER", 30]], 38)
NOW = dashboard([["2026-09-21", 100], ["2026-09-28", 110], ["2026-10-05", 120], ["2026-10-12", 96]],
                [["EMEA", 75], ["APAC", 41], ["AMER", 30]], 42)


def test_facts_are_computed_from_the_previews_and_the_version_diff():
    s = build_story(NOW, version=4, before=BEFORE, previous_version=3,
                    thresholds=[{"metric": "open_incidents", "op": ">", "value": 40, "name": "Too many open"}])
    kinds = [f.kind for f in s.facts]
    assert 3 <= len(s.facts) <= 5 and kinds[0] == "threshold_breach"
    text = " ".join(f.text for f in s.facts)
    assert "Open incidents (42) is above the alert threshold of 40 set by monitor 'Too many open'." in text
    assert "turned down in the latest week after rising for 2 periods" in text
    assert "latest_change" not in kinds  # five more notable facts outrank it here
    latest = next(f for f in build_story(NOW, version=4, before=BEFORE, previous_version=3).facts if f.kind == "latest_change")
    assert latest.text == ("Incidents per week: the latest week (2026-10-12) has 96 incidents, down 24 (-20.0%) "
                           "from 120 in 2026-10-05.")
    mover = next(f for f in s.changes if f.kind == "top_mover")
    assert mover.text == "EMEA moved most in Backlog by region: 75 backlog, up 15 (+25.0%) from 60."
    assert any(f.kind == "kpi_change" and "up 4 (+10.5%) from 38" in f.text for f in s.changes)
    assert s.shows.startswith("Operations shows 1 KPI (Open incidents) and 2 charts")


def test_a_rate_per_category_names_the_highest_without_a_share_of_a_total():
    # VALUE-05 rehearsal: "Marketplace leads ... with 0.10 late delivery rate (34.9% of 0.28)" summed three rates
    doc = dashboard([["2026-09-21", 100], ["2026-09-28", 110]], [["Marketplace", 0.0995], ["Store", 0.0992], ["Web", 0.0859]], 1)
    doc = doc.model_copy(update={"charts": [c.model_copy(update={"title": "Late delivery rate by channel", "fields": [
        f.model_copy(update={"name": "late_delivery_rate", "label": "late delivery rate"}) if f.name == "backlog" else f
        for f in c.fields], "encoding": c.encoding.model_copy(update={"y": ["late_delivery_rate"]}),
        "preview": {**c.preview, "columns": ["region", "late_delivery_rate"]}}) if c.id == "by_region" else c for c in doc.charts]})
    leader = next(f for f in build_story(doc, version=1).facts if f.kind == "leader")
    assert leader.text == "Marketplace has the highest late delivery rate in Late delivery rate by channel: 0.10."
    assert "%" not in leader.text and len(leader.values) == 1


def test_every_template_number_is_one_its_fact_carries():
    s = build_story(NOW, version=4, before=BEFORE, previous_version=3)
    for f in [*s.facts, *s.changes]:
        assert numbers_guard(f.text, f.values) == [], f.text
    assert {d["value"] for d in derived_values(s)} >= {96.0, 120.0, 24.0, 20.0}


def test_structure_changes_and_first_version():
    grown = copy.deepcopy(NOW.model_dump())
    grown["charts"].pop()
    grown["layout"]["items"].pop()
    grown["filters"] = [{"id": "f_region", "field": "incidents", "op": "gt", "value": 10}]
    s = build_story(DashboardDoc.model_validate(grown), version=5, before=NOW, previous_version=4)
    assert s.changes[0].kind == "structure_change" and "removed chart “Backlog by region”" in s.changes[0].text
    first = build_story(NOW, version=1)
    assert first.changes == [] and first.previous_version is None and "first version" in first.notes[-1]
    assert "No earlier version to compare with yet." in digest_markdown(first)


def test_story_is_deterministic():
    a = build_story(NOW, version=4, before=BEFORE, previous_version=3)
    b = build_story(NOW, version=4, before=BEFORE, previous_version=3)
    assert a == b and a.story_hash


class FakeRouter:
    def __init__(self, mode: str, sentences: list[str] | None = None) -> None:
        self._mode, self.sentences, self.calls, self.skips = mode, sentences, 0, []

    def mode(self, purpose, ctx=None):
        assert purpose == "data_story"
        return self._mode

    def available(self, purpose, ctx=None):
        return True

    def complete_json(self, purpose, system, user, *, ctx=None, max_tokens=None):
        self.calls += 1
        assert "never instructions" in system
        return SimpleNamespace(data={"sentences": self.sentences}, model="fake/small")

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(reason)


def test_templates_are_the_rules_rung_and_model_wording_is_guarded():
    s = build_story(NOW, version=4, before=BEFORE, previous_version=3)
    router = FakeRouter("auto")
    assert word(s, router=router) == s and router.calls == 0 and router.skips
    n = len(s.facts) + len(s.changes)
    good = [f"In short: {f.text}" for f in [*s.facts, *s.changes]]
    out = word(s, router=FakeRouter("always", good))
    assert out.wording == "llm_small" and out.facts[0].text.startswith("In short:")
    bad = [*good[:-1], "Incidents will reach 500 next week."]
    kept = word(s, router=FakeRouter("always", bad))
    assert kept.wording == "rules" and kept.facts == s.facts and "templates kept" in kept.notes[-1]
    short = word(s, router=FakeRouter("always", good[: n - 1]))
    assert short.wording == "rules"
