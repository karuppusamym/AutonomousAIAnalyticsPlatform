"""The cost ledger (spec S12 §4, FR-052): one priced line per model call, gateway statement, compute task and
artifact write, rolled up per request, run, conversation, user, workspace, tenant and day.

Lines come from the owner tables (model_call.cost_usd; query_execution.scan_bytes and credits; worker_task
usage.cpu_seconds; artifact payload bytes), priced with `config/cost_rates.yaml` (models: their own recorded
USD). Migration 0066 also creates the `cost_ledger` view (security_invoker) with the same unpriced lines for
SQL/BI readers. Everything aggregates in the database and returns at most `MAX_GROUPS` groups (ADR-014);
exports stream in batches. Reads run in the caller's session, so row-level security bounds them to its
tenant; the platform-wide view is the caller's `system_scope`.

Blocked model families (config/models.yaml `blocked_families`, ADR S05) are never routed, but spend already
recorded on them is real: it stays in every total and is flagged `blocked` per model line, and
`blocked_model_usd` reports it separately.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Float, String, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from analystos.core.config import DATA_ROOT
from analystos.core.errors import InvalidInput
from analystos.db.models import AnalysisRun, Artifact, AskTurn, ModelCall, QueryExecution, WorkerTask, Workspace

DIMENSIONS = ("request", "run", "conversation", "user", "workspace", "tenant", "day", "category", "item")
MEASURES = ("cost_usd", "tokens", "scan_bytes", "credits", "cpu_seconds", "storage_bytes", "lines")
MAX_GROUPS = 5000
BILLED_MODEL_STATUSES = ("ok", "error")
PLATFORM = "platform"


@dataclass(frozen=True)
class Rates:
    version: str
    scan_usd_per_tb: float
    credit_usd: float
    cpu_usd_per_hour: float
    storage_usd_per_gb_month: float


@lru_cache(maxsize=4)
def load_rates(path: str | None = None) -> Rates:
    raw = yaml.safe_load(Path(path or DATA_ROOT / "config" / "cost_rates.yaml").read_text(encoding="utf-8"))
    r = raw["rates"]
    return Rates(version=str(raw["version"]), scan_usd_per_tb=float(r["scan_usd_per_tb"]), credit_usd=float(r["credit_usd"]),
                 cpu_usd_per_hour=float(r["cpu_usd_per_hour"]), storage_usd_per_gb_month=float(r["storage_usd_per_gb_month"]))


def _day(session: Session, col: Any) -> Any:
    if session.get_bind().dialect.name == "postgresql":
        return func.to_char(func.date_trunc("day", col), "YYYY-MM-DD")
    return func.substr(cast(col, String), 1, 10)


def _f(expr: Any) -> Any:
    return cast(func.coalesce(expr, 0), Float)


def _zero() -> Any:
    return cast(literal(0), Float)


def _lines(session: Session, since: datetime, until: datetime, workspace_ids: list[str] | None, rates: Rates) -> Any:
    """The union of priced lines (a subquery) with columns DIMENSIONS + MEASURES (+ unit-specific zeros)."""
    def ws_filter(col: Any) -> Any:
        return col.in_(workspace_ids) if workspace_ids else literal(True)

    tenant = func.coalesce(Workspace.tenant_id, literal(PLATFORM))
    model = (
        select(literal("model").label("category"), tenant.label("tenant"), ModelCall.workspace_id.label("workspace"),
               ModelCall.run_id.label("run"), ModelCall.task_id.label("request"), AskTurn.thread_id.label("conversation"),
               func.coalesce(AskTurn.user_id, AnalysisRun.requested_by).label("user"),
               _day(session, ModelCall.created_at).label("day"), ModelCall.model.label("item"),
               _f(ModelCall.cost_usd).label("cost_usd"), _f(ModelCall.input_tokens + ModelCall.output_tokens).label("tokens"),
               _zero().label("scan_bytes"), _zero().label("credits"), _zero().label("cpu_seconds"), _zero().label("storage_bytes"))
        .select_from(ModelCall)
        .outerjoin(Workspace, Workspace.id == ModelCall.workspace_id)
        .outerjoin(AskTurn, AskTurn.id == ModelCall.task_id)
        .outerjoin(AnalysisRun, AnalysisRun.id == ModelCall.run_id)
        .where(ModelCall.created_at >= since, ModelCall.created_at < until, ModelCall.status.in_(BILLED_MODEL_STATUSES),
               ws_filter(ModelCall.workspace_id)))
    actor_user = case((QueryExecution.actor.like("user:%"), func.substr(QueryExecution.actor, 6)), else_=None)
    scan = _f(QueryExecution.scan_bytes)
    credits = _f(QueryExecution.credits)
    query = (
        select(literal("query").label("category"), tenant.label("tenant"), QueryExecution.workspace_id.label("workspace"),
               QueryExecution.run_id.label("run"), QueryExecution.task_id.label("request"), AskTurn.thread_id.label("conversation"),
               func.coalesce(AskTurn.user_id, AnalysisRun.requested_by, actor_user).label("user"),
               _day(session, QueryExecution.created_at).label("day"),
               func.coalesce(QueryExecution.source_id, literal("staged")).label("item"),
               (scan / 1e12 * rates.scan_usd_per_tb + credits * rates.credit_usd).label("cost_usd"), _zero().label("tokens"),
               scan.label("scan_bytes"), credits.label("credits"), _zero().label("cpu_seconds"), _zero().label("storage_bytes"))
        .select_from(QueryExecution)
        .outerjoin(Workspace, Workspace.id == QueryExecution.workspace_id)
        .outerjoin(AskTurn, AskTurn.id == QueryExecution.task_id)
        .outerjoin(AnalysisRun, AnalysisRun.id == QueryExecution.run_id)
        .where(QueryExecution.created_at >= since, QueryExecution.created_at < until, QueryExecution.status == "ok",
               QueryExecution.cache_hit.is_(False), ws_filter(QueryExecution.workspace_id)))
    cpu = _f(WorkerTask.usage["cpu_seconds"].as_float())
    compute = (
        select(literal("compute").label("category"), tenant.label("tenant"), WorkerTask.workspace_id.label("workspace"),
               WorkerTask.run_id.label("run"), WorkerTask.work_order_id.label("request"), cast(literal(None), String).label("conversation"),
               AnalysisRun.requested_by.label("user"), _day(session, WorkerTask.created_at).label("day"),
               WorkerTask.pool.label("item"), (cpu / 3600.0 * rates.cpu_usd_per_hour).label("cost_usd"), _zero().label("tokens"),
               _zero().label("scan_bytes"), _zero().label("credits"), cpu.label("cpu_seconds"), _zero().label("storage_bytes"))
        .select_from(WorkerTask)
        .outerjoin(Workspace, Workspace.id == WorkerTask.workspace_id)
        .outerjoin(AnalysisRun, AnalysisRun.id == WorkerTask.run_id)
        .where(WorkerTask.created_at >= since, WorkerTask.created_at < until, ws_filter(WorkerTask.workspace_id)))
    size = _f(func.length(cast(Artifact.content, String)))
    storage = (
        select(literal("storage").label("category"), tenant.label("tenant"), Artifact.workspace_id.label("workspace"),
               Artifact.run_id.label("run"), cast(literal(None), String).label("request"), cast(literal(None), String).label("conversation"),
               func.coalesce(Artifact.creator_user, AnalysisRun.requested_by).label("user"),
               _day(session, Artifact.created_at).label("day"), Artifact.type.label("item"),
               (size / 1e9 * rates.storage_usd_per_gb_month).label("cost_usd"), _zero().label("tokens"),
               _zero().label("scan_bytes"), _zero().label("credits"), _zero().label("cpu_seconds"), size.label("storage_bytes"))
        .select_from(Artifact)
        .outerjoin(Workspace, Workspace.id == Artifact.workspace_id)
        .outerjoin(AnalysisRun, AnalysisRun.id == Artifact.run_id)
        .where(Artifact.created_at >= since, Artifact.created_at < until, ws_filter(Artifact.workspace_id)))
    return union_all(model, query, compute, storage).subquery("ledger")


def _window(since: date | datetime | None, until: date | datetime | None, days: int) -> tuple[datetime, datetime]:
    from analystos.core.ids import utcnow

    def at(d: date | datetime) -> datetime:
        return d if isinstance(d, datetime) else datetime.combine(d, time.min)

    end = at(until) if until else utcnow()
    start = at(since) if since else end - timedelta(days=max(1, min(int(days), 400)))
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    if end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    if start >= end:
        raise InvalidInput("`since` must be before `until`")
    return start, end


def parse_group_by(raw: str | list[str] | None) -> list[str]:
    keys = [k.strip() for k in (raw.split(",") if isinstance(raw, str) else raw or []) if k and k.strip()]
    bad = [k for k in keys if k not in DIMENSIONS]
    if bad:
        raise InvalidInput(f"group_by must name {', '.join(DIMENSIONS)}; unknown: {', '.join(bad)}")
    return list(dict.fromkeys(keys)) or ["day"]


def _blocked() -> set[str]:
    from analystos.llm.config import load_models_config

    return set(load_models_config().blocked_families)


def spend(session: Session, *, group_by: list[str] | str | None = None, since: date | datetime | None = None,
          until: date | datetime | None = None, days: int = 30, workspace_ids: list[str] | None = None,
          categories: list[str] | None = None, limit: int = 1000) -> dict[str, Any]:
    """Spend grouped by `group_by` over the window, most expensive groups first, plus totals per category."""
    from analystos.llm.config import family

    keys = parse_group_by(group_by)
    start, end = _window(since, until, days)
    rates = load_rates()
    ledger = _lines(session, start, end, workspace_ids, rates)
    where = ledger.c.category.in_(categories) if categories else literal(True)
    sums = [func.sum(ledger.c[m]).label(m) for m in MEASURES if m != "lines"] + [func.count().label("lines")]
    limit = max(1, min(int(limit), MAX_GROUPS))
    rows = session.execute(select(*[ledger.c[k] for k in keys], *sums).where(where)
                           .group_by(*[ledger.c[k] for k in keys]).order_by(func.sum(ledger.c.cost_usd).desc())
                           .limit(limit + 1)).all()
    blocked = _blocked()
    groups = []
    for row in rows[:limit]:
        m = row._mapping
        g = {k: m[k] for k in keys} | {k: _round(m[k]) for k in MEASURES}
        if "item" in keys and ("category" not in keys or m["category"] == "model") and m["item"] and "/" in str(m["item"]):
            g["blocked"] = family(str(m["item"])) in blocked
        groups.append(g)
    totals: dict[str, Any] = {"cost_usd": 0.0, "by_category": {}}
    for cat, cost, lines in session.execute(select(ledger.c.category, func.sum(ledger.c.cost_usd), func.count())
                                            .where(where).group_by(ledger.c.category)).all():
        totals["by_category"][cat] = {"cost_usd": _round(cost), "lines": int(lines)}
        totals["cost_usd"] += float(cost or 0.0)
    totals["cost_usd"] = _round(totals["cost_usd"])
    blocked_usd = 0.0
    if blocked:
        conds = [ledger.c.item.like(f"{fam}/%") for fam in sorted(blocked)]
        blocked_usd = float(session.scalar(select(func.coalesce(func.sum(ledger.c.cost_usd), 0.0))
                                           .where(where, ledger.c.category == "model", or_(*conds))) or 0.0)
    return {"since": start.isoformat(), "until": end.isoformat(), "group_by": keys, "currency": "USD",
            "rates_version": rates.version, "groups": groups, "truncated": len(rows) > limit, "totals": totals,
            "blocked_families": sorted(blocked), "blocked_model_usd": _round(blocked_usd),
            "estimated": {"query": "scan bytes and credits at config/cost_rates.yaml; statements without them cost 0",
                          "compute": "worker_task cpu_seconds x cpu_usd_per_hour",
                          "storage": "artifact payload bytes written, charged once per month"}}


def scope_cost(session: Session, *, run_id: str | None = None, workspace_id: str | None = None,
               since: datetime | None = None) -> float:
    """All-category spend of one run or workspace since a time: what a budget reads (S05/S06)."""
    if not run_id and not workspace_id:
        raise InvalidInput("scope_cost needs a run or a workspace")
    start, end = _window(since, None, 400 if since is None else 1)
    ledger = _lines(session, start, end, [workspace_id] if workspace_id else None, load_rates())
    cond = ledger.c.run == run_id if run_id else literal(True)
    return float(session.scalar(select(func.coalesce(func.sum(ledger.c.cost_usd), 0.0)).where(cond)) or 0.0)


EXPORT_COLUMNS = ("day", "tenant", "workspace", "run", "request", "conversation", "user", "category", "item", *MEASURES)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    try:
        first = datetime.strptime(month, "%Y-%m")
    except ValueError:
        raise InvalidInput("month must be YYYY-MM") from None
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    from datetime import UTC

    return first.replace(tzinfo=UTC), nxt.replace(tzinfo=UTC)


def export_rows(session: Session, month: str, *, workspace_ids: list[str] | None = None,
                batch: int = 5000) -> Iterator[list[dict[str, Any]]]:
    """A month's ledger at full grain (one row per day x tenant x workspace x run x request x conversation x user x
    category x item), streamed in batches: never the whole month in memory."""
    start, end = month_bounds(month)
    ledger = _lines(session, start, end, workspace_ids, load_rates())
    dims = [ledger.c[k] for k in EXPORT_COLUMNS if k in DIMENSIONS]
    sums = [func.sum(ledger.c[m]).label(m) for m in MEASURES if m != "lines"] + [func.count().label("lines")]
    stmt = select(*dims, *sums).group_by(*dims).order_by(ledger.c.day, ledger.c.tenant, ledger.c.workspace)
    result = session.execute(stmt.execution_options(stream_results=True, yield_per=batch))
    for part in result.partitions(batch):
        yield [{k: (_round(r._mapping[k]) if k in MEASURES else r._mapping[k]) for k in EXPORT_COLUMNS} for r in part]


def export_csv(session: Session, month: str, **kw: Any) -> Iterator[str]:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    yield buf.getvalue()
    for rows in export_rows(session, month, **kw):
        buf.seek(0)
        buf.truncate()
        writer.writerows(rows)
        yield buf.getvalue()


def export_file(session: Session, month: str, out_dir: Path, fmt: str = "parquet", **kw: Any) -> dict[str, Any]:
    """Write `cost-ledger-<month>.<fmt>` (parquet via pyarrow, batch by batch, or csv); returns path and counts."""
    if fmt not in ("parquet", "csv"):
        raise InvalidInput("format must be parquet or csv")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cost-ledger-{month}.{fmt}"
    rows = 0
    cost = 0.0
    if fmt == "csv":
        with path.open("w", encoding="utf-8", newline="") as fh:
            for chunk in export_csv(session, month, **kw):
                fh.write(chunk)
        with path.open(encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                rows += 1
                cost += float(rec["cost_usd"] or 0)
    else:
        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = pa.schema([(k, pa.string()) for k in EXPORT_COLUMNS if k not in MEASURES]
                           + [(m, pa.int64() if m == "lines" else pa.float64()) for m in MEASURES])
        with pq.ParquetWriter(path, schema) as writer:
            for batch in export_rows(session, month, **kw):
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                rows += len(batch)
                cost += sum(float(r["cost_usd"] or 0) for r in batch)
    return {"path": str(path), "month": month, "format": fmt, "rows": rows, "cost_usd": _round(cost),
            "rates_version": load_rates().version}


def _round(v: Any) -> float | int:
    if v is None:
        return 0.0
    if isinstance(v, int):
        return v
    return round(float(v), 9)  # storage lines are fractions of a cent


__all__ = ["DIMENSIONS", "MEASURES", "export_csv", "export_file", "export_rows", "load_rates", "month_bounds",
           "parse_group_by", "scope_cost", "spend", "and_"]
