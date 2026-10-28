"""The query-worker protocol (S02-T1): every statement against customer data is a ``QueryRequest`` handed to a
``QueryService``, which runs it in the `worker:query` role.

    lite / tests      InProcessQueryService: the gateway in this process
    standard          TemporalQueryService: a QueryWorkflow on the `<prefix>-query` task queue, whose activity
                      runs the gateway in `analystos worker --queues query` (ANALYSTOS_PROCESS_ROLE=query);
                      the API and the agent pools then hold no source credential (connectors/secrets.py refuses
                      source refs outside `credential_roles`)

``query_service()`` picks one from ``ANALYSTOS_QUERY_TRANSPORT``; ``RemoteQueryGateway`` gives existing callers
(`run_sql_for`, `execute(scope, sql)`) the same object shape over the remote service.

The request carries the caller's tenant, workspace and principal. A run passes its narrowed ``scope``; other
callers name ``user_id`` and the worker resolves the scope server-side (`governance.policy.resolve_scope`).
``params`` are bound as SQL literals by sqlglot before validation (never string-formatted), and a
``semantic_query`` is compiled from the approved semantic model in the worker, so a caller never supplies the
SQL of a governed metric.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from analystos.contracts.policy import DataScope
from analystos.core.errors import AnalystOSError, InvalidInput
from analystos.core.ids import new_id
from analystos.core.logging import get_logger
from analystos.gateway.types import CostEstimate, QueryResult

_log = get_logger(__name__)


class QueryBudget(BaseModel):
    max_scan_bytes: int | None = Field(default=None, ge=0)
    max_rows: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1)


class QueryRequest(BaseModel):
    tenant: str | None = None
    workspace: str
    principal: str  # the audited actor: user:<id> | agent:<id>
    user_id: str | None = None  # whose scope the worker resolves when `scope` is absent
    source_id: str | None = None
    sql: str | None = None
    semantic_query: dict[str, Any] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    purpose: str = "analysis"
    run_id: str | None = None
    task_id: str | None = None
    request_id: str | None = None
    budget: QueryBudget = Field(default_factory=QueryBudget)
    result_mode: Literal["inline", "auto", "handle"] = "auto"
    federated: bool = False
    use_cache: bool = True
    retain_rows: bool = True
    scope: DataScope | None = None
    query_id: str | None = None  # the audit id; set by the client so the statement can be cancelled

    @model_validator(mode="after")
    def _one_statement(self) -> QueryRequest:
        if (self.sql is None) == (self.semantic_query is None):
            raise ValueError("a QueryRequest carries exactly one of sql or semantic_query")
        if self.scope is None and not self.user_id:
            raise ValueError("a QueryRequest without a scope must name the user_id whose scope applies")
        if self.scope is not None and self.scope.workspace_id != self.workspace:
            raise ValueError("the scope belongs to another workspace")
        return self


class QueryService(Protocol):
    def execute(self, req: QueryRequest) -> QueryResult: ...

    def estimate(self, req: QueryRequest) -> CostEstimate: ...

    def cancel(self, query_execution_id: str) -> bool: ...


# ---------------------------------------------------------------------------------------------- binding
def bind_params(sql: str, params: dict[str, Any], dialect: str) -> str:
    """Replace named placeholders (`:name`) with literals built by sqlglot. Unknown or unused names refuse."""
    if not params:
        return sql
    import sqlglot
    from sqlglot import exp

    def literal(value: Any) -> exp.Expression:
        if value is None:
            return exp.Null()
        if isinstance(value, bool):
            return exp.Boolean(this=value)
        if isinstance(value, (int, float)):
            return exp.Literal.number(value)
        if isinstance(value, str):
            return exp.Literal.string(value)
        if isinstance(value, (list, tuple)) and value and all(not isinstance(v, (list, tuple, dict)) for v in value):
            return exp.Tuple(expressions=[literal(v) for v in value])
        raise InvalidInput(f"parameter values must be scalars or flat lists, not {type(value).__name__}")

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        raise InvalidInput(f"the statement could not be parsed for parameter binding: {exc.__class__.__name__}") from None
    used: set[str] = set()

    def swap(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Placeholder) and node.name:
            if node.name not in params:
                raise InvalidInput(f"no value for parameter :{node.name}")
            used.add(node.name)
            return literal(params[node.name])
        return node

    out = tree.transform(swap).sql(dialect=dialect)
    unused = set(params) - used
    if unused:
        raise InvalidInput(f"parameters not used by the statement: {sorted(unused)}")
    return out


# ---------------------------------------------------------------------------------------------- in-process
class InProcessQueryService:
    """The gateway in this process (lite, tests, and the worker:query activity itself)."""

    def __init__(self, gateway: Any | None = None, *, session_factory: Callable[[], Any] | None = None) -> None:
        self._gateway = gateway
        self._session_factory = session_factory

    @property
    def gateway(self) -> Any:
        if self._gateway is None:
            from analystos.runtime.context import default_gateway

            gw = default_gateway()
            self._gateway = gw.local if isinstance(gw, RemoteQueryGateway) else gw
        return self._gateway

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from analystos.db.base import SessionLocal

        return SessionLocal()

    def _scope_and_sql(self, req: QueryRequest) -> tuple[DataScope, str]:
        scope = req.scope
        session = None
        try:
            if scope is None or req.semantic_query is not None:
                session = self._session()
            if scope is None:
                from analystos.db.models import User
                from analystos.governance.policy import resolve_scope

                user = session.get(User, req.user_id)
                if user is None:
                    raise InvalidInput("unknown user for the query request")
                scope = resolve_scope(session, user, req.workspace)
            if req.semantic_query is not None:
                from analystos.contracts.semantic import SemanticQuery
                from analystos.semantic.compiler import compile_query, load_catalog

                catalog = load_catalog(session, req.workspace)
                if catalog is None:
                    raise InvalidInput("this workspace has no approved semantic model to compile the query against")
                sql = compile_query(SemanticQuery.model_validate(req.semantic_query), catalog, scope).sql
            else:
                sql = req.sql or ""
        finally:
            if session is not None:
                session.close()
        if req.source_id is not None:
            from analystos.gateway.service import _bind_scope

            if req.source_id not in set(scope.source_ids) | set(scope.asset_sources.values()):
                from analystos.core.errors import Forbidden

                raise Forbidden(f"source {req.source_id} is not in the authorized scope")
            scope = _bind_scope(scope, req.source_id)
        dialect = "duckdb" if req.federated else scope.source_dialects.get(req.source_id or "", None) or next(
            iter(scope.source_dialects.values()), "postgres")
        return scope, bind_params(sql, req.params, dialect)

    def _bound(self, req: QueryRequest):  # noqa: ANN202
        """Run as the request's tenant (the worker serves every tenant; RLS binds each call to its own)."""
        import contextlib

        if not req.tenant:
            return contextlib.nullcontext()
        from analystos.tenancy.context import tenant_scope

        return tenant_scope(req.tenant)

    def execute(self, req: QueryRequest) -> QueryResult:
        with self._bound(req):
            scope, sql = self._scope_and_sql(req)
            return self.gateway.execute(
                scope, sql, actor=req.principal, purpose=req.purpose, run_id=req.run_id, task_id=req.task_id,
                use_cache=req.use_cache, max_rows=req.budget.max_rows, timeout_seconds=req.budget.timeout_seconds,
                retain_rows=req.retain_rows, federated=req.federated, result_mode=req.result_mode,
                max_scan_bytes=req.budget.max_scan_bytes, query_id=req.query_id)

    def estimate(self, req: QueryRequest) -> CostEstimate:
        with self._bound(req):
            scope, sql = self._scope_and_sql(req)
            return self.gateway.estimate(scope, sql, actor=req.principal, timeout_seconds=req.budget.timeout_seconds)

    def cancel(self, query_execution_id: str) -> bool:
        return bool(self.gateway.cancel(query_execution_id))


