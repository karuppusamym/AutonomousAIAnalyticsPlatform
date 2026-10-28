"""OpenTelemetry spans (spec S12 §2): api → desk → context → router → gateway → compute → publisher.

Attributes are limited to `ATTRIBUTES` (tenant, workspace, request_id, run_id, step, purpose, model, rung and a
few bounded status fields). A string value must be a short token (no spaces): anything else — a prompt, a SQL
statement, a row — is replaced by `sha256:<16 hex>`, so a trace can be correlated but never read back. The
exception of a failed span is recorded by type only (messages can quote SQL or data).

The OpenTelemetry API ships with FastAPI; without the SDK (`analystos[otel]`) or with `ANALYSTOS_OTEL_ENABLED`
unset, every span is a no-op. `setup_tracing` installs the SDK with the OTLP/HTTP exporter, which reads the
standard `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` variables.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger(__name__)

ATTRIBUTES = frozenset({
    "tenant", "workspace", "request_id", "run_id", "step", "purpose", "model", "rung",
    "provider", "status", "outcome", "reason", "queue", "pool", "class", "method", "http.status_code",
    "kind", "action", "decision", "cache", "attempt", "rows", "statement_hash", "prompt_hash",
})
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+-]{0,120}$")
_sinks: list[Callable[[str, dict[str, Any], float, str | None], None]] = []
_state: dict[str, Any] = {"enabled": False, "reason": "not configured"}


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def scrub(attrs: dict[str, Any]) -> dict[str, Any]:
    """Allowlisted attributes only; strings that are not short tokens become hashes."""
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key not in ATTRIBUTES or value is None:
            continue
        if isinstance(value, bool | int | float) or (isinstance(value, str) and _TOKEN.match(value)):
            out[key] = value
        else:
            out[key] = digest(value)
    return out


def context_attrs() -> dict[str, Any]:
    """The ambient correlation: bound tenant, request id, run id."""
    from analystos.core.logging import correlation_id, run_id_var

    try:
        from analystos.tenancy.context import current_tenant

        tenant = current_tenant()
    except Exception:  # noqa: BLE001
        tenant = None
    return {"tenant": tenant, "request_id": correlation_id.get(), "run_id": run_id_var.get()}


def add_sink(fn: Callable[[str, dict[str, Any], float, str | None], None]) -> None:
    """Receive every finished span as (name, scrubbed attributes, seconds, error type). Tests and debugging."""
    _sinks.append(fn)


def remove_sink(fn: Callable[..., None]) -> None:
    if fn in _sinks:
        _sinks.remove(fn)


def _tracer() -> Any:
    try:
        from opentelemetry import trace

        return trace.get_tracer("analystos")
    except Exception:  # noqa: BLE001
        return None


def _emit(name: str, attrs: dict[str, Any], seconds: float, error: str | None) -> None:
    for sink in list(_sinks):
        try:
            sink(name, attrs, seconds, error)
        except Exception:  # noqa: BLE001
            log.debug("span sink failed", exc_info=True)


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[dict[str, Any]]:
    """A span around a block. The yielded dict takes more attributes (scrubbed at the end)."""
    extra: dict[str, Any] = {}
    started = time.perf_counter()
    tracer = _tracer()
    cm = tracer.start_as_current_span(name, record_exception=False, set_status_on_exception=False) if tracer else None
    otel_span = cm.__enter__() if cm is not None else None
    error: str | None = None
    try:
        yield extra
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        final = scrub({**context_attrs(), **attrs, **extra})
        if error:
            final["error.type"] = error
        if otel_span is not None:
            try:
                otel_span.set_attributes(final)
                if error:
                    from opentelemetry.trace import Status, StatusCode

                    otel_span.set_status(Status(StatusCode.ERROR, error))
            except Exception:  # noqa: BLE001
                pass
        if cm is not None:
            cm.__exit__(None, None, None)
        _emit(name, final, time.perf_counter() - started, error)


def record_span(name: str, seconds: float, *, error: str | None = None, **attrs: Any) -> None:
    """A span for work that already finished (a model call or a statement recorded after the fact),
    ending now and starting `seconds` ago, as a child of the current span."""
    final = scrub({**context_attrs(), **attrs})
    if error:
        final["error.type"] = error
    tracer = _tracer()
    if tracer is not None:
        try:
            end = time.time_ns()
            s = tracer.start_span(name, start_time=end - int(max(seconds, 0.0) * 1e9), attributes=final)
            if error:
                from opentelemetry.trace import Status, StatusCode

                s.set_status(Status(StatusCode.ERROR, error))
            s.end(end_time=end)
        except Exception:  # noqa: BLE001
            log.debug("span %s not recorded", name, exc_info=True)
    _emit(name, final, seconds, error)


def current_ids() -> tuple[str | None, str | None]:
    """(trace id, span id) of the active span as hex, for log lines; (None, None) without one."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
    except Exception:  # noqa: BLE001
        pass
    return None, None


def setup_tracing(service: str) -> dict[str, Any]:
    """Install the OTel SDK + OTLP exporter when ANALYSTOS_OTEL_ENABLED is true and the SDK is importable."""
    if _state.get("enabled"):
        return dict(_state)
    if os.environ.get("ANALYSTOS_OTEL_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        _state.update(enabled=False, reason="ANALYSTOS_OTEL_ENABLED is not set")
        return dict(_state)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        _state.update(enabled=False, reason="opentelemetry SDK not installed (pip install analystos[otel])")
        log.warning("tracing requested but the OpenTelemetry SDK is not installed")
        return dict(_state)
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME") or f"analystos-{service}",
                                "service.namespace": "analystos"})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _state.update(enabled=True, reason="", service=service)
    return dict(_state)


def status() -> dict[str, Any]:
    return dict(_state)
