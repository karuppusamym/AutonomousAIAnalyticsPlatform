"""Usage as a ranking and trust signal (Google Knowledge Catalog review P1-4).

Read from audited `query_execution` rows only: successful statements of the last `WINDOW_DAYS`, the platform's own
measuring statements (crawl, profile, relationships, quality, ...) excluded. Two outputs:

* `asset_usage` / `factor`: distinct users and runs that read an asset, turned into a **bounded** rerank factor in
  [1.0, 1.0 + USAGE_CAP]. It can only lift an entry a little among already-relevant results; it never admits an
  entry the access filter withheld, and an unused asset keeps factor 1.0 (no penalty).
* `common_filters`: which columns of an asset are filtered and with which operator class (`=`, `range`, `in`, ...),
  mined value-free by `skills/query_history` (spec v3 §6.3). Literals never leave this module, and a column the
  caller may not see is never named.

Both are bounded (rows read, statements parsed) so a busy workspace costs the same as a quiet one (ADR-014).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.ids import utcnow

WINDOW_DAYS = 30
MAX_ROWS = 20_000  # audited rows read for counts (metadata columns only, never results)
MAX_MINED = 2_000  # statements parsed for filter patterns
USAGE_CAP = 0.10  # the factor lies in [1.0, 1.10]
SATURATION = 50  # distinct users + runs at which the factor reaches its cap
MAX_FILTERS = 10


@dataclass(frozen=True)
class AssetUsage:
    users: int = 0
    runs: int = 0
    statements: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"users": self.users, "runs": self.runs, "statements": self.statements}


def factor(usage: AssetUsage | None) -> float:
    """1.0 for an unused asset, rising with log(distinct users + runs) to 1 + USAGE_CAP at SATURATION."""
    if usage is None:
        return 1.0
    n = usage.users + usage.runs
    return 1.0 + USAGE_CAP * min(1.0, math.log1p(n) / math.log1p(SATURATION))


def entry_factor(refs: list[Any] | None, factors: dict[str, float] | None) -> float:
    """An entry's factor: the best-used table it references (a ref is `schema.table[.column]`)."""
    if not factors or not refs:
        return 1.0
    return max((factors.get(".".join(str(r).split(".")[:2]).lower(), 1.0) for r in refs), default=1.0)


def _rows(session: Session, workspace_id: str, now: datetime | None, columns: tuple[Any, ...], limit: int) -> list[Any]:
    from analystos.db.models import QueryExecution
    from analystos.semantic.review import PLATFORM_PURPOSES

    since = (now or utcnow()) - timedelta(days=WINDOW_DAYS)
    stmt = (select(*columns).where(QueryExecution.workspace_id == workspace_id, QueryExecution.status == "ok",
                                   QueryExecution.created_at >= since,
                                   *[QueryExecution.purpose.notlike(p) for p in PLATFORM_PURPOSES])
            .order_by(QueryExecution.created_at.desc(), QueryExecution.id).limit(limit))
    return list(session.execute(stmt))


def asset_usage(session: Session, workspace_id: str, *, now: datetime | None = None,
                assets: set[str] | None = None) -> dict[str, AssetUsage]:
    """`{schema.table (lower case): AssetUsage}` over the window, optionally only for `assets`."""
    from analystos.db.models import QueryExecution

    wanted = {a.lower() for a in assets} if assets is not None else None
    users: dict[str, set[str]] = {}
    runs: dict[str, set[str]] = {}
    statements: dict[str, int] = {}
    for actor, run_id, referenced in _rows(session, workspace_id, now, (QueryExecution.actor, QueryExecution.run_id,
                                                                        QueryExecution.referenced_assets), MAX_ROWS):
        for asset in {str(a).lower() for a in referenced or []}:
            if wanted is not None and asset not in wanted:
                continue
            statements[asset] = statements.get(asset, 0) + 1
            if run_id:
                runs.setdefault(asset, set()).add(run_id)
            elif str(actor).startswith("user:"):
                users.setdefault(asset, set()).add(str(actor))
    return {a: AssetUsage(users=len(users.get(a, ())), runs=len(runs.get(a, ())), statements=n)
            for a, n in sorted(statements.items())}


def factors(session: Session, workspace_id: str, *, now: datetime | None = None) -> dict[str, float]:
    return {a: factor(u) for a, u in asset_usage(session, workspace_id, now=now).items()}


def common_filters(session: Session, workspace_id: str, assets: list[str], *, hidden: set[str] | frozenset[str] = frozenset(),
                   dialects: dict[str, str] | None = None, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    """`{asset: [{column, operator, count}]}`: filter columns by operator class over the window's statements that read
    the asset, most frequent first. `hidden` holds `schema.table.column` names (and `*.column`) the caller may not
    see; those are never named. `dialects` maps a source id to the dialect its SQL is parsed in."""
    from analystos.db.models import QueryExecution
    from analystos.skills.query_history import mine

    wanted = {a.lower(): a for a in assets}
    by_dialect: dict[str, list[str]] = {}
    for sql, source_id, referenced in _rows(session, workspace_id, now, (QueryExecution.sql, QueryExecution.source_id,
                                                                         QueryExecution.referenced_assets), MAX_MINED):
        if wanted.keys() & {str(a).lower() for a in referenced or []}:
            by_dialect.setdefault((dialects or {}).get(source_id or "", "postgres"), []).append(sql)
    counts: dict[tuple[str, str, str], int] = {}
    for dialect, statements in sorted(by_dialect.items()):
        for (table, column, op), n in mine(statements, dialect=dialect).filters.items():
            if table in wanted:
                key = (wanted[table], column, op)
                counts[key] = counts.get(key, 0) + n
    hidden_l = {h.lower() for h in hidden}
    out: dict[str, list[dict[str, Any]]] = {}
    for (asset, column, op), n in sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1], kv[0][2])):
        if f"{asset}.{column}".lower() in hidden_l or f"*.{column}".lower() in hidden_l:
            continue
        rows = out.setdefault(asset, [])
        if len(rows) < MAX_FILTERS:
            rows.append({"column": column, "operator": op, "count": n})
    return out
