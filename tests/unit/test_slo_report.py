"""scripts/load/slo_report.py pieces that need no stack: host-load sampling and the SLO verdicts (S12-T6)."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "load"))
slo_report = pytest.importorskip("slo_report")


def test_host_load_samples_the_whole_host():
    times = slo_report._cpu_times()
    if times is None:
        pytest.skip("no host CPU counters here")
    busy, total = times
    assert 0 <= busy <= total
    with slo_report.HostLoad(every=0.2) as load:
        time.sleep(0.9)
    summary = load.summary()
    assert summary["n"] >= 2 and 0 <= summary["mean_busy"] <= 1 and summary["max_busy"] <= 1
    assert "samples" in slo_report._load_text(summary)
    assert slo_report._load_text({"n": 0}) == "not sampled"


def test_refusals_count_against_the_latency_slo_and_a_slow_ask_is_a_miss():
    slo = slo_report.load_slo()
    rec = slo_report.Recorder()
    for i in range(19):
        rec.add("ask", 0.2, 200, {"outcome": "answered" if i % 2 else "refused:budget_exceeded"})
    rec.add("ask", 9.0, 200, {"outcome": "answered"})
    client = slo_report.client_slis(rec)
    assert client["ask"]["outcomes"] == {"answered": 10, "refused:budget_exceeded": 10}
    runs = {"outcomes": {"WAITING_USER": 2}, "not_finished": 0, "seconds": {"p95": 10.0}}
    verdict = slo_report.evaluate(client, {}, runs, slo)
    assert verdict["ask_latency"]["client"] == 0.95 and verdict["ask_latency"]["met"] is True
    assert verdict["run_success"]["met"] is True and verdict["availability"]["met"] is True
    runs["not_finished"] = 2  # a run still going at the deadline is not a success
    assert slo_report.evaluate(client, {}, runs, slo)["run_success"]["met"] is False
