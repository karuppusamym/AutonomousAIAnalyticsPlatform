"""S07-T6 time-series and forecast method pack, planted-effect benchmark: seasonality (STL), level shift
(CUSUM), forecast with a rolling-origin backtest, distribution shift (KS + PSI). Each finds its planted
effect, rejects its null control, is verified by its independent second method, compiles to SQL the
gateway accepts, and writes template text the numbers guard binds. DuckDB, deterministic seeds, no services."""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest
from tests.skills_fixtures import DuckRunSQL, GatewayRunSQL, TsqlViaDuckRunSQL

from analystos import methods
from analystos.agents.insight import template_text
from analystos.agents.investigator import validate_spec
from analystos.contracts.analysis import AnalysisSpec, Derivation
from analystos.contracts.policy import DataScope
from analystos.evidence import claims
from analystos.evidence.facts import bind_finding
from analystos.gateway.validator import validate_sql
from analystos.skills import sqlbuild as sb
from analystos.skills.analysis import run_analysis, verify_analysis

PACK = ("seasonality", "level_shift", "forecast_backtest", "distribution_shift")
COLUMNS = ["opened_at", "amount"]
TABLES = ("seasonal", "flat", "shift", "trend", "walk", "drift", "stable")


def _month(i: int) -> dt.datetime:
    return dt.datetime(2022 + i // 12, i % 12 + 1, 1)


def _events(counts: list[int], rng: np.random.Generator, amount=lambda i, n, rng: rng.gamma(4, 25, n)) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(counts):
        start, days = _month(i), (_month(i + 1) - _month(i)).days
        secs = rng.integers(0, days * 86400, size=c)
        for s, a in zip(secs, amount(i, c, rng), strict=True):
            rows.append((start + dt.timedelta(seconds=int(s)), float(a)))
    return pd.DataFrame(rows, columns=COLUMNS)


def make(seed: int) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    months = 36
    season = [int(200 + 80 * math.sin(2 * math.pi * i / 12) + rng.normal(0, 8)) for i in range(months)]
    flat = [int(200 + rng.normal(0, 12)) for _ in range(months)]
    shift = [int((150 if i < 14 else 215) + rng.normal(0, 8)) for i in range(24)]
    trend = [int(120 + 10 * i + 40 * math.sin(2 * math.pi * i / 12) + rng.normal(0, 3)) for i in range(48)]
    # the forecast's null: a random walk, where the last value is already the best forecast
    walk = [int(v) for v in 300 + np.cumsum(rng.normal(0, 25, months))]
    drift = _events([120] * 18, rng, amount=lambda i, n, rng: rng.gamma(4, 25, n) * (1.6 if i == 17 else 1.0))
    stable = _events([120] * 18, rng)
    return {"seasonal": _events(season, rng), "flat": _events(flat, rng), "shift": _events(shift, rng),
            "trend": _events(trend, rng), "walk": _events(walk, rng), "drift": drift, "stable": stable}


def _duck(seed: int) -> DuckRunSQL:
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE SCHEMA bench")
    for name, df in make(seed).items():
        con.register("_tmp", df)
        con.execute(f"CREATE TABLE bench.events_{name} AS SELECT * FROM _tmp")
        con.unregister("_tmp")
    return DuckRunSQL(con)


@pytest.fixture(scope="module", params=[1, 2])
def duck(request):
    return _duck(request.param)


def scope(dialect: str) -> DataScope:
    assets = {f"bench.events_{t}": COLUMNS for t in TABLES}
    return DataScope(workspace_id="ws", user_id="u", role="analyst", source_ids=["src"], assets=list(assets),
                     asset_sources={a: "src" for a in assets}, columns=assets, source_dialects={"src": dialect})


def spec(method: str, table: str, *, outcome: bool = False) -> AnalysisSpec:
    return AnalysisSpec(method=method, asset=f"bench.events_{table}",
                        outcome=Derivation(column="amount", label="order value") if outcome else None,
                        time=Derivation(type="date_trunc", column="opened_at", grain="month"))


CASES = [("seasonality", "seasonal", "flat", False), ("level_shift", "shift", "flat", False),
         ("forecast_backtest", "trend", "walk", False), ("distribution_shift", "drift", "stable", True)]


@pytest.mark.parametrize(("method", "planted", "null", "outcome"), CASES)
def test_planted_effect_found_verified_and_null_rejected(duck, method, planted, null, outcome):
    s = spec(method, planted, outcome=outcome)
    prim = run_analysis(s, duck)
    assert prim.stat.supported is True, prim.stat
    assert prim.stat.method == method and len(prim.query_ids) == 1
    ver = verify_analysis(s, duck, prim.stat)
    assert ver.agrees is True and ver.stat.supported is True, ver.stat
    ctrl = run_analysis(spec(method, null, outcome=outcome), duck)
    assert ctrl.stat.supported is False, ctrl.stat
    assert verify_analysis(spec(method, null, outcome=outcome), duck, ctrl.stat).agrees is True


def test_seasonality_reads_peak_and_trough(duck):
    st = run_analysis(spec("seasonality", "seasonal"), duck).stat
    hl = st.highlights
    assert hl["season_length"] == 12 and hl["seasonal_strength"] >= 0.8
    assert hl["peak_season"] in ("March", "April", "May") and hl["trough_season"] in ("September", "October", "November")
    assert st.details["engine"] in ("stl", "classical")


def test_level_shift_finds_the_planted_month(duck):
    st = run_analysis(spec("level_shift", "shift"), duck).stat
    assert st.highlights["change_period"].startswith("2023-03")  # month 14 of a series starting January 2022
    assert st.highlights["direction"] == "increase" and 0.3 <= st.highlights["pct_change"] <= 0.55


def test_forecast_backtest_reported(duck):
    """The forecast is judged by a rolling-origin backtest whose every fold is reported."""
    out = run_analysis(spec("forecast_backtest", "trend"), duck)
    st = out.stat
    bt = st.details["backtest"]
    assert bt["n_folds"] == st.highlights["backtest_folds"] >= 3 and len(bt["folds"]) == bt["n_folds"]
    for i, fold in enumerate(bt["folds"]):
        assert {"origin", "train_n", "horizon", "mae", "naive_mae", "coverage", "engine"} <= set(fold)
        assert fold["train_n"] == fold["origin"]  # fitted only on periods before its origin
        if i:
            assert fold["origin"] == bt["folds"][i - 1]["origin"] + bt["horizon"]  # rolling, non-overlapping
    assert bt["relative_mae"] < 1 and 0 <= bt["coverage"] <= 1 and bt["p_value"] < 0.05
    assert st.highlights["relative_mae"] == round(bt["relative_mae"], 4)
    forecast = [g for g in st.groups if g["kind"] == "forecast"]
    assert len(forecast) == bt["horizon"] and all(g["lower"] <= g["forecast"] <= g["upper"] for g in forecast)
    assert out.table["columns"][:4] == ["fold", "origin", "train_n", "horizon"] and len(out.table["rows"]) == bt["n_folds"]


def test_distribution_shift_quotes_psi_and_medians(duck):
    st = run_analysis(spec("distribution_shift", "drift", outcome=True), duck).stat
    assert st.highlights["psi"] >= 0.1 and st.highlights["latest_median"] > st.highlights["reference_median"]
    assert st.highlights["latest_period"].startswith("2023-06")


@pytest.mark.parametrize(("method", "planted", "outcome"), [(m, p, o) for m, p, _, o in CASES])
def test_template_binds_and_is_not_causal(duck, method, planted, outcome):
    s = spec(method, planted, outcome=outcome)
    stat = run_analysis(s, duck).stat.model_dump()
    title, text = template_text(stat, s.model_dump())
    facts = methods.get(method).facts(s.model_dump(), stat)
    b = bind_finding(s.model_dump(), stat, facts, title, text, claim_label="observed")
    assert b.ok, (text, b.problems)
    assert not claims.causal_terms(f"{title} {text}")


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
@pytest.mark.parametrize("method", PACK)
def test_compiled_sql_passes_the_gateway(method, dialect):
    s = spec(method, "seasonal", outcome=method == "distribution_shift")
    cq = sb.compile_spec(s, dialect)
    v = validate_sql(scope(dialect), cq.sql, max_rows=cq.max_rows)
    assert v.referenced_assets == ["bench.events_seasonal"]


def test_tsql_runs_the_same_analysis(duck):
    gw = GatewayRunSQL(TsqlViaDuckRunSQL(duck), scope("tsql"))
    assert run_analysis(spec("seasonality", "seasonal"), gw).stat.supported is True


def test_each_method_is_registered_with_schema_assumptions_and_minimum_sample():
    types = {f"bench.events_{t}": {"opened_at": "datetime", "amount": "numeric"} for t in TABLES}
    for name in PACK:
        m = methods.current().manifests[name]
        body = m.spec or {}
        assert body["claim"] == "observation" and int(body["min_sample"]) > 0
        assert body["inputs"]["time"]["required"] is True and body["outputs"] and body["assumptions"]
        assert m.certification.evidence == "tests/methods/test_timeseries_pack.py"
        assert claims.claim_kind(name) == "observation"
        assert validate_spec(spec(name, "seasonal", outcome=name == "distribution_shift"), scope("duckdb"), types) == []
    assert methods.current().manifests["seasonality"].spec["seasonality"] is True
    no_time = AnalysisSpec(method="seasonality", asset="bench.events_flat")
    assert validate_spec(no_time, scope("duckdb"), types)


def test_too_short_a_series_is_no_data_not_a_finding(duck):
    short = AnalysisSpec(method="seasonality", asset="bench.events_shift",
                         time=Derivation(type="date_trunc", column="opened_at", grain="month"))
    st = run_analysis(short, duck).stat  # 24 months is exactly two seasons: still enough
    assert st.n == 24
    q = AnalysisSpec(method="seasonality", asset="bench.events_shift", filters=[{"column": "opened_at", "op": "<", "value": "2023-01-01"}],
                     time=Derivation(type="date_trunc", column="opened_at", grain="month"))
    st = run_analysis(q, duck).stat
    assert st.supported is False and "two full seasons" in st.warnings[0]


def test_lite_install_falls_back_to_the_classical_decomposition(duck, monkeypatch):
    """Without the `ml` extra (statsmodels), seasonality uses the classical decomposition and says so."""
    from analystos.core import profiles
    from analystos.core.errors import FeatureUnavailable

    def missing(extra, what):
        raise FeatureUnavailable(f"{what} needs the `{extra}` extra")

    monkeypatch.setattr(profiles, "require_extra", missing)
    st = run_analysis(spec("seasonality", "seasonal"), duck).stat
    assert st.details["engine"] == "classical" and st.supported is True
    assert any("classical moving-average decomposition" in w for w in st.warnings)
