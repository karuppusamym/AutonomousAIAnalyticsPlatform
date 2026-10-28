"""Token-authenticated routes for isolated compute workers (ADR-0022, P7-06).

These routes never see a user session: every call presents a scoped task token (`workers/tokens.py`)
bound to one task, its readable artifact ids, its writable output names, its verbs and its model
purposes, inside one workspace. The store checks the token *and* the artifact's own workspace, so a
token can reach nothing it was not issued for. The only database read is the tenant of the token's
workspace (`tenant_resolver`, S01-T4); the conformance suite's stand-alone store overrides it.
"""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.concurrency import run_in_threadpool

from analystos.contracts.worker import ModelCallbackRequest, ModelCallbackResponse
from analystos.core.errors import BudgetExceeded, Forbidden, InvalidInput, Unauthenticated
from analystos.workers.store import ArtifactStore, control_store
from analystos.workers.tokens import TokenClaims, secret_from_settings, verify

router = APIRouter(prefix="/api/worker", tags=["worker"])


def worker_store() -> ArtifactStore:
    return control_store()


def worker_secret() -> bytes:
    from analystos.core.config import get_settings

    return secret_from_settings(get_settings())


def tenant_resolver():  # noqa: ANN201 - overridable: the stand-alone conformance store has no control database
    from analystos.tenancy.background import workspace_tenant

    return workspace_tenant


async def worker_claims(authorization: str | None = Header(default=None), secret: bytes = Depends(worker_secret),
                        resolve_tenant=Depends(tenant_resolver)) -> TokenClaims:  # noqa: ANN001
    """The task token from `Authorization: Bearer <token>`. A session JWT is not a task token. The request
    then acts for the tenant of the token's workspace (S01-T4); async so the binding reaches the route."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthenticated("a worker task token is required")
    claims = verify(secret, authorization.split(" ", 1)[1].strip())
    from analystos.tenancy import set_current_tenant

    tenant = await run_in_threadpool(resolve_tenant, claims.workspace_id)
    if tenant:
        set_current_tenant(tenant)
    return claims


def model_router():  # noqa: ANN201 - overridable in tests; the control plane's router with its usage sink
    from analystos.runtime.context import default_router

    return default_router()


def call_context():  # noqa: ANN201 - overridable in tests: the workspace policy travels with the call
    from analystos.runtime.context import workspace_call_ctx

    return lambda claims: workspace_call_ctx(claims.workspace_id, run_id=claims.run_id, task_id=claims.task_id,
                                             agent_id="worker")


@router.get("/artifacts/{artifact_id}")
async def read_artifact(artifact_id: str, claims: TokenClaims = Depends(worker_claims),
                        store: ArtifactStore = Depends(worker_store)) -> Response:
    claims.require_read(artifact_id)
    ref, workspace_id = await run_in_threadpool(store.ref, artifact_id)
    if workspace_id != claims.workspace_id:
        raise Forbidden("the artifact belongs to another workspace", details={"artifact_id": artifact_id})
    ref, data = await run_in_threadpool(store.read, artifact_id)
    return Response(content=data, media_type=ref.media_type,
                    headers={"X-Content-SHA256": ref.content_hash, "X-Artifact-Ref": json.dumps(ref.model_dump())})


@router.put("/tasks/{task_id}/outputs/{name}")
async def write_output(task_id: str, name: str, request: Request, claims: TokenClaims = Depends(worker_claims),
                       store: ArtifactStore = Depends(worker_store),
                       x_artifact_kind: str = Header(...), x_content_sha256: str = Header(...)) -> dict:
    claims.require_write(task_id, name)
    declared = int(request.headers.get("content-length") or 0)
    if claims.max_output_bytes and declared > claims.max_output_bytes:
        raise BudgetExceeded(f"output '{name}' is larger than the task's max_output_bytes ({claims.max_output_bytes})",
                             details={"limit": "output_bytes", "max_output_bytes": claims.max_output_bytes, "bytes": declared})
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if claims.max_output_bytes and len(body) > claims.max_output_bytes:
            raise BudgetExceeded(f"output '{name}' is larger than the task's max_output_bytes ({claims.max_output_bytes})",
                                 details={"limit": "output_bytes", "max_output_bytes": claims.max_output_bytes})
    others = [n for n in claims.writes if n != name]
    bound = await run_in_threadpool(store.bound_outputs, claims.workspace_id, claims.idempotency_key, others)
    ref, created = await run_in_threadpool(
        lambda: store.bind_output(claims.workspace_id, claims.idempotency_key, name, bytes(body), kind=x_artifact_kind,
                                  media_type=request.headers.get("content-type") or "application/octet-stream",
                                  content_hash=x_content_sha256, max_bytes=claims.max_output_bytes or None,
                                  already_bound=sum(r.bytes for r in bound.values())))
    return {"ref": ref.model_dump(), "created": created}


# Model calls a token has made, per API process. The purpose budget itself is the router's (usage sink).
_calls: dict[str, int] = {}
_calls_lock = threading.Lock()


@router.post("/model", response_model=ModelCallbackResponse)
def model_callback(body: ModelCallbackRequest, claims: TokenClaims = Depends(worker_claims),
                   router_=Depends(model_router), ctx_for=Depends(call_context)) -> ModelCallbackResponse:
    """The narrow model callback: only a purpose the token names, at most `max_model_calls` per token, through
    the control plane's router (its ladders, policy, spend caps and usage records). Workers hold no provider key."""
    claims.require_purpose(body.purpose)
    with _calls_lock:
        used = _calls.get(claims.token_id, 0)
        if used >= claims.max_model_calls:
            raise BudgetExceeded("this task has used its model calls", details={"limit": "model_calls",
                                                                                "max_model_calls": claims.max_model_calls})
        _calls[claims.token_id] = used + 1
    if any(set(m) - {"role", "content"} for m in body.messages):
        raise InvalidInput("messages carry role and content only")
    resp = router_.complete(body.purpose, body.messages, ctx=ctx_for(claims), max_tokens=body.max_tokens)
    return ModelCallbackResponse(text=resp.text, model=getattr(resp, "model", None), purpose=body.purpose,
                                 calls_left=claims.max_model_calls - used - 1)
