"""P4-S02: SQL statements per engine operation, counted with a `before_cursor_execute` listener on
the control-plane engine (Postgres), on a fixture of 3 sources x 10 selected assets x 30 columns, a
12-task run and a workspace holding 3,000 lineage edges unrelated to the artifact looked up.

The upper bounds below lock in the P4-S02 numbers. Set ``ANALYSTOS_ENGINE_QUERIES_OUT`` to a file
path to also write the measured table (the evidence report is built from it)."""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, insert, select

pytestmark = pytest.mark.integration

SOURCES, ASSETS, COLUMNS = 3, 10, 30
NOISE_EDGES, OTHER_WS_EDGES = 3000, 1000


# The per-transaction tenant binding (ADR-009, strict tenancy): two fixed statements, not data access; they are
# counted apart so the budgets below keep measuring the engine's own reads and writes.
TENANCY_PRELUDE = ("SET LOCAL ROLE", "SELECT set_config('app.tenant_id'")


class _Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.tenancy = 0
        self.rows = 0
        self.marks: dict[str, int] = {}

    @property
    def for_update(self) -> int:
        return sum("FOR UPDATE" in s for s in self.statements)

    def mark(self, name: str) -> None:
        self.marks[name] = len(self.statements)


@contextmanager
def count_sql():
    """Statements (and rows returned) on control-plane connections of this thread inside the block."""
    from sqlalchemy.engine import Engine, make_url

    from analystos.core.config import get_settings

    engine, counter = Engine, _Counter()
    database, thread = make_url(get_settings().database_url).database, threading.get_ident()

    def ours(conn) -> bool:  # noqa: ANN001
        return threading.get_ident() == thread and conn.engine.url.database == database

    def before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if ours(conn):
            if statement.lstrip().startswith(TENANCY_PRELUDE):
                counter.tenancy += 1
            else:
                counter.statements.append(statement)

    def after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if ours(conn) and cursor.description is not None and cursor.rowcount > 0:
            counter.rows += cursor.rowcount

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)


@pytest.fixture(scope="module")
def bench(control_db):
    from analystos.core.ids import new_id, utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import (
        AnalysisRun,
        LineageEdge,
        RunTask,
        Source,
        SourceAsset,
        SourceColumn,
        User,
        Workspace,
        WorkspaceMember,
    )

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        analyst = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        ws, other = new_id("ws"), new_id("ws")
        for w in (ws, other):
            s.add(Workspace(id=w, name=f"engine queries {w}", created_by=admin.id))
        s.flush()
        s.add(WorkspaceMember(workspace_id=ws, user_id=analyst.id, role="analyst"))
        for i in range(SOURCES):
            sid = new_id("src")
            s.add(Source(id=sid, workspace_id=ws, kind="csv", name=f"bench {i}", config={}, status="ready"))
            s.flush()
            for j in range(ASSETS + 2):  # two unselected assets per source are not in scope
                aid = new_id("ast")
                s.add(SourceAsset(id=aid, source_id=sid, workspace_id=ws, schema_name=f"src_bench_{i}", name=f"t{j:02d}",
                                  source_name=f"t{j:02d}", kind="table", selected=j < ASSETS))
                s.flush()
                s.execute(insert(SourceColumn), [
                    {"asset_id": aid, "name": f"c{k:02d}", "ordinal": COLUMNS - k, "data_type": "text",
                     "tags": ["pii"] if k < 2 else [], "profile": {}, "semantics": {}} for k in range(COLUMNS)])
        run = new_id("run")
        s.add(AnalysisRun(id=run, workspace_id=ws, objective="bench", status="RUNNING", plan={"steps": []}, plan_version=1,
                          scope={"hash": "h"}, instructions=[], constraints={}, control="run", requested_by=analyst.id,
                          summary={}, origin={}))
        s.flush()
        tasks = [("done0", "COMPLETED", []), ("done1", "COMPLETED", ["done0"]), ("done2", "COMPLETED", ["done0"]),
                 ("busy", "RUNNING", ["done0"]), ("ready0", "NEW", ["done1"]), ("ready1", "NEW", ["done1"]),
                 ("ready2", "NEW", ["done2"]), ("later0", "NEW", ["ready0"]), ("later1", "NEW", ["ready1"]),
                 ("later2", "NEW", ["later0", "later1"]), ("later3", "NEW", ["later2"]), ("later4", "NEW", ["later3"])]
        for i, (key, st, deps) in enumerate(tasks):
            s.add(RunTask(id=new_id("tsk"), run_id=run, key=key, agent_id="a", title=key, status=st, depends_on=deps,
                          input={}, output={}, plan_version=1, seq=i * 10, started_at=utcnow() if st == "RUNNING" else None))
        # Lineage: a small neighbourhood around the artifact looked up, plus unrelated edges in the same
        # workspace (other runs) and in another workspace.
        near = [(("table", "src_bench_0.t00"), "built_from", ("dataset", "ds_1")),
                (("dataset", "ds_1"), "feeds", ("artifact", "art_target")),
                (("artifact", "art_target"), "renders", ("chart", "ch_1")),
                (("chart", "ch_1"), "part_of", ("dashboard", "db_1")),
                (("insight", "ins_1"), "about", ("artifact", "art_target")),
                (("query", "q_1"), "produced", ("dataset", "ds_1"))]
        rows = [{"workspace_id": ws, "from_type": a[0], "from_id": a[1], "relation": r, "to_type": b[0], "to_id": b[1]}
                for a, r, b in near]
        rows += [{"workspace_id": ws, "from_type": "query", "from_id": f"q_noise_{n}", "relation": "produced",
                  "to_type": "artifact", "to_id": f"art_noise_{n}"} for n in range(NOISE_EDGES)]
        rows += [{"workspace_id": other, "from_type": "artifact", "from_id": "art_target", "relation": "renders",
                  "to_type": "chart", "to_id": f"ch_other_{n}"} for n in range(OTHER_WS_EDGES)]
        s.execute(insert(LineageEdge), rows)
    return SimpleNamespace(ws=ws, run=run, analyst_email="analyst@analystos.local")


