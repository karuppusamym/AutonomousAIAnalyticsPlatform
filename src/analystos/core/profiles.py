"""What this installation has (ADR-0025): optional Python extras and deployment features.

A feature that needs something the installation lacks is *unavailable with a reason*, never a broken
button: capability manifests declare `requires: [profile:bi]`, `[extra:ml]` or an isolated compute pool
(`[pool:compute-ml]`, ADR-0022), the registry
reports the reason (capabilities/registry.py `unavailable_reason`), and code paths that need an extra
call `require_extra` so a direct call fails with `FeatureUnavailable` naming the remedy.

Features are derived from settings, not declared twice: `standard` = the Temporal orchestrator,
`bi` = a Superset URL, `graph` = the Neo4j projection on, `demo` = the ServiceNow mock URL,
`sandbox` = container isolation, `pooled` = a transaction pooler.
"""
from __future__ import annotations

import importlib.util
from functools import lru_cache
from typing import Any

from analystos.core.errors import FeatureUnavailable

# extra -> (import names that must resolve, distributions to install). Mirrors pyproject.toml.
EXTRAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ml": (("sklearn", "statsmodels"), ("scikit-learn", "statsmodels")),
    "reports": (("matplotlib", "fpdf", "openpyxl"), ("matplotlib", "fpdf2", "openpyxl")),
    "temporal": (("temporalio",), ("temporalio",)),
    "graph": (("neo4j",), ("neo4j",)),
    "langgraph": (("langgraph",), ("langgraph",)),  # the optional LangGraph orchestrator backend (ADR-015)
}
FEATURES = ("standard", "bi", "graph", "demo", "sandbox", "pooled")
FEATURE_REMEDY = {
    "standard": "run the `standard` profile (Redis, Temporal, a worker): ANALYSTOS_PROFILE=standard, "
                "docker compose --profile standard up -d",
    "bi": "add the `bi` profile (Superset): docker compose --profile bi up -d and set ANALYSTOS_SUPERSET_URL",
    "graph": "add the `graph` profile (Neo4j): docker compose --profile graph up -d and ANALYSTOS_GRAPH_ENABLED=true",
    "demo": "add the `demo` profile (the ServiceNow mock): docker compose --profile demo up -d",
    "sandbox": "build the sandbox image (docker compose --profile sandbox build sandbox) and set "
               "ANALYSTOS_SANDBOX_ISOLATION=container",
    "pooled": "add the `pooled` profile (PgBouncer) and ANALYSTOS_DB_TRANSACTION_POOLER=true",
}


@lru_cache
def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def importable(module: str) -> bool:
    """Whether `module` resolves here (for a feature that needs one library of an extra, not all of it)."""
    return _importable(module)


def missing_modules(extra: str) -> list[str]:
    modules, _ = EXTRAS[extra]
    return [m for m in modules if not _importable(m)]


def has_extra(extra: str) -> bool:
    return not missing_modules(extra)


def extra_reason(extra: str) -> str | None:
    """None when the extra is installed, else why the feature is unavailable and how to add it."""
    if extra not in EXTRAS:
        return f"unknown extra {extra!r}"
    missing = missing_modules(extra)
    if not missing:
        return None
    return (f"needs the `{extra}` extra ({', '.join(EXTRAS[extra][1])}); not installed here: {', '.join(missing)}. "
            f"Install analystos[{extra}] or use an image that has it")


def require_extra(extra: str, feature: str) -> None:
    reason = extra_reason(extra)
    if reason:
        raise FeatureUnavailable(f"{feature} is unavailable: {reason}",
                                 details={"extra": extra, "feature": feature, "missing": missing_modules(extra)})


def active_features(settings: Any = None) -> set[str]:
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    out = set()
    from analystos.workflows.backends import capabilities

    if capabilities(settings).durable:  # the Temporal orchestrator and its workers
        out.add("standard")
    if settings.superset_url:
        out.add("bi")
    if settings.graph_enabled:
        out.add("graph")
    if settings.servicenow_mock_url:
        out.add("demo")
    if settings.sandbox_isolation == "container":
        out.add("sandbox")
    if settings.db_transaction_pooler:
        out.add("pooled")
    return out


def feature_reason(feature: str, settings: Any = None) -> str | None:
    if feature not in FEATURES:
        return f"unknown profile {feature!r}"
    if feature in active_features(settings):
        return None
    return f"needs the `{feature}` profile, which this installation does not run; {FEATURE_REMEDY[feature]}"


def requirement_reason(req: str, settings: Any = None) -> str | None:
    """Reason a manifest `requires` entry (profile:<name> | extra:<name>) is unmet here; None = met or
    not an installation requirement (capability ids and engine features are checked elsewhere)."""
    if req.startswith("profile:"):
        return feature_reason(req.split(":", 1)[1], settings)
    if req.startswith("extra:"):
        return extra_reason(req.split(":", 1)[1])
    if req.startswith("pool:"):  # an isolated compute pool (ADR-0022, P7-06), e.g. pool:compute-ml
        from analystos.workers.dispatch import pool_reason

        return pool_reason(req.split(":", 1)[1], settings)
    return None


def summary(settings: Any = None) -> dict[str, Any]:
    """For /api/health and the admin view: the profile, its features and the installed extras."""
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    return {"profile": settings.profile, "features": sorted(active_features(settings)),
            "extras": {e: has_extra(e) for e in EXTRAS}, "orchestrator": orchestrator_summary(settings)}


def orchestrator_summary(settings: Any) -> dict[str, Any]:
    """The selected orchestrator backend, its capabilities, and why it cannot start here (None = it can)."""
    from analystos.workflows.backends import capabilities

    caps = capabilities(settings)
    reason = extra_reason(caps.extra) if caps.extra else None
    return {"backend": caps.name, "durable": caps.durable, "needs_worker": caps.needs_worker, "parks": caps.parks,
            "run_driver": getattr(settings, "run_driver", "api") if caps.parks else None,
            "available": reason is None, "reason": reason}
