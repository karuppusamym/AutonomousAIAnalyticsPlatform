"""S09-T3: the anti-noise gate as pure functions — safe conditions, durations, quiet hours (explicit zone, DST),
hysteresis that does not flap, debounce, cooldown, frequency caps and correlation values. No database."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from analystos.core.errors import InvalidInput
from analystos.triggers import conditions, noise

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SPEC_CONDITION = {"enter": "observed >= baseline + 0.02 AND n >= 1000", "exit": "observed < baseline + 0.01"}


# ------------------------------------------------------------------------------------ conditions
def test_condition_from_the_spec_example_evaluates():
    env = {"observed": 0.05, "baseline": 0.02, "n": 1500}
    assert conditions.evaluate(SPEC_CONDITION["enter"], env) is True
    assert conditions.evaluate(SPEC_CONDITION["enter"], {**env, "n": 999}) is False
    assert conditions.evaluate(SPEC_CONDITION["exit"], {**env, "observed": 0.025}) is True
    assert conditions.names(SPEC_CONDITION["enter"]) == {"observed", "baseline", "n"}


def test_missing_value_makes_a_condition_unknown_not_true():
    assert conditions.evaluate("observed > 1", {}) is None
    assert conditions.evaluate("observed / n > 1", {"observed": 1, "n": 0}) is None
    assert conditions.evaluate("abs(observed - baseline) > 0.1 or not flag", {"observed": 0.5, "baseline": 0.1,
                                                                              "flag": True}) is True


@pytest.mark.parametrize("bad", ["__import__('os').system('x')", "observed.__class__", "[x for x in y]",
                                 "open('f')", "observed if n else 0", "'text' == observed", "lambda: 1", ""])
def test_conditions_refuse_anything_but_arithmetic_and_logic(bad):
    with pytest.raises(InvalidInput):
        conditions.parse(bad)


# ------------------------------------------------------------------------------------ configuration
def test_parse_validates_every_setting():
    cfg = noise.parse({"condition": SPEC_CONDITION, "debounce": "2h", "cooldown": "30m",
                       "quiet_hours": {"window": "22:00-07:00", "tz": "America/New_York"},
                       "correlation_key": ["metric", "processor"], "caps": {"per_dataset_per_day": 5}})
    assert cfg.debounce == timedelta(hours=2) and cfg.cooldown == timedelta(minutes=30)
    assert cfg.quiet_hours.tz == "America/New_York" and cfg.correlation_key == ("metric", "processor")
    assert noise.parse_duration(90, "x") == timedelta(seconds=90) and noise.parse_duration("1d", "x") == timedelta(days=1)
    for bad in ({"debounce": "soon"}, {"cooldown": "40d"}, {"quiet_hours": "22:00-07:00"},
                {"quiet_hours": {"window": "25:00-07:00", "tz": "UTC"}}, {"quiet_hours": {"window": "22:00-07:00", "tz": "Mars/Base"}},
                {"condition": {"exit": "x < 1"}}, {"condition": {"enter": "import os"}}, {"caps": {"per_hour": 3}},
                {"caps": {"per_dataset_per_day": 0}}, {"correlation_key": ["a", "b"] * 4}):
        with pytest.raises(InvalidInput):
            noise.parse(bad)
    assert noise.parse({"quiet_hours": "22:00-07:00", "timezone": "Europe/London"}).quiet_hours.tz == "Europe/London"
    assert not noise.configured({"metric": "x", "op": ">"}) and noise.configured({"debounce": "1h"})


def test_quiet_hours_window_wraps_midnight_in_its_own_zone():
    q = noise.parse_quiet_hours({"window": "22:00-07:00", "tz": "America/New_York"}, None)
    # 03:00 UTC on 27 Oct = 23:00 in New York (EDT): inside; the window ends 07:00 New York = 11:00 UTC.
    night = datetime(2026, 10, 27, 3, 0, tzinfo=UTC)
    assert q.active(night) and q.ends(night) == datetime(2026, 10, 27, 11, 0, tzinfo=UTC)
    assert not q.active(datetime(2026, 10, 27, 15, 0, tzinfo=UTC))  # 11:00 in New York
    # Across the DST change (1 Nov 2026, 02:00 EDT -> EST) the end is 07:00 EST = 12:00 UTC.
    assert q.ends(datetime(2026, 11, 1, 3, 30, tzinfo=UTC)) == datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    critical = noise.parse_quiet_hours({"window": "22:00-07:00", "tz": "UTC", "allow_critical": True}, None)
    late = datetime(2026, 10, 27, 23, 0, tzinfo=UTC)
    assert critical.defers(late, "critical") is None and critical.defers(late, "warning") is not None


# ------------------------------------------------------------------------------------ the gate
def _run(cfg: noise.NoiseConfig, readings: list[dict], start: datetime = T0, step: timedelta = timedelta(hours=1)):
    state: dict = {}
    actions = []
    for i, result in enumerate(readings):
        env = noise.variables(result)
        breach, exited = noise.breach_and_exit(cfg, result, env)
        d = noise.decide(cfg, state, breach=breach, exited=exited, now=start + i * step)
        state = d.state
        actions.append(d.action)
    return actions, state


def test_hysteresis_no_flap():
    """A failure rate oscillating between the exit (base + 1pt) and enter (base + 2pt) thresholds fires once."""
    cfg = noise.parse({"condition": SPEC_CONDITION})
    rates = [0.020, 0.045, 0.035, 0.041, 0.031, 0.044, 0.032, 0.025, 0.046]
    actions, _ = _run(cfg, [{"value": r, "baseline_median": 0.02, "n": 5000} for r in rates])
    assert actions == ["ok", "fire", "hold", "hold", "hold", "hold", "hold", "clear", "fire"]
    # Without hysteresis the same series flaps: the monitor's own verdict, or an enter-only condition, fires 4 times.
    plain, _ = _run(noise.parse({"debounce": 0}), [{"alert": r >= 0.04} for r in rates])
    assert plain.count("fire") == 4
    flappy = noise.parse({"condition": {"enter": "observed >= 0.04"}})
    assert _run(flappy, [{"value": r} for r in rates])[0].count("fire") == 4


def test_debounce_needs_the_condition_to_persist():
    cfg = noise.parse({"condition": {"enter": "observed > 10"}, "debounce": "2h"})
    spike, _ = _run(cfg, [{"value": 50}, {"value": 1}, {"value": 2}])
    assert spike == ["pending", "ok", "ok"]  # a one-reading spike never alerts
    sustained, state = _run(cfg, [{"value": 50}, {"value": 60}, {"value": 70}])
    assert sustained == ["pending", "pending", "fire"] and state["phase"] == "alerting"


def test_cooldown_suppresses_reentry_then_allows_it():
    cfg = noise.parse({"condition": {"enter": "observed > 10", "exit": "observed < 5"}, "cooldown": "3h"})
    actions, state = _run(cfg, [{"value": 20}, {"value": 1}, {"value": 20}, {"value": 20}, {"value": 20}])
    assert actions == ["fire", "clear", "suppressed", "fire", "hold"]  # re-entry at +2h suppressed, at +3h allowed
    assert state["suppressed"] == 1 and state["fired"] == 2


def test_quiet_hours_defer():
    cfg = noise.parse({"condition": {"enter": "observed > 10"}, "quiet_hours": {"window": "22:00-07:00", "tz": "UTC"}})
    night = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
    d = noise.decide(cfg, {}, breach=True, exited=False, now=night)
    assert d.fire and d.defer_until == datetime(2026, 10, 28, 7, 0, tzinfo=UTC)
    day = noise.decide(cfg, {}, breach=True, exited=False, now=datetime(2026, 10, 27, 9, 0, tzinfo=UTC))
    assert day.fire and day.defer_until is None


def test_cap_and_unknown_readings():
    cfg = noise.parse({"condition": {"enter": "observed > 10"}, "caps": {"per_tenant_per_day": 3}})
    capped = noise.decide(cfg, {}, breach=True, exited=False, now=T0, cap_hit="per_tenant_per_day=3")
    assert capped.action == "suppressed" and "frequency cap" in capped.reason
    unknown = noise.decide(cfg, {"phase": "alerting"}, breach=None, exited=None, now=T0)
    assert unknown.action == "unknown" and unknown.keep_open and unknown.phase == "alerting"


def test_correlation_values_come_from_payload_reading_scope_and_config():
    values = noise.correlation_values(("metric", "processor", "region", "missing"),
                                      monitor_config={"metric": "payment_failure_rate", "scope": {"filters": {"region": "US"}}},
                                      result={"value": 1}, payload={"processor": "adyen"})
    assert values == {"metric": "payment_failure_rate", "processor": "adyen", "region": "US", "missing": None}