@pytest.fixture()
def fake_dispatch(monkeypatch):
    from analystos.agents import dispatch as dispatch_mod
    from analystos.runtime import engine

    state = SimpleNamespace(counter=None)

    def fake(ctx):
        if state.counter is not None:
            state.counter.mark("dispatch")
        return {"ok": True}

    monkeypatch.setattr(dispatch_mod, "dispatch", fake)
    monkeypatch.setattr(engine, "RunContext", SimpleNamespace(load=lambda run_id, key, services: SimpleNamespace(key=key)))
    return state


def _measure(bench, fake_dispatch) -> dict[str, dict]:
    from analystos.artifacts.registry import lineage_for
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, User
    from analystos.governance.policy import resolve_scope
    from analystos.runtime import engine

    out: dict[str, dict] = {}

    def record(name: str, c: _Counter, statements: int | None = None) -> None:
        out[name] = {"statements": len(c.statements) if statements is None else statements, "rows": c.rows,
                     "for_update": c.for_update}

    engine.get_state(bench.run)  # warm-up: registry/playbook caches, connection pool
    with count_sql() as c:
        state = engine.get_state(bench.run)
    assert state == {"ready": ["ready0", "ready1", "ready2"]}
    record("get_state (steady: 3 ready, 1 running)", c)

    with session_scope() as s:
        s.get(AnalysisRun, bench.run).status = "READY"
    with count_sql() as c:
        assert engine.get_state(bench.run)["ready"]
    record("get_state (transition READY -> RUNNING)", c)

    with count_sql() as c:
        fake_dispatch.counter = c
        assert engine.execute_task(bench.run, "ready0", services=object())["status"] == "COMPLETED"
        fake_dispatch.counter = None
    out["execute_task claim"] = {"statements": c.marks["dispatch"],
                                 "rows": None, "for_update": sum("FOR UPDATE" in st for st in c.statements[:c.marks["dispatch"]])}
    record("execute_task (claim + complete)", c)

    with count_sql() as c:
        assert engine.execute_task(bench.run, "ready0", services=object())["status"] == "COMPLETED"
    record("execute_task (already completed)", c)

    with session_scope() as s:
        lineage_for(s, bench.ws, ("artifact", "art_target"))  # warm-up
    with session_scope() as s, count_sql() as c:
        graph = lineage_for(s, bench.ws, ("artifact", "art_target"))
    assert len(graph["edges"]) == 6 and len(graph["nodes"]) == 7
    record("lineage_for (depth 6)", c)

    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == bench.analyst_email))
        with count_sql() as c:
            scope = resolve_scope(s, user, bench.ws)
    assert len(scope.assets) == SOURCES * ASSETS and len(scope.denied_columns) == SOURCES * ASSETS * 2
    assert all(cols == [f"c{k:02d}" for k in reversed(range(COLUMNS))] for cols in scope.columns.values())
    record(f"resolve_scope ({SOURCES} sources x {ASSETS} assets x {COLUMNS} columns)", c)
    return out


# Upper bounds after P4-S02 (statements per call). The claim is read + compare-and-set + agent.started
# event; resolve_scope is workspace, membership, policy, sources, assets, columns and the tenant (S01 ABAC
# attributes, e.g. residency) whatever the size. The tenant-binding prelude is counted apart (TENANCY_PRELUDE).
BOUNDS = {
    "get_state (steady: 3 ready, 1 running)": 2,
    "get_state (transition READY -> RUNNING)": 6,
    "execute_task claim": 3,
    "execute_task (claim + complete)": 7,
    "execute_task (already completed)": 1,
    "lineage_for (depth 6)": 1,
    f"resolve_scope ({SOURCES} sources x {ASSETS} assets x {COLUMNS} columns)": 7,
}


def test_engine_query_counts(bench, fake_dispatch):
    measured = _measure(bench, fake_dispatch)
    lines = ["| Operation | Statements | Rows returned | FOR UPDATE |", "|---|---:|---:|---:|"]
    lines += [f"| {k} | {v['statements']} | {'-' if v['rows'] is None else v['rows']} | {v['for_update']} |"
              for k, v in measured.items()]
    table = "\n".join(lines)
    print("\n" + table)
    if path := os.environ.get("ANALYSTOS_ENGINE_QUERIES_OUT"):
        with open(path, "w") as fh:
            fh.write(table + "\n")
    if os.environ.get("ANALYSTOS_ENGINE_QUERIES_BASELINE"):
        return  # measuring the unmodified code: no bounds
    for name, bound in BOUNDS.items():
        assert measured[name]["statements"] <= bound, (name, measured[name])
    assert measured["get_state (steady: 3 ready, 1 running)"]["for_update"] == 0
    assert measured["execute_task claim"]["for_update"] == 0
    assert measured["lineage_for (depth 6)"]["rows"] <= 20  # the neighbourhood, not the workspace's 3,006 edges
