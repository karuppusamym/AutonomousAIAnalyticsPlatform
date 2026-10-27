"""Live end-to-end proof of the MVP definition of done (spec v1 §62) through the HTTP API.

    python scripts/e2e_demo.py [--api http://localhost:8000] [--out docs/60-delivery/evidence]

Needs the running stack (API, worker or ANALYSTOS_ORCHESTRATOR=local, ServiceNow mock, Superset)
and OPENROUTER_API_KEY in the API's environment. Writes e2e-<timestamp>.md (readable) and .json
(raw evidence). Each DoD item is checked against API responses, not assumed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

PASSWORD = os.getenv("ANALYSTOS_DEMO_PASSWORD", "ChangeMe123!")
OBJECTIVE = ("Analyze Incident and Change data for the last 12 months. Identify the drivers of SLA breaches, recurring "
             "operational problems, teams with unusually high reassignment, and applications generating repeated critical "
             "incidents. Create an executive dashboard and an operations dashboard.")
REDIRECT = "Exclude inquiry-category incidents from the analysis; they are requests, not operational failures."


class Api:
    def __init__(self, base: str, email: str):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=300)
        r = self.c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        r.raise_for_status()
        self.c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        self.user = r.json()["user"]

    def get(self, path, **kw):
        r = self.c.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path, body=None, expect=None):
        r = self.c.post(path, json=body or {})
        if expect is not None:
            return r.status_code, r.json()
        r.raise_for_status()
        return r.json()

    def put(self, path, body):
        r = self.c.put(path, json=body)
        r.raise_for_status()
        return r.json()


def wait(api: Api, ws: str, run: str, predicate, *, timeout=1800, label=""):
    started = time.time()
    last = None
    while time.time() - started < timeout:
        detail = api.get(f"/api/workspaces/{ws}/analysis/{run}")
        if detail["status"] != last:
            print(f"  [{int(time.time() - started):>4}s] {label} status={detail['status']}", flush=True)
            last = detail["status"]
        if predicate(detail):
            return detail
        if detail["status"] in ("FAILED", "CANCELLED"):
            raise SystemExit(f"run ended {detail['status']}: {detail.get('error')}")
        time.sleep(3)
    raise SystemExit(f"timeout waiting for {label}")


def run_seconds(detail: dict | None) -> float | None:
    """Wall-clock duration of an analysis run from its own timestamps (None while it has not finished)."""
    if not detail or not detail.get("started_at") or not detail.get("finished_at"):
        return None
    return round((datetime.fromisoformat(detail["finished_at"]) - datetime.fromisoformat(detail["started_at"])).total_seconds(), 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.getenv("ANALYSTOS_API", "http://localhost:8000"))
    ap.add_argument("--servicenow", default=os.getenv("ANALYSTOS_SERVICENOW_MOCK_URL", "http://localhost:8090"))
    ap.add_argument("--out", default="docs/60-delivery/evidence")
    args = ap.parse_args()
    started = datetime.now(UTC)
    ev: dict = {"api": args.api, "started_at": started.isoformat(), "checks": {}}
    check = ev["checks"]

    admin, analyst, approver = Api(args.api, "admin@analystos.local"), Api(args.api, "analyst@analystos.local"), Api(args.api, "approver@analystos.local")
    ev["health"] = admin.get("/api/health")
    print("health:", {k: v.get("ok") for k, v in ev["health"]["checks"].items()})

    # 1. workspace
    ws = admin.post("/api/workspaces", {"name": f"ServiceNow Incident Intelligence {started:%Y%m%d-%H%M}", "objective": OBJECTIVE,
                                        "description": "MVP DoD demo", "autonomy_level": 3,
                                        # v1 §62 predates the P4-K03 approved-metric gate (tests/integration/test_semantic_layer.py)
                                        "policy": {"require_approved_metrics": False}})
    wid = ws["id"]
    admin.post(f"/api/workspaces/{wid}/members", {"email": "analyst@analystos.local", "role": "editor"})
    admin.post(f"/api/workspaces/{wid}/members", {"email": "approver@analystos.local", "role": "approver"})
    check["1_workspace_created"] = bool(wid)

    # 2-3. ServiceNow source + select Incident and Change Request (+ lookup tables for names)
    src = analyst.post(f"/api/workspaces/{wid}/sources", {"kind": "servicenow", "name": "ServiceNow",
                                                          "config": {"instance_url": args.servicenow, "username": "admin",
                                                                     "tables": ["incident", "change_request", "sys_user_group", "cmdb_ci"]},
                                                          "secret_ref": "env:ANALYSTOS_SOURCE_SECRET_SERVICENOW"})
    disc = analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/discover")
    sel = analyst.put(f"/api/workspaces/{wid}/sources/{src['id']}/selection", {"assets": ["incident", "change_request"]})
    check["2_servicenow_source_added"] = len(disc["assets"]) >= 2
    check["3_incident_and_change_selected"] = {a["asset"].split(".")[-1] for a in sel["loaded"]} == {"incident", "change_request"}
    ev["source"] = {"discovered": [a["name"] for a in disc["assets"]], "loaded": sel["loaded"]}

    # run
    run = analyst.post(f"/api/workspaces/{wid}/analysis", {"objective": OBJECTIVE})
    rid = run["id"]
    print("run", rid)
    detail = wait(analyst, wid, rid, lambda d: any(h["status"] in ("supported", "rejected", "inconclusive") for h in d["hypotheses"]),
                  label="first hypothesis tested")
    # 19. add a requirement and continue (dynamic replanning)
    fb = analyst.post(f"/api/workspaces/{wid}/analysis/{rid}/feedback", {"text": REDIRECT})
    ev["redirect"] = fb
    check["19_requirement_added_and_replanned"] = bool(fb.get("replan", {}).get("plan_version", 0) >= 2)
    print("redirect:", fb.get("kind"), fb.get("classified_by"), fb.get("interpretation", {}).get("filters"))

    detail = wait(analyst, wid, rid, lambda d: any(a["status"] == "pending" and a["action"] == "publish_dashboard" for a in d["approvals"]),
                  label="publication approval requested")
    approval = next(a for a in detail["approvals"] if a["status"] == "pending" and a["action"] == "publish_dashboard")

    # 18. interrupt before publishing: pause + resume while the approval waits; analyst cannot approve
    analyst.post(f"/api/workspaces/{wid}/analysis/{rid}/pause")
    paused = wait(analyst, wid, rid, lambda d: d["status"] == "PAUSED", timeout=120, label="pause")
    analyst.post(f"/api/workspaces/{wid}/analysis/{rid}/resume")
    code, body = analyst.post(f"/api/approvals/{approval['id']}/approve", expect=True)
    check["18_user_can_interrupt_before_publish"] = paused["status"] == "PAUSED" and not any(
        t["key"] == "publish" and t["status"] == "COMPLETED" for t in paused["tasks"])
    check["approval_requires_approver_role"] = code == 403
    ev["analyst_approve_attempt"] = {"status": code, "body": body}

    # 16. approve as approver -> publish
    ev["approval"] = approver.post(f"/api/approvals/{approval['id']}/approve", {"reason": "e2e demo"})
    detail = wait(analyst, wid, rid, lambda d: d["status"] == "COMPLETED", label="publication + finalize")
    ev["run"] = {k: detail[k] for k in ("id", "status", "plan_version", "plan_hash", "cost_usd", "tokens", "summary", "instructions", "constraints")}

    # 4-17. verify artifacts
    arts = analyst.get(f"/api/workspaces/{wid}/artifacts", params={"run_id": rid})
    by_type: dict[str, list] = {}
    for a in arts:
        by_type.setdefault(a["type"], []).append(a)
    tasks = {t["key"]: t for t in detail["tasks"]}
    verified = [i for i in detail["insights"] if i["status"] == "verified"]
    check["4_context_loaded"] = tasks["context"]["status"] == "COMPLETED" and bool(by_type.get("context_package"))
    check["5_datasets_profiled"] = len([a for a in by_type.get("profile", []) if a["name"].startswith("Profile ")]) >= 2
    quality = next((a for a in by_type.get("quality_report", [])), None)
    q_issues = (quality or {}).get("content", {}).get("issues", [])
    check["6_quality_issues_identified"] = len([i for i in q_issues if i.get("severity") in ("warning", "critical")]) >= 1
    check["7_hypotheses_proposed"] = len(detail["hypotheses"]) >= 3
    iterations = {h["iteration"] for h in detail["hypotheses"] if h["status"] != "superseded"}
    check["8_iterative_analysis"] = max(iterations or {0}) >= 2
    check["9_three_evidence_backed_findings"] = len(verified) >= 3 and all(any(e["type"] == "query" for e in i["evidence"]) for i in verified)
    check["10_rev_verified"] = all(i["verified"] and i["verification"].get("evaluate") for i in verified)
    check["11_reusable_dataset"] = bool(by_type.get("dataset"))
    check["12_kpi_definitions"] = len(by_type.get("metric", [])) >= 3
    check["13_five_charts"] = len(by_type.get("chart", [])) >= 5
    dashes = {a["name"]: a for a in by_type.get("dashboard", [])}
    check["14_executive_dashboard"] = "executive" in dashes
    check["15_operational_dashboard"] = "operational" in dashes
    publication = (detail["summary"] or {}).get("publication") or {}
    check["16_published_to_superset"] = all(d.get("platform") == "superset" and d.get("external_id") for d in dashes.values()) and bool(publication.get("urls"))
    console = analyst.get(f"/api/workspaces/{wid}/analysis/{rid}/console")
    lineage = analyst.get(f"/api/artifacts/{dashes['executive']['id']}")["lineage"] if "executive" in dashes else {"nodes": []}
    check["17_queries_artifacts_lineage_stored"] = len(console["queries"]) > 10 and any(n["type"] == "table" for n in lineage["nodes"])

    # 20. evidence behind every finding and KPI
    evidence = []
    for i in verified:
        full = analyst.get(f"/api/insights/{i['id']}")
        evidence.append({"code": i["code"], "title": i["title"], "finding": i["finding"], "confidence": i["confidence"],
                         "queries": [{"id": q["id"], "sql": q["sql"][:600], "rows": q["row_count"], "result_hash": q["result_hash"]} for q in full["queries"]],
                         "checks": [(c["check"], c["passed"]) for c in i["verification"].get("evaluate", [])],
                         "independent_model": (i["verification"].get("verify") or {}).get("independent_model"),
                         "jev": (i["verification"].get("verify") or {}).get("jev")})
    metrics = [a["content"] for a in by_type.get("metric", [])]
    check["20_evidence_inspectable"] = all(e["queries"] for e in evidence) and all(m.get("validation", {}).get("query_id") for m in metrics)
    ev["insights"], ev["metrics"] = evidence, [{"name": m["name"], "expr": m["sql_expression"], "value": m["validation"].get("value")} for m in metrics]
    ev["charts"] = [{"key": a["name"], "type": a["content"]["chart_type"], "external_id": a.get("external_id")} for a in by_type.get("chart", [])]
    ev["publication"] = publication

    # Safety checks
    denied = detail["scope"]["denied_columns"]
    if denied:
        col = denied[0]
        code, body = analyst.post(f"/api/workspaces/{wid}/query", {"sql": f"SELECT {col.split('.')[-1]} FROM {'.'.join(col.split('.')[:2])}"}, expect=True)
        check["safety_restricted_column_rejected"] = code == 422 and body["error"]["code"] == "sql_rejected"
        ev["safety_restricted_column"] = body
    code, body = analyst.post(f"/api/workspaces/{wid}/query", {"sql": f"DELETE FROM {detail['scope']['assets'][0]}"}, expect=True)
    check["safety_write_rejected"] = code == 422
    code, body = analyst.post(f"/api/workspaces/{wid}/query", {"sql": "SELECT * FROM analystos.public.app_user"}, expect=True)
    check["safety_control_plane_unreachable"] = code == 422
    outsider_ws = approver.get("/api/workspaces")
    check["safety_cross_workspace_isolation"] = True
    r = approver.c.get(f"/api/workspaces/{wid}/analysis/{rid}")
    ev["approver_can_view_as_member"] = r.status_code
    lonely = Api(args.api, "admin@analystos.local").post("/api/workspaces", {"name": "isolation probe", "objective": "x" * 12})
    r = analyst.c.get(f"/api/workspaces/{lonely['id']}")
    check["safety_cross_workspace_isolation"] = r.status_code == 404
    ev["model_usage"] = {"model_calls": console["cost"]["model_calls"], "jev_calls": console["cost"]["jev_calls"],
                         "failed_calls": console["cost"]["failed_calls"], "cost_usd": console["cost"]["usd"],
                         "purposes": sorted({c["purpose"] for c in console["model_calls"]})}
    # Deterministic rungs answer first (spec v3 §4.1), so a run may bill no model at all; what must hold is
    # that every decision the run took is recorded with the rung that answered it.
    ev["model_usage"]["by_rung"] = console["cost"].get("by_rung")
    decided = {c["purpose"] for c in console["model_calls"]}
    check["decisions_recorded_with_rung"] = {"hypothesis_priority", "chart_selection"} <= decided and bool(console["cost"].get("by_rung"))
    del outsider_ws

    # Report
    finished = datetime.now(UTC)
    ev["finished_at"] = finished.isoformat()
    ev["duration"] = {"scenario_seconds": round((finished - started).total_seconds(), 1),
                      "analysis_run_seconds": run_seconds(analyst.get(f"/api/workspaces/{wid}/analysis/{rid}"))}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y%m%d-%H%M%S")
    (out / f"e2e-{stamp}.json").write_text(json.dumps(ev, indent=2, default=str))
    passed = sum(1 for v in check.values() if v)
    lines = [f"# MVP end-to-end evidence — {started:%Y-%m-%d %H:%M} UTC", "",
             f"Live run through the HTTP API at `{args.api}` (workspace `{wid}`, run `{rid}`). Raw evidence: `e2e-{stamp}.json`.",
             "Synthetic ServiceNow-shaped data served by the Table-API mock — **not** a certification of the ServiceNow connector.", "",
             f"**{passed}/{len(check)} checks passed.**", "",
             f"Duration: scenario {ev['duration']['scenario_seconds']} s (started {started:%H:%M:%S}, finished {finished:%H:%M:%S} UTC); "
             f"analysis run {ev['duration']['analysis_run_seconds']} s (started → finished, including the approval wait and the redirect).",
             "", "| Check | Result |", "|---|---|"]
    lines += [f"| {k} | {'PASS' if v else '**FAIL**'} |" for k, v in check.items()]
    lines += ["", "## Verified findings", ""]
    for e in evidence:
        lines += [f"### {e['code']} — {e['title']} (confidence {e['confidence']:.0%})", "", e["finding"], "",
                  "Checks: " + ", ".join(f"{c}={'ok' if p else 'fail'}" for c, p in e["checks"]), ""]
        for q in e["queries"][:2]:
            lines += [f"- evidence query `{q['id']}` ({q['rows']} rows, hash `{(q['result_hash'] or '')[:12]}`)", "", "```sql", q["sql"], "```", ""]
    lines += ["## KPIs", "", "| Metric | Expression | Value |", "|---|---|---|"]
    lines += [f"| {m['name']} | `{m['expr']}` | {m['value']} |" for m in ev["metrics"]]
    lines += ["", "## Publication", "", *[f"- {k}: {v}" for k, v in (publication.get("urls") or {}).items()], "",
              "## Model usage", "", f"```json\n{json.dumps(ev['model_usage'], indent=2)}\n```", "",
              "## Redirect (dynamic replanning)", "", f"```json\n{json.dumps({k: fb.get(k) for k in ('kind', 'classified_by', 'consequential_p', 'interpretation', 'replan')}, indent=2, default=str)}\n```", ""]
    (out / f"e2e-{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:len(check) + 10]))
    print(f"wrote {out / f'e2e-{stamp}.md'}")
    return 0 if passed == len(check) else 1


if __name__ == "__main__":
    sys.exit(main())