# ---------------------------------------------------------------------------------------------- Temporal
QUERY_WORKFLOW = "QueryWorkflow"


def workflow_id(query_id: str) -> str:
    return f"query-{query_id}"


class TemporalQueryService:
    """Requests go to the `query` task queue; the caller waits for the result (or the transport timeout)."""

    def __init__(self, settings: Any, *, submit: Callable[[Any], Any] | None = None) -> None:
        self.settings = settings
        self._submit = submit

    def _call(self, op: str, req: QueryRequest | None = None, *, query_id: str | None = None) -> Any:
        from analystos.workflows.orchestrator import _submit, _temporal_client
        from analystos.workflows.queues import queue_name

        submit = self._submit or _submit
        timeout = int(getattr(self.settings, "query_transport_timeout_seconds", 300))
        queue = queue_name(self.settings.temporal_queue_prefix, "query")

        async def run() -> Any:
            from datetime import timedelta

            client = await _temporal_client()
            if op == "cancel":
                handle = client.get_workflow_handle(workflow_id(query_id or ""))
                await handle.cancel()
                return True
            assert req is not None
            return await client.execute_workflow(
                QUERY_WORKFLOW, {"op": op, "request": req.model_dump(mode="json")}, id=workflow_id(req.query_id or ""),
                task_queue=queue, execution_timeout=timedelta(seconds=timeout))

        try:
            return submit(run()).result(timeout=timeout + 5)
        except AnalystOSError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface worker-side errors with their own class
            raise _remote_error(exc) from None

    def execute(self, req: QueryRequest) -> QueryResult:
        req = req.model_copy(update={"query_id": req.query_id or new_id("qry")})
        return QueryResult.model_validate(self._call("execute", req))

    def estimate(self, req: QueryRequest) -> CostEstimate:
        req = req.model_copy(update={"query_id": req.query_id or new_id("qry")})
        return CostEstimate.model_validate(self._call("estimate", req))

    def cancel(self, query_execution_id: str) -> bool:
        try:
            return bool(self._call("cancel", query_id=query_execution_id))
        except AnalystOSError:
            return False


