"""run_analysis / verify_analysis end to end on a synthetic dataset with KNOWN planted effects."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter  # noqa: E402
from analystos.skills.analysis import AnalysisOutcome, build_design, run_analysis, verify_analysis  # noqa: E402
from skills_fixtures import PG_DSN, PgRunSQL, TsqlViaDuckRunSQL, duck_dataset, pg_available, pg_load  # noqa: E402

D = Derivation
DUR = D(type="duration_hours", column="opened_at", end_column="resolved_at")
BREACH = D(type="is_true", column="sla_breached")


def planted(asset: str = "itsm.incident") -> dict[str, AnalysisSpec]:
    return {
        "reassignment_breach": AnalysisSpec(method="rate_by_segment", asset=asset, outcome=BREACH,
                                            segment=D(type="bucket", column="reassignment_count", edges=[0, 1, 2, 3])),
        "after_hours_duration": AnalysisSpec(method="numeric_by_segment", asset=asset, outcome=DUR,
                                             segment=D(type="after_hours", column="opened_at")),
        "volume_trend": AnalysisSpec(method="trend", asset=asset, time=D(type="date_trunc", column="opened_at", grain="month")),
        "p1_app_concentration": AnalysisSpec(method="pareto", asset=asset, segment=D(column="app"),
                                             filters=[Filter(column="priority", op="=", value="1 - Critical")]),
        "reassign_duration_corr": AnalysisSpec(method="correlation", asset=asset, outcome=DUR,
                                               drivers=[D(column="reassignment_count")]),
        "breach_drivers": AnalysisSpec(method="driver_model", asset=asset, outcome=BREACH,
                                       drivers=[D(column="reassignment_count"), D(column="priority"), D(column="noise_value")]),
    }


def null_controls(asset: str = "itsm.incident") -> dict[str, AnalysisSpec]:
    return {
        "breach_by_noise": AnalysisSpec(method="rate_by_segment", asset=asset, outcome=BREACH, segment=D(column="noise_group")),
        "duration_by_noise": AnalysisSpec(method="numeric_by_segment", asset=asset, outcome=DUR, segment=D(column="noise_group")),
        "noise_trend": AnalysisSpec(method="trend", asset=asset, outcome=D(column="noise_value"),
                                    time=D(type="date_trunc", column="opened_at", grain="month")),
        "noise_concentration": AnalysisSpec(method="pareto", asset=asset, segment=D(column="noise_group")),
        "noise_corr": AnalysisSpec(method="correlation", asset=asset, outcome=DUR, drivers=[D(column="noise_value")]),
        "noise_drivers": AnalysisSpec(method="driver_model", asset=asset, outcome=D(type="is_true", column="noise_flag"),
                                      drivers=[D(column="noise_group"), D(column="noise_value")]),
    }


@pytest.fixture(scope="module")
def duck():
    return duck_dataset()


@pytest.fixture(scope="module")
def results(duck):
    out = {}
    for name, spec in {**planted(), **null_controls()}.items():
        prim = run_analysis(spec, duck)
        out[name] = (prim, verify_analysis(spec, duck, prim.stat))
    return out


@pytest.mark.parametrize("name", list(planted()))
def test_planted_effects_supported_and_verified(results, name):
    prim, ver = results[name]
    assert isinstance(prim, AnalysisOutcome)
    assert prim.stat.supported is True, (name, prim.stat)
    assert ver.agrees is True and ver.stat.supported is True, (name, ver.stat)
    assert prim.query_ids and len(prim.sql) == len(prim.query_ids)
    assert prim.table["columns"] and prim.table["rows"]


@pytest.mark.parametrize("name", list(null_controls()))
def test_null_controls_not_supported(results, name):
    prim, ver = results[name]
    assert prim.stat.supported is False, (name, prim.stat)
    assert ver.stat.supported is False and ver.agrees is True


def test_reassignment_triples_breach_rate(results):
    h = results["reassignment_breach"][0].stat.highlights
    assert h["top_segment"] == "3+"
    assert 0.30 <= h["top_rate"] <= 0.42
    assert h["reference_segment"] == "0" and 0.09 <= h["reference_rate"] <= 0.15
    assert 2.4 <= h["rate_ratio_vs_reference"] <= 4.0
    assert h["overall_rate"] == pytest.approx(0.187, abs=0.001)
    groups = results["reassignment_breach"][0].stat.groups
    assert [g["segment"] for g in groups] == ["0", "1", "2", "3+"]  # bucket order preserved
    v = results["reassignment_breach"][1].stat
    assert v.effect_label == "odds_ratio_top_vs_baseline" and v.ci_low > 1


def test_after_hours_duration_ratio(results):
    prim = results["after_hours_duration"][0].stat
    h = prim.highlights
    assert h["top_segment"] == "true" and h["baseline_segment"] == "false"
    assert 1.45 <= h["median_ratio"] <= 1.8
    assert prim.effect_label == "rank_biserial" and prim.effect_size > 0.2
    assert prim.details["excluded_null_outcome_rows"] == 281  # open incidents (NULL resolved_at) counted
    assert all(g["full_n"] for g in prim.groups)


def test_payments_share_of_p1(results):
    h = results["p1_app_concentration"][0].stat.highlights
    assert h["top_segment"] == "Payments" and 0.30 <= h["top_share"] <= 0.40
    ver = results["p1_app_concentration"][1].stat
    assert ver.highlights["top_segment_stability"] >= 0.99


def test_trend_and_driver_highlights(results):
    t = results["volume_trend"][0].stat
    assert t.highlights["direction"] == "increasing" and t.highlights["pct_change"] > 0.8
    assert t.highlights["n_periods"] == 18
    d = results["breach_drivers"][0].stat
    assert d.highlights["top_driver"] == "reassignment_count"
    assert results["breach_drivers"][1].stat.highlights["ranking"][0] == "reassignment_count"
    c = results["reassign_duration_corr"][0].stat
    assert c.highlights["direction"] == "positive" and 0.15 <= c.effect_size <= 0.3


def test_sampling_path_still_finds_effects(duck):
    specs = planted()
    for name in ("after_hours_duration", "reassign_duration_corr", "breach_drivers"):
        prim = run_analysis(specs[name], duck, sample_rows=1500)
        assert prim.stat.supported, name
        assert prim.stat.details.get("sampled") is True or prim.stat.highlights.get("sampled") is True
        ver = verify_analysis(specs[name], duck, prim.stat, sample_rows=1500)
        assert ver.agrees, name


def test_verification_disagrees_when_primary_is_wrong(duck):
    """A verifier that rubber-stamps would be worthless: feed it a fabricated 'supported' primary."""
    spec = null_controls()["breach_by_noise"]
    prim = run_analysis(spec, duck).stat
    fake = prim.model_copy(update={"supported": True})
    assert verify_analysis(spec, duck, fake).agrees is False


def test_min_group_size_and_top_k(duck):
    spec = AnalysisSpec(method="rate_by_segment", asset="itsm.incident", outcome=BREACH, segment=D(column="app"),
                        top_k=5, min_group_size=30)
    stat = run_analysis(spec, duck).stat
    assert len(stat.groups) == 5 and any("largest segments" in w for w in stat.warnings)
    spec2 = spec.model_copy(update={"min_group_size": 10_000, "top_k": 12})
    stat2 = run_analysis(spec2, duck).stat
    assert stat2.supported is False and any("excluded" in w for w in stat2.warnings)


def test_purposes_passed_to_gateway(duck):
    duck.calls.clear()
    run_analysis(planted()["after_hours_duration"], duck, sample_rows=777)
    purposes = [c["purpose"] for c in duck.calls]
    assert purposes == ["analysis.numeric_by_segment.summary", "analysis.numeric_by_segment.primary"]
    assert duck.calls[1]["max_rows"] == 777


def test_build_design_one_hot():
    rows = [{"d_0": "a", "d_1": 1.0}, {"d_0": "b", "d_1": 2.0}, {"d_0": "a", "d_1": 3.0}, {"d_0": "c", "d_1": 4.0}]
    X, names, groups = build_design(rows, [D(column="cat"), D(column="num")])
    assert names == ["cat=b", "cat=c", "num"] and groups == {"cat": [0, 1], "num": [2]}
    assert X.shape == (4, 3) and X[:, 2].tolist() == [1.0, 2.0, 3.0, 4.0]


def test_tsql_sql_gives_same_answers(duck, results):
    """T-SQL generated for SQL Server, transpiled to DuckDB and executed: same planted findings."""
    t = TsqlViaDuckRunSQL(duck)
    for name, spec in planted().items():
        if name == "breach_drivers":
            continue  # identical SQL shape to the correlation sample; keeps the test fast
        prim = run_analysis(spec, t)
        assert prim.stat.supported is True, name
        duck_h = results[name][0].stat.highlights
        for key in ("top_segment", "top_rate", "baseline_rate", "top_share", "n_periods", "median_ratio"):
            if key in duck_h:
                expected = duck_h[key]  # a segment name compares exactly; pytest.approx refuses str with rel=
                if not isinstance(expected, str):
                    expected = pytest.approx(expected, rel=1e-3)
                assert prim.stat.highlights[key] == expected, (name, key)


# ----------------------------------------------------------------------------- postgres integration
@pytest.fixture(scope="module")
def pg():
    if not pg_available():
        pytest.skip("local Postgres (analystos/analystos@localhost:5432) not reachable")
    import psycopg

    conn = psycopg.connect(PG_DSN, autocommit=False)
    schema = f"aos_skills_{uuid.uuid4().hex[:8]}"
    try:
        pg_load(conn, schema)
        yield PgRunSQL(conn), schema
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


@pytest.mark.integration
def test_postgres_planted_effects_match_duckdb(pg, results):
    run, schema = pg
    for name, spec in planted(f"{schema}.incident").items():
        prim = run_analysis(spec, run)
        assert prim.stat.supported is True, (name, prim.stat)
        assert verify_analysis(spec, run, prim.stat).agrees, name
        duck_h = results[name][0].stat.highlights
        for key in ("top_segment", "top_rate", "baseline_rate", "top_share", "n_periods", "top_driver"):
            if key in duck_h:
                assert prim.stat.highlights[key] == duck_h[key], (name, key)
    for name, spec in null_controls(f"{schema}.incident").items():
        assert run_analysis(spec, run).stat.supported is False, name


def test_trend_leaves_out_periods_dated_after_today():
    """Future-dated rows (a planted ServiceNow data-quality defect) must not read as a volume collapse."""
    from analystos.contracts.analysis import AnalysisSpec
    from analystos.contracts.analysis import Derivation as D
    from analystos.methods.trend import series

    spec = AnalysisSpec(method="trend", asset="s.t", time=D(type="date_trunc", column="opened_at", grain="month"))
    rows = [{"period": f"2026-0{m}-01", "n_rows": 400} for m in range(1, 9)] + [
        {"period": "2026-11-01", "n_rows": 3}, {"period": "2027-01-01", "n_rows": 2}]
    periods, y, raw, excluded = series(rows, spec, today="2026-09-27")
    assert len(periods) == 8 and y == [400.0] * 8 and len(raw) == 8
    assert excluded["excluded_future_periods"] == 2 and excluded["excluded_future_rows"] == 5
    assert "excluded_future_periods" not in series(rows[:8], spec, today="2026-09-27")[3]
