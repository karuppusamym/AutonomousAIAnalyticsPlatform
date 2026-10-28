"""S12 §2 observability: the Prometheus registry (bounded series, histograms, multi-process merge), span
attributes that never carry prompts / SQL / rows, the request middleware and `/metrics`, the hooks on model
calls and gateway statements, and the shipped alert rules and dashboards naming only real metrics. No services."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from analystos.observability import metrics, telemetry
from analystos.observability.metrics import CATALOG, Registry

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "deploy" / "helm" / "analystos" / "files"
# Metrics the rules and dashboards read from other exporters (Temporal server, kube-state-metrics).
EXTERNAL = {"temporal_approximate_backlog_count", "kube_job_status_failed"}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.delenv("ANALYSTOS_METRICS_DIR", raising=False)
    reg = Registry()
    monkeypatch.setattr(metrics, "REGISTRY", reg)
    return reg


def _sample(text: str, name: str, **labels: str) -> float | None:
    for line in text.splitlines():
        if line.startswith("#") or not line.startswith(name):
            continue
        head, _, value = line.rpartition(" ")
        if head.split("{", 1)[0] != name:
            continue
        got = dict(re.findall(r'(\w+)="([^"]*)"', head))
        if all(got.get(k) == v for k, v in labels.items()):
            return float(value)
    return None


def test_counters_histograms_and_gauges_render_in_prometheus_format(registry):
    metrics.inc("analystos_runs_total", outcome="COMPLETED")
    metrics.inc("analystos_runs_total", 2, outcome="COMPLETED")
    metrics.observe("analystos_http_request_duration_seconds", 0.2, **{"class": "ask", "method": "POST"})
    metrics.observe("analystos_http_request_duration_seconds", 4.0, **{"class": "ask", "method": "POST"})
    metrics.set_gauge("analystos_budget_used_ratio", 0.85, scope_kind="workspace_month", scope="ws_1")
    out = metrics.render()
    assert "# TYPE analystos_runs_total counter" in out
    assert _sample(out, "analystos_runs_total", outcome="COMPLETED") == 3
    assert _sample(out, "analystos_http_request_duration_seconds_bucket", le="0.25") == 1
    assert _sample(out, "analystos_http_request_duration_seconds_bucket", le="3") == 1  # the Ask SLO bound
    assert _sample(out, "analystos_http_request_duration_seconds_bucket", le="+Inf") == 2
    assert _sample(out, "analystos_http_request_duration_seconds_count") == 2
    assert _sample(out, "analystos_budget_used_ratio", scope="ws_1") == 0.85


def test_unknown_metrics_and_bad_values_never_raise(registry):
    metrics.inc("analystos_not_declared_total")
    metrics.inc("analystos_runs_total", -1, outcome="X")  # counters only go up
    metrics.observe("analystos_run_steps", float("nan"), outcome="X")
    assert _sample(metrics.render(), "analystos_runs_total", outcome="X") is None


def test_series_are_bounded_and_label_values_sanitised(registry, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_SERIES", 3)
    for i in range(10):
        metrics.inc("analystos_gateway_rejections_total", reason=f"r{i}")
    metrics.inc("analystos_rls_denied_total", where='api"} evil 1\n')
    out = metrics.render()
    reasons = {m for m in re.findall(r'analystos_gateway_rejections_total\{reason="([^"]*)"\}', out)}
    assert reasons == {"r0", "r1", "r2", metrics.OVERFLOW}
    assert _sample(out, "analystos_gateway_rejections_total", reason=metrics.OVERFLOW) == 7
    assert '"}' not in out.split("analystos_rls_denied_total{where=")[1].split("\n")[0][:-4]


def test_processes_of_one_pod_merge_through_the_metrics_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYSTOS_METRICS_DIR", str(tmp_path))
    other = {"analystos_runs_total": [[["COMPLETED"], 5.0, [], 0.0, 0.0, 9e18]],
             "analystos_run_steps": [[["COMPLETED"], 0.0, [1, 1, 2, 2, 2, 2, 2], 2.0, 9.0, 9e18]],
             "analystos_budget_used_ratio": [[["workspace_month", "ws_1"], 0.9, [], 0.0, 0.0, 9e18]]}
    (tmp_path / "99999.json").write_text(json.dumps(other), encoding="utf-8")
    reg = Registry()
    monkeypatch.setattr(metrics, "REGISTRY", reg)
    metrics.inc("analystos_runs_total", outcome="COMPLETED")
    metrics.observe("analystos_run_steps", 3, outcome="COMPLETED")
    metrics.set_gauge("analystos_budget_used_ratio", 0.5, scope_kind="workspace_month", scope="ws_1")
    out = metrics.render()
    assert _sample(out, "analystos_runs_total", outcome="COMPLETED") == 6  # counters add up across processes
    assert _sample(out, "analystos_run_steps_count", outcome="COMPLETED") == 3
    assert _sample(out, "analystos_budget_used_ratio", scope="ws_1") == 0.9  # gauges: the highest live process
    assert any(p.name.endswith(".json") and p.name != "99999.json" for p in tmp_path.iterdir())  # own snapshot written


def test_span_attributes_are_allowlisted_and_never_carry_text():
    seen = []
    sink = lambda name, attrs, seconds, error: seen.append((name, attrs, error))  # noqa: E731
    telemetry.add_sink(sink)
    try:
        with pytest.raises(ValueError), telemetry.span("gateway.execute", workspace="ws_1", purpose="analysis",
                            statement_hash="SELECT secret_salary FROM payroll WHERE name = 'Ann'",
                            prompt="Summarise these rows: Ann 120000", rows=[[1, 2]]) as extra:
            extra["model"] = "qwen/qwen3-32b"
            raise ValueError("SELECT * FROM payroll failed")
        telemetry.record_span("model.call", 0.25, purpose="sql_generation", prompt_hash="tell me about Ann's salary",
                              model="deepseek/deepseek-v4-flash-0731", rung="llm_large")
    finally:
        telemetry.remove_sink(sink)
    (name, attrs, error), (name2, attrs2, _) = seen
    assert name == "gateway.execute" and error == "ValueError" and attrs["error.type"] == "ValueError"
    assert attrs["workspace"] == "ws_1" and attrs["model"] == "qwen/qwen3-32b"
    assert "prompt" not in attrs  # not on the allowlist
    assert attrs["statement_hash"].startswith("sha256:") and "payroll" not in json.dumps(attrs)
    assert attrs["rows"].startswith("sha256:")  # a non-scalar value becomes a hash
    assert name2 == "model.call" and attrs2["prompt_hash"].startswith("sha256:") and "salary" not in json.dumps(attrs2)
    assert set(attrs) <= telemetry.ATTRIBUTES | {"error.type"}


def test_tracing_stays_off_without_the_flag(monkeypatch):
    monkeypatch.delenv("ANALYSTOS_OTEL_ENABLED", raising=False)
    monkeypatch.setitem(telemetry._state, "enabled", False)
    assert telemetry.setup_tracing("api")["enabled"] is False


def test_rls_denials_and_gateway_reasons_are_classified(registry):
    class PgError(Exception):
        sqlstate = "42501"

    exc = RuntimeError("wrapped")
    exc.__cause__ = PgError('new row violates row-level security policy for table "workspace"')
    assert metrics.rls_denied(exc, "api") is True
    assert metrics.rls_denied(RuntimeError("deadlock detected"), "api") is False
    assert _sample(metrics.render(), "analystos_rls_denied_total", where="api") == 1

    from analystos.core.errors import AnalystOSError

    def err(msg, **details):
        e = AnalystOSError(msg)
        e.details = details
        return e

    assert metrics.gateway_reason(err("DELETE statements are not allowed. Only read-only SELECT queries")) == "write"
    assert metrics.gateway_reason(err("Could not parse the statement as postgres SQL")) == "parse"
    assert metrics.gateway_reason(err("anything", rule="row_limit")) == "row_limit"


def test_model_call_records_feed_metrics_and_a_span(registry, sqlite_db):
    from analystos.llm.router import CallContext
    from analystos.runtime.usage import DbUsageSink

    seen = []
    sink = lambda name, attrs, seconds, error: seen.append((name, attrs))  # noqa: E731
    telemetry.add_sink(sink)
    try:
        DbUsageSink().record(ctx=CallContext(workspace_id="ws_1"), purpose="sql_generation", profile="coding",
                             provider="openrouter", model="qwen/qwen3-32b", status="ok", attempt=2, latency_ms=1500,
                             input_tokens=100, output_tokens=20, cost_usd=0.002, request_hash="a" * 64, error=None,
                             answered_by="llm_large")
    finally:
        telemetry.remove_sink(sink)
    out = metrics.render()
    assert _sample(out, "analystos_model_calls_total", model="qwen/qwen3-32b", status="ok", rung="llm_large") == 1
    assert _sample(out, "analystos_model_tokens_total", direction="input") == 100
    assert _sample(out, "analystos_model_cost_usd_total", provider="openrouter") == pytest.approx(0.002)
    assert _sample(out, "analystos_model_fallbacks_total", purpose="sql_generation", reason="retry") == 1
    assert seen and seen[0][0] == "model.call" and seen[0][1]["prompt_hash"] == "a" * 64


def test_gateway_statements_are_counted_with_a_hashed_span(registry):
    from analystos.gateway.service import _observe_statement

    seen = []
    sink = lambda name, attrs, seconds, error: seen.append(attrs)  # noqa: E731
    telemetry.add_sink(sink)
    try:
        _observe_statement({"status": "ok", "cache_hit": True, "duration_ms": 30, "workspace_id": "ws_1",
                            "sql": "SELECT ssn FROM people", "row_count": 4})
        _observe_statement({"status": "rejected", "duration_ms": 1, "sql": "DROP TABLE people"})
        _observe_statement({"status": "explained", "duration_ms": 1, "sql": "SELECT 1"})
    finally:
        telemetry.remove_sink(sink)
    out = metrics.render()
    assert _sample(out, "analystos_gateway_statements_total", status="ok") == 1
    assert _sample(out, "analystos_gateway_statements_total", status="rejected") == 1
    assert _sample(out, "analystos_cache_requests_total", cache="query_result", result="hit") == 1
    assert len(seen) == 2 and all("people" not in json.dumps(a) for a in seen)


def test_metrics_endpoint_and_request_classes(registry, monkeypatch):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    client = TestClient(app)
    monkeypatch.delenv("ANALYSTOS_METRICS_TOKEN", raising=False)
    client.get("/api/health")
    client.get("/api/no-such-route")
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert _sample(r.text, "analystos_http_requests_total", method="GET", status="404", **{"class": "unmatched"}) == 1
    assert _sample(r.text, "analystos_http_request_duration_seconds_count", **{"class": "health"}) >= 1
    monkeypatch.setenv("ANALYSTOS_METRICS_TOKEN", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_worker_metrics_server_serves_the_registry(registry):
    import urllib.request

    from analystos.observability.server import serve_metrics

    server = serve_metrics("worker-test", port=0) or None
    assert server is None  # port 0 = off (the default without ANALYSTOS_METRICS_PORT)
    from http.server import ThreadingHTTPServer

    from analystos.observability.server import _Handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        metrics.inc("analystos_runs_total", outcome="FAILED")
        body = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/metrics", timeout=5).read().decode()
        assert _sample(body, "analystos_runs_total", outcome="FAILED") == 1
    finally:
        srv.shutdown()


def _names(text: str) -> set[str]:
    found = set(re.findall(r"\b(analystos_[a-z_]+|temporal_[a-z_]+|kube_[a-z_]+)\b", text))
    out = set()
    for n in found:
        for suffix in ("_bucket", "_count", "_sum"):
            if n.endswith(suffix) and n[: -len(suffix)] in CATALOG:
                n = n[: -len(suffix)]
        out.add(n)
    return out


def test_alert_rules_and_dashboards_reference_only_real_metrics():
    rules = (FILES / "prometheus" / "rules.yaml").read_text(encoding="utf-8")
    dashboards = [p.read_text(encoding="utf-8") for p in sorted((FILES / "grafana").glob("*.json"))]
    assert len(dashboards) == 3
    used = _names(rules) | set().union(*(_names(d) for d in dashboards))
    unknown = {n for n in used if n not in CATALOG and n not in EXTERNAL}
    assert unknown == set()
    for name in ("analystos_http_request_duration_seconds", "analystos_runs_total", "analystos_model_calls_total",
                 "analystos_budget_used_ratio", "analystos_rls_denied_total"):
        assert name in used
    for d in dashboards:
        dash = json.loads(d)
        assert dash["uid"].startswith("analystos-") and dash["panels"]
    # the spec's alerts (S12 §2): SLO burn rates, queue backlog, provider errors, spend, audit chain, RLS coverage
    assert all(f'"{slo}"' in rules for slo in ("AskLatency", "RunSuccess", "Availability"))  # BudgetBurnFast/Slow each
    assert "BudgetBurnFast" in rules and "BudgetBurnSlow" in rules
    for alert in ("QueueBacklog", "ModelProviderErrors", "SpendNearBudget", "AuditChainVerifyFailed", "RlsCoverageFailed"):
        assert f"AnalystOS{alert}" in rules
    assert "severity: page" in rules.split("AnalystOSAuditChainVerifyFailed")[1].split("- alert")[0]


def test_every_catalogued_metric_has_a_help_and_valid_name():
    for name, spec in CATALOG.items():
        assert re.fullmatch(r"analystos_[a-z_]+", name) and spec.help
        assert not (spec.kind == "counter" and not name.endswith("_total")), name
        assert (spec.kind == "histogram") == bool(spec.buckets), name
