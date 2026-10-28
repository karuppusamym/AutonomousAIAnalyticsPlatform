"""P4-01 (SEC-002): every API route that addresses a workspace child by id resolves it through
`load_in_workspace` (directly, or through a helper marked `@scoped_loader` that does), passing the
path's `workspace_id` when the path has one. The app's routes are enumerated, so a new route that
forgets the binding fails here instead of leaking a run, finding or thread across workspaces."""
from __future__ import annotations

import ast
import importlib
import inspect
import textwrap

from fastapi.routing import APIRoute

from analystos.api.app import app
from analystos.api.deps import admin_user
from analystos.governance.policy import SCOPED_LOADERS, load_in_workspace

LOADER = f"{load_in_workspace.__module__}.{load_in_workspace.__qualname__}"

# Path parameters that do not name a workspace child, or are looked up by name inside the path's workspace.
NOT_A_CHILD_ID = {
    "workspace_id": "the workspace itself (require_role)",
    "action": "a verb, not an object",
    "column": "a column name of the (bound) asset",
    "tool_name": "a tool name inside the (bound) MCP server's snapshot",
    "server_name": "looked up by name inside the path's workspace (server_by_name filters workspace_id)",
    "name": "a metric name, queried inside the workspace_id query parameter (semantic service filters by it)",
    "capability_id": "a platform-wide registry entry, not a workspace child; enablement is per workspace",
    "user_id": "a platform user; membership changes are owner-only in the path's workspace",
    "tenant_id": "above workspaces (ADR-009): must equal the tenant the request acts for, else 404 (tenants.py)",
    "tool_id": "a registry tool id, resolved only inside the workspace_id the caller names (S10: toolreg.store filters "
               "workspace_id; the caller's role or grant there is checked first)",
}
# Routes whose child id is never read (a fixed answer for any id), or that no user session can reach.
WORKER_TOKEN = ("isolated-worker route (P7-06): authenticated only by a scoped task token bound to its workspace, "
                "task, artifact ids / output names and verbs; the store also checks the artifact's own workspace "
                "(test_worker_routes_accept_only_task_tokens)")
EXEMPT_ROUTES = {
    ("POST", "/api/dashboards/{artifact_id}/schedule"): "always answers with where to create a schedule; reads nothing",
    ("GET", "/api/worker/artifacts/{artifact_id}"): WORKER_TOKEN,
    ("PUT", "/api/worker/tasks/{task_id}/outputs/{name}"): WORKER_TOKEN,
    ("POST", "/v2/triggers/webhook/{rule_id}"): "signed trigger ingress (S09-T1): no user session; authenticated by the "
                                                "rule's own HMAC secret, then written as the rule's owning tenant",
    ("POST", "/v2/channels/{kind}/{binding_id}"): "signed inbound channel message (S09-T5): no user session; authenticated "
                                                  "by the binding's own secret, then written as its owning tenant",
}


def _api_routes(routes=None, prefix: str = ""):
    """(path, route) for every APIRoute, through included routers (FastAPI keeps them nested)."""
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router.routes, prefix + route.include_context.prefix)


def _child_params(route: APIRoute) -> list[str]:
    return [p for p in route.param_convertors if p not in NOT_A_CHILD_ID]


def _is_admin_only(route: APIRoute) -> bool:
    def calls(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from calls(dep)
    return admin_user in set(calls(route.dependant))


def _tree(fn) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(fn))))


def _local_imports(tree: ast.AST) -> dict[str, object]:
    names: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            module = importlib.import_module(node.module)
            for alias in node.names:
                names[alias.asname or alias.name] = getattr(module, alias.name, None)
    return names


def _resolve(fn, tree: ast.AST, func: ast.AST) -> object | None:
    scope = {**vars(inspect.getmodule(fn)), **_local_imports(tree)}
    if isinstance(func, ast.Name):
        return scope.get(func.id)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = scope.get(func.value.id)
        return getattr(owner, func.attr, None) if owner is not None else None
    return None


def _qual(obj: object) -> str | None:
    obj = inspect.unwrap(obj) if callable(obj) else obj
    return f"{obj.__module__}.{obj.__qualname__}" if hasattr(obj, "__qualname__") else None


def _scoped_calls(fn) -> list[tuple[str, set[str]]]:
    """(target, argument names) of each call in `fn` to load_in_workspace or a registered loader."""
    tree = _tree(fn)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _qual(_resolve(fn, tree, node.func))
        if target == LOADER or target in SCOPED_LOADERS:
            args = [*node.args, *(k.value for k in node.keywords)]
            out.append((target, {a.id for a in args if isinstance(a, ast.Name)}))
    return out


def test_every_scoped_loader_goes_through_load_in_workspace():
    assert SCOPED_LOADERS, "no scoped loaders registered"
    for qual in sorted(SCOPED_LOADERS):
        module, _, name = qual.rpartition(".")
        while module and not _importable(module):
            module, _, outer = module.rpartition(".")
            name = f"{outer}.{name}"
        obj = importlib.import_module(module)
        for part in name.split("."):
            obj = getattr(obj, part)
        assert _scoped_calls(obj), f"{qual} is marked @scoped_loader but never calls load_in_workspace or another loader"


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def test_every_child_id_route_binds_the_child_to_its_workspace():
    checked, missing = 0, []
    for path, route in _api_routes():
        if _is_admin_only(route):
            continue
        children = _child_params(route)
        if not children:
            continue
        method = sorted(route.methods)[0]
        if (method, path) in EXEMPT_ROUTES:
            continue
        calls = _scoped_calls(route.endpoint)
        has_ws = "workspace_id" in route.param_convertors
        for child in children:
            if not any(child in args and (not has_ws or "workspace_id" in args) for _, args in calls):
                missing.append(f"{method} {path} ({child}{' + workspace_id' if has_ws else ''})")
        checked += 1
    assert checked > 40, checked  # the enumeration really saw the app's child routes
    assert not missing, "routes that do not bind their child id through load_in_workspace:\n  " + "\n  ".join(missing)


def test_exemptions_still_name_real_routes():
    paths = {(sorted(r.methods)[0], path) for path, r in _api_routes()}
    assert set(EXEMPT_ROUTES) <= paths


def test_worker_routes_accept_only_task_tokens():
    """Every /api/worker route depends on the task-token check and on no user-session dependency, and each
    calls the claim check for what it touches (read, write or model purpose)."""
    from analystos.api.deps import current_user
    from analystos.api.routers import worker as worker_routes

    def calls(dependant):
        for dep in dependant.dependencies:
            yield dep.call
            yield from calls(dep)

    routes = [(path, r) for path, r in _api_routes() if path.startswith("/api/worker/")]
    assert len(routes) == 3
    for path, route in routes:
        deps = set(calls(route.dependant))
        assert worker_routes.worker_claims in deps and current_user not in deps, path
        source = inspect.getsource(route.endpoint)
        assert any(check in source for check in ("require_read(", "require_write(", "require_purpose(")), path
