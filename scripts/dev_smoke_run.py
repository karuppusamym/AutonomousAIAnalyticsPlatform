"""Developer smoke run: whole Phase-1 flow in-process on the local orchestrator.

    OPENROUTER_API_KEY=... ANALYSTOS_ORCHESTRATOR=local python scripts/dev_smoke_run.py [--no-llm] [--approve]

Uses the ServiceNow mock (ANALYSTOS_SERVICENOW_MOCK_URL) and whatever Postgres/Superset the settings point at.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    os.environ["ANALYSTOS_ORCHESTRATOR"] = "local"
    os.environ.setdefault("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")
    if args.no_llm:
        os.environ.pop("OPENROUTER_API_KEY", None)
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import AnalysisRun, Approval, Insight, RunTask, User
    from analystos.governance.approvals import decide
    from analystos.services.runs import create_run
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import add_member, create_workspace
    from analystos.workflows.orchestrator import run_local

    settings = get_settings()
    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == settings.bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"ServiceNow smoke {time.strftime('%H:%M:%S')}",
                              objective="Identify the drivers of SLA breaches and recurring operational problems in IT incidents",
                              autonomy_level=3, policy={"require_approved_metrics": False})  # smoke run: no KPI review
        s.flush()
        add_member(s, admin, ws.id, "approver@analystos.local", "approver")
        src = register_source(s, admin, ws.id, kind="servicenow", name="ServiceNow (mock)",
                              config={"instance_url": settings.servicenow_mock_url, "username": "admin",
                                      "tables": ["incident", "change_request", "sys_user_group", "cmdb_ci"]},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_SERVICENOW")
        s.flush()
        ws_id, src_id = ws.id, src.id
        s.expunge(admin)
    t0 = time.time()
    discover_source(admin, src_id)
    print("selected:", select_assets(admin, src_id, ["incident", "change_request", "sys_user_group", "cmdb_ci"])["loaded"])
    run = create_run(admin, ws_id, objective=None)
    print("run", run.id, "started", round(time.time() - t0, 1), "s")
    # create_run started a local thread; wait for it to reach approval or finish
    while True:
        with session_scope() as s:
            r = s.get(AnalysisRun, run.id)
            status = r.status
            tasks = [(t.key, t.status, (t.error or "")[:160]) for t in s.scalars(select(RunTask).where(RunTask.run_id == run.id).order_by(RunTask.seq))]
        if status in ("WAITING_USER", "COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(2)
    for t in tasks:
        print(f"  {t[0]:<16} {t[1]:<10} {t[2]}")
    print("status:", status, round(time.time() - t0, 1), "s")
    if status == "WAITING_USER" and args.approve:
        with session_scope() as s:
            approver = s.scalar(select(User).where(User.email == "approver@analystos.local"))
            a = s.scalar(select(Approval).where(Approval.run_id == run.id, Approval.status == "pending"))
            decide(s, a.id, approver, approve=True, reason="smoke")
        print("approved; waiting for publish...")
        run_local(run.id)
    with session_scope() as s:
        r = s.get(AnalysisRun, run.id)
        for i in s.scalars(select(Insight).where(Insight.run_id == run.id)):
            print(f"  {i.code} [{i.status}] conf={i.confidence} {i.title} :: {i.finding}")
        print("final:", r.status, r.error, "cost", r.cost_usd, "summary keys", list((r.summary or {}).keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
