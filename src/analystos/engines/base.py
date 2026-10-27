"""The `Engine` protocol (spec v3 §7.1, ADR-0014, P4-E01).

An engine is *where* a validated statement runs: a source's own database (pushdown), the
analytics DB (staged snapshots) or a federating engine (DuckDB, later Trino/Spark). Engines sit
behind ``QueryGateway``; they are never called with SQL the validator has not produced, and they
never pick their own identity: the gateway hands them a ``ReaderIdentity`` built from the source's
least-privilege configuration (or the analytics reader). Writes are not part of this increment:
``plan_write`` / ``execute_write`` refuse until the BuildGateway (P4-E06) exists.

An engine is ``draft`` until its kind is certified against a live instance; the status is derived
from the connector evidence files (``connectors/certification.py``), never declared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from analystos.core.errors import Forbidden
from analystos.gateway.types import CostEstimate, ValidatedSQL

Feature = Literal["pushdown", "federate", "materialize", "write", "spark_sql", "python_udf"]
EngineStatus = Literal["draft", "certified"]
Rows = tuple[list[str], list[list[Any]]]


@dataclass(frozen=True)
class Limits:
    max_rows: int
    timeout_seconds: int


@dataclass(frozen=True)
class ReaderIdentity:
    """Who the engine connects as. ``url`` carries a secret: it is excluded from repr and never logged."""

    url: str | None = field(default=None, repr=False)
    role: str | None = None  # staged: the workspace reader role, SET LOCAL for the transaction
    session_sql: tuple[str, ...] = ()  # read-only / timeout statements run first on the connection
    path: str | None = None  # file engines: the database file (already confined to the upload dir)


@runtime_checkable
class Engine(Protocol):
    id: str
    dialect: str
    features: frozenset[str]

    def execute_read(self, stmt: ValidatedSQL, *, identity: ReaderIdentity, limits: Limits) -> Rows: ...

    def estimate(self, stmt: ValidatedSQL) -> CostEstimate: ...

    def plan_write(self, build: Any) -> Any: ...

    def execute_write(self, approved: Any) -> Any: ...


class ReadOnlyEngine:
    """Shared behaviour: no writes in this increment, no cost model unless an engine has one."""

    id = "engine"
    dialect = "postgres"
    features: frozenset[str] = frozenset()

    def estimate(self, stmt: ValidatedSQL) -> CostEstimate:  # noqa: ARG002 - engines override when they can
        return CostEstimate(basis="not available for this engine")

    def plan_write(self, build: Any) -> Any:  # noqa: ARG002
        raise Forbidden(f"{self.id} is read-only here: writes go through the BuildGateway (P4-E06), not a query engine")

    def execute_write(self, approved: Any) -> Any:  # noqa: ARG002
        raise Forbidden(f"{self.id} is read-only here: writes go through the BuildGateway (P4-E06), not a query engine")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self.id!r}, dialect={self.dialect!r})"
