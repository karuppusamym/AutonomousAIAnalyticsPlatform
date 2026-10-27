"""Per-step execution context. Built fresh for every task execution so that authorization,
policy and scope are re-checked at each step (§12.2): a permission revoked while the run was
paused or waiting for approval blocks the next protected action."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from sqlalchemy import func, select

from analystos.capabilities.agents import AgentBudget
from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.policy import DataScope, ExecutionIdentity, WorkspacePolicyDoc
from analystos.contracts.registry import AgentSpec
from analystos.core.config import get_settings
from analystos.core.errors import BudgetExceeded, NotFound, PolicyDenied, RunCancelled
from analystos.db.base import session_scope
from analystos.db.models import AgentDefinition, AgentMessage, AnalysisRun, QueryExecution, RunTask, User, Workspace
from analystos.decisions import DecisionService
from analystos.events.bus import emit
from analystos.governance.policy import load_policy, resolve_scope
from analystos.llm.jev import JevDecisions
from analystos.llm.router import CallContext, ModelRouter
from analystos.runtime.usage import DbUsageSink
from analystos.tools.registry import ToolRuntime, get_agent_spec


@lru_cache
def default_router() -> ModelRouter:
    from analystos.llm.cache import ResponseCache

    return ModelRouter(sink=DbUsageSink(), cache=ResponseCache(get_settings().redis_url))


@lru_cache
def default_gateway():
    from analystos.events.bus import emit as _emit
    from analystos.gateway.cache import QueryCache
    from analystos.gateway.service import QueryGateway

    settings = get_settings()

    def on_event(type_: str, payload: dict) -> None:
        ws = payload.get("workspace_id")
        if ws:
            _emit(ws, type_, {k: v for k, v in payload.items() if k in ("query_id", "status", "row_count", "cache_hit",
                                                                         "duration_ms", "reason", "purpose")},
                  run_id=payload.get("run_id"))

    try:
        cache = QueryCache(settings)
    except Exception:
        cache = None
    gateway = QueryGateway(settings, cache=cache, on_event=on_event)
    if getattr(settings, "query_transport", "inprocess") == "temporal":  # S02-T1: statements run in worker:query
        from analystos.gateway.protocol import RemoteQueryGateway, TemporalQueryService

        return RemoteQueryGateway(gateway, TemporalQueryService(settings))
    return gateway


def workspace_call_ctx(workspace_id: str, **kwargs: Any) -> CallContext:
    """CallContext for model calls outside a run step (feedback, monitors, crawler) that still
    carries the workspace policy, so provider/residency/approval rules apply everywhere."""
    with session_scope() as s:
        workspace = s.get(Workspace, workspace_id)
        policy = load_policy(s, workspace) if workspace else WorkspacePolicyDoc()
        kwargs.setdefault("tenant_id", getattr(workspace, "tenant_id", None))
        kwargs.setdefault("policy_version", getattr(workspace, "policy_version", None))
    if "knowledge_version" not in kwargs:
        from analystos.context.version import workspace_knowledge_version

        kwargs["knowledge_version"] = workspace_knowledge_version(workspace_id, policy=policy)
    return CallContext.for_policy(policy, workspace_id=workspace_id, **kwargs)


@dataclass
class Services:
    router: ModelRouter
    gateway: Any

    @property
    def jev(self) -> JevDecisions:
        return JevDecisions(self.router)

    @property
    def decisions(self) -> DecisionService:
        return DecisionService(self.router)


def default_services() -> Services:
    return Services(router=default_router(), gateway=default_gateway())


def _agent(session, run: AnalysisRun, agent_id: str) -> tuple[CapabilityManifest | None, AgentSpec]:
    """The agent manifest this run bound (else the current registry's) and the AgentSpec derived from
    it. agent_definition stays the platform-wide kill switch: a disabled row stops the agent."""
    from analystos.capabilities import registry
    from analystos.capabilities.agents import to_agent_spec
    from analystos.capabilities.binding import bound_manifest

    cap_id = f"agent.{agent_id}"
    manifest = bound_manifest(run, cap_id) or registry.current().manifests.get(cap_id)
    row = session.get(AgentDefinition, agent_id)
    if row is not None and not row.enabled:
        raise NotFound(f"agent {agent_id} is not registered or disabled")
    if manifest is not None and manifest.kind == "Agent":
        return manifest, to_agent_spec(manifest)
    return None, get_agent_spec(session, agent_id)


@dataclass
class RunContext:
    run: AnalysisRun
    task: RunTask
    user: User
    workspace: Workspace
    policy: WorkspacePolicyDoc
    scope: DataScope
    agent: AgentSpec
    services: Services
    manifest: CapabilityManifest | None = None  # the agent manifest this run bound (None: agent_definition only)
    notes: list[str] = field(default_factory=list)
    # Per task execution, against the agent manifest's budget (capabilities/agents.AgentBudget).
    usage: dict[str, float] = field(default_factory=lambda: {"llm_calls": 0, "usd": 0.0, "queries": 0})
    _authorized: set[str] = field(default_factory=set, repr=False)

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, run_id: str, task_key: str, services: Services) -> RunContext:
        with session_scope() as s:
            run = s.get(AnalysisRun, run_id)
            task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == task_key))
            user = s.get(User, run.requested_by)
            workspace = s.get(Workspace, run.workspace_id)
            if user is None or not user.active:
                raise PolicyDenied("run owner is no longer active")
            manifest, agent = _agent(s, run, task.agent_id)
            # The agent manifest's policies only tighten the workspace policy.
            policy = load_policy(s, workspace)
            policy.max_iterations = min(policy.max_iterations, agent.policies.max_iterations)
            # Re-resolve: revocations and policy tightening apply from the next step on.
            scope = resolve_scope(s, user, run.workspace_id, source_ids=run.scope.get("source_ids") or None,
                                  pii_access=agent.policies.pii_access)
            original = set(run.scope.get("assets") or [])
            scope.assets = [a for a in scope.assets if a in original]  # a run never widens beyond its start scope
            scope.denied_columns = sorted(set(scope.denied_columns) | set(run.scope.get("denied_columns") or []))
            scope.max_rows = min(scope.max_rows, agent.policies.max_rows_extract)
            if not scope.assets:
                raise PolicyDenied("no authorized assets remain in scope for this run")
            s.expunge_all()
        from analystos.registries.replay import services_for

        # A scheduled re-analysis replays the hypothesis registry with every model purpose off (P4-T05).
        return cls(run=run, task=task, user=user, workspace=workspace, policy=policy, scope=scope, agent=agent,
                   services=services_for(run, services), manifest=manifest)

    # ------------------------------------------------------------------ budget
    @property
    def budget(self) -> AgentBudget | None:
        from analystos.capabilities.agents import body

        return body(self.manifest).budget if self.manifest is not None and self.manifest.kind == "Agent" else None

    def check_output(self, type_: str, content: Any) -> None:
        """The agent's output contract (FND-006), immediately before it writes a run record:
        an undeclared type or content off its declared schema raises OutputContractViolation."""
        from analystos.capabilities.agents import enforce_output

        enforce_output(self.agent, self.agent.id, type_, content)

    def spend(self, kind: str, amount: float = 1) -> None:
        self.usage[kind] = self.usage.get(kind, 0) + amount

    def budget_left(self, kind: str) -> bool:
        """False once this task execution used the agent's `budget.<kind>` (llm_calls | usd | queries)."""
        budget = self.budget
        limit = getattr(budget, kind, None) if budget is not None else None
        return limit is None or self.usage.get(kind, 0) < limit

    # ------------------------------------------------------------------ helpers
    @property
    def identity(self) -> ExecutionIdentity:
        return ExecutionIdentity(user_id=self.user.id, workspace_id=self.workspace.id, agent_id=self.agent.id,
                                 purpose="analysis", run_id=self.run.id, task_id=self.task.id)

    def call_ctx(self, *, exclude_families: list[str] | None = None) -> CallContext:
        """Carries the workspace policy to the router. prompt_version here is the agent's default;
        `agents.common.llm_json` replaces it with `<prompt name>@<text hash>` for the prompt it sends."""
        return CallContext.for_policy(self.policy, workspace_id=self.workspace.id, run_id=self.run.id, task_id=self.task.id,
                                      agent_id=self.agent.id, prompt_version=f"{self.agent.id}.{self.agent.prompt_version}",
                                      exclude_families=exclude_families or [], knowledge_version=self.knowledge_version,
                                      tenant_id=self.workspace.tenant_id, scope_hash=self.scope.scope_hash(),
                                      policy_version=self.scope.policy_version)

    @property
    def knowledge_version(self) -> str | None:
        """Workspace knowledge version for the L0 cache key (P4-T06), once per step: the context is
        rebuilt for every step, so an edit applies from the next step on."""
        if "_kv" not in self.__dict__:
            from analystos.capabilities import packs
            from analystos.context.version import workspace_knowledge_version

            self.__dict__["_kv"] = workspace_knowledge_version(
                self.workspace.id, pack_refs=[p.ref for p in packs.for_scope(self.scope, self.policy)])
        return self.__dict__["_kv"]

    @property
    def router(self) -> ModelRouter:
        return self.services.router

    @property
    def jev(self) -> JevDecisions:
        return self.services.jev

    @property
    def decisions(self) -> DecisionService:
        """Every bounded choice an agent delegates (ADR-0015): authority classes, fallbacks, persisted."""
        return self.services.decisions

    def tools(self) -> ToolRuntime:
        return ToolRuntime(user=self.user, identity=self.identity, agent=self.agent, scope=self.scope)

    def authorize_tool(self, tool_id: str) -> None:
        """Implicit tool gate (spec v2 §6) for paths that act as a tool without invoking it by name.
        Checked once per step: the context is rebuilt for every step, so a denylist change or a
        revoked role applies from the next step on, as for invoked tools."""
        if tool_id not in self._authorized:
            self.tools().authorize(tool_id, {"implicit": True, "task": self.task.key}, bound=False)
            self._authorized.add(tool_id)

    def check_query_budget(self) -> None:
        """Every statement a run executes (skills, verification re-runs) counts toward the per-run budget.

        Counted with an atomic Redis increment (P4-T07); the gateway's audit rows only seed a missing
        counter, and are counted directly only while Redis is unavailable."""
        from analystos.runtime.budget_counters import RUN_TTL_SECONDS, default_budget_counters

        def counted() -> int:
            with session_scope() as s:
                return s.scalar(select(func.count()).select_from(QueryExecution).where(QueryExecution.run_id == self.run.id)) or 0

        counters = default_budget_counters()
        after = counters.incr(counters.run_key(self.run.id, "queries"), counted, RUN_TTL_SECONDS)
        used = counted() if after is None else int(after) - 1  # statements before this one
        if used >= self.policy.max_queries_per_run:
            raise BudgetExceeded(f"per-run query budget ({self.policy.max_queries_per_run}) exhausted")
        if (getattr(self.run, "limits", None) or {}).get("max_scan_bytes") is not None:  # a goal's scan budget (S06 §3.4)
            from analystos.autonomy.budget import check_scan

            check_scan(self.run.id, self.run.limits, 0)

    def run_sql(self, source_id: str | None = None, *, federated: bool = False):
        """Governed SQL runner for skills and agents: tool gate (``sql.execute``), per-run query budget,
        then the gateway. ``federated=True`` (cross-source runs, P4-E03) spans every source of the scope;
        each source's leg is still validated and audited under that source's own scope."""
        self.authorize_tool("sql.execute")
        gateway = self.services.gateway
        kwargs: dict = {"source_id": source_id} if source_id else {}
        if federated:
            kwargs = {"federated": True}
        inner = gateway.run_sql_for(self.scope, actor=f"agent:{self.agent.id}", run_id=self.run.id, task_id=self.task.id,
                                    **kwargs)
        ctx = self

        class _Budgeted:
            dialect = inner.dialect

            def __call__(self, sql: str, *, purpose: str = "analysis", max_rows: int | None = None, use_cache: bool = True):
                ctx.check_control()
                ctx.check_query_budget()
                if not ctx.budget_left("queries"):
                    raise BudgetExceeded(f"agent {ctx.agent.id} query budget ({ctx.budget.queries}) exhausted for this step")
                ctx.spend("queries")
                if use_cache:
                    return inner(sql, purpose=purpose, max_rows=max_rows)
                return inner(sql, purpose=purpose, max_rows=max_rows, use_cache=False)

        return _Budgeted()

    def check_control(self) -> None:
        with session_scope() as s:
            control, plan_version = s.execute(select(AnalysisRun.control, AnalysisRun.plan_version)
                                              .where(AnalysisRun.id == self.run.id)).one()
        if control == "cancel":
            raise RunCancelled("run cancelled by user")
        if plan_version != self.task.plan_version:
            raise RunCancelled("plan changed while this task was running; result discarded")

    def say(self, content: str, *, kind: str = "observation", data: dict | None = None) -> None:
        with session_scope() as s:
            s.add(AgentMessage(run_id=self.run.id, task_id=self.task.id, agent_id=self.agent.id, kind=kind,
                               content=content[:4000], data=data or {}))
            emit(self.workspace.id, "agent.message", {"agent": self.agent.id, "task": self.task.key, "kind": kind,
                                                       "content": content[:500]}, run_id=self.run.id, session=s)

    def event(self, type_: str, payload: dict) -> None:
        emit(self.workspace.id, type_, payload, run_id=self.run.id, actor=f"agent:{self.agent.id}")
