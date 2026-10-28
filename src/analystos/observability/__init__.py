"""Platform observability (spec S12 §2): Prometheus metrics (`metrics`) and OpenTelemetry spans (`telemetry`).

Both are safe to call from any code path: a metric with an unknown label set or a tracing backend that is
missing never raises into the caller. Span attributes and metric labels pass an allowlist, so prompts, SQL
literals and result rows cannot reach a trace or a time series (hashes only).
"""
from __future__ import annotations

from analystos.observability.metrics import inc, observe, render, set_gauge
from analystos.observability.telemetry import record_span, span

__all__ = ["inc", "observe", "record_span", "render", "set_gauge", "span"]
