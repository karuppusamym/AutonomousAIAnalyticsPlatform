"""The six S06 template playbooks end to end on Postgres (S06-T5): each one, started as a supervised goal
over the ServiceNow mock, plans, waits for plan approval where it has a gate, and completes with every
required step done. No model is called (no provider key): each step runs its deterministic path. The
router's choice for each template's fixture question is checked against the same workspace."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from tests.integration.test_goals_v2 import _drive, _enable, _goal, api, servicenow_url, world  # noqa: F401

pytestmark = pytest.mark.integration

FIXTURES = yaml.safe_load((Path(__file__).resolve().parents[1] / "fixtures" / "playbooks" / "templates.yaml")
                          .read_text(encoding="utf-8"))


def _approve_waiting(api, world, run_id: str) -> None:  # noqa: F811
    from analystos.db.base import session_scope
    from analystos.db.models import RunTask

    with session_scope() as s:
        waiting = [t.key for t in s.scalars(select(RunTask).where(RunTask.run_id == run_id, RunTask.status == "WAITING_USER"))]
    assert waiting, "a run parked WAITING_USER without a waiting step"
    for key in waiting:
        r = api.post(f"/v2/runs/{run_id}/signals", headers=world["approver"],
                     json={"type": "approve_step", "payload": {"step": key}})
        assert r.status_code == 200, r.text


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_template_runs_end_to_end_as_a_goal(api, world, name):  # noqa: F811
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, RunEvent, RunTask

    fx = FIXTURES[name]
    _enable(api, world, fx["id"])
    outputs = ["dashboard_draft"] if name == "dashboard_build" else ["investigation"]
    routed = _goal(api, world, f"route-{name}", question=fx["question"], requested_outputs=outputs)
    assert routed.status_code == 202, routed.text
    assert routed.json()["playbook"] == fx["id"], routed.json()  # the router picks the template in this workspace
    run_id = routed.json()["run_id"]
    for _ in range(4):  # plan approval, then (dashboard_build) the publication approval
        outcome = _drive(run_id)
        if outcome != "WAITING_USER":
            break
        _approve_waiting(api, world, run_id)
    assert outcome == "COMPLETED", outcome
    with session_scope() as s:
        run = s.get(AnalysisRun, run_id)
        tasks = {t.key: t for t in s.scalars(select(RunTask).where(RunTask.run_id == run_id)) if not t.key.startswith("_")}
        budget_events = s.scalars(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == "run.budget")).all()
    assert run.capabilities["playbook"] == fx["id"] and fx["id"] + "@" + fx["version"] in run.capabilities["refs"]
    assert set(fx["steps_autonomy_2"]) <= set(tasks)
    required = [k for k in fx["steps_autonomy_2"] if not tasks[k].input.get("optional")]
    assert all(tasks[k].status in ("COMPLETED", "SKIPPED") for k in required), {k: (tasks[k].status, tasks[k].error) for k in required}
    assert tasks["finalize"].status == "COMPLETED"
    assert run.limits == {**run.limits, **{k: v for k, v in fx["default_limits"].items()}}
    assert budget_events and all(e.payload["exhausted"] == [] for e in budget_events)
    if name == "dashboard_build":
        assert tasks["publish"].status == "COMPLETED"  # published to the preview destination under the approval
    if name == "dq_review":
        assert tasks["quality"].status == "COMPLETED" and run.cost_usd == 0
