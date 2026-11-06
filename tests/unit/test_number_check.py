"""S07-T8 number check: claim parsing, picking the one computed number, the rounding rule and the verdicts (pure)."""
from __future__ import annotations

import pytest

from analystos.core.errors import InvalidInput
from analystos.evidence.number_check import compare, evaluate, parse_claim, pick_value


def _turn(columns, rows, *, status="answered", answered_by="model", governance="ad_hoc", refusal=None, truncated=False):
    return {"id": "askt_1", "thread_id": "ask_1", "status": status, "answered_by": answered_by, "governance": governance,
            "refusal": refusal, "sql": "SELECT 1", "provenance": {"assets": [{"asset": "analytics.incident"}]},
            "result": {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated,
                       "query_id": "q1", "result_hash": "h1"}}


@pytest.mark.parametrize("text,value,decimals,percent", [
    ("1204", 1204.0, 0, False), ("1,204.5", 1204.5, 1, False), ("-3.25", -3.25, 2, False), ("12.5%", 12.5, 1, True),
    (" 7 % ", 7.0, 0, True), (42, 42.0, 0, False)])
def test_parse_claim_keeps_written_precision(text, value, decimals, percent):
    c = parse_claim(text)
    assert (c.value, c.decimals, c.percent) == (value, decimals, percent)


@pytest.mark.parametrize("bad", ["", "about 12", "12 and 13", "1e5", None, True, "12.5%%"])
def test_parse_claim_refuses_anything_but_one_number(bad):
    with pytest.raises(InvalidInput):
        parse_claim(bad)


def test_rounding_rule_follows_the_written_digits():
    assert compare(parse_claim("1204"), 1204.4)["agrees"]
    assert not compare(parse_claim("1204"), 1204.6)["agrees"]
    assert compare(parse_claim("12.5%"), 0.1253)["agrees"]  # a percent quotes a fraction ...
    assert compare(parse_claim("12.5%"), 12.46)["agrees"]  # ... or the value already in percent
    assert not compare(parse_claim("12.5%"), 0.131)["agrees"]
    assert compare(parse_claim("1200"), 1230, tolerance=0.05)["agrees"]  # stated as approximate
    out = compare(parse_claim("1300"), 1204)
    assert out["difference"] == 96 and round(out["relative"], 4) == round(96 / 1204, 4)


def test_pick_scalar_label_and_column():
    assert pick_value(["n"], [[1204]]).value == 1204
    rows = [["1 - Critical", 12, 0.25], ["2 - High", 30, 0.5]]
    p = pick_value(["priority", "n", "share"], rows, label="2 - high", column="n")
    assert (p.value, p.column, p.row_label) == (30, "n", "2 - High")
    assert "numeric columns" in pick_value(["priority", "n", "share"], rows, label="2 - High").reason
    assert "2 rows" in pick_value(["priority", "n"], [r[:2] for r in rows]).reason
    assert "no row is labelled" in pick_value(["priority", "n"], [r[:2] for r in rows], label="P5", truncated=True).reason
    assert "no column" in pick_value(["n"], [[1]], column="total").reason
    assert pick_value(["n"], []).reason == "the answer returned no rows"


def test_label_may_match_a_numeric_period_column():
    p = pick_value(["year", "revenue"], [[2024, 10.0], [2025, 12.5]], label="2025")
    assert (p.value, p.column) == (12.5, "revenue")


def test_verdicts_and_authority():
    certified = evaluate(_turn(["n"], [[1204]], answered_by="registry", governance="governed"), parse_claim("1,204"))
    assert (certified["verdict"], certified["authority"], certified["note"]) == ("confirmed", "certified", None)
    assert certified["evidence"]["query_id"] == "q1" and certified["evidence"]["assets"] == ["analytics.incident"]
    wrong = evaluate(_turn(["n"], [[1204]]), parse_claim("1300"))
    assert (wrong["verdict"], wrong["authority"]) == ("contradicted", "ad_hoc")
    assert "definition difference" in wrong["note"] and wrong["reason"] == "claimed 1300, computed 1204"
    refused = evaluate(_turn([], [], status="refused", refusal={"title": "Out of scope", "message": "no such table",
                                                                 "remedy": "Select the table"}), parse_claim("5"))
    assert refused["verdict"] == "unverifiable" and refused["remedy"] == "Select the table"
    assert "Out of scope" in refused["reason"]
    ambiguous = evaluate(_turn(["p", "n"], [["a", 1], ["b", 2]]), parse_claim("2"))
    assert ambiguous["verdict"] == "unverifiable" and "label" in ambiguous["reason"]
