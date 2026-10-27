"""Workspace notebooks (P7-12): markdown, SQL and restricted-Python cells, each cell a step.

DataPilot's notebook *cell model* is the idea borrowed (ordered cells with a type and source, outputs
kept per execution); its runtime is not ported. Here a cell is an `AnalysisStep` in the notebook's
container, so it inherits the step model:

* **SQL** cells are `query` steps: through `QueryGateway` under the caller's scope, self-checked.
* **Python** cells are `method` steps: restricted Python in `steps.execute_python` (the isolated `compute-py`
  pool when configured, else the sandbox, behind the same function) on the results of the cells they read, passed in as `inputs`
  (keyed by step id and by `cell<N>`). The code can open no connection: data arrives only through SQL
  cells, i.e. through the gateway.
* **Markdown** cells are `claim` steps; when they read other cells, every number in them must bind to
  those cells' results.

Every execution is a new version; editing a cell voids the verdicts of the cells that read it and
re-runs them (`steps.edit`).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.step import CellEdit, CellIn, NotebookIn, StepEdit, StepIn
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, ordered_utcnow, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, AnalysisStepVersion, Notebook, User
from analystos.events.bus import emit
from analystos.governance.policy import require_role
from analystos.services import steps as steps_svc

CELL_KIND = {"sql": "query", "python": "method", "markdown": "claim"}


def cell_type(step: AnalysisStep, spec: dict[str, Any]) -> str:
    return spec.get("cell") or {"query": "sql", "method": "python", "claim": "markdown"}.get(step.kind, step.kind)


def cell_spec(cell: str, source: str, source_id: str | None = None) -> dict[str, Any]:
    if cell == "sql":
        return {"cell": "sql", "sql": source, **({"source_id": source_id} if source_id else {})}
    if cell == "python":
        return {"cell": "python", "code": source}
    if cell == "markdown":
        return {"cell": "markdown", "text": source}
    raise InvalidInput("cell must be markdown, sql or python")


def cell_view(session: Session, step: AnalysisStep, ver: AnalysisStepVersion | None = None, *, inherited: bool = False) -> dict[str, Any]:
    """A cell as the notebook shows it: the step view plus its cell type and source (one shape for GET and POST)."""
    view = steps_svc.view(session, step, ver, inherited=inherited)
    spec = view["spec"]
    return {**view, "cell": cell_type(step, spec), "source": spec.get("sql") or spec.get("code") or spec.get("text") or ""}


def notebook_view(session: Session, nb: Notebook) -> dict[str, Any]:
    branch = steps_svc.main_branch(session, nb.workspace_id, "notebook", nb.id, nb.created_by)
    cells = [cell_view(session, st, v, inherited=inh) for st, v, inh in steps_svc.effective_steps(session, branch)]
    return {"id": nb.id, "workspace_id": nb.workspace_id, "title": nb.title, "revision": nb.revision, "archived": nb.archived,
            "branch_id": branch.id, "created_by": nb.created_by,
            "created_at": nb.created_at.isoformat() if nb.created_at else None,
            "updated_at": nb.updated_at.isoformat() if nb.updated_at else None, "cells": cells}


def create(session: Session, user: User, workspace_id: str, body: NotebookIn) -> dict[str, Any]:
    require_role(session, user, workspace_id, "analyst")
    nb = Notebook(id=new_id("nb"), workspace_id=workspace_id, title=body.title.strip()[:300], revision=1, archived=False,
                  created_by=f"user:{user.id}", created_at=ordered_utcnow(), updated_at=utcnow())
    session.add(nb)
    session.flush()
    steps_svc.main_branch(session, workspace_id, "notebook", nb.id, nb.created_by)
    emit(workspace_id, "notebook.created", {"notebook_id": nb.id, "title": nb.title}, actor=f"user:{user.id}", session=session)
    return notebook_view(session, nb)


def list_notebooks(session: Session, user: User, workspace_id: str) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    return [{"id": n.id, "title": n.title, "revision": n.revision, "archived": n.archived, "created_by": n.created_by,
             "updated_at": n.updated_at.isoformat() if n.updated_at else None}
            for n in session.scalars(select(Notebook).where(Notebook.workspace_id == workspace_id)
                                     .order_by(Notebook.updated_at.desc(), Notebook.id))]


def _touch(session: Session, notebook_id: str, actor: str, what: str, **extra: Any) -> None:
    nb = session.get(Notebook, notebook_id)
    nb.revision += 1
    nb.updated_at = utcnow()
    emit(nb.workspace_id, "notebook.updated", {"notebook_id": nb.id, "revision": nb.revision, "change": what, **extra},
         actor=actor, session=session)


def add_cell(user: User, workspace_id: str, notebook_id: str, body: CellIn, *, runtime: Any = None) -> dict[str, Any]:
    with session_scope() as s:
        nb = s.get(Notebook, notebook_id)
        if nb is None or nb.workspace_id != workspace_id:
            raise NotFound("notebook not found")
        branch = steps_svc.main_branch(s, workspace_id, "notebook", notebook_id, f"user:{user.id}")
        deps = body.depends_on
        if deps is None:
            deps = [st.id for st in s.scalars(select(AnalysisStep).where(AnalysisStep.branch_id == branch.id)
                                              .order_by(AnalysisStep.seq)) if st.kind in ("query", "method")] \
                if body.cell == "python" else []
        branch_id = branch.id
    out = steps_svc.create(user, workspace_id, branch_id,
                           StepIn(kind=CELL_KIND[body.cell], title=body.title or f"{body.cell} cell",  # type: ignore[arg-type]
                                  spec=cell_spec(body.cell, body.source, body.source_id), depends_on=deps),
                           runtime=runtime, origin={"type": "cell", "notebook_id": notebook_id})
    with session_scope() as s:
        _touch(s, notebook_id, f"user:{user.id}", "cell_added", step_id=out["id"])
        step = s.get(AnalysisStep, out["id"])
        return cell_view(s, step, steps_svc.version_row(s, step, out["version"]))


def edit_cell(user: User, step_id: str, body: CellEdit, *, expected_version: int | None, runtime: Any = None) -> dict[str, Any]:
    with session_scope() as s:
        st = s.get(AnalysisStep, step_id)
        if st is None or st.container_type != "notebook":
            raise NotFound("cell not found")
        ver = steps_svc.version_row(s, st)
        spec = cell_spec(cell_type(st, ver.spec), body.source, ver.spec.get("source_id"))
        notebook_id = st.container_id
    out = steps_svc.edit(user, step_id, StepEdit(spec=spec, title=body.title), expected_version=expected_version,
                         runtime=runtime)
    with session_scope() as s:
        _touch(s, notebook_id, f"user:{user.id}", "cell_edited", step_id=step_id, voided=len(out["voided_records"]))
    return out


def run_all(user: User, workspace_id: str, notebook_id: str, *, runtime: Any = None) -> dict[str, Any]:
    """Execute every cell once more, in dependency order: each a new version, on today's data."""
    with session_scope() as s:
        nb = s.get(Notebook, notebook_id)
        if nb is None or nb.workspace_id != workspace_id:
            raise NotFound("notebook not found")
        branch = steps_svc.main_branch(s, workspace_id, "notebook", notebook_id, f"user:{user.id}")
        order = [st.id for st in s.scalars(select(AnalysisStep).where(AnalysisStep.branch_id == branch.id)
                                           .order_by(AnalysisStep.seq))]
    rt = runtime if runtime is not None else steps_svc.GatewayRuntime(user, workspace_id)
    results = []
    for sid in order:
        with session_scope() as s:
            steps_svc.new_version(s, s.get(AnalysisStep, sid), actor=f"user:{user.id}", reason="rerun")
        results.append(steps_svc.execute(user, sid, runtime=rt))
    with session_scope() as s:
        _touch(s, notebook_id, f"user:{user.id}", "run_all")
        return {**notebook_view(s, s.get(Notebook, notebook_id)),
                "executed": [{"id": r["id"], "version": r["version"], "status": r["status"]} for r in results]}
