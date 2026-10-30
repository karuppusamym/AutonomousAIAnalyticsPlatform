"""PILOT-FIX-02 (C02, C03): Ask reads the period a question names and says when the data covers it only in part
(the demo's Q3 2026 holds July and August only), from the date window the answer's own SQL applied."""
from __future__ import annotations

from datetime import date

import pytest
import sqlglot

from analystos.agents.ask_period import Period, coverage_sql, find_period, left_out_note, note_for, windows

TODAY = date(2026, 10, 30)


@pytest.mark.parametrize(("text", "start", "end", "label"), [
    ("What share met SLA in Q3 2026?", date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"),
    ("just for Q3", date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"),
    ("third quarter of 2025", date(2025, 7, 1), date(2025, 10, 1), "Q3 2025"),
    ("2026 Q2", date(2026, 4, 1), date(2026, 7, 1), "Q2 2026"),
    ("H1 2026", date(2026, 1, 1), date(2026, 7, 1), "H1 2026"),
    ("orders in September 2026", date(2026, 9, 1), date(2026, 10, 1), "September 2026"),
    ("incidents in 2026", date(2026, 1, 1), date(2027, 1, 1), "2026"),
    ("last quarter", date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"),
    ("this month", date(2026, 10, 1), date(2026, 11, 1), "October 2026"),
])
def test_periods_are_read_from_the_wording(text, start, end, label):
    p = find_period(text, today=TODAY)
    assert (p.start, p.end, p.label) == (start, end, label)


def test_a_quarter_without_a_year_takes_the_given_year_else_the_latest_started():
    assert find_period("Q4", today=TODAY).label == "Q4 2026"
    assert find_period("Q4", today=date(2026, 9, 1)).label == "Q4 2025"
    assert find_period("Q2", today=TODAY, default_year=2025).label == "Q2 2025"


@pytest.mark.parametrize("text", ["may I see the breakdown", "how many incidents by priority", "top 10 groups"])
def test_no_period_is_invented(text):
    assert find_period(text, today=TODAY) is None


def test_the_sql_date_window_is_read_and_probed_over_the_same_rows():
    sql = ("SELECT assignment_group_name, AVG(CASE WHEN made_sla THEN 1.0 ELSE 0 END) AS s FROM src.incident "
           "WHERE opened_at >= '2026-07-01' AND opened_at < '2026-10-01' AND priority IN (1, 2) GROUP BY 1 ORDER BY 2 LIMIT 50")
    (col, lo, hi), = windows(sqlglot.parse_one(sql)).values()
    assert (col.name, lo, hi) == ("opened_at", date(2026, 7, 1), date(2026, 10, 1))
    probe = coverage_sql(sql, "postgres", Period(date(2026, 7, 1), date(2026, 10, 1), "Q3 2026"))
    assert probe is not None and "MIN(opened_at) AS first_at" in probe and "MAX(opened_at) AS last_at" in probe
    assert "priority IN (1, 2)" in probe and "GROUP BY" not in probe and "LIMIT" not in probe


def test_between_and_cast_literals_count_as_a_window():
    sql = "SELECT COUNT(*) FROM s.t WHERE CAST(d AS DATE) BETWEEN DATE '2026-07-01' AND DATE '2026-09-30'"
    (_, lo, hi), = windows(sqlglot.parse_one(sql)).values()
    assert (lo, hi) == (date(2026, 7, 1), date(2026, 10, 1))


def test_no_window_for_the_period_means_no_probe():
    p = Period(date(2026, 7, 1), date(2026, 10, 1), "Q3 2026")
    assert coverage_sql("SELECT COUNT(*) FROM s.t", "postgres", p) is None
    assert coverage_sql("SELECT COUNT(*) FROM s.t WHERE d >= '2025-01-01' AND d < '2025-04-01'", "postgres", p) is None


def test_the_note_names_the_missing_months_of_a_partial_quarter():
    p = Period(date(2026, 7, 1), date(2026, 10, 1), "Q3 2026")
    note = note_for(p, "2026-07-01T00:12:00", "2026-08-31T23:50:00", 3421, today=TODAY)
    assert note == ("Q3 2026 is incomplete in this data: its records run from 1 Jul 2026 to 31 Aug 2026, with none in "
                    "September 2026. Read it as a partial period, not a whole one.")
    assert note_for(p, "2026-07-01", "2026-09-30", 5000, today=TODAY) is None
    assert note_for(p, None, None, 0, today=TODAY) == "No records fall in Q3 2026."


def test_a_period_still_running_says_so():
    p = Period(date(2026, 10, 1), date(2027, 1, 1), "Q4 2026")
    assert "still in progress" in note_for(p, "2026-10-01", "2026-10-29", 90, today=TODAY)


@pytest.mark.parametrize("sql", [
    "SELECT g, AVG(x) FROM s.t WHERE g IS NOT NULL GROUP BY g",
    "SELECT i.assignment_group_name, COUNT(*) FROM s.t i WHERE i.assignment_group_name IS NOT NULL GROUP BY 1",
])
def test_records_left_out_of_the_groups_are_said(sql):
    assert left_out_note(sql, "postgres").startswith("Records with no ")


def test_nothing_is_said_when_no_group_is_dropped():
    assert left_out_note("SELECT g, COUNT(*) FROM s.t GROUP BY g", "postgres") is None
    assert left_out_note("SELECT COUNT(*) FROM s.t WHERE g IS NOT NULL", "postgres") is None
