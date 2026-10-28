"""Prometheus metrics without a client library (spec S12 §2).

Every metric is declared once in `CATALOG` (name, type, labels, help); the Grafana dashboards and alert rules
shipped in the Helm chart may only reference these names (tests/unit/test_observability.py checks). Each
metric keeps at most `MAX_SERIES` label sets; further ones land in one `__overflow__` series, so a caller
passing an unbounded value (a workspace id, a path) cannot grow memory without bound (ADR-014).

Several processes per pod (uvicorn workers, the compute pool's children) each count on their own. With
`ANALYSTOS_METRICS_DIR` set, every process writes its snapshot to `<dir>/<pid>.json` (at most every
`FLUSH_SECONDS`, and at exit) and `render()` merges all of them: counters and histograms add up, gauges take
the maximum of the processes that wrote within `GAUGE_TTL_SECONDS`. Without it, a scrape sees one process.
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

Kind = Literal["counter", "histogram", "gauge"]
MAX_SERIES = 1000
OVERFLOW = "__overflow__"
FLUSH_SECONDS = 2.0
GAUGE_TTL_SECONDS = 300.0
_LABEL_VALUE = re.compile(r"[^A-Za-z0-9_.:/@+-]")

LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0)
RUN_BUCKETS = (10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0, 7200.0)
STEP_BUCKETS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0)
APPROVAL_BUCKETS = (60.0, 300.0, 900.0, 3600.0, 4 * 3600.0, 24 * 3600.0, 72 * 3600.0)


@dataclass(frozen=True)
class Spec:
    kind: Kind
    labels: tuple[str, ...]
    help: str
    buckets: tuple[float, ...] = ()


CATALOG: dict[str, Spec] = {
    "analystos_http_requests_total": Spec("counter", ("class", "method", "status"), "HTTP requests by route class and status"),
    "analystos_http_request_duration_seconds": Spec(
        "histogram", ("class", "method"), "Time to the response start (SSE: to the stream opening) by route class",
        LATENCY_BUCKETS),
    "analystos_runs_total": Spec("counter", ("outcome",), "Analysis runs finished, by terminal status"),
    "analystos_run_duration_seconds": Spec("histogram", ("outcome",), "Run wall clock from start to finish", RUN_BUCKETS),
    "analystos_run_steps": Spec("histogram", ("outcome",), "Steps (tasks) a finished run had", STEP_BUCKETS),
    "analystos_gateway_statements_total": Spec("counter", ("status",), "QueryGateway statements by audit status"),
    "analystos_gateway_rejections_total": Spec("counter", ("reason",), "Statements the gateway refused, by error code"),
    "analystos_model_calls_total": Spec("counter", ("provider", "model", "purpose", "status", "rung"),
                                        "Model calls by provider, model, purpose, status and answering rung"),
    "analystos_model_tokens_total": Spec("counter", ("provider", "model", "direction"), "Model tokens (input/output)"),
    "analystos_model_cost_usd_total": Spec("counter", ("provider", "model"), "Recorded model spend in USD"),
    "analystos_model_fallbacks_total": Spec("counter", ("provider", "purpose", "reason"),
                                            "Calls made because an earlier model failed or its answer was rejected"),
    "analystos_model_call_duration_seconds": Spec("histogram", ("provider", "purpose"), "Provider call latency",
                                                  LATENCY_BUCKETS),
    "analystos_cache_requests_total": Spec("counter", ("cache", "result"), "Cache lookups by cache and hit/miss"),
    "analystos_approval_latency_seconds": Spec("histogram", ("action", "decision"),
                                               "Time from an approval request to its decision", APPROVAL_BUCKETS),
    "analystos_void_sweeps_total": Spec("counter", ("outcome",), "Verification (VOID) sweeps run"),
    "analystos_findings_voided_total": Spec("counter", (), "Findings voided by sweeps because their data changed"),
    "analystos_desk_resolutions_total": Spec("counter", ("mode",), "Request Desk requests resolved (auto or analyst)"),
    "analystos_rls_denied_total": Spec("counter", ("where",), "Statements Postgres row-level security refused"),
    "analystos_budget_used_ratio": Spec("gauge", ("scope_kind", "scope"), "Spend used / budget for a capped scope"),
    "analystos_build_info": Spec("gauge", ("version", "component"), "Constant 1, labelled with the build"),
}


@dataclass
class _Series:
    value: float = 0.0
    buckets: list[float] = field(default_factory=list)
    count: float = 0.0
    total: float = 0.0
    updated: float = 0.0


class Registry:
    def __init__(self, catalog: dict[str, Spec] | None = None) -> None:
        self.catalog = dict(catalog or CATALOG)
        self._data: dict[str, dict[tuple[str, ...], _Series]] = {name: {} for name in self.catalog}
        self._lock = threading.Lock()
        self._dirty = False
        self._flushed = 0.0
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ recording
    def _key(self, name: str, labels: dict[str, Any]) -> tuple[Spec, tuple[str, ...]] | None:
        spec = self.catalog.get(name)
        if spec is None:
            if name not in self._warned:
                self._warned.add(name)
                log.warning("metric %s is not in the catalog; ignored", name)
            return None
        return spec, tuple(_clean(labels.get(label)) for label in spec.labels)

    def _series(self, name: str, spec: Spec, key: tuple[str, ...]) -> _Series:
        table = self._data[name]
        series = table.get(key)
        if series is None:
            if len(table) >= MAX_SERIES:
                key = tuple(OVERFLOW for _ in spec.labels)
                series = table.get(key)
            if series is None:
                series = table[key] = _Series(buckets=[0.0] * len(spec.buckets))
        return series

    def inc(self, name: str, value: float = 1.0, **labels: Any) -> None:
        found = self._key(name, labels)
        if found is None or value < 0:
            return
        spec, key = found
        with self._lock:
            s = self._series(name, spec, key)
            s.value += value
            s.updated = time.time()
            self._dirty = True
        self._maybe_flush()

    def observe(self, name: str, value: float, **labels: Any) -> None:
        found = self._key(name, labels)
        if found is None or value is None or math.isnan(value):
            return
        spec, key = found
        with self._lock:
            s = self._series(name, spec, key)
            for i, bound in enumerate(spec.buckets):
                if value <= bound:
                    s.buckets[i] += 1
            s.count += 1
            s.total += value
            s.updated = time.time()
            self._dirty = True
        self._maybe_flush()

    def set_gauge(self, name: str, value: float, **labels: Any) -> None:
        found = self._key(name, labels)
        if found is None:
            return
        spec, key = found
        with self._lock:
            s = self._series(name, spec, key)
            s.value = float(value)
            s.updated = time.time()
            self._dirty = True
        self._maybe_flush()

    def reset(self) -> None:
        with self._lock:
            self._data = {name: {} for name in self.catalog}

    # ------------------------------------------------------------------ multi-process snapshots
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {name: [[list(k), s.value, list(s.buckets), s.count, s.total, s.updated] for k, s in table.items()]
                    for name, table in self._data.items() if table}

    def _maybe_flush(self, force: bool = False) -> None:
        directory = metrics_dir()
        if directory is None or not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._flushed < FLUSH_SECONDS:
            return
        self._flushed = now
        self._dirty = False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{os.getpid()}.json"
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot()), encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:  # metrics never break the caller
            log.debug("metrics snapshot not written: %s", exc)

    def merged(self) -> dict[str, dict[tuple[str, ...], _Series]]:
        """This process's series plus every other process's snapshot in ANALYSTOS_METRICS_DIR."""
        own = self.snapshot()
        snapshots = [own]
        directory = metrics_dir()
        if directory is not None and directory.is_dir():
            mine = f"{os.getpid()}.json"
            for path in sorted(directory.glob("*.json"))[:256]:
                if path.name == mine:
                    continue
                try:
                    snapshots.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue
        now = time.time()
        out: dict[str, dict[tuple[str, ...], _Series]] = {name: {} for name in self.catalog}
        for snap in snapshots:
            for name, rows in snap.items():
                spec = self.catalog.get(name)
                if spec is None:
                    continue
                table = out[name]
                for labels, value, buckets, count, total, updated in rows:
                    key = tuple(labels)
                    if spec.kind == "gauge":
                        if now - float(updated or 0) > GAUGE_TTL_SECONDS and snap is not own:
                            continue
                        cur = table.get(key)
                        if cur is None or value > cur.value:
                            table[key] = _Series(value=value, updated=updated)
                        continue
                    cur = table.setdefault(key, _Series(buckets=[0.0] * len(spec.buckets)))
                    cur.value += value
                    cur.count += count
                    cur.total += total
                    for i, b in enumerate(buckets[: len(cur.buckets)]):
                        cur.buckets[i] += b
        return out

    # ------------------------------------------------------------------ exposition
    def render(self) -> str:
        self._maybe_flush(force=True)
        lines: list[str] = []
        for name, table in self.merged().items():
            spec = self.catalog[name]
            lines.append(f"# HELP {name} {spec.help}")
            lines.append(f"# TYPE {name} {spec.kind}")
            for key, s in sorted(table.items()):
                labels = list(zip(spec.labels, key, strict=True))
                if spec.kind == "histogram":
                    for bound, n in zip(spec.buckets, s.buckets, strict=True):
                        lines.append(f"{name}_bucket{_fmt([*labels, ('le', _num(bound))])} {_num(n)}")
                    lines.append(f"{name}_bucket{_fmt([*labels, ('le', '+Inf')])} {_num(s.count)}")
                    lines.append(f"{name}_sum{_fmt(labels)} {_num(s.total)}")
                    lines.append(f"{name}_count{_fmt(labels)} {_num(s.count)}")
                else:
                    lines.append(f"{name}{_fmt(labels)} {_num(s.value)}")
        return "\n".join(lines) + "\n"


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    return _LABEL_VALUE.sub("_", text)[:80]


def _fmt(labels: list[tuple[str, str]]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in labels)
    return "{" + inner + "}"


def _num(value: float) -> str:
    if value == math.inf:
        return "+Inf"
    return repr(int(value)) if float(value).is_integer() else repr(float(value))


def metrics_dir() -> Path | None:
    raw = os.environ.get("ANALYSTOS_METRICS_DIR", "").strip()
    return Path(raw) if raw else None


REGISTRY = Registry()
atexit.register(lambda: REGISTRY._maybe_flush(force=True))


def inc(name: str, value: float = 1.0, **labels: Any) -> None:
    try:
        REGISTRY.inc(name, value, **labels)
    except Exception:  # noqa: BLE001 - observability never breaks the caller
        log.debug("metric %s not recorded", name, exc_info=True)


def observe(name: str, value: float, **labels: Any) -> None:
    try:
        REGISTRY.observe(name, float(value), **labels)
    except Exception:  # noqa: BLE001
        log.debug("metric %s not recorded", name, exc_info=True)


def set_gauge(name: str, value: float, **labels: Any) -> None:
    try:
        REGISTRY.set_gauge(name, float(value), **labels)
    except Exception:  # noqa: BLE001
        log.debug("metric %s not recorded", name, exc_info=True)


def render() -> str:
    return REGISTRY.render()


# Gateway refusals as a bounded reason (the validator's messages are prose written for the model).
_REASONS = (("parse", "could not parse"), ("empty", "is empty"), ("multiple_statements", "single"),
            ("write", "not allowed"), ("write", "read-only"), ("restricted_column", "restricted"),
            ("restricted_column", "column"), ("out_of_scope", "not in scope"), ("out_of_scope", "not authorized"),
            ("out_of_scope", "unknown table"), ("out_of_scope", "relation"), ("function", "function"),
            ("budget", "budget"), ("cost", "cost"), ("timeout", "timeout"), ("policy", "policy"), ("abac", "abac"))


def gateway_reason(exc: BaseException) -> str:
    details = getattr(exc, "details", None) or {}
    rule = details.get("rule") or details.get("reason_code") if isinstance(details, dict) else None
    if isinstance(rule, str) and re.fullmatch(r"[a-z_]{1,40}", rule):
        return rule
    text = str(getattr(exc, "message", exc)).lower()
    for reason, needle in _REASONS:
        if needle in text:
            return reason
    return str(getattr(exc, "code", "other"))[:40]


def rls_denied(exc: BaseException, where: str) -> bool:
    """Count an exception that is Postgres refusing a row under RLS (ADR-009); True when it was one."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        sqlstate = getattr(cur, "sqlstate", None) or getattr(getattr(cur, "orig", None), "sqlstate", None)
        text = str(cur).lower()
        if (sqlstate == "42501" and "row-level security" in text) or "violates row-level security" in text:
            inc("analystos_rls_denied_total", where=where)
            return True
        cur = cur.__cause__ or cur.__context__ or getattr(cur, "orig", None)
    return False
