"""Egress guard for the LangGraph backend (ADR-015). Kept apart from `langgraph_backend.py` so a core install
(no `langgraph` extra) can still report why the backend cannot start.

LangChain's tracing client (LangSmith) sends run inputs and outputs to a hosted service when the
environment turns it on. Under this platform a graph's state is only run ids and outcomes, but a run id
names a customer's work and tracing could later pick up more, so nothing leaves by default: the backend
refuses to start while tracing is on, unless an operator opts in with ANALYSTOS_LANGGRAPH_ALLOW_TRACING=true.
Each invocation also runs with tracing explicitly disabled unless that opt-in is set."""
from __future__ import annotations

import os
from typing import Any

from analystos.core.errors import FeatureUnavailable

# Every variable the LangChain / LangSmith clients read to turn tracing on.
TRACING_VARS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
_TRUE = {"1", "true", "yes", "on"}


def tracing_requested(environ: Any = None) -> list[str]:
    """The tracing variables that are on in `environ` (default: the process environment)."""
    env = os.environ if environ is None else environ
    return [v for v in TRACING_VARS if str(env.get(v, "")).strip().lower() in _TRUE]


def tracing_allowed(settings: Any) -> bool:
    return bool(getattr(settings, "langgraph_allow_tracing", False))


def check_tracing(settings: Any, environ: Any = None) -> None:
    """Refuse the langgraph backend while LangSmith tracing is on without the operator's opt-in."""
    on = tracing_requested(environ)
    if on and not tracing_allowed(settings):
        raise FeatureUnavailable(
            f"the langgraph orchestrator will not start while {', '.join(on)} is on: LangSmith tracing sends run "
            "state to a hosted service. Unset it, or set ANALYSTOS_LANGGRAPH_ALLOW_TRACING=true to allow it",
            details={"tracing_vars": on})
