"""What-if scenarios (N-9): parameterise a governed `SemanticQuery`, keep observed and simulated apart.

1. **Validate** — the spec (`contracts/scenario.py`) and that every change names something the query
   returns (`skills/scenario.validate`); an Ask turn it refers to must be the caller's own.
2. **Observe** — the query compiles against the approved model version (`semantic/compiler.py`) and runs
   through `QueryGateway.execute` under the caller's scope, tool gate and Ask budget. A threshold change
   is the same query with the changed filter, compiled and executed the same way.
3. **Simulate** — `skills/scenario.apply`: deterministic arithmetic on those results. No model computes,
   chooses or words a number; the summary is a template checked by the scenario numbers guard.
4. **Record** — spec, assumptions (the person's plus one statement per change) and result, each hashed;
   lineage to the queries (and the Ask turn); event `scenario.computed`; an audit row.

A scenario is not an artifact: there is no path from it to a chart, dashboard, report or finding, and
`ChartSpec.value_basis="simulated"` is refused by `PublishBundle` should one ever be built from it.
"""
from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.scenario import SCENARIO_VERSION, ScenarioResult, ScenarioSpec
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, ordered_utcnow, stable_hash
from analystos.db.base import session_scope
from analystos.db.models import User, WhatIfScenario
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.services.steps import GatewayRuntime
from analystos.skills import scenario as skill

PURPOSE = "what_if"


class Runtime(Protocol):
    catalog: dict[str, Any] | None

    def semantic(self, query: dict[str, Any], *, step_id: str) -> tuple[str, dict[str, Any], Any]: ...


class GatewayScenarioRuntime(GatewayRuntime):
    """The steps' governed runtime (scope, `sql.execute` gate, Ask budget, compiler, gateway) under the
    `what_if` gateway purpose, so the audit trail says why the query ran."""

    @property
    def catalog(self) -> dict[str, Any] | None:
        return self.ctx.semantic_catalog

    def sql(self, sql: str, *, source_id: str | None, step_id: str) -> Any:
        self._gate()
        return self.ctx.services.gateway.execute(self.ctx.scope, sql, actor=self._actor, purpose=PURPOSE, run_id=None,
                                                 task_id=step_id, use_cache=False)


def _receipt(sql: str, provenance: dict[str, Any], result: Any, basis: str) -> dict[str, Any]:
    return {"basis": basis, "query_id": result.query_id, "sql": sql, "sql_hash": provenance.get("sql_hash") or stable_hash(sql),
            "result_hash": getattr(result, "result_hash", None), "row_count": result.row_count,
            "truncated": bool(getattr(result, "truncated", False)), "semantic": provenance}


def _labels(catalog: dict[str, Any], metrics: list[str]) -> dict[str, str]:
    return {m: (catalog["metrics"].get(m, {}).get("definition") or {}).get("display_name") or m for m in metrics}


