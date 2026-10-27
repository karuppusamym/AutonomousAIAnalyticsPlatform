"""Carry the tenant through Temporal (spec S01-T4).

The client adds the bound tenant as the ``analystos-tenant`` header when it starts a workflow; the
workflow copies it onto every activity and child workflow it starts; the worker runs each activity in
``tenant_scope`` of that header, in the thread or process that executes it. Engine activities then
confirm the tenant owns the run (``tenancy.background.owned_by``), which also covers workflows started
before this header existed.

Workflow-side code here runs inside the workflow sandbox: it imports only temporalio and contextvars.
"""
from __future__ import annotations

import contextvars
import inspect
from collections.abc import Mapping
from typing import Any

import temporalio.client
import temporalio.converter
import temporalio.worker

HEADER = "analystos-tenant"
_workflow_tenant: contextvars.ContextVar[str | None] = contextvars.ContextVar("analystos_workflow_tenant", default=None)


def _payloads() -> temporalio.converter.PayloadConverter:
    return temporalio.converter.default().payload_converter


def with_tenant(headers: Mapping[str, Any], tenant: str | None) -> Mapping[str, Any]:
    if not tenant:
        return headers
    return {**headers, HEADER: _payloads().to_payloads([tenant])[0]}


def read_tenant(headers: Mapping[str, Any] | None) -> str | None:
    payload = (headers or {}).get(HEADER)
    if payload is None:
        return None
    value = _payloads().from_payloads([payload])[0]
    return value if isinstance(value, str) and value else None


class TenantBound:
    """A sync activity function bound to a tenant. Picklable (top level), so it crosses into the
    compute pool's processes, where the scope is entered."""

    def __init__(self, fn: Any, tenant: str) -> None:
        self.fn, self.tenant = fn, tenant

    def __call__(self, *args: Any) -> Any:
        from analystos.tenancy import tenant_scope

        with tenant_scope(self.tenant):
            return self.fn(*args)


class TenantInterceptor(temporalio.client.Interceptor, temporalio.worker.Interceptor):
    """Registered on the client (``Client.connect(interceptors=...)``); workers built from that client
    use it too."""

    def intercept_client(self, next: temporalio.client.OutboundInterceptor) -> temporalio.client.OutboundInterceptor:
        return _ClientOutbound(next)

    def intercept_activity(self, next: temporalio.worker.ActivityInboundInterceptor) -> temporalio.worker.ActivityInboundInterceptor:
        return _ActivityInbound(next)

    def workflow_interceptor_class(self, input: temporalio.worker.WorkflowInterceptorClassInput) -> type[temporalio.worker.WorkflowInboundInterceptor]:
        return _WorkflowInbound


class _ClientOutbound(temporalio.client.OutboundInterceptor):
    async def start_workflow(self, input: temporalio.client.StartWorkflowInput) -> Any:
        from analystos.tenancy import current_tenant

        input.headers = with_tenant(input.headers, current_tenant())
        return await super().start_workflow(input)


class _ActivityInbound(temporalio.worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: temporalio.worker.ExecuteActivityInput) -> Any:
        tenant = read_tenant(input.headers)
        if not tenant:
            return await super().execute_activity(input)
        if inspect.iscoroutinefunction(input.fn):
            from analystos.tenancy import tenant_scope

            with tenant_scope(tenant):
                return await super().execute_activity(input)
        input.fn = TenantBound(input.fn, tenant)
        return await super().execute_activity(input)


class _WorkflowInbound(temporalio.worker.WorkflowInboundInterceptor):
    def init(self, outbound: temporalio.worker.WorkflowOutboundInterceptor) -> None:
        super().init(_WorkflowOutbound(outbound))

    async def execute_workflow(self, input: temporalio.worker.ExecuteWorkflowInput) -> Any:
        _workflow_tenant.set(read_tenant(input.headers))
        return await super().execute_workflow(input)


class _WorkflowOutbound(temporalio.worker.WorkflowOutboundInterceptor):
    def start_activity(self, input: temporalio.worker.StartActivityInput) -> Any:
        input.headers = with_tenant(input.headers, _workflow_tenant.get())
        return super().start_activity(input)

    def start_local_activity(self, input: temporalio.worker.StartLocalActivityInput) -> Any:
        input.headers = with_tenant(input.headers, _workflow_tenant.get())
        return super().start_local_activity(input)

    async def start_child_workflow(self, input: temporalio.worker.StartChildWorkflowInput) -> Any:
        input.headers = with_tenant(input.headers, _workflow_tenant.get())
        return await super().start_child_workflow(input)


INTERCEPTORS = [TenantInterceptor()]