def _remote_error(exc: BaseException) -> AnalystOSError:
    """A worker-side AnalystOSError arrives as an ApplicationError whose type is the class name and whose
    details carry the message: rebuild the same class, so HTTP status and retryability survive the hop."""
    from analystos.core import errors

    cause = exc
    for _ in range(4):
        if getattr(cause, "type", None) or getattr(cause, "cause", None) is None:
            break
        cause = cause.cause  # type: ignore[assignment]
    name = getattr(cause, "type", None)
    details = list(getattr(cause, "details", None) or [])
    message = str(details[0]) if details else str(getattr(cause, "message", None) or exc)
    cls = getattr(errors, str(name), None) if name else None
    if isinstance(cls, type) and issubclass(cls, AnalystOSError):
        return cls(message)
    from analystos.core.errors import UpstreamUnavailable

    return UpstreamUnavailable(f"the query worker did not answer: {message.splitlines()[0][:300] if message else 'error'}")


# ---------------------------------------------------------------------------------------------- legacy shape
class RemoteQueryGateway:
    """`QueryGateway`'s call shape (`execute(scope, sql, ...)`, `run_sql_for`, `estimate`, `cancel`) over a
    remote QueryService, so existing callers send their statements to worker:query unchanged."""

    def __init__(self, local: Any, service: QueryService) -> None:
        self.local = local  # validation-free helpers only; it never opens a source connection here
        self.service = service
        self.settings = local.settings

    def run_sql_for(self, scope: DataScope, **kwargs: Any) -> Any:
        from analystos.gateway.service import QueryGateway

        return QueryGateway.run_sql_for(self, scope, **kwargs)  # type: ignore[arg-type]

    def _request(self, scope: DataScope, sql: str, actor: str, **kw: Any) -> QueryRequest:
        from analystos.tenancy.context import current_tenant

        return QueryRequest(
            tenant=current_tenant(), workspace=scope.workspace_id, principal=actor, user_id=scope.user_id, sql=sql,
            purpose=kw.get("purpose") or "analysis", run_id=kw.get("run_id"), task_id=kw.get("task_id"),
            budget=QueryBudget(max_rows=kw.get("max_rows"), timeout_seconds=kw.get("timeout_seconds"),
                               max_scan_bytes=kw.get("max_scan_bytes")),
            result_mode=kw.get("result_mode") or "inline", federated=bool(kw.get("federated")),
            use_cache=kw.get("use_cache", True), retain_rows=kw.get("retain_rows", True), scope=scope,
            query_id=kw.get("query_id"))

    def execute(self, scope: DataScope, sql: str, *, actor: str, **kw: Any) -> QueryResult:
        return self.service.execute(self._request(scope, sql, actor, **kw))

    def estimate(self, scope: DataScope, sql: str, *, actor: str, timeout_seconds: int | None = None) -> CostEstimate:
        return self.service.estimate(self._request(scope, sql, actor, timeout_seconds=timeout_seconds))

    def explain(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"available": False, "reason": "plan explain runs in worker:query; use estimate()"}

    def cancel(self, query_id: str) -> bool:
        return self.service.cancel(query_id)


def query_service(settings: Any | None = None) -> QueryService:
    from analystos.core.config import get_settings

    settings = settings or get_settings()
    if getattr(settings, "query_transport", "inprocess") == "temporal":
        return TemporalQueryService(settings)
    return InProcessQueryService()
