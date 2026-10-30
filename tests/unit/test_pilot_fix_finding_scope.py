"""PILOT-FIX-06: a finding's sentence says which rows its figures describe (rehearsal cards C03, C07, C11).

The rehearsal's findings were measured on the discovery rows (about 70%; the rest held out to confirm them)
but read as if they covered every record, so an analyst's full-table check disagreed. Now a confirmed finding
quotes its test re-run on every row (bound to those figures by the numbers guard), keeping the discovery
figures in the evidence; any other finding says its figures come from the discovery rows.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.agents.critic import full_data_text  # noqa: E402
from analystos.agents.insight import template_text  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec, Derivation  # noqa: E402
from analystos.evidence import confirmation as C  # noqa: E402
from analystos.evidence import holdout as H  # noqa: E402
from analystos.evidence.strength import ALL_ROWS_NOTE, DISCOVERY_NOTE, UNCONFIRMED_NOTE, qualify  # noqa: E402
from analystos.methods.base import fmt_pct  # noqa: E402
from analystos.registries.hypotheses import spec_hash  # noqa: E402
from analystos.skills.analysis import run_analysis  # noqa: E402
from retail_fixture import retail_duck  # noqa: E402

ASSET = "retail.orders"
RETURNS_BY_CHANNEL = AnalysisSpec(method="rate_by_segment", asset=ASSET,
                                  outcome=Derivation(type="equals", column="Returned", value="Yes", label="return"),
                                  segment=Derivation(column="Sales Channel", label="sales channel"))


@pytest.fixture(scope="module")
def duck():
    return retail_duck()


def _confirmed(duck):
    disc, why = H.discovery_partition(fraction=0.3, dialect="duckdb", readable=["Order ID"], key=(["Order ID"], "declared_key"))
    assert disc is not None, why
    stat = run_analysis(RETURNS_BY_CHANNEL, duck, partition=disc).stat.model_dump()
    top, direction = H.top_of(stat), H.direction_of(RETURNS_BY_CHANNEL.model_dump(), stat)
    locked = H.lock_claim(spec=RETURNS_BY_CHANNEL.model_dump(), stat=stat, top=top, direction=direction,
                          spec_hash=spec_hash(RETURNS_BY_CHANNEL.model_dump()))
    record, _ = H.test_on_holdout(RETURNS_BY_CHANNEL, duck, locked, disc, alpha=0.05, min_n=100)
    conf = C.evaluate(verified=True, origin="agent", top=top, direction=direction, holdout=record.model_dump())
    assert conf.passed and conf.rule == "holdout_partition"
    return stat


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?%", text))


def test_a_confirmed_finding_quotes_the_full_table_and_keeps_the_discovery_figures_apart(duck):
    discovery = _confirmed(duck)
    full = run_analysis(RETURNS_BY_CHANNEL, duck).stat.model_dump()  # every row, as an analyst's own check
    assert full["n"] > discovery["n"]
    _, discovery_text = template_text(discovery, RETURNS_BY_CHANNEL.model_dump())
    swapped = full_data_text(RETURNS_BY_CHANNEL.model_dump(), discovery, full)
    assert swapped is not None
    title, finding, facts, binding = swapped
    assert binding.ok and facts  # the numbers guard binds the new sentence to the full-data facts
    want = {fmt_pct(full["highlights"]["top_rate"]), fmt_pct(full["highlights"]["baseline_rate"])}
    assert want <= _numbers(finding), (finding, want)
    assert _numbers(finding) != _numbers(discovery_text)  # the figures did change: they are the full-table ones
    assert full["highlights"]["top_segment"] == "Marketplace" and "Marketplace" in finding
    worded = qualify(finding, confirmed=True, strength="moderate", scope="all_rows")
    assert worded.endswith(ALL_ROWS_NOTE) and DISCOVERY_NOTE not in worded and UNCONFIRMED_NOTE not in worded


def test_full_data_figures_that_tell_another_story_are_not_used(duck):
    discovery = _confirmed(duck)
    full = run_analysis(RETURNS_BY_CHANNEL, duck).stat.model_dump()
    other_top = {**full, "highlights": {**full["highlights"], "top_segment": "Web"}}
    assert full_data_text(RETURNS_BY_CHANNEL.model_dump(), discovery, other_top) is None
    assert full_data_text(RETURNS_BY_CHANNEL.model_dump(), discovery, {**full, "supported": False}) is None


def test_an_unconfirmed_discovery_finding_states_its_scope_in_the_sentence():
    text = "Marketplace orders have a return rate of 11.3% versus 6.1% for Store."
    lead = qualify(text, confirmed=False, strength=None, scope="discovery")
    assert lead.startswith(text) and DISCOVERY_NOTE in lead and UNCONFIRMED_NOTE in lead
    assert qualify(lead, confirmed=False, strength=None, scope="discovery") == lead  # idempotent
    again = qualify(lead, confirmed=True, strength=None, scope="all_rows")  # a re-verification that confirmed it
    assert ALL_ROWS_NOTE in again and DISCOVERY_NOTE not in again
    assert qualify(text, confirmed=True, strength=None) == text  # a test that read every row needs no note
    for note in (ALL_ROWS_NOTE, DISCOVERY_NOTE):
        assert not re.search(r"\d", note)  # plain words: the numbers guard is unaffected
