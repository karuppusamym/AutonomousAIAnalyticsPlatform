"""Result handles (S02-T3, ADR-010): large results leave the gateway as Parquet files, not rows.

A statement's rows stream through a ``ResultSink``. Up to the inline thresholds (256 rows, 64 KB by default)
they stay in memory and the result is returned inline as before. Past either threshold the sink opens a
Parquet writer at ``tenant/{t}/results/{qe_id}.parquet`` in the result store and writes every further batch
there, so the gateway never holds more than one batch. The finished file is hashed (sha256) and recorded as
a ``result_handle`` row (tenant-owned, RLS) with its schema, size, highest column classification and expiry.

Readers (multi-source joins, compute, steps) open a handle only through ``open_dataset``: the row must be
visible to the caller's tenant (RLS), unexpired and unpurged, and the file must still hash to the recorded
sha256. ``purge_expired_results`` (scheduler housekeeping) deletes files whose TTL passed.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any

from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.gateway.types import ResultHandle

_log = get_logger(__name__)

_URI = re.compile(r"^tenant/[A-Za-z0-9_.-]{1,64}/results/[A-Za-z0-9_-]{1,64}\.parquet$")
CLASSIFICATION_ORDER = ("public", "internal", "sensitive", "pii", "restricted")
# DBAPI type codes (PostgreSQL OIDs) whose values keep a numeric/bool Arrow type; everything else is text
# (json_safe already turned dates, uuids and decimals into strings or floats).
PG_TYPE_HINTS = {16: "bool", 20: "int", 21: "int", 23: "int", 26: "int", 700: "float", 701: "float", 1700: "float"}
BATCH_ROWS = 2048


def store_root(settings: Any) -> Path:
    return Path(settings.result_store_dir).expanduser()


def uri_for(tenant_id: str | None, query_id: str) -> str:
    tenant = re.sub(r"[^A-Za-z0-9_.-]", "_", tenant_id or "_none")[:64]
    uri = f"tenant/{tenant}/results/{query_id}.parquet"
    if not _URI.match(uri):
        raise InvalidInput("query id is not usable in a result handle uri")
    return uri


def path_for(settings: Any, uri: str) -> Path:
    if not _URI.match(uri):
        raise Forbidden("not a result handle uri")
    root = store_root(settings).resolve()
    path = (root / uri).resolve()
    if not path.is_relative_to(root):
        raise Forbidden("result handle escapes the result store")
    return path


def classification_of(tags: Iterable[str]) -> str:
    tags = {str(t).lower() for t in tags}
    for level in reversed(CLASSIFICATION_ORDER):
        if level in tags:
            return level
    return "internal"


def max_classification(levels: Iterable[str]) -> str:
    best = 1  # internal: data from a customer source is never public by default
    for level in levels:
        if level in CLASSIFICATION_ORDER:
            best = max(best, CLASSIFICATION_ORDER.index(level))
    return CLASSIFICATION_ORDER[best]


# ---------------------------------------------------------------------------------------------- writing
def _infer(hint: str | None, values: list[Any]) -> str:
    if hint in ("int", "float", "bool"):
        return hint
    seen = {type(v) for v in values if v is not None}
    if not seen:
        return "string"
    if seen == {bool}:
        return "bool"
    if seen == {int}:
        return "int"
    if seen <= {int, float}:
        return "float"
    return "string"


def _arrow_type(kind: str):  # noqa: ANN202
    import pyarrow as pa

    return {"int": pa.int64(), "float": pa.float64(), "bool": pa.bool_()}.get(kind, pa.string())


def _cell(kind: str, value: Any, column: str) -> Any:
    if value is None:
        return None
    if kind == "string":
        return value if isinstance(value, str) else json.dumps(value) if isinstance(value, (dict, list)) else str(value)
    if kind == "float":
        if isinstance(value, (int, float)):
            return float(value)
    elif kind == "int":
        if isinstance(value, (bool, int)):
            return int(value)
        if isinstance(value, float) and value.is_integer():
            return int(value)
    elif kind == "bool" and isinstance(value, bool):
        return value
    raise InvalidInput(f"result column {column} changed type within the result ({kind} then "
                       f"{type(value).__name__}); cast it in the SQL")


class ResultSink:
    """Rows of one statement: inline until a threshold, then streamed into a Parquet file.

    ``mode``: ``inline`` never spills (the caller's own row cap bounds memory, the pre-S02 behaviour);
    ``auto`` spills past the thresholds; ``handle`` always writes a file."""

    def __init__(self, settings: Any, *, uri: str, mode: str = "auto", max_rows: int) -> None:
        self.settings = settings
        self.uri = uri
        self.mode = mode
        self.max_rows = max_rows  # rows past this are counted as truncation, never kept
        self.inline_rows = int(getattr(settings, "result_inline_max_rows", 256))
        self.inline_bytes = int(getattr(settings, "result_inline_max_bytes", 65_536))
        self.columns: list[str] = []
        self.hints: list[str | None] = []
        self.kinds: list[str] = []
        self.rows: list[list[Any]] = []
        self.row_count = 0
        self.inline_size = 2
        self.truncated = False
        self._writer: Any = None
        self._tmp: Path | None = None

    # -- feeding -------------------------------------------------------------------------------------
    def begin(self, columns: list[str], type_hints: list[str | None] | None = None) -> None:
        self.columns = list(columns)
        self.hints = list(type_hints or [None] * len(columns))

    @property
    def spilled(self) -> bool:
        return self._writer is not None

    @property
    def wants_more(self) -> bool:
        return not self.truncated

    def add(self, rows: list[list[Any]]) -> None:
        room = self.max_rows - self.row_count
        if len(rows) > room:
            self.truncated = True
            rows = rows[:max(room, 0)]
        if not rows:
            return
        self.row_count += len(rows)
        if self._writer is not None:
            self._write(rows)
            return
        self.rows.extend(rows)
        if self.mode == "inline":
            return
        if self.mode == "auto":
            self.inline_size += sum(len(json.dumps(r, default=str)) + 1 for r in rows)
            if len(self.rows) <= self.inline_rows and self.inline_size <= self.inline_bytes:
                return
        self._spill()

    def _spill(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        self.kinds = [_infer(self.hints[i] if i < len(self.hints) else None, [r[i] for r in self.rows])
                      for i in range(len(self.columns))]
        schema = pa.schema([pa.field(c or f"col{i}", _arrow_type(k)) for i, (c, k) in enumerate(zip(self.columns, self.kinds, strict=True))])
        final = path_for(self.settings, self.uri)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._tmp = final.with_name(f".{final.name}.{os.getpid()}.tmp")
        self._writer = pq.ParquetWriter(str(self._tmp), schema, compression="zstd")
        buffered, self.rows = self.rows, []
        for start in range(0, len(buffered), BATCH_ROWS):
            self._write(buffered[start:start + BATCH_ROWS])

    def _write(self, rows: list[list[Any]]) -> None:
        import pyarrow as pa

        arrays = [pa.array([_cell(k, r[i], self.columns[i]) for r in rows], type=_arrow_type(k))
                  for i, k in enumerate(self.kinds)]
        self._writer.write_batch(pa.record_batch(arrays, schema=self._writer.schema))

    # -- finishing -----------------------------------------------------------------------------------
    def finish(self) -> dict[str, Any] | None:
        """``None`` when the result stays inline; else the file facts (uri, schema, bytes, sha256)."""
        if self._writer is None and self.mode == "handle":
            self._spill()
        if self._writer is None:
            return None
        self._writer.close()
        self._writer = None
        assert self._tmp is not None
        final = path_for(self.settings, self.uri)
        digest = hashlib.sha256()
        with self._tmp.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        os.replace(self._tmp, final)
        return {"uri": self.uri, "bytes": final.stat().st_size, "sha256": digest.hexdigest(),
                "schema": [{"name": c, "type": k} for c, k in zip(self.columns, self.kinds, strict=True)]}

    def abort(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            self._writer = None
        if self._tmp is not None and self._tmp.exists():
            self._tmp.unlink(missing_ok=True)


def record_handle(session: Any, *, query_id: str, workspace_id: str, run_id: str | None, source_id: str | None,
                  facts: dict[str, Any], row_count: int, classification: str, settings: Any) -> ResultHandle:
    from analystos.db.models import ResultHandle as Row

    expires = utcnow() + timedelta(hours=int(getattr(settings, "result_ttl_hours", 24)))
    session.add(Row(id=query_id, workspace_id=workspace_id, run_id=run_id, source_id=source_id, uri=facts["uri"],
                    result_schema=facts["schema"], row_count=row_count, bytes=facts["bytes"], sha256=facts["sha256"],
                    classification_max=classification, expires_at=expires))
    return ResultHandle(query_execution_id=query_id, uri=facts["uri"], result_schema=facts["schema"], row_count=row_count,
                        bytes=facts["bytes"], sha256=facts["sha256"], expires_at=expires, classification_max=classification,
                        workspace_id=workspace_id)


# ---------------------------------------------------------------------------------------------- reading
def _verified_path(settings: Any, session: Any, handle: ResultHandle | str, workspace_id: str) -> tuple[Path, Any]:
    from analystos.db.models import ResultHandle as Row

    qid = handle if isinstance(handle, str) else handle.query_execution_id
    row = session.get(Row, qid)  # RLS: another tenant's handle is simply not there
    if row is None or row.workspace_id != workspace_id:
        raise NotFound("no such result handle in this workspace")
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    if row.purged_at is not None or expires <= utcnow():
        raise NotFound("the result handle has expired; run the query again")
    if not isinstance(handle, str) and handle.uri != row.uri:
        raise Forbidden("the result handle does not match its record")
    path = path_for(settings, row.uri)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        raise NotFound("the result file is gone; run the query again") from None
    if digest.hexdigest() != row.sha256:
        raise Forbidden("the result file does not match its recorded sha256")
    return path, row


def open_dataset(handle: ResultHandle | str, *, workspace_id: str, settings: Any | None = None, session: Any | None = None):  # noqa: ANN201
    """A pyarrow dataset over the handle's file (lazy: DuckDB scans it batch by batch), after the checks above."""
    import pyarrow.dataset as ds

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope

    settings = settings or get_settings()
    if session is not None:
        path, _ = _verified_path(settings, session, handle, workspace_id)
    else:
        with session_scope() as s:
            path, _ = _verified_path(settings, s, handle, workspace_id)
    return ds.dataset(str(path), format="parquet")


def read_rows(handle: ResultHandle | str, *, workspace_id: str, limit: int, settings: Any | None = None,
              session: Any | None = None) -> tuple[list[str], list[list[Any]]]:
    """The first ``limit`` rows of a handle (previews, small consumers); never the whole file."""
    dataset = open_dataset(handle, workspace_id=workspace_id, settings=settings, session=session)
    table = dataset.head(max(int(limit), 0))
    return list(table.column_names), [list(r.values()) for r in table.to_pylist()]


# ---------------------------------------------------------------------------------------------- TTL purge
def purge_expired_results(settings: Any | None = None, *, limit: int = 500,
                          session_factory: Any | None = None) -> int:
    """Delete result files whose TTL passed and mark their rows purged. A platform loop (every tenant)."""
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.db.models import ResultHandle as Row
    from analystos.tenancy.context import system_scope

    settings = settings or get_settings()
    if session_factory is None:
        from analystos.db.base import SessionLocal as session_factory  # noqa: N813
    purged = 0
    with system_scope():
        session = session_factory()
        try:
            rows = session.scalars(select(Row).where(Row.purged_at.is_(None), Row.expires_at <= utcnow())
                                   .order_by(Row.expires_at).limit(limit)).all()
            for row in rows:
                try:
                    path_for(settings, row.uri).unlink(missing_ok=True)
                except (OSError, Forbidden) as exc:
                    _log.warning("could not delete result file %s: %s", row.id, exc.__class__.__name__)
                    continue
                row.purged_at = utcnow()
                purged += 1
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return purged
