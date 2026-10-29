"""The governed query gateway: the only path from user/model SQL to source data.

execute():
  1. effective limits = min(requested, scope limits, platform ceilings);
  2. validate (validator.validate_sql) — fail closed, precise SQLRejected messages;
  3. resolve the source from the control plane and check it belongs to the scope's workspace;
  3b. ABAC (S01-T6, governance/abac.py): classification, purpose, region, grain, audience; a denial is a 403
      policy_denied {rule, reason, remedy}, audited as a rejection, before any cache lookup;
  4. result cache lookup (Redis; key `tenant:{t}:scope:{s}:policy:{v}:` + hash of fingerprint, scope hash,
     source version, row cap; S01-T7);
  5. execute the *generated* SQL:
       staged sources  -> analytics DB with the READER identity only, READ ONLY transaction,
                          SET LOCAL ROLE to the owning workspace's reader role (the reader login
                          itself holds no grant on staged schemas), SET LOCAL statement_timeout;
       pushdown pg     -> connector.sqlalchemy_url(), READ ONLY transaction + statement_timeout;
       pushdown tsql   -> connector URL with the driver query timeout, plus the source-kind
                          catalog's session statements (connectors/kinds.py);
       pushdown duckdb -> the DuckDB engine on the file: read_only, external access off;
       pushdown trino  -> the Trino engine: a READ ONLY transaction the driver opens, the timeout as a
                          session property, cancel through the cursor (engines/trino.py);
       federated       -> (cross-source runs, P4-E03) validated against every source's scope in the
                          duckdb dialect; each source's leg (the referenced columns of a referenced
                          table) is itself run through execute() with that source's own bound scope,
                          identity and audit row; the statement then runs on the sandboxed in-memory
                          DuckDB federation engine over those legs only. When every source is a Trino
                          catalog of one cluster and identity (TRN-004), the statement is validated in
                          the trino dialect instead, each table is prefixed with its source's catalog
                          and it runs once on Trino, read-only (no legs leave Trino);
     each path is an `Engine` (engines/, P4-E01); a connector whose catalog kind cannot be pushed
     down is refused (staged kinds only ever run in the analytics DB);
  6. JSON-safe values, result hash, audit row (QueryExecution) for every attempt in its own short
     transaction, optional on_event("query.executed" | "query.rejected", payload).

S02 additions: a scan budget (request, workspace policy `max_scan_bytes`, platform `query_max_scan_bytes`) is
checked against the engine's estimate before execution (gateway/cost.py), refusals carry a reason code and
keep only the hashed + redacted statement; pushdown connections hand the end user to the source's own row
security (catalog `capabilities.session_identity`); results stream through a ResultSink and, past the inline
thresholds (`result_mode="auto"`), leave as a Parquet ResultHandle (gateway/handles.py); `cancel(query_id)`
interrupts a statement in flight; federated legs are handles and need `data_movement: staged_ok`.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from analystos.connectors.naming import staging_schema_for, workspace_reader_role
from analystos.contracts.policy import DataScope
from analystos.core.errors import (
    AnalystOSError,
    BudgetExceeded,
    Forbidden,
    InvalidInput,
    NotFound,
    PolicyDenied,
    QueryTimeout,
    SQLRejected,
    UpstreamUnavailable,
)
from analystos.core.ids import new_id, stable_hash
from analystos.core.logging import get_logger
from analystos.db.models import QueryExecution, Source, SourceAsset, User, Workspace
from analystos.engines.base import Limits, ReaderIdentity
from analystos.engines.sql import run_postgres, run_session_sql
from analystos.engines.trino import cluster_of, qualify_catalogs, run_trino
from analystos.gateway.cache import QueryCache
from analystos.gateway.cost import EXPLAINERS, over_budget, row_cap_estimate, trino_estimate
from analystos.gateway.handles import ResultSink, uri_for
from analystos.gateway.types import CostEstimate, QueryResult, ValidatedSQL
from analystos.gateway.validator import federation_dialect, validate_federated_sql, validate_sql
from analystos.gateway.values import json_safe

__all__ = ["PREVIEW_ROWS", "QueryGateway", "json_safe", "result_hash"]

PREVIEW_ROWS = 20
_log = get_logger(__name__)


def _default_session_factory():  # noqa: ANN202
    from analystos.db.base import SessionLocal

    return SessionLocal()


FLOAT_DIGITS = 12  # significant digits a result hash keeps


def _canonical_value(value: Any) -> Any:
    if isinstance(value, float):
        return float(format(value, f".{FLOAT_DIGITS}g")) if value == value and value not in (float("inf"), float("-inf")) else str(value)
    if isinstance(value, (list, tuple)):
        return [_canonical_value(v) for v in value]
    return value


def result_hash(columns: list[str], rows: list[list[Any]]) -> str:
    """Identity of a result for evidence and REV's re-run check. Floats keep 12 significant digits: parallel engines
    (DuckDB, warehouses) sum in a varying order, so an identical query can differ in the last bits between runs."""
    return stable_hash({"columns": columns, "rows": [_canonical_value(r) for r in rows]})


class _Runner:
    """RunSQL bound to one source of a scope."""

    def __init__(self, gateway: QueryGateway, scope: DataScope, *, actor: str, run_id: str | None, task_id: str | None,
                 source_id: str | None, federated: bool = False) -> None:
        self._gateway = gateway
        self._scope = scope
        self._actor = actor
        self._run_id = run_id
        self._task_id = task_id
        self.source_id = source_id
        self.federated = federated
        self.dialect = federation_dialect(scope) if federated else scope.source_dialects.get(source_id or "", "postgres")

    def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None,
                 retain_rows: bool = True, use_cache: bool = True) -> QueryResult:
        extra = {"federated": True} if self.federated else {}
        return self._gateway.execute(
            self._scope, sql, actor=self._actor, purpose=purpose, run_id=self._run_id, task_id=self._task_id,
            max_rows=max_rows, retain_rows=retain_rows, use_cache=use_cache, **extra,
        )


class QueryGateway:
    def __init__(
        self,
        settings: Any,
        session_factory: Callable[[], Any] = _default_session_factory,
        cache: QueryCache | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        *,
        connector_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.cache = cache
        self.on_event = on_event
        if connector_factory is None:
            from analystos.connectors.registry import build_connector

            connector_factory = build_connector
        self.connector_factory = connector_factory
        self._check_identities()

    # ------------------------------------------------------------------ configuration guard
    def _check_identities(self) -> None:
        reader = make_url(self.settings.analytics_reader_url)
        control = make_url(self.settings.database_url)
        loader = make_url(self.settings.analytics_loader_url)
        if (reader.host, reader.port, reader.database) == (control.host, control.port, control.database):
            raise InvalidInput("analytics_reader_url must not point at the control-plane database")
        if reader.username in (control.username, loader.username):
            raise InvalidInput("analytics_reader_url must use the dedicated reader identity, not the loader/control identity")
        builder = getattr(self.settings, "analytics_builder_url", None)
        if builder and make_url(builder).username == reader.username:
            raise InvalidInput("analytics_reader_url must not be the build (write) identity")

    # ------------------------------------------------------------------ public API
    def run_sql_for(self, scope: DataScope, *, actor: str, run_id: str | None = None, task_id: str | None = None,
                    source_id: str | None = None, federated: bool = False) -> _Runner:
        """A runner bound to one source of the scope, or (``federated=True``, cross-source runs) to the
        whole scope: its statements are `federation_dialect(scope)` SQL (duckdb, or trino when every source is a
        Trino catalog) over assets of any of the scope's sources."""
        sources = sorted(set(scope.source_ids) | set(scope.asset_sources.values()))
        if federated:
            if source_id is not None:
                raise InvalidInput("a federated runner spans every source of the scope; do not pass source_id")
            if not sources:
                raise InvalidInput("the scope has no source to federate")
            return _Runner(self, scope, actor=actor, run_id=run_id, task_id=task_id, source_id=None, federated=True)
        if source_id is None:
            if len(sources) != 1:
                raise InvalidInput(
                    f"The scope has {len(sources)} sources; pass source_id to bind the SQL runner to one of: {sources}"
                )
            source_id = sources[0]
        elif source_id not in sources:
            raise Forbidden(f"source {source_id} is not in the authorized scope")
        return _Runner(self, _bind_scope(scope, source_id), actor=actor, run_id=run_id, task_id=task_id,
                       source_id=source_id)

    def execute(
        self,
        scope: DataScope,
        sql: str,
        *,
        actor: str,
        purpose: str = "analysis",
        run_id: str | None = None,
        task_id: str | None = None,
        use_cache: bool = True,
        max_rows: int | None = None,
        timeout_seconds: int | None = None,
        retain_rows: bool = True,
        federated: bool = False,
        result_mode: str = "inline",
        max_scan_bytes: int | None = None,
        query_id: str | None = None,
        _federation_leg: bool = False,
    ) -> QueryResult:
        """`retain_rows=False` (e.g. crawler PII value sampling): the statement is validated, executed
        and audited as usual, but no result value is stored anywhere — no audit preview, no result cache.
        `federated=True`: a cross-source statement (module docstring, P4-E03).
        `result_mode`: `inline` (rows in memory, capped by max_rows, as before), `auto` (a ResultHandle past
        the inline thresholds) or `handle` (always a handle). `max_scan_bytes`: this call's scan budget.
        `query_id`: the audit id to use (the query worker passes one so the caller can cancel it)."""
        if result_mode not in ("inline", "auto", "handle"):
            raise InvalidInput("result_mode must be inline, auto or handle")
        if not retain_rows:
            use_cache = False
        if result_mode != "inline":
            use_cache = False  # the result cache holds rows; handles are their own store
        started = time.perf_counter()
        query_id = query_id or new_id("qry")
        eff_rows = min(x for x in (max_rows, scope.max_rows, self.settings.query_max_rows) if x is not None and x > 0)
        eff_timeout = min(
            x for x in (timeout_seconds, scope.timeout_seconds, self.settings.query_timeout_seconds) if x is not None and x > 0
        )
        audit = {
            "id": query_id,
            "workspace_id": scope.workspace_id,
            "run_id": run_id,
            "task_id": task_id,
            "actor": actor[:80],
            "purpose": (purpose or "analysis")[:120],
            "sql": sql if isinstance(sql, str) else str(sql),
        }

        if federated:
            return self._execute_federated(scope, sql, audit, started, eff_rows=eff_rows, eff_timeout=eff_timeout,
                                           use_cache=use_cache, retain_rows=retain_rows, result_mode=result_mode,
                                           max_scan_bytes=max_scan_bytes, purpose=purpose)

        # 1. validate
        try:
            validated = validate_sql(scope, sql, max_rows=eff_rows)
            source = self._load_source(scope, validated)
        except SQLRejected as exc:
            self._reject(audit, exc, started)
            raise
        except (Forbidden, NotFound) as exc:
            self._reject(audit, exc, started)
            raise
        audit.update(
            source_id=validated.source_id,
            executed_sql=validated.executable_sql,
            fingerprint=validated.fingerprint,
            referenced_assets=validated.referenced_assets,
        )
        # 1b. ABAC on every statement, before the cache (S01-T6): a cached result is never a way around a denial.
        # A federation leg was decided as part of its federated statement.
        if not _federation_leg:
            try:
                self._abac(scope, validated, source.get("tenant_id"), purpose)
            except PolicyDenied as exc:
                self._reject(audit, exc, started)
                raise

        # 2. scan budget (S02-T4): estimate before running, refuse above the budget
        estimate = self._check_budget(scope, source, validated, audit, started, eff_timeout, max_scan_bytes)

        # 3. cache
        key = None
        if use_cache and self.cache is not None:
            key = self.cache.key(validated.fingerprint, scope.scope_hash(), source["version"], eff_rows,
                                 tenant=source.get("tenant_id"), policy_version=scope.policy_version)
            hit = self.cache.get(key)
            if hit is not None and isinstance(hit.get("columns"), list) and isinstance(hit.get("rows"), list):
                return self._finish(audit, validated, hit["columns"], hit["rows"], bool(hit.get("truncated")), started,
                                    cache_hit=True, hash_=hit.get("result_hash"), estimate=estimate)

        # 4. execute (rows stream into the sink; past the inline thresholds it writes the Parquet handle)
        sink = ResultSink(self.settings, uri=uri_for(source.get("tenant_id"), query_id), mode=result_mode,
                          max_rows=eff_rows)
        try:
            columns, rows = self._run(source, validated, eff_rows, eff_timeout, sink=sink, scope=scope,
                                      query_id=query_id)
            facts = sink.finish()
        except QueryTimeout as exc:
            sink.abort()
            self._fail(audit, "timeout", exc, started)
            raise
        except AnalystOSError as exc:
            sink.abort()
            self._fail(audit, "error", exc, started)
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected; audit then surface as upstream failure
            sink.abort()
            err = UpstreamUnavailable(f"Query execution failed: {exc.__class__.__name__}")
            self._fail(audit, "error", err, started)
            raise err from exc
        if facts is not None:
            return self._finish_handle(audit, validated, sink, facts, started, source=source, scope=scope,
                                       estimate=estimate, retain_rows=retain_rows)
        if sink.columns or sink.rows:
            columns, rows, truncated = sink.columns, sink.rows, sink.truncated
        else:
            truncated = len(rows) > eff_rows
            rows = rows[:eff_rows]
        result = self._finish(audit, validated, columns, rows, truncated, started, cache_hit=False, retain_rows=retain_rows,
                              estimate=estimate)
        if key is not None and self.cache is not None:
            self.cache.set(key, {
                "columns": result.columns, "rows": result.rows, "truncated": result.truncated,
                "result_hash": result.result_hash,
            })
        return result

    def explain(self, scope: DataScope, sql: str, *, actor: str, purpose: str = "explain",
                timeout_seconds: int | None = None) -> dict[str, Any]:
        """Dry explain (paste-SQL explain in Ask): validated exactly as `execute` validates, then the
        source is asked for its plan without running the statement (`EXPLAIN (FORMAT JSON)`, never
        ANALYZE, inside the same read-only transaction). Rejections are audited like an execution's;
        a source with no plan explain reports `available: False`. No rows are read or kept."""
        started = time.perf_counter()
        eff_rows = min(x for x in (scope.max_rows, self.settings.query_max_rows) if x is not None and x > 0)
        eff_timeout = min(
            x for x in (timeout_seconds, scope.timeout_seconds, self.settings.query_timeout_seconds) if x is not None and x > 0
        )
        audit = {"id": new_id("qry"), "workspace_id": scope.workspace_id, "run_id": None, "task_id": None,
                 "actor": actor[:80], "purpose": (purpose or "explain")[:120], "sql": sql if isinstance(sql, str) else str(sql)}
        try:
            validated = validate_sql(scope, sql, max_rows=eff_rows)
            source = self._load_source(scope, validated)
        except (SQLRejected, Forbidden, NotFound) as exc:
            self._reject(audit, exc, started)
            raise
        audit.update(source_id=validated.source_id, executed_sql=validated.executable_sql, fingerprint=validated.fingerprint,
                     referenced_assets=validated.referenced_assets)
        if validated.dialect != "postgres":
            return {"available": False, "reason": f"plan explain is not supported for {validated.dialect} sources"}
        try:
            _, rows = self._run(source, validated, 1, eff_timeout, sql="EXPLAIN (FORMAT JSON) " + validated.executable_sql)
        except AnalystOSError as exc:
            self._fail(audit, "error", exc, started)
            raise
        self._persist(dict(audit, status="explained", row_count=0, duration_ms=int((time.perf_counter() - started) * 1000)),
                      strict=False)
        summary = plan_summary(rows[0][0] if rows and rows[0] else None)
        shown = visible_relations(summary["relations"], scope.assets, validated.referenced_assets)
        seen = set(visible_relations(sorted({s["relation"] for s in summary["scans"]}), scope.assets,
                                     validated.referenced_assets))
        summary.update(relations=shown, relations_hidden=len(summary["relations"]) - len(shown),
                       scans=[s for s in summary["scans"] if s["relation"] in seen])
        return {"available": True, **summary}

    def estimate(self, scope: DataScope, sql: str, *, actor: str, timeout_seconds: int | None = None) -> CostEstimate:
        """The engine's pre-execution estimate for a statement (S02-T4), validated exactly as `execute` validates.
        Rejections are audited; nothing is read."""
        started = time.perf_counter()
        eff_rows = min(x for x in (scope.max_rows, self.settings.query_max_rows) if x is not None and x > 0)
        eff_timeout = min(
            x for x in (timeout_seconds, scope.timeout_seconds, self.settings.query_timeout_seconds) if x is not None and x > 0
        )
        audit = {"id": new_id("qry"), "workspace_id": scope.workspace_id, "run_id": None, "task_id": None,
                 "actor": actor[:80], "purpose": "estimate", "sql": sql if isinstance(sql, str) else str(sql)}
        try:
            validated = validate_sql(scope, sql, max_rows=eff_rows)
            source = self._load_source(scope, validated)
        except (SQLRejected, Forbidden, NotFound) as exc:
            self._reject(audit, exc, started)
            raise
        return self._estimate(source, validated, eff_rows, eff_timeout)

    def cancel(self, query_id: str) -> bool:
        """Interrupt a statement this process is running (S02-T1). False when it is not running here."""
        from analystos.engines.sql import cancel_running

        return cancel_running(query_id)

    def _estimate(self, source: dict[str, Any], validated: ValidatedSQL, max_rows: int, timeout: int) -> CostEstimate:
        """Per dialect (gateway/cost.py): staged sources and PostgreSQL pushdown run EXPLAIN on the same
        read-only path as the query; a dialect without an explainer is row-cap only."""
        dialect = "postgres" if source["execution_mode"] == "staged" else validated.dialect
        if dialect == "trino":
            _, rows = self._run(source, validated, 1, timeout, sql="EXPLAIN (FORMAT JSON) " + validated.executable_sql)
            return trino_estimate(rows[0][0] if rows and rows[0] else None)
        explainer = EXPLAINERS.get(dialect)
        if explainer is None or dialect != "postgres":
            return row_cap_estimate(max_rows)
        from analystos.gateway.cost import relation_size_sql, seq_scanned_relations

        wrapper, parse = explainer
        _, rows = self._run(source, validated, 1, timeout, sql=wrapper.format(sql=validated.executable_sql))
        plan = rows[0][0] if rows and rows[0] else None
        try:
            whole = seq_scanned_relations(plan)
            sizes: dict[str, int] = {}
            if whole:  # a sequential scan reads the relation, whatever its filter keeps
                _, found = self._run(source, validated, len(whole), timeout, sql=relation_size_sql(whole))
                sizes = {str(name): int(size or 0) for name, size in found}
            return parse(plan, sizes)
        except AnalystOSError:
            raise
        except Exception:  # noqa: BLE001 - an unreadable plan is no estimate, never a pass
            return CostEstimate(basis="explain_unreadable")

    def _run_limits(self, run_id: str | None) -> dict[str, Any]:
        """A goal run's limits (S06), cached by `autonomy.budget.run_limits`; none outside a run."""
        if not run_id:
            return {}
        from analystos.autonomy.budget import run_limits

        session = self.session_factory()
        try:
            return run_limits(session, run_id)  # no run row (ad-hoc ids): {}
        finally:
            session.close()

    def _check_budget(self, scope: DataScope, source: dict[str, Any], validated: ValidatedSQL, audit: dict[str, Any],
                      started: float, timeout: int, requested: int | None) -> CostEstimate | None:
        limits = [x for x in (requested, source.get("max_scan_bytes"), getattr(self.settings, "query_max_scan_bytes", 0))
                  if x is not None and x > 0]
        run_limits = self._run_limits(audit.get("run_id"))
        run_cap = run_limits.get("max_scan_bytes")
        if not limits and run_cap is None:
            return None  # no budget applies: no estimate, no extra round trip
        budget = min(limits) if limits else None
        try:
            estimate = self._estimate(source, validated, scope.max_rows, timeout)
        except AnalystOSError as exc:
            self._fail(audit, "error", exc, started)
            raise
        audit["cost_estimate"] = dict(estimate.model_dump(), budget=budget, run_budget=run_cap)
        if estimate.basis == "explain_unreadable":  # a budget applies and the plan could not be read: fail closed
            exc = BudgetExceeded("The statement's cost could not be estimated, so the scan budget cannot be checked.")
            self._reject(audit, exc, started, reason_code="budget.unestimated")
            raise exc
        if budget is not None and over_budget(estimate, budget):
            exc = BudgetExceeded(f"The statement would scan about {estimate.bytes} bytes, above the scan budget of "
                                 f"{budget} bytes. Filter or aggregate more, or ask for a larger budget.")
            self._reject(audit, exc, started, reason_code="budget.max_scan_bytes")
            raise exc
        if run_cap is not None:  # S06: the run's scan budget is charged each admitted statement's estimate
            from analystos.autonomy.budget import check_scan, record_scan

            try:
                check_scan(audit["run_id"], run_limits, estimate.bytes or 0)
            except BudgetExceeded as exc:
                self._reject(audit, exc, started, reason_code="budget.run_scan_bytes")
                raise
            record_scan(audit["run_id"], estimate.bytes or 0)
        return estimate

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _abac(scope: DataScope, validated: ValidatedSQL, tenant_id: str | None, purpose: str) -> None:
        """S01-T6: classification of the columns read, the statement's grain, the purpose, the caller's region."""
        from analystos.governance import abac

        verdict = abac.decide_query(scope, tenant_id=tenant_id, source_id=validated.source_id or None,
                                    sql=validated.executable_sql, dialect=validated.dialect,
                                    referenced_assets=validated.referenced_assets,
                                    referenced_columns=validated.referenced_columns, purpose=purpose)
        if not verdict.allow:
            raise abac.denial(verdict, what="query")

    def _load_source(self, scope: DataScope, validated: ValidatedSQL) -> dict[str, Any]:
        session = self.session_factory()
        try:
            ws = session.get(Workspace, scope.workspace_id)
            if ws is not None and (ws.status != "active" or ws.deleted_at is not None):
                # a disabled or deleted workspace runs no query (its sources keep their rows)
                raise NotFound(f"workspace {scope.workspace_id} not found")
            row = session.get(Source, validated.source_id)
            if row is None or row.workspace_id != scope.workspace_id:
                raise NotFound(f"source {validated.source_id} not found in workspace {scope.workspace_id}")
            if scope.source_ids and row.id not in scope.source_ids:
                raise Forbidden(f"source {row.id} is not in the authorized scope")
            agg = session.execute(
                select(func.count(SourceAsset.id), func.max(SourceAsset.freshness_at), func.sum(SourceAsset.row_count))
                .where(SourceAsset.source_id == row.id)
            ).one()
            version = stable_hash([row.id, row.status, row.execution_mode, str(row.last_discovered_at), agg[0], str(agg[1]),
                                   agg[2], row.staging_schema])
            policy = _policy_facts(session, ws)
            user = session.get(User, scope.user_id) if scope.user_id else None
            source = {
                "id": row.id,
                "kind": row.kind,
                "config": dict(row.config or {}),
                "secret_ref": row.secret_ref,
                "execution_mode": row.execution_mode,
                "staging_schema": row.staging_schema or staging_schema_for(row.id),
                "workspace_id": row.workspace_id,
                "version": version,
                "tenant_id": getattr(ws, "tenant_id", None) or _tenant_of(row.workspace_id),
                "user_email": getattr(user, "email", None) or "",
                **policy,
            }
        finally:
            session.close()
        if source["execution_mode"] == "staged":
            wrong = [a for a in validated.referenced_assets if a.split(".", 1)[0] != source["staging_schema"]]
            if wrong:
                raise SQLRejected(
                    f"Assets {wrong} are not in the staging schema {source['staging_schema']} of source {source['id']}."
                )
        return source

    def _run(self, source: dict[str, Any], validated: ValidatedSQL, max_rows: int, timeout: int,
             sql: str | None = None, *, sink: Any | None = None, scope: DataScope | None = None,
             query_id: str | None = None) -> tuple[list[str], list[list[Any]]]:
        """`sql` replaces the validated statement only for the gateway's own wrappers of it (EXPLAIN).
        `sink`: rows stream into it (S02-T3). `scope`: whose identity a pushdown source sees (S02-T5)."""
        from analystos.engines.registry import engine_for_kind

        statement = sql or validated.executable_sql
        stream: dict[str, Any] = {"sink": sink, "query_id": query_id}
        if source["execution_mode"] == "staged":
            role = workspace_reader_role(source["workspace_id"],
                                         getattr(self.settings, "analytics_workspace_role_prefix", "analystos_r_"))
            return self._run_postgres(self.settings.analytics_reader_url, statement, max_rows, timeout,
                                      role=role, **stream)
        connector = self.connector_factory(_SourceView(source), self.settings)
        if getattr(connector, "execution_mode", "pushdown") != "pushdown":
            # The catalog does not allow this kind to be queried in place (e.g. a row marked
            # pushdown for MySQL): refuse rather than send SQL the analysis compiler never targeted.
            raise InvalidInput(f"Source {source['id']} ({source['kind']}) cannot be queried in place; stage it instead")
        dialect = getattr(connector, "dialect", "postgres")
        if dialect != validated.dialect:
            raise SQLRejected(f"Source {source['id']} expects {dialect} SQL")
        identity = _identity_sql(source, scope)
        if dialect == "postgres":
            return self._run_postgres(connector.sqlalchemy_url(), statement, max_rows, timeout,
                                      identity_sql=identity, **stream)
        if dialect == "tsql":
            url = connector.query_url(timeout) if hasattr(connector, "query_url") else connector.sqlalchemy_url()
            session_sql = connector.session_statements(timeout) if hasattr(connector, "session_statements") else []
            return self._run_generic(url, statement, max_rows, session_sql, identity_sql=identity, **stream)
        if dialect == "trino":
            return run_trino(connector.sqlalchemy_url(), statement, max_rows, timeout, **stream)
        if dialect == "duckdb":
            path = str((getattr(connector, "config", None) or {}).get("path") or "")
            columns, rows = engine_for_kind(source["kind"], dialect).execute_read(
                validated, identity=ReaderIdentity(path=path), limits=Limits(max_rows=max_rows, timeout_seconds=timeout))
            if sink is not None:  # a file engine returns rows already capped at max_rows + 1
                sink.begin(columns)
                sink.add(rows)
                return columns, []
            return columns, rows
        raise InvalidInput(f"Unsupported pushdown dialect {dialect}")

    # ------------------------------------------------------------------ federation (P4-E03)
    def _execute_federated(self, scope: DataScope, sql: str, audit: dict[str, Any], started: float, *, eff_rows: int,
                           eff_timeout: int, use_cache: bool, retain_rows: bool, result_mode: str = "inline",
                           max_scan_bytes: int | None = None, purpose: str = "analysis") -> QueryResult:
        """Multi-source (S02-T7, FR-051): each source's leg runs through `execute` on its own scope, identity,
        budget and audit row and comes back as a ResultHandle; the join runs in DuckDB over those handles only
        (scanned lazily from Parquet), and lineage records every leg handle. Needs `data_movement: staged_ok`."""
        from analystos.engines.registry import federation_engine
        from analystos.gateway.handles import open_dataset

        try:
            validated = validate_federated_sql(scope, sql, max_rows=eff_rows)
            sources = {sid: self._load_source(_bind_scope(scope, sid), _leg_view(validated, sid))
                       for sid in sorted(set(validated.asset_sources.values()))}
            if len(sources) > 1 and any(s.get("data_movement") != "staged_ok" for s in sources.values()):
                raise PolicyDenied("This workspace's policy does not allow moving data between sources "
                                   "(data_movement is not staged_ok); query one source at a time.")
        except PolicyDenied as exc:
            self._reject(audit, exc, started, reason_code="policy.data_movement")
            raise
        except (SQLRejected, Forbidden, NotFound) as exc:
            self._reject(audit, exc, started)
            raise
        audit.update(executed_sql=validated.executable_sql, fingerprint=validated.fingerprint,
                     referenced_assets=validated.referenced_assets, purpose=f"federated:{audit['purpose']}"[:120])
        tenants = {sources[s].get("tenant_id") for s in sources}
        tenant = next(iter(tenants)) if len(tenants) == 1 else None
        try:
            self._abac(scope, validated, tenant, purpose)
        except PolicyDenied as exc:
            self._reject(audit, exc, started)
            raise
        key = None
        if use_cache and self.cache is not None and len(tenants) == 1:  # every leg's workspace has one tenant
            versions = "|".join(sources[s]["version"] for s in sorted(sources))
            key = self.cache.key(validated.fingerprint, scope.scope_hash(), versions, eff_rows, tenant=tenant,
                                 policy_version=scope.policy_version)
            hit = self.cache.get(key)
            if hit is not None and isinstance(hit.get("columns"), list) and isinstance(hit.get("rows"), list):
                return self._finish(audit, validated, hit["columns"], hit["rows"], bool(hit.get("truncated")), started,
                                    cache_hit=True, hash_=hit.get("result_hash"))
        if validated.dialect == "trino":
            return self._federate_on_trino(scope, validated, sources, audit, started, eff_rows=eff_rows,
                                           eff_timeout=eff_timeout, retain_rows=retain_rows,
                                           max_scan_bytes=max_scan_bytes, cache_key=key)
        leg_cap = min(x for x in (scope.max_rows, self.settings.query_max_rows) if x is not None and x > 0)
        try:
            tables: dict[str, Any] = {}
            legs: list[tuple[str, str, str]] = []  # (asset, source id, leg query id)
            for asset, source_id in validated.asset_sources.items():
                bound = _bind_scope(scope, source_id)
                leg_sql = _leg_sql(asset, validated.referenced_columns, bound.source_dialects[source_id])
                leg = self.execute(bound, leg_sql, actor=audit["actor"], purpose=f"federation.leg:{audit['id']}",
                                   run_id=audit["run_id"], task_id=audit["task_id"], max_rows=leg_cap,
                                   timeout_seconds=eff_timeout, retain_rows=False, use_cache=False,
                                   result_mode="handle", max_scan_bytes=max_scan_bytes, _federation_leg=True)
                if leg.truncated:
                    raise InvalidInput(
                        f"{asset} has more than {leg_cap} rows, the federation limit per source; a partial leg would "
                        "change the join, so the query was not run. Filter or aggregate within the source first."
                    )
                legs.append((asset, source_id, leg.query_id))
                if leg.handle is not None:
                    hs = self.session_factory()
                    try:
                        tables[asset] = open_dataset(leg.handle, workspace_id=scope.workspace_id,
                                                     settings=self.settings, session=hs)
                    finally:
                        hs.close()
                else:
                    tables[asset] = (_leg_columns(asset, validated.referenced_columns) or ["_row"], leg.rows)
            engine = federation_engine(getattr(self.settings, "federation_engine", "duckdb"))
            columns, rows = engine.federate(tables, validated,
                                            limits=Limits(max_rows=eff_rows, timeout_seconds=eff_timeout))
            audit["cost_estimate"] = {"legs": [q for _, _, q in legs], "basis": "legs"}
        except QueryTimeout as exc:
            self._fail(audit, "timeout", exc, started)
            raise
        except AnalystOSError as exc:
            self._fail(audit, "error", exc, started)
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected; audit then surface as upstream failure
            err = UpstreamUnavailable(f"Federated query failed: {exc.__class__.__name__}")
            self._fail(audit, "error", err, started)
            raise err from exc
        truncated = len(rows) > eff_rows
        rows = rows[:eff_rows]
        result = self._finish(audit, validated, columns, rows, truncated, started, cache_hit=False,
                              retain_rows=retain_rows)
        self._link_legs(scope, audit, legs)
        if key is not None and self.cache is not None:
            self.cache.set(key, {"columns": result.columns, "rows": result.rows, "truncated": result.truncated,
                                 "result_hash": result.result_hash})
        return result

    def _federate_on_trino(self, scope: DataScope, validated: ValidatedSQL, sources: dict[str, dict[str, Any]],
                           audit: dict[str, Any], started: float, *, eff_rows: int, eff_timeout: int, retain_rows: bool,
                           max_scan_bytes: int | None, cache_key: str | None) -> QueryResult:
        """TRN-004: every source is a Trino catalog queried in place. The statement was validated in the trino
        dialect against each source's scope; it runs once on Trino with every table prefixed by its source's
        catalog, so the sources must share one cluster and one identity (otherwise each would need its own)."""
        try:
            for sid, src in sources.items():
                if src["kind"] != "trino" or src["execution_mode"] != "pushdown":
                    raise InvalidInput(f"Source {sid} is not a Trino catalog queried in place; it cannot join a "
                                       "federated Trino statement")
                if not (src["config"] or {}).get("catalog"):
                    raise InvalidInput(f"Source {sid} names no Trino catalog (config.catalog)")
            clusters = {cluster_of(src["config"], src.get("secret_ref")) for src in sources.values()}
            if len(clusters) != 1:
                raise InvalidInput("These Trino sources are on different clusters or use different identities, so one "
                                   "statement cannot read them all; query each source separately.")
            catalogs = {asset.lower(): sources[sid]["config"]["catalog"] for asset, sid in validated.asset_sources.items()}
            statement = validated.model_copy(update={"executable_sql": qualify_catalogs(validated.executable_sql,
                                                                                         catalogs)})
        except AnalystOSError as exc:
            self._reject(audit, exc, started)
            raise
        audit["executed_sql"] = statement.executable_sql
        first = sources[sorted(sources)[0]]
        estimate = self._check_budget(scope, first, statement, audit, started, eff_timeout, max_scan_bytes)
        try:
            connector = self.connector_factory(_SourceView(first), self.settings)
            columns, rows = run_trino(connector.sqlalchemy_url(), statement.executable_sql, eff_rows, eff_timeout,
                                      query_id=audit["id"])
        except QueryTimeout as exc:
            self._fail(audit, "timeout", exc, started)
            raise
        except AnalystOSError as exc:
            self._fail(audit, "error", exc, started)
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected; audit then surface as upstream failure
            err = UpstreamUnavailable(f"Federated query failed: {exc.__class__.__name__}")
            self._fail(audit, "error", err, started)
            raise err from exc
        truncated = len(rows) > eff_rows
        result = self._finish(audit, statement, columns, rows[:eff_rows], truncated, started, cache_hit=False,
                              retain_rows=retain_rows, estimate=estimate)
        self._link_sources(scope, audit, validated)
        if cache_key is not None and self.cache is not None:
            self.cache.set(cache_key, {"columns": result.columns, "rows": result.rows, "truncated": result.truncated,
                                       "result_hash": result.result_hash})
        return result

    def _link_sources(self, scope: DataScope, audit: dict[str, Any], validated: ValidatedSQL) -> None:
        """Lineage of an in-place federated result: the query read each table, from its own source."""
        from analystos.artifacts.registry import link

        session = self.session_factory()
        try:
            for asset, source_id in validated.asset_sources.items():
                link(session, scope.workspace_id, ("query", audit["id"]), "reads", ("table", asset), run_id=audit["run_id"])
                link(session, scope.workspace_id, ("query", audit["id"]), "from_source", ("source", source_id),
                     run_id=audit["run_id"])
            session.commit()
        except Exception as exc:  # noqa: BLE001 - lineage never withholds a result already audited
            session.rollback()
            _log.warning("could not record federated lineage for %s: %s", audit["id"], exc.__class__.__name__)
        finally:
            session.close()

    def _link_legs(self, scope: DataScope, audit: dict[str, Any], legs: list[tuple[str, str, str]]) -> None:
        """Lineage of a multi-source result: the joined query derives from every leg handle, and each leg
        handle was read from its source table."""
        from analystos.artifacts.registry import link

        session = self.session_factory()
        try:
            for asset, source_id, leg_id in legs:
                link(session, scope.workspace_id, ("query", audit["id"]), "derived_from", ("result_handle", leg_id),
                     run_id=audit["run_id"])
                link(session, scope.workspace_id, ("result_handle", leg_id), "reads", ("table", asset),
                     run_id=audit["run_id"])
                link(session, scope.workspace_id, ("result_handle", leg_id), "from_source", ("source", source_id),
                     run_id=audit["run_id"])
            session.commit()
        except Exception as exc:  # noqa: BLE001 - lineage never withholds a result already audited
            session.rollback()
            _log.warning("could not record multi-source lineage for %s: %s", audit["id"], exc.__class__.__name__)
        finally:
            session.close()

    def _run_postgres(self, url: str, sql: str, max_rows: int, timeout: int,
                      role: str | None = None, **stream: Any) -> tuple[list[str], list[list[Any]]]:
        """The Postgres engine (engines/sql.py): READ ONLY transaction, timeout, optional reader role."""
        return run_postgres(url, sql, max_rows, timeout, role=role, **stream)

    def _run_generic(self, url: str, sql: str, max_rows: int,
                     session_sql: list[str] | tuple[str, ...] = (), **stream: Any) -> tuple[list[str], list[list[Any]]]:
        """The session-SQL engine (engines/sql.py): catalog read-only/timeout statements first."""
        return run_session_sql(url, sql, max_rows, session_sql, **stream)

    def _finish(self, audit: dict[str, Any], validated: ValidatedSQL, columns: list[str], rows: list[list[Any]],
                truncated: bool, started: float, *, cache_hit: bool, hash_: str | None = None,
                retain_rows: bool = True, estimate: CostEstimate | None = None) -> QueryResult:
        import json

        rhash = hash_ or result_hash(columns, rows)
        duration = int((time.perf_counter() - started) * 1000)
        record = dict(audit, status="ok", row_count=len(rows), truncated=truncated, columns=list(columns),
                      result_hash=rhash, result_preview=rows[:PREVIEW_ROWS] if retain_rows else [], cache_hit=cache_hit,
                      duration_ms=duration, result_bytes=len(json.dumps(rows, default=str).encode()))
        self._persist(record, strict=True)
        self._emit("query.executed", record)
        return QueryResult(
            query_id=audit["id"],
            columns=list(columns),
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            cache_hit=cache_hit,
            fingerprint=validated.fingerprint,
            result_hash=rhash,
            duration_ms=duration,
            referenced_assets=validated.referenced_assets,
            sql=validated.executable_sql,
            estimate=estimate,
        )

    def _finish_handle(self, audit: dict[str, Any], validated: ValidatedSQL, sink: ResultSink, facts: dict[str, Any],
                       started: float, *, source: dict[str, Any], scope: DataScope, estimate: CostEstimate | None,
                       retain_rows: bool) -> QueryResult:
        """A result written to the result store: the audit row and the handle row commit together; the
        audit keeps a preview and the file's sha256 as its result hash."""
        from analystos.gateway.handles import record_handle

        duration = int((time.perf_counter() - started) * 1000)
        classification = self._classification(validated, scope)
        preview: list[list[Any]] = []
        if retain_rows:
            import pyarrow.parquet as pq

            from analystos.gateway.handles import path_for

            first = pq.ParquetFile(str(path_for(self.settings, facts["uri"]))).iter_batches(batch_size=PREVIEW_ROWS)
            batch = next(first, None)
            preview = [list(r.values()) for r in batch.to_pylist()] if batch is not None else []
        record = dict(audit, status="ok", row_count=sink.row_count, truncated=sink.truncated, columns=list(sink.columns),
                      result_hash=facts["sha256"], result_preview=preview, cache_hit=False, duration_ms=duration,
                      result_bytes=facts["bytes"], result_uri=facts["uri"])
        _observe_statement(record)
        session = self.session_factory()
        try:
            session.add(QueryExecution(**record))
            handle = record_handle(session, query_id=audit["id"], workspace_id=scope.workspace_id, run_id=audit["run_id"],
                                   source_id=source["id"], facts=facts, row_count=sink.row_count,
                                   classification=classification, settings=self.settings)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            from analystos.gateway.handles import path_for

            path_for(self.settings, facts["uri"]).unlink(missing_ok=True)
            _log.error("could not write query audit/handle row %s: %s", audit["id"], exc.__class__.__name__)
            raise UpstreamUnavailable("query audit could not be written; result withheld") from exc
        finally:
            session.close()
        self._emit("query.executed", record)
        return QueryResult(query_id=audit["id"], columns=list(sink.columns), rows=[], row_count=sink.row_count,
                           truncated=sink.truncated, fingerprint=validated.fingerprint, result_hash=facts["sha256"],
                           duration_ms=duration, referenced_assets=validated.referenced_assets,
                           sql=validated.executable_sql, handle=handle, estimate=estimate)

    def _classification(self, validated: ValidatedSQL, scope: DataScope) -> str:
        """Highest tag of any column the statement read (restricted > pii > sensitive > internal)."""
        from analystos.db.models import SourceColumn
        from analystos.gateway.handles import classification_of, max_classification

        wanted: dict[str, set[str]] = {}
        for ref in validated.referenced_columns:
            asset, _, col = ref.rpartition(".")
            wanted.setdefault(asset, set()).add(col)
        if not wanted:
            return "internal"
        session = self.session_factory()
        try:
            rows = session.execute(
                select(SourceAsset.schema_name, SourceAsset.name, SourceColumn.name, SourceColumn.tags)
                .join(SourceColumn, SourceColumn.asset_id == SourceAsset.id)
                .where(SourceAsset.source_id.in_(set(validated.asset_sources.values()) or {validated.source_id}))
            ).all()
        except Exception:  # noqa: BLE001 - unknown classification is the strictest one
            return "restricted"
        finally:
            session.close()
        levels = [classification_of(tags or []) for schema, table, col, tags in rows
                  if col in wanted.get(f"{schema}.{table}", set())]
        return max_classification(levels)

    def _reject(self, audit: dict[str, Any], exc: AnalystOSError, started: float, *,
                reason_code: str | None = None) -> None:
        """A refused statement keeps its hash and its redacted text (literals removed), never the values."""
        from analystos.observability.metrics import gateway_reason, inc

        inc("analystos_gateway_rejections_total", reason=gateway_reason(exc))
        original = audit.get("sql") or ""
        record = dict(audit, status="rejected", rejected_reason=exc.message[:4000],
                      reason_code=(reason_code or _reason_code(exc))[:60], sql_hash=stable_hash(original),
                      sql=redact_sql(original), duration_ms=int((time.perf_counter() - started) * 1000))
        self._persist(record, strict=False)
        self._emit("query.rejected", record)

    def _fail(self, audit: dict[str, Any], status: str, exc: AnalystOSError, started: float) -> None:
        record = dict(audit, status=status, rejected_reason=exc.message[:4000], reason_code=_reason_code(exc)[:60],
                      duration_ms=int((time.perf_counter() - started) * 1000))
        self._persist(record, strict=False)

    def _persist(self, record: dict[str, Any], *, strict: bool) -> None:
        _observe_statement(record)
        session = self.session_factory()
        try:
            session.add(QueryExecution(**record))
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            _log.error("could not write query audit row %s: %s", record.get("id"), exc.__class__.__name__)
            if strict:
                raise UpstreamUnavailable("query audit could not be written; result withheld") from exc
        finally:
            session.close()

    def _emit(self, type_: str, record: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        payload = {
            "workspace_id": record.get("workspace_id"),
            "run_id": record.get("run_id"),
            "task_id": record.get("task_id"),
            "query_id": record.get("id"),
            "source_id": record.get("source_id"),
            "status": record.get("status"),
            "row_count": record.get("row_count", 0),
            "cache_hit": record.get("cache_hit", False),
            "duration_ms": record.get("duration_ms", 0),
            "reason": record.get("rejected_reason"),
            "purpose": record.get("purpose"),
            "fingerprint": record.get("fingerprint"),
            "referenced_assets": record.get("referenced_assets", []),
            "reason_code": record.get("reason_code"),
            "result_uri": record.get("result_uri"),
        }
        try:
            self.on_event(type_, payload)
        except Exception as exc:  # noqa: BLE001 - observers never break queries
            _log.warning("gateway on_event handler failed: %s", exc.__class__.__name__)


def _tenant_of(workspace_id: str) -> str | None:
    try:
        from analystos.tenancy.background import workspace_tenant
        from analystos.tenancy.context import current_tenant

        return current_tenant() or workspace_tenant(workspace_id)
    except Exception:  # noqa: BLE001 - tenancy off
        return None


def _policy_facts(session: Any, ws: Workspace | None) -> dict[str, Any]:
    """The workspace policy facts the gateway enforces itself: scan budget and data movement (S02)."""
    if ws is None or getattr(ws, "policy_version", None) is None:  # no workspace row (unit fakes): defaults
        return {"max_scan_bytes": 0, "data_movement": "staged_ok"}
    from analystos.governance.policy import load_policy

    doc = load_policy(session, ws)
    return {"max_scan_bytes": int(getattr(doc, "max_scan_bytes", 0) or 0),
            "data_movement": getattr(doc, "data_movement", "staged_ok")}


def _identity_sql(source: dict[str, Any], scope: DataScope | None) -> tuple[tuple[str, dict[str, Any]], ...]:
    """The kind's session-identity statements (S02-T5) with the end user bound as driver parameters."""
    if scope is None:
        return ()
    from analystos.connectors import kinds

    try:
        statements = kinds.get_kind(source["kind"]).capabilities.session_identity
    except InvalidInput:
        return ()
    params = {"user": scope.user_id or "", "user_email": source.get("user_email") or "", "workspace": scope.workspace_id}
    return tuple((stmt, params) for stmt in statements)


def _reason_code(exc: AnalystOSError) -> str:
    """A stable machine-readable reason: validator refusals keep their rule when the message names one."""
    if isinstance(exc, SQLRejected):
        text_ = exc.message.lower()
        for needle, code in (("not in the authorized", "validator.scope"), ("restricted", "validator.restricted_column"),
                             ("denied", "validator.denied"), ("only select", "validator.not_select"),
                             ("function", "validator.function"), ("join", "validator.join"),
                             ("parse", "validator.parse"), ("multiple statements", "validator.multiple_statements")):
            if needle in text_:
                return code
        return "validator"
    return str(getattr(exc, "code", "error") or "error")


def redact_sql(sql: str) -> str:
    """The statement with every literal replaced by `?` (a refusal keeps its shape, never its values)."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:  # noqa: BLE001
        return "[statement could not be parsed; redacted]"

    def scrub(node: exp.Expression) -> exp.Expression:
        if isinstance(node, (exp.Literal, exp.HexString, exp.ByteString)):
            return exp.Placeholder()
        return node

    out = [t.transform(scrub).sql() for t in trees if t is not None]
    return ";\n".join(out)[:20000] or "[empty statement]"
def _observe_statement(record: dict[str, Any]) -> None:
    """Metrics + a span per audited statement: status, cache, duration, the statement's hash (never its text).
    Never raises: the audit row written after it must not depend on observability."""
    from analystos.observability import metrics, telemetry

    status = str(record.get("status") or "unknown")
    metrics.inc("analystos_gateway_statements_total", status=status)
    if status == "ok":
        metrics.inc("analystos_cache_requests_total", cache="query_result", result="hit" if record.get("cache_hit") else "miss")
    if status != "explained":
        with contextlib.suppress(Exception):
            telemetry.record_span("gateway.execute", float(record.get("duration_ms") or 0) / 1000.0, status=status,
                                  workspace=record.get("workspace_id"), run_id=record.get("run_id"),
                                  purpose=record.get("purpose"), rows=record.get("row_count"),
                                  statement_hash=record.get("fingerprint") or telemetry.digest(record.get("sql", "")))


class _SourceView:
    """Minimal Source-like object handed to the connector factory (detached from the session)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.id = data["id"]
        self.kind = data["kind"]
        self.config = data["config"]
        self.secret_ref = data["secret_ref"]
        self.execution_mode = data["execution_mode"]
        self.staging_schema = data["staging_schema"]
        self.workspace_id = data["workspace_id"]


def _first_line(exc: BaseException | None) -> str:
    text_ = str(exc or "").strip()
    return text_.splitlines()[0][:500] if text_ else (exc.__class__.__name__ if exc else "error")


def visible_relations(relations: list[str], assets: list[str], referenced: list[str]) -> list[str]:
    """A plan can name relations the caller may not see (a view's base tables, catalog tables): only the
    caller's own assets are shown, and a bare (unqualified) name only when this statement referenced it."""
    full, bare = set(assets), {a.rsplit(".", 1)[-1] for a in referenced if a in assets}
    return [r for r in relations if (r in full if "." in r else r in bare)]


def _plan_top(raw: Any) -> dict[str, Any]:
    import json

    doc = json.loads(raw) if isinstance(raw, str) else raw
    return (doc[0] if isinstance(doc, list) and doc else doc or {}).get("Plan") or {}


def plan_summary(raw: Any) -> dict[str, Any]:
    """The top of a Postgres JSON plan: estimated rows and cost, every node and relation in it, and its
    scan nodes (for the index advisor, N-11)."""
    top = _plan_top(raw)
    nodes: list[str] = []
    relations: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        nodes.append(str(node.get("Node Type")))
        if node.get("Relation Name"):
            relations.append(f"{node.get('Schema') or ''}.{node['Relation Name']}".lstrip("."))
        for child in node.get("Plans") or []:
            walk(child)

    walk(top)
    return {"node": top.get("Node Type"), "estimated_rows": top.get("Plan Rows"), "total_cost": top.get("Total Cost"),
            "nodes": nodes[:40], "relations": sorted(set(relations)), "scans": plan_scans(raw)}


_INDEX_NODES = {"Index Scan", "Index Only Scan", "Bitmap Index Scan", "Bitmap Heap Scan"}


def plan_columns(expression: Any) -> list[str]:
    """Column names named by a plan condition (`Filter`, `Index Cond`); literals are dropped, never kept."""
    if not isinstance(expression, str) or not expression.strip():
        return []
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(expression, read="postgres")
    except Exception:  # noqa: BLE001
        return []
    return sorted({str(c.name) for c in tree.find_all(exp.Column)}) if tree is not None else []


def plan_scans(raw: Any) -> list[dict[str, Any]]:
    """The scan nodes of a Postgres JSON plan: node type, relation, estimated rows and cost, and the column
    names of its filter / index condition (never their values)."""
    top = _plan_top(raw)
    scans: list[dict[str, Any]] = []

    def walk(node: dict[str, Any]) -> None:
        kind = str(node.get("Node Type"))
        if node.get("Relation Name") or kind in _INDEX_NODES:
            scans.append({
                "node": kind,
                "relation": f"{node.get('Schema') or ''}.{node.get('Relation Name') or ''}".strip("."),
                "index": node.get("Index Name"),
                "filter_columns": plan_columns(node.get("Filter")),
                "index_columns": plan_columns(node.get("Index Cond") or node.get("Recheck Cond")),
                "estimated_rows": node.get("Plan Rows"),
                "total_cost": node.get("Total Cost"),
            })
        for child in node.get("Plans") or []:
            walk(child)

    walk(top)
    return scans[:40]


def _bind_scope(scope: DataScope, source_id: str) -> DataScope:
    """The scope narrowed to one source: its assets, columns and dialect only."""
    bound = scope.model_copy(deep=True)
    bound.source_ids = [source_id]
    bound.assets = [a for a in scope.assets if scope.asset_sources.get(a, source_id) == source_id]
    bound.asset_sources = {a: s for a, s in scope.asset_sources.items() if s == source_id}
    bound.columns = {a: c for a, c in scope.columns.items() if a in bound.assets}
    bound.source_dialects = {source_id: scope.source_dialects.get(source_id, "postgres")}
    return bound


def _leg_view(validated: ValidatedSQL, source_id: str) -> ValidatedSQL:
    """One source's part of a federated statement, as `_load_source` checks it."""
    assets = sorted(a for a, s in validated.asset_sources.items() if s == source_id)
    return validated.model_copy(update={"source_id": source_id, "referenced_assets": assets})


def _leg_columns(asset: str, referenced_columns: list[str]) -> list[str]:
    prefix = asset + "."
    return [c[len(prefix):] for c in referenced_columns if c.startswith(prefix)]


def _leg_sql(asset: str, referenced_columns: list[str], dialect: str) -> str:
    """A federated statement's extraction from one source: only the referenced columns of one table,
    in the source's own dialect, validated again against that source's scope by execute()."""
    from sqlglot import exp

    schema, table = asset.split(".", 1)
    cols = _leg_columns(asset, referenced_columns)
    select = ([exp.column(c, quoted=True).as_(exp.to_identifier(c, quoted=True)) for c in cols]
              or [exp.Literal.number(1).as_(exp.to_identifier("_row", quoted=True))])
    return exp.select(*select).from_(exp.table_(table, db=schema, quoted=True)).sql(dialect=dialect)
