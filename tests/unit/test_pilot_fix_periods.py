"""PILOT-FIX-01: guided templates take the period and baseline as typed date ranges (rehearsal card C03).

The rehearsal asked for "Q3 2026 vs Q2 2026" in words; the run analysed all history and never compared the
quarters. Here: the ranges are typed, the baseline follows from a comparison mode, every test reads only the
requested period (comparison tests: baseline and period), the constraint is part of the plan hash, and an
incomplete period and future-dated rows are stated as data facts. Also: a guided run picks its source.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.agents.investigator import with_constraints  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec, Derivation  # noqa: E402
from analystos.contracts.investigation import GuidedInvestigationIn  # noqa: E402
from analystos.contracts.policy import DataScope  # noqa: E402
from analystos.core.errors import InvalidInput  # noqa: E402
from analystos.gateway.validator import validate_sql  # noqa: E402
from analystos.runtime.plan import plan_hash  # noqa: E402
from analystos.services import investigation_value as svc  # noqa: E402
from analystos.services import periods as P  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from analystos.skills.analysis import run_analysis  # noqa: E402
from retail_fixture import retail_duck  # noqa: E402

ASSET = "retail.orders"
LATE = {"type": "later_than", "column": "Promised Date", "end_column": "Delivered Date", "label": "late delivery"}


def q3_vs_q2(columns=None):
    base = P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "previous_period")
    return P.constraint(date(2026, 7, 1), date(2026, 9, 30), mode="previous_period", baseline=base,
                        columns=columns or {ASSET: {"column": "Order Date", "reason": "test"}}, period_label="Q3 2026")


@pytest.fixture(scope="module")
def duck():
    return retail_duck()


# ------------------------------------------------------------------------------------ typed inputs
def test_the_baseline_follows_the_comparison_mode():
    assert P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "previous_period") == (date(2026, 4, 1), date(2026, 6, 30))
    assert P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "same_period_last_year") == (date(2025, 7, 1), date(2025, 9, 30))
    assert P.baseline_for(date(2026, 3, 1), date(2026, 3, 31), "previous_period") == (date(2026, 2, 1), date(2026, 2, 28))
    assert P.baseline_for(date(2026, 7, 10), date(2026, 7, 19), "previous_period") == (date(2026, 6, 30), date(2026, 7, 9))
    assert P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "none") is None
    custom = (date(2026, 1, 1), date(2026, 3, 31))
    assert P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "custom", custom) == custom
    with pytest.raises(InvalidInput, match="overlaps"):
        P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "custom", (date(2026, 6, 1), date(2026, 7, 31)))
    with pytest.raises(InvalidInput):
        P.baseline_for(date(2026, 7, 1), date(2026, 9, 30), "custom")


def test_labels_share_one_style_and_sort_in_time_order():
    assert P.labels((date(2026, 7, 1), date(2026, 9, 30)), (date(2026, 4, 1), date(2026, 6, 30))) == ["2026 Q3", "2026 Q2"]
    assert P.labels((date(2026, 7, 1), date(2026, 7, 31)), (date(2026, 4, 1), date(2026, 6, 30))) == ["2026-07", "2026-04..2026-06"]
    odd = P.labels((date(2026, 7, 10), date(2026, 7, 19)), (date(2026, 6, 30), date(2026, 7, 9)))
    assert odd == ["2026-07-10..2026-07-19", "2026-06-30..2026-07-09"] and sorted(odd)[0] == odd[1]
    assert P.labels((date(2026, 1, 1), date(2026, 3, 31)), (date(2025, 10, 1), date(2025, 12, 31))) == ["2026 Q1", "2025 Q4"]


def test_the_api_takes_date_ranges_not_words():
    ok = GuidedInvestigationIn(template_id="sla_change", period={"start": "2026-07-01", "end": "2026-09-30", "label": "Q3 2026"},
                               decision="Which teams to review first")
    assert ok.comparison_mode == "previous_period" and ok.period.start == date(2026, 7, 1)
    for bad in ({"period": "Q3 2026"}, {"comparison": "Q2 2026"},
                {"period": {"start": "2026-09-30", "end": "2026-07-01"}},
                {"comparison_mode": "custom"},
                {"baseline": {"start": "2026-04-01", "end": "2026-06-30"}}):
        body = {"template_id": "sla_change", "period": {"start": "2026-07-01", "end": "2026-09-30"},
                "decision": "Which teams to review first", **bad}
        with pytest.raises(ValidationError):
            GuidedInvestigationIn(**body)


def test_the_period_column_is_the_records_start_timestamp():
    incident = [("number", "id", "identifier"), ("opened_at", "datetime", "timestamp"), ("resolved_at", "datetime", "timestamp"),
                ("closed_at", "datetime", "timestamp"), ("sla_due", "datetime", "timestamp"), ("priority", "categorical", "code")]
    assert P.pick_time_column(incident)[0] == "opened_at"
    assert P.pick_time_column(incident[2:])[0] is None  # resolved, closed, due: none is a start
    orders = [("Order ID", "id", None), ("Promised Date", "datetime", "date"), ("Order Date", "datetime", "date"),
              ("Delivered Date", "datetime", "date")]
    assert P.pick_time_column(orders)[0] == "Order Date"
    assert P.pick_time_column([("a", "numeric", None)]) == (None, "the table has no date or time column")
    assert P.pick_time_column([("x_ts", "datetime", "timestamp"), ("logged_at", "datetime", "timestamp")], hints=["x"])[0] == "x_ts"


# ------------------------------------------------------------------------------------ applied as filters
def test_every_spec_reads_only_the_requested_period_and_a_comparison_reads_both():
    p = q3_vs_q2()
    seg = AnalysisSpec(method="rate_by_segment", asset=ASSET, outcome=Derivation(**LATE), segment=Derivation(column="Sales Channel"))
    applied = P.apply(seg, p)
    assert [(f.column, f.op, f.value) for f in applied.filters] == [("Order Date", ">=", "2026-07-01"), ("Order Date", "<", "2026-10-01")]
    assert P.apply(applied, p) == applied and with_constraints(seg, {"period": p}) == applied  # idempotent, and at execution
    cmp_ = AnalysisSpec(method="rate_by_segment", asset=ASSET, outcome=Derivation(**LATE), segment=P.derivation(p, ASSET))
    assert [f.value for f in P.apply(cmp_, p).filters] == ["2026-04-01", "2026-10-01"]
    other = Derivation(type="period", column="Order Date", value=[{"label": "a", "start": "2025-01-01", "end": "2025-02-01"},
                                                                  {"label": "b", "start": "2025-02-01", "end": "2025-03-01"}])
    with pytest.raises(InvalidInput, match="run's requested period"):
        P.apply(cmp_.model_copy(update={"segment": other}), p)
    with pytest.raises(InvalidInput, match="cannot be applied to retail.products"):
        P.apply(seg.model_copy(update={"asset": "retail.products"}),
                {**p, "columns": {"retail.products": None}, "column_reasons": {"retail.products": "the table has no date or time column"}})
    assert P.apply(seg, None) == seg


def test_the_period_is_part_of_the_plan_hash():
    plan = {"steps": ["x"]}
    q2 = P.constraint(date(2026, 4, 1), date(2026, 6, 30), mode="none", baseline=None, columns={ASSET: {"column": "Order Date"}})
    hashes = {plan_hash(plan, constraints=c, scope_hash="s", plan_version=1) for c in ({}, {"period": q3_vs_q2()}, {"period": q2})}
    assert len(hashes) == 3


def test_comparison_hypotheses_follow_the_playbook_outcomes():
    p = q3_vs_q2()
    props = [{"spec": {"method": "rate_by_segment", "asset": ASSET, "outcome": LATE, "segment": {"type": "column", "column": c}}}
             for c in ("Sales Channel", "Status", "Region")]
    out = P.comparison_proposals(p, props)
    assert [o["spec"]["method"] for o in out] == ["rate_by_segment", "contribution_decomposition", "contribution_decomposition"]
    assert out[0]["spec"]["segment"]["type"] == "period" and "2026 Q2 and 2026 Q3" in out[0]["question"]
    assert [o["spec"]["segment"]["column"] for o in out[1:]] == ["Sales Channel", "Status"]
    for o in out:
        AnalysisSpec.model_validate(o["spec"])
    assert P.comparison_proposals({**p, "baseline": None}, props) == [] and P.comparison_proposals(None, props) == []


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb", "trino"])
def test_period_comparisons_compile_and_pass_the_gateway(dialect):
    inc = "itsm.incident"
    p = q3_vs_q2({inc: {"column": "opened_at", "reason": "test"}})
    scope = DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src"], assets=[inc],
                      asset_sources={inc: "src"}, columns={inc: ["number", "opened_at", "made_sla", "assignment_group"]},
                      source_dialects={"src": dialect})
    breach = Derivation(type="equals", column="made_sla", value=False, label="SLA breach")
    for spec in (AnalysisSpec(method="rate_by_segment", asset=inc, outcome=breach, segment=P.derivation(p, inc)),
                 AnalysisSpec(method="contribution_decomposition", asset=inc, outcome=breach,
                              segment=Derivation(column="assignment_group"), time=P.derivation(p, inc))):
        cq = sb.compile_spec(P.apply(spec, p), dialect)
        assert "2026-04-01" in cq.sql and "'2026 Q3'" in cq.sql
        validate_sql(scope, cq.sql, max_rows=cq.max_rows or 1000)


def test_on_retail_data_the_planted_q3_rise_in_late_deliveries_is_found_by_period(duck):
    """The fixture plants late deliveries rising in the West from July 2026: Q3 vs Q2 must show it."""
    p = q3_vs_q2()
    rate = run_analysis(P.apply(AnalysisSpec(method="rate_by_segment", asset=ASSET, outcome=Derivation(**LATE),
                                             segment=P.derivation(p, ASSET)), p), duck).stat
    assert [g["segment"] for g in rate.groups] == ["2026 Q2", "2026 Q3"]  # time order, baseline first
    assert rate.highlights["top_segment"] == "2026 Q3" and rate.highlights["reference_segment"] == "2026 Q2"
    assert rate.supported and sum(g["n"] for g in rate.groups) < 6000  # never all history
    dec = run_analysis(P.apply(AnalysisSpec(method="contribution_decomposition", asset=ASSET, outcome=Derivation(**LATE),
                                            segment=Derivation(column="Sales Channel"), time=P.derivation(p, ASSET)), p), duck).stat
    assert (dec.highlights["period_before"], dec.highlights["period_after"]) == ("2026 Q2", "2026 Q3")
    assert dec.highlights["within_segment_direction"] == "increase"


# ------------------------------------------------------------------------------------ data facts
def test_an_incomplete_period_and_future_rows_are_stated_as_data_facts(duck):
    """Orders run to 30 Sep 2026. Asking for Jul-Dec 2026 on 15 Aug 2026: the period has not ended, the data ends
    before it does (October to December are empty), and rows after "today" are future-dated."""
    p = P.constraint(date(2026, 7, 1), date(2026, 12, 31), mode="previous_period",
                     baseline=P.baseline_for(date(2026, 7, 1), date(2026, 12, 31), "previous_period"),
                     columns={ASSET: {"column": "Order Date", "reason": "test"}})
    [table] = P.data_facts(p, lambda _a: duck, today=date(2026, 8, 15))
    text = " ".join(table["facts"])
    assert "has not ended yet" in text
    assert "is incomplete: the data ends on 2026-09-30" in text and "2026-10, 2026-11, 2026-12" in text
    assert "dated after today (2026-08-16 to 2026-09-30)" in text and table["future_rows"] > 0
    assert table["windows"]["current"]["rows"] > 0 and table["windows"]["baseline"]["rows"] > 0
    whole = P.data_facts(q3_vs_q2(), lambda _a: duck, today=date(2026, 10, 30))[0]
    assert whole["facts"] == []  # a complete period with no future rows has nothing to say


def test_c03_shape_of_incident_data_reads_as_incomplete_with_future_rows():
    """The ServiceNow demo: Q3 2026 holds July and August only, plus five incidents dated 2026-11 to 2027-01."""
    p = P.constraint(date(2026, 7, 1), date(2026, 9, 30), mode="previous_period",
                     baseline=(date(2026, 4, 1), date(2026, 6, 30)), columns={"s.incident": {"column": "opened_at"}})
    monthly = [{"month": f"2026-{m:02d}-01", "n": 1600, "latest": f"2026-{m:02d}-28"} for m in (4, 5, 6, 7)] + \
              [{"month": "2026-08-01", "n": 1646, "latest": "2026-08-31T23:10:00"}]
    out = P.summarize("s.incident", "opened_at", p, monthly, {"n": 5, "first": "2026-11-03", "last": "2027-01-12"},
                      today=date(2026, 10, 30))
    assert out["facts"] == [
        "incident: the requested period 2026 Q3 is incomplete: the data ends on 2026-08-31, before the period ends on "
        "2026-09-30 (no rows in 2026-09).",
        "incident: 5 rows are dated after today (2026-11-03 to 2027-01-12) by opened_at; they are outside every period "
        "and are not analysed."]


# ------------------------------------------------------------------------------------ the source (friction)
def _scope(sources: dict[str, dict[str, list[str]]]) -> DataScope:
    assets = {a: cols for tables in sources.values() for a, cols in tables.items()}
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=list(sources), assets=list(assets),
                     asset_sources={a: s for s, tables in sources.items() for a in tables}, columns=assets)


def test_a_guided_run_reads_the_source_its_template_fits(monkeypatch):
    two = _scope({"src_sn": {"sn.incident": ["number", "opened_at", "made_sla"]},
                  "src_retail": {"rt.orders": ["Order ID", "Order Date"]}})
    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: two)
    monkeypatch.setattr(P, "time_columns", lambda _s, scope, _h=(): {a: {"column": "d"} for a in scope.assets})
    session = type("S", (), {"merge": lambda self, u: u, "get": lambda self, *a: None})()
    body = GuidedInvestigationIn(template_id="sla_change", period={"start": "2026-07-01", "end": "2026-09-30"},
                                 decision="Which teams to review first")
    sla = next(t for t in svc.templates() if t["id"] == "sla_change")
    assert svc.guided_sources(session, None, "ws", sla, body) == ["src_sn"]
    chosen = body.model_copy(update={"source_ids": ["src_retail"]})
    assert svc.guided_sources(session, None, "ws", sla, chosen) == ["src_retail"]  # the caller's choice wins
    quality = next(t for t in svc.templates() if t["id"] == "data_quality")  # domain-neutral: truly ambiguous
    with pytest.raises(InvalidInput, match="choose which one to read"):
        svc.guided_sources(session, None, "ws", quality, body)
    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: _scope({"src_sn": {"sn.incident": ["opened_at"]}}))
    assert svc.guided_sources(session, None, "ws", quality, body) is None  # one source: nothing to choose


# ------------------------------------------------------------------------------------ the comparison, stated
def test_the_requested_comparison_reads_with_its_direction_even_when_not_significant(monkeypatch):
    from analystos.agents import supervisor
    from analystos.agents.critic import comparison_sentence

    c03 = {"hypothesis": "H-1", "outcome": "missed SLA", "measure": "rate", "significant": False,
           "windows": [{"label": "2026 Q2", "n": 5029, "value": 0.142}, {"label": "2026 Q3", "n": 3421, "value": 0.135}]}
    assert comparison_sentence(c03) == (
        "Missed SLA was 14.2% in 2026 Q2 (5,029 records) and 13.5% in 2026 Q3 (3,421 records), a decrease; the run's "
        "test did not find the difference statistically significant (H-1).")
    up = {**c03, "measure": "median", "significant": True, "outcome": "resolution hours",
          "windows": [{"label": "2026 Q2", "n": 10, "value": 30.0}, {"label": "2026 Q3", "n": 12, "value": 41.25}]}
    assert "41.2 (median) in 2026 Q3 (12 records), an increase; the run's test found" in comparison_sentence(up)
    assert "no data" in comparison_sentence({**c03, "windows": c03["windows"][:1]})
    outputs = {"verify": {"period_comparison": [{**c03, "sentence": comparison_sentence(c03)}]},
               "hypotheses": {"period": {"facts": ["incident: the requested period 2026 Q3 is incomplete"]}}}
    monkeypatch.setattr("analystos.agents.common.task_output", lambda _run, key: outputs.get(key, {}))
    note = supervisor.period_note("run")
    assert note.startswith("**Requested period**\n- Missed SLA was 14.2%") and "- Data fact: incident: the requested" in note
    monkeypatch.setattr("analystos.agents.common.task_output", lambda _run, key: {})
    assert supervisor.period_note("run") == ""