def run(user: User, workspace_id: str, spec: ScenarioSpec, *, runtime: Runtime | None = None) -> dict[str, Any]:
    with session_scope() as s:
        me = s.merge(user)
        require_role(s, me, workspace_id, "viewer")
        if spec.ask_turn_id:
            from analystos.services.ask import _turn_for

            _turn_for(s, me, spec.ask_turn_id, workspace_id)
    skill.validate(spec)
    rt = runtime if runtime is not None else GatewayScenarioRuntime(user, workspace_id)
    catalog = rt.catalog
    if not catalog:
        raise InvalidInput("A what-if scenario needs an approved semantic model in this workspace.")
    scenario_id = new_id("scn")
    q = spec.semantic_query
    sql, provenance, observed = rt.semantic(q.model_dump(mode="json"), step_id=scenario_id)
    baseline = _receipt(sql, provenance, observed, "observed")
    remeasured, alt = None, skill.remeasure_query(spec)
    if alt is not None:
        alt_sql, alt_prov, alt_result = rt.semantic(alt.model_dump(mode="json"), step_id=scenario_id)
        remeasured = _receipt(alt_sql, alt_prov, alt_result, "simulated")
    definitions = {m: catalog["metrics"][m]["definition"] for m in q.metrics if m in catalog["metrics"]}
    summable = [m for m, d in definitions.items() if skill.summable((d.get("expressions") or [{}])[0].get("expression", ""))]
    rows, totals = skill.apply(spec, observed.columns, observed.rows,
                               remeasured=(alt_result.columns, alt_result.rows) if remeasured else None,
                               summable_metrics=summable,
                               truncated=baseline["truncated"] or bool(remeasured and remeasured["truncated"]))
    labels = _labels(catalog, q.metrics)
    assumptions = skill.assumption_statements(spec, labels)
    text = skill.summary(rows, totals, labels)
    obs_values, sim_values = skill.values(rows, totals)
    members = [str(v) for r in rows for v in r.key.values() if v is not None and not isinstance(v, int | float)]
    checked = skill.guard(text, obs_values, sim_values, labels=members)
    if not checked["ok"]:  # the template should never fail its own guard; if it does, no text is shown
        text = "The scenario summary was withheld by the numbers guard; the labelled table below is the result."
    spec_json = spec.model_dump(mode="json")
    spec_hash = stable_hash({"spec": spec_json, "model_hash": provenance.get("model_hash"),
                             "compiler_version": provenance.get("compiler_version"), "scenario_version": SCENARIO_VERSION})
    assumptions_hash = stable_hash(assumptions)
    body = {"rows": [r.model_dump(mode="json") for r in rows], "totals": [c.model_dump(mode="json") for c in totals]}
    result_hash = stable_hash({**body, "baseline": baseline["result_hash"],
                               "remeasured": remeasured["result_hash"] if remeasured else None,
                               "assumptions_hash": assumptions_hash, "spec_hash": spec_hash})
    columns = [*skill.outputs(q), *q.metrics]
    with session_scope() as s:
        row = WhatIfScenario(id=scenario_id, workspace_id=workspace_id, name=spec.name, ask_turn_id=spec.ask_turn_id,
                             spec=spec_json, spec_hash=spec_hash, assumptions=assumptions, assumptions_hash=assumptions_hash,
                             baseline={"observed": baseline, "remeasured": remeasured},
                             result={**body, "columns": columns, "summary": text, "guard": checked},
                             result_hash=result_hash, semantic_model_version=provenance.get("model_version"),
                             compiler_version=provenance.get("compiler_version"), scenario_version=SCENARIO_VERSION,
                             created_by=user.id, created_at=ordered_utcnow())
        s.add(row)
        s.flush()
        from analystos.artifacts.registry import link, link_queries

        link_queries(s, workspace_id, ("scenario", scenario_id),
                     [baseline["query_id"], *([remeasured["query_id"]] if remeasured else [])])
        if spec.ask_turn_id:
            link(s, workspace_id, ("scenario", scenario_id), "derived_from", ("ask_turn", spec.ask_turn_id))
        payload = {"scenario_id": scenario_id, "name": spec.name, "spec_hash": spec_hash, "assumptions_hash": assumptions_hash,
                   "result_hash": result_hash, "label": "simulated"}
        emit(workspace_id, "scenario.computed", payload, actor=f"user:{user.id}", session=s)
        audit(f"user:{user.id}", "scenario.computed", workspace_id=workspace_id, target=scenario_id, decision="simulated",
              details=payload, session=s)
        return view(row)


def view(row: WhatIfScenario) -> dict[str, Any]:
    r = row.result or {}
    b = row.baseline or {}
    return ScenarioResult(id=row.id, workspace_id=row.workspace_id, name=row.name, spec=row.spec, spec_hash=row.spec_hash,
                          assumptions=list(row.assumptions or []), assumptions_hash=row.assumptions_hash,
                          baseline=b.get("observed") or {}, remeasured=b.get("remeasured"), columns=r.get("columns") or [],
                          rows=r.get("rows") or [], totals=r.get("totals") or [], summary=r.get("summary") or "",
                          guard=r.get("guard") or {}, result_hash=row.result_hash, ask_turn_id=row.ask_turn_id,
                          created_by=row.created_by, created_at=row.created_at,
                          scenario_version=row.scenario_version).model_dump(mode="json")


@scoped_loader
def load(session: Session, user: User, scenario_id: str, workspace_id: str | None = None) -> WhatIfScenario:
    """Scenarios are private to their author (the query reveals intent, like an Ask thread)."""
    row = load_in_workspace(session, WhatIfScenario, scenario_id, workspace_id, user=user, label="scenario")
    if row.created_by != user.id and not user.is_admin:
        raise NotFound("scenario not found")
    return row


def list_for(session: Session, user: User, workspace_id: str, *, ask_turn_id: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
    require_role(session, user, workspace_id, "viewer")
    stmt = select(WhatIfScenario).where(WhatIfScenario.workspace_id == workspace_id, WhatIfScenario.created_by == user.id)
    if ask_turn_id:
        stmt = stmt.where(WhatIfScenario.ask_turn_id == ask_turn_id)
    rows = session.scalars(stmt.order_by(WhatIfScenario.created_at.desc(), WhatIfScenario.id.desc()).limit(min(limit, 200)))
    return [view(r) for r in rows]
