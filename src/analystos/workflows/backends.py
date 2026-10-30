"""Orchestrator backends (ADR-015): what each one can do, and the one interface callers use.

`ANALYSTOS_ORCHESTRATOR` picks a backend. Callers ask for a *capability* (`durable`, `needs_worker`, the
isolated-pool transport) instead of comparing the name with "temporal", so a third backend does not
mean another round of `if orchestrator == ...` edits. All run state stays in Postgres under every backend;
a backend is only the driver of the engine loop (`runtime/engine.py`, `runtime/loop.py`).

| Backend | Drives runs in | Waits | Durable without the API process |
|---|---|---|---|
| `temporal` | `analystos worker` (AnalysisWorkflow) | nudge signal + timer | yes (Temporal history) |
| `local` | the API process (`LocalRuntime`) | parks; signal / sweep / resume | no (resume at start) |
| `langgraph` | the API process (a stateless LangGraph graph on `LocalRuntime`) | parks, as local | no (resume at start) |

`ANALYSTOS_RUN_DRIVER=process` moves the drive loop of `local` and `langgraph` out of the API into
`analystos driver` processes that hold Postgres run leases (ORC-T7, `workflows/driver.py`).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, runtime_checkable

Name = Literal["temporal", "local", "langgraph"]


@dataclass(frozen=True)
class Capabilities:
    name: str
    # Runs survive the loss of every API process without a resume step (Temporal history + workers).
    durable: bool
    # Runs execute only while `analystos worker` processes poll the run queues (health checks them).
    needs_worker: bool
    # The drive loop lives in this platform's own processes (API or `analystos driver`): it parks waits
    # and is resumed from Postgres task state after a restart.
    parks: bool
    # How `isolated_transport=auto` reaches isolated compute pools (ADR-0022, P7-06).
    isolated_transport: Literal["temporal", "subprocess"]
    # The optional extra the backend needs (None = core install).
    extra: str | None
    # Crawls, recipe and ML compute go to Temporal pools (else in process or the subprocess transport).
    remote_compute: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


CAPABILITIES: dict[str, Capabilities] = {
    "temporal": Capabilities("temporal", durable=True, needs_worker=True, parks=False, isolated_transport="temporal",
                             extra="temporal", remote_compute=True),
    "local": Capabilities("local", durable=False, needs_worker=False, parks=True, isolated_transport="subprocess",
                          extra=None, remote_compute=False),
    "langgraph": Capabilities("langgraph", durable=False, needs_worker=False, parks=True,
                              isolated_transport="subprocess", extra="langgraph", remote_compute=False),
}


def capabilities(orchestrator: Any = None) -> Capabilities:
    """Capabilities of a backend name, of a settings object's `orchestrator`, or of the configured one."""
    if orchestrator is None:
        from analystos.core.config import get_settings

        orchestrator = get_settings()
    name = orchestrator if isinstance(orchestrator, str) else getattr(orchestrator, "orchestrator", "temporal")
    try:
        return CAPABILITIES[name]
    except KeyError:
        raise ValueError(f"unknown orchestrator {name!r}; choose one of {', '.join(CAPABILITIES)}") from None


@runtime_checkable
class OrchestratorBackend(Protocol):
    """What the API needs from an orchestrator. Implementations hold no run state of their own."""

    name: str
    capabilities: Capabilities

    def start_run(self, run_id: str) -> str:
        """Start driving a run (idempotent: a run already driven is nudged). Returns a handle id."""

    def signal_run(self, run_id: str) -> None:
        """Something the run waits on changed (an approval, a control, feedback)."""

    def resume(self, run_ids: list[str] | None = None) -> list[str]:
        """Re-drive non-terminal runs after a restart; returns the runs it took up ([] if not applicable)."""

    def health(self) -> dict[str, Any]:
        """Backend-specific status for /api/health (never raises)."""


def validate(settings: Any) -> None:
    """Startup check: the selected backend's extra is installed, and its guards hold. Raises
    `FeatureUnavailable` with the remedy, the same way a missing `temporal` extra does."""
    from analystos.core.profiles import require_extra

    caps = capabilities(settings)
    if caps.extra:
        require_extra(caps.extra, f"the {caps.name} orchestrator (ANALYSTOS_ORCHESTRATOR={caps.name})")
    if caps.name == "langgraph":
        from analystos.workflows.langgraph_guard import check_tracing

        check_tracing(settings)


def get_backend(settings: Any = None) -> OrchestratorBackend:
    if settings is None:
        from analystos.core.config import get_settings

        settings = get_settings()
    caps = capabilities(settings)
    from analystos.workflows import orchestrator

    if caps.name == "temporal":
        return orchestrator.TemporalBackend()
    return orchestrator.InProcessBackend(caps)
