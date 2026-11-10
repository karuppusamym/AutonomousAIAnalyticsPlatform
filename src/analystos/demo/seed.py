"""`analystos demo-seed`: the ready demo workspaces, built through the running API.

    analystos demo-seed [--api http://localhost:8000] [--servicenow http://localhost:8090] [--web http://localhost:5173]
                        [--only investigation|process|metrics]

Two workspaces, both by default: the investigation one (`packs/itsm/demo.yaml`: investigation, dashboards,
file, recipe, schedule, monitor) and the process mining one (`packs/itsm/process_mining_demo.yaml`: the ServiceNow
task activity log and the records it describes, a brief, and saved process analyses per task type). Each demo workspace
also gets an approved semantic model with its key metrics (the pack's `semantic` section; ADR-016): proposed by
admin@, approved by approver@. `--only metrics` does only that, for every demo workspace that exists (the Retail one
included, whose spec is `packs/sales/retail_demo.yaml`).

It goes through the HTTP API as the seeded users (never around it), so every governed path is the one the demo
shows: the source is registered, discovered and loaded through the loader, the investigation runs on whatever
orchestrator the API has (Temporal worker or the local runner), and publication waits for the approver's approval
like any other. Idempotent: each piece is looked up first and only what is missing is made, so running it again
(or after a partial failure) finishes the workspace without duplicating anything, and the workspace stays usable
for live work afterwards. The domain content (workspace, tables, brief, file, recipe, schedule, monitor) is data in
the ITSM pack, `packs/itsm/demo.yaml`. Needs the seeded users (`analystos seed`) and the ServiceNow mock source
(compose `--profile demo`, or `uvicorn analystos.connectors.servicenow_mock:app`).
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
import yaml

from analystos.core.config import DATA_ROOT

DEMO_FILE = DATA_ROOT / "packs" / "itsm" / "demo.yaml"
PROCESS_DEMO_FILE = DATA_ROOT / "packs" / "itsm" / "process_mining_demo.yaml"
RETAIL_DEMO_FILE = DATA_ROOT / "packs" / "sales" / "retail_demo.yaml"
DEMO_FILES = (DEMO_FILE, PROCESS_DEMO_FILE, RETAIL_DEMO_FILE)
DONE, ACTIVE = ("COMPLETED",), ("NEW", "PLANNED", "RUNNING", "WAITING_USER", "PAUSED")


@lru_cache
def demo(path: Path = DEMO_FILE) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def process_demo() -> dict[str, Any]:
    return demo(PROCESS_DEMO_FILE)


def log(status: str, text: str) -> None:
    print(f"[{status:>7}] {text}", flush=True)


class Api:
    """A signed-in HTTP session as one seeded user."""

    def __init__(self, base: str, email: str, password: str):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=120)
        r = self.c.post("/api/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            raise SystemExit(f"cannot sign in as {email} ({r.status_code}): run `analystos seed` first")
        self.c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def call(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        r = self.c.request(method, path, json=body, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    def get(self, path: str, **kw: Any) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body if body is not None else {})


def wait_for_api(base: str, seconds: float) -> dict:
    deadline = time.time() + seconds
    last = "no answer"
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base.rstrip('/')}/api/health", timeout=5)
            if r.status_code == 200 and r.json().get("ok"):
                return r.json()
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(2)
    raise SystemExit(f"the API at {base} is not healthy after {seconds:.0f}s ({last})")


def _assets(api: Api, wid: str, source_id: str) -> dict[str, dict]:
    return {a["name"]: a for a in api.get(f"/api/workspaces/{wid}/assets") if a.get("source_id") == source_id}


def ensure_workspace(admin: Api, d: dict[str, Any] | None = None) -> str:
    spec = (d or demo())["workspace"]
    ws = next((w for w in admin.get("/api/workspaces") if w["name"] == spec["name"]), None)
    if ws:
        log("exists", f"workspace {ws['id']}")
    else:
        ws = admin.post("/api/workspaces", {"name": spec["name"], "objective": spec["objective"], "autonomy_level": 3,
                                            "description": spec["description"], "policy": spec["policy"]})
        log("created", f"workspace {ws['id']}")
    wid = ws["id"]
    members = {m["email"]: m["role"] for m in admin.get(f"/api/workspaces/{wid}").get("members", [])}
    for email, role in (("analyst@analystos.local", "editor"), ("approver@analystos.local", "approver")):
        if email not in members:
            admin.post(f"/api/workspaces/{wid}/members", {"email": email, "role": role})
            log("added", f"{email} as {role}")
    return wid


def ensure_source(analyst: Api, wid: str, instance_url: str, d: dict[str, Any] | None = None) -> None:
    spec = (d or demo())["source"]
    src = next((s for s in analyst.get(f"/api/workspaces/{wid}/sources") if s["kind"] == spec["kind"]), None)
    if src is None:
        config = {"instance_url": instance_url, "username": spec["username"], "tables": spec["tables"],
                  **({"page_size": spec["page_size"]} if spec.get("page_size") else {})}
        src = analyst.post(f"/api/workspaces/{wid}/sources", {
            "kind": spec["kind"], "name": spec["name"], "secret_ref": spec["secret_ref"], "config": config})
        log("created", f"{spec['name']} source {src['id']} ({instance_url})")
    assets = _assets(analyst, wid, src["id"])
    if not assets:
        found = analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/discover")
        log("found", f"{len(found['assets'])} tables")
        assets = _assets(analyst, wid, src["id"])
    wanted = spec["select"]
    if all(assets.get(t, {}).get("selected") and assets[t].get("row_count") for t in wanted):
        log("exists", f"{', '.join(wanted)} selected and loaded")
        return
    loaded = analyst.call("PUT", f"/api/workspaces/{wid}/sources/{src['id']}/selection", {"assets": wanted})
    log("loaded", ", ".join(f"{x['asset']} ({x.get('row_count', '?')} rows)" for x in loaded.get("loaded", [])))


def ensure_brief(analyst: Api, wid: str, d: dict[str, Any] | None = None) -> None:
    d = d or demo()
    wanted = d["brief"]
    brief = analyst.get(f"/api/workspaces/{wid}/brief")
    have = {(a["group"], a["field"]): a.get("value") for a in brief.get("assertions", []) if a.get("review_state") != "rejected"}
    ops = [{"op": "set", "assertion": a} for a in wanted if have.get((a["group"], a["field"])) != a["value"]]
    if ops:
        brief = analyst.call("PATCH", f"/api/workspaces/{wid}/brief", {"ops": ops, "reason": "demo seed"},
                             headers={"If-Match": f'"{brief["version"]}"'})
        log("filled", f"brief v{brief['version']} ({len(ops)} assertions)")
    else:
        log("exists", f"brief v{brief['version']} ({len(wanted)} demo assertions)")
    # The rules infer each table's grain, entity and event time as suggestions (keys are validated on the profiles);
    # a person reviews the grain of the investigated tables, so the Explain readiness check reads "ready". The file's
    # grain stays a suggestion to review live.
    if not any(a["key"].startswith("data_semantics.grain:") for a in brief.get("assertions", [])):
        brief = analyst.post(f"/api/workspaces/{wid}/brief/suggestions")
        log("inferred", f"brief v{brief['version']}: suggestions from the catalog")
    tables = tuple(f".{t}" for t in d["source"]["select"])
    review = [{"op": "review", "key": a["key"]} for a in brief.get("assertions", [])
              if a["key"].startswith("data_semantics.grain:") and a["key"].endswith(tables) and a["review_state"] == "suggested"]
    if review:
        brief = analyst.call("PATCH", f"/api/workspaces/{wid}/brief", {"ops": review, "reason": "demo seed: grain reviewed"},
                             headers={"If-Match": f'"{brief["version"]}"'})
        log("reviewed", f"brief v{brief['version']}: grain of {len(review)} tables")


def ensure_investigation(analyst: Api, approver: Api, wid: str, timeout: float, *, objective: str | None = None,
                         source_ids: list[str] | None = None) -> str:
    runs = analyst.get(f"/api/workspaces/{wid}/analysis")
    for r in runs:
        if r["status"] in DONE and (r.get("origin") or {}).get("type", "user") == "user":
            detail = analyst.get(f"/api/workspaces/{wid}/analysis/{r['id']}")
            if any(i["status"] == "verified" for i in detail["insights"]):
                log("exists", f"completed investigation {r['id']}")
                return r["id"]
    run = next((r for r in runs if r["status"] in ACTIVE and (r.get("origin") or {}).get("type", "user") == "user"), None)
    if run:
        log("resumes", f"investigation {run['id']} ({run['status']})")
        if run["status"] == "PAUSED":
            analyst.post(f"/api/workspaces/{wid}/analysis/{run['id']}/resume")
    else:
        # The workspace also holds the file source: the investigation is scoped to the demo's system of record.
        kind = demo()["source"]["kind"]
        ids = source_ids or [s["id"] for s in analyst.get(f"/api/workspaces/{wid}/sources") if s["kind"] == kind]
        run = analyst.post(f"/api/workspaces/{wid}/analysis", {"objective": objective or demo()["workspace"]["objective"],
                                                               "source_ids": ids})
        log("started", f"investigation {run['id']} (runs on the worker; a few minutes at most)")
    rid, started, last = run["id"], time.time(), None
    while time.time() - started < timeout:
        d = analyst.get(f"/api/workspaces/{wid}/analysis/{rid}")
        if d["status"] != last:
            log("status", f"{rid}: {d['status']} after {time.time() - started:.0f}s")
            last = d["status"]
        if d["status"] in DONE:
            verified = [i for i in d["insights"] if i["status"] == "verified"]
            urls = [u for u in (((d.get("summary") or {}).get("publication") or {}).get("urls") or {}).values()
                    if str(u).startswith("http")]
            log("done", f"{len(verified)} verified findings; dashboards: "
                        f"{', '.join(urls) or 'in-platform preview (Superset not configured or not reachable)'}")
            return rid
        if d["status"] in ("FAILED", "CANCELLED"):
            raise SystemExit(f"investigation {rid} ended {d['status']}: {d.get('error')}")
        pending = next((a for a in d["approvals"] if a["status"] == "pending" and a["action"] == "publish_dashboard"), None)
        if pending:
            approver.post(f"/api/approvals/{pending['id']}/approve", {"reason": "demo seed"})
            log("approved", f"publication {pending['id']} as approver@analystos.local")
        time.sleep(3)
    raise SystemExit(f"investigation {rid} did not finish within {timeout:.0f}s: is a worker running? (/api/health checks.worker)")


def ensure_file_and_recipe(analyst: Api, wid: str) -> None:
    spec = demo()["file"]
    src = next((s for s in analyst.get(f"/api/workspaces/{wid}/sources")
                if s["kind"] == "csv" and s["name"] == spec["source_name"]), None)
    # The upload lands in <upload_dir>/<workspace>/, the folder the source reads. Re-uploading the same bytes is harmless.
    analyst.call("POST", f"/api/workspaces/{wid}/uploads", files={"file": (spec["filename"], spec["content"].encode(), "text/csv")})
    if src is None:
        src = analyst.post(f"/api/workspaces/{wid}/sources", {"kind": "csv", "name": spec["source_name"], "config": {"path": wid}})
        log("created", f"file source {src['id']}")
    table = spec["filename"].rsplit(".", 1)[0]
    assets = _assets(analyst, wid, src["id"])
    if table not in assets:
        analyst.post(f"/api/workspaces/{wid}/sources/{src['id']}/discover")
    if assets.get(table, {}).get("selected") and assets[table].get("row_count"):
        log("exists", f"{spec['filename']} loaded")
    else:
        analyst.call("PUT", f"/api/workspaces/{wid}/sources/{src['id']}/selection", {"assets": [table]})
        log("loaded", spec["filename"])
        assets = _assets(analyst, wid, src["id"])
    recipe_spec = copy.deepcopy(demo()["recipe"])
    name = recipe_spec["name"]
    recipe = next((r for r in analyst.get(f"/api/workspaces/{wid}/recipes") if r.get("name") == name), None)
    if recipe is None:
        asset = assets[table]
        columns = [{"name": c["name"], "type": c["data_type"]} for c in asset["columns"]]
        recipe_spec["nodes"].insert(0, {"op": "source", "id": "targets", "asset": asset["fq"], "schema": columns})
        recipe = analyst.post(f"/api/workspaces/{wid}/recipes", {"spec": recipe_spec})
        log("created", f"recipe {name} ({recipe['id']})")
    else:
        log("exists", f"recipe {name} ({recipe['id']})")
    if not analyst.get(f"/api/workspaces/{wid}/recipe-runs", params={"recipe": name}):
        run = analyst.post(f"/api/workspaces/{wid}/recipes/{recipe['id']}/runs", {"mode": "preview"})
        outputs = run.get("preview") or {}
        rows = sum(len((o or {}).get("rows") or []) for o in outputs.values())
        log("preview", f"recipe {name}: {rows} output rows, gates checked")


def ensure_schedule(analyst: Api, wid: str, baseline_run: str) -> None:
    spec = demo()["schedule"]
    if any(s["name"] == spec["name"] for s in analyst.get(f"/api/workspaces/{wid}/schedules")):
        log("exists", f"schedule '{spec['name']}'")
        return
    body = {"name": spec["name"], "kind": spec["kind"], "cron": spec["cron"], "timezone": spec["timezone"],
            "config": {"baseline_run_id": baseline_run, "refresh_first": True, "publish": "skip", "report": dict(spec["report"])}}
    r = analyst.c.post(f"/api/workspaces/{wid}/schedules", json=body)
    if r.status_code == 422:  # an install without the `reports` extra: HTML only
        body["config"]["report"]["formats"] = ["html"]
        r = analyst.c.post(f"/api/workspaces/{wid}/schedules", json=body)
    if r.status_code >= 400:
        raise SystemExit(f"schedule -> {r.status_code}: {r.text[:500]}")
    log("created", f"schedule '{spec['name']}' ({spec['cron']} {spec['timezone']}, next {r.json().get('next_run_at')})")


def ensure_monitor(analyst: Api, wid: str, *, evaluate: bool) -> None:
    """A metric monitor reads the investigation's dataset, so it is evaluated once there is one."""
    spec = demo()["monitor"]
    mon = next((m for m in analyst.get(f"/api/workspaces/{wid}/monitors") if m["name"] == spec["name"]), None)
    if mon is None:
        mon = analyst.post(f"/api/workspaces/{wid}/monitors", {"name": spec["name"], "kind": spec["kind"],
                                                               "auto_investigate": False, "config": spec["config"]})
        log("created", f"monitor '{spec['name']}' ({mon['id']})")
    else:
        log("exists", f"monitor '{spec['name']}' ({mon['id']})")
    if evaluate and not mon.get("last_evaluated_at"):
        result = analyst.post(f"/api/monitors/{mon['id']}/evaluate")
        log("checked", f"monitor: {result.get('message', '')[:140]}")


def ensure_process_analyses(analyst: Api, wid: str, d: dict[str, Any] | None = None) -> None:
    """One saved process analysis per listed segment of the event log, through Work → Process's own API."""
    spec = (d or process_demo())["process"]
    have = {a["name"] for a in analyst.get(f"/api/workspaces/{wid}/process/analyses")}
    todo = [a for a in spec["analyses"] if a["name"] not in have]
    if not todo:
        log("exists", f"{len(spec['analyses'])} saved process analyses")
        return
    found = analyst.get(f"/api/workspaces/{wid}/process/candidates")["candidates"]
    cand = next((c for c in found if c["name"] == spec["table"]), None)
    if cand is None:
        raise SystemExit(f"{spec['table']} is not detected as an event log in workspace {wid} (is it selected and loaded?)")
    segment = cand["segments"][0]["column"] if cand["segments"] else None
    for a in todo:
        body = {"asset_id": cand["asset_id"], **cand["mapping"], "save": True, "name": a["name"],
                "filters": [{"column": segment, "op": "=", "value": a["segment"]}] if segment and a.get("segment") else []}
        result = analyst.post(f"/api/workspaces/{wid}/process/analyze", body)
        s = result["summary"]
        log("saved", f"{a['name']}: {s['cases']} cases, {s['variants']} paths, "
                     f"{round(100 * s['fitness'])}% follow the expected path")


def ensure_process_tables(analyst: Api, wid: str, d: dict[str, Any] | None = None) -> str:
    """The event log as the workspace's case and transition tables (Work → Process → Use this process everywhere),
    so Ask, investigations, metrics and dashboards work on process data. Returns the tables' source id."""
    spec = (d or process_demo())["process"]
    names = {f"{spec['table']}_cases", f"{spec['table']}_transitions"}
    src = next((s for s in analyst.get(f"/api/workspaces/{wid}/sources") if s["name"] == "Process mining tables"), None)
    if src is not None and names <= {n for n, a in _assets(analyst, wid, src["id"]).items() if a.get("selected")}:
        log("exists", f"process tables {', '.join(sorted(names))}")
        return src["id"]
    cand = next((c for c in analyst.get(f"/api/workspaces/{wid}/process/candidates")["candidates"] if c["name"] == spec["table"]), None)
    if cand is None:
        raise SystemExit(f"{spec['table']} is not detected as an event log in workspace {wid}")
    built = analyst.post(f"/api/workspaces/{wid}/process/tables", {"asset_id": cand["asset_id"], **cand["mapping"]})
    log("built", f"process tables: {built['cases']} cases, {built['transitions']} transitions "
                 f"({', '.join(t['name'] for t in built['tables'])})")
    return built["source_id"]


def structure_proposal(spec: dict[str, Any], assets: dict[str, dict]) -> list[dict[str, Any]]:
    """The pack's `semantic.datasets` as Ossie datasets over the workspace's catalogued tables (`table` -> its fq name).
    A plain field reads its own column; `expression` derives one; `time` / `dimension` make it groupable."""
    datasets = []
    for d in spec["datasets"]:
        asset = assets.get(d["table"])
        if asset is None:
            raise SystemExit(f"semantic dataset {d['name']}: table {d['table']} is not selected in this workspace")
        fields = []
        for f in d["fields"]:
            field: dict[str, Any] = {"name": f["name"], "expressions": [{"dialect": "ANSI_SQL",
                                                                         "expression": f.get("expression") or f["name"]}]}
            if f.get("time") or f.get("dimension"):
                field["dimension"] = {"is_time": bool(f.get("time"))}
            if f.get("label"):
                field["label"] = f["label"]
            fields.append(field)
        datasets.append({"name": d["name"], "source": asset["fq"], "description": d.get("description"), "fields": fields,
                         **({"primary_key": d["primary_key"]} if d.get("primary_key") else {})})
    return datasets


def metric_proposals(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The pack's `semantic.metrics` as proposal bodies, groupable by every dimension field of their dataset."""
    by_name = {d["name"]: d for d in spec["datasets"]}
    out = []
    for m in spec["metrics"]:
        ds = by_name[m.get("dataset") or spec["datasets"][0]["name"]]
        dims = [f["name"] for f in ds["fields"] if f.get("time") or f.get("dimension")]
        out.append({"name": m["name"], "expression": m["expression"], "display_name": m.get("display_name"),
                    "description": m.get("description"), "format": m.get("format"), "grain": m.get("grain", "record"),
                    "dataset": ds["name"], "dimensions": dims, "ai_context": {"synonyms": list(m.get("synonyms") or [])}})
    return out


def ensure_semantic_model(admin: Api, approver: Api, wid: str, d: dict[str, Any]) -> None:
    """An approved semantic model with the pack's key metrics, through the review API like any person's proposal:
    admin@ proposes the structure and each metric, approver@ approves them (separation of duties, hash-bound
    approvals, a new version only when something changed). Dev and demo only: the seeded approver exists only there.
    A metric already proposed or approved with the same expression is reused as is (the semantic service matches on
    the expression), so a later edit to a spec's synonyms or dimensions alone does not reach an existing workspace."""
    spec = d.get("semantic")
    if not spec:
        return
    base = f"/api/workspaces/{wid}/semantic"
    assets = {a["name"]: a for a in admin.get(f"/api/workspaces/{wid}/assets") if a.get("selected")}
    r = admin.c.post(f"{base}/model/proposals", json={"datasets": structure_proposal(spec, assets),
                                                      "description": spec.get("description")})
    if r.status_code == 201:
        log("proposed", f"semantic model v{r.json()['version']} ({len(spec['datasets'])} datasets) as admin@analystos.local")
    elif r.status_code != 409:  # 409: the current version already holds this structure
        raise SystemExit(f"POST {base}/model/proposals -> {r.status_code}: {r.text[:500]}")
    current = admin.get(base).get("model") or {}
    if current.get("status") == "proposed":
        approver.post(f"{base}/model/approve", {"version": current["version"], "reason": "demo seed (ADR-016)"})
        log("approved", f"semantic model v{current['version']} as approver@analystos.local")
    else:
        log("exists", f"semantic model v{current.get('version')} ({current.get('status')})")
    approved = []
    # Looked up first, like every other piece of the seed: a metric already approved with this expression is not
    # proposed again, so a re-run writes nothing.
    have = {(m["name"], " ".join(str(m.get("expression") or "").split())): m
            for m in admin.get(f"{base}/metrics", params={"status": "approved"})}
    for body in metric_proposals(spec):
        row = have.get((body["name"], " ".join(body["expression"].split())))
        if row is not None:
            approved.append(f"{body['display_name'] or body['name']} v{row['version']}")
            continue
        row = admin.post(f"{base}/metrics", body)["metric"]
        if row["status"] == "proposed":
            approver.post(f"{base}/metrics/{body['name']}/approve", {"version": row["version"], "reason": "demo seed (ADR-016)"})
        elif row["status"] != "approved":
            raise SystemExit(f"metric {body['name']} v{row['version']} is {row['status']}: review it in Catalog -> Metrics")
        on = (row.get("definition") or {}).get("dataset")
        # Same name and expression as a run's earlier proposal: that proposal is the one reviewed (said, not hidden).
        approved.append(f"{body['display_name'] or body['name']} v{row['version']}"
                        + (f" (the run's definition on {on})" if on and on != body["dataset"] else ""))
    log("metrics", f"approved: {', '.join(approved)}")


def ensure_demo_metrics(admin: Api, approver: Api) -> list[tuple[str, str]]:
    """`--only metrics`: the approved semantic model in every demo workspace that exists; a missing one is skipped."""
    have = {w["name"]: w["id"] for w in admin.get("/api/workspaces")}
    done = []
    for path in DEMO_FILES:
        d = demo(path)
        name = d["workspace"]["name"]
        if name not in have:
            log("skipped", f"{name}: no such workspace")
            continue
        log("demo", name)
        ensure_semantic_model(admin, approver, have[name], d)
        done.append((name, have[name]))
    return done


def ensure_process_workspace(admin: Api, analyst: Api, approver: Api, instance_url: str, *, timeout: float,
                             investigate: bool = True) -> str:
    d = process_demo()
    wid = ensure_workspace(admin, d)
    ensure_source(analyst, wid, instance_url, d)
    ensure_brief(analyst, wid, d)
    ensure_process_analyses(analyst, wid, d)
    tables_source = ensure_process_tables(analyst, wid, d)
    inv = (d.get("process") or {}).get("investigation")
    if investigate and inv:  # an investigation on the process tables: the same agents, gateway and approvals
        ensure_investigation(analyst, approver, wid, timeout, objective=inv["objective"], source_ids=[tables_source])
    ensure_semantic_model(admin, approver, wid, d)  # after the run, so its own proposals are reviewed in the same version
    return wid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="analystos demo-seed", description=__doc__.split("\n\n")[0])
    ap.add_argument("--api", default=os.getenv("ANALYSTOS_API", "http://localhost:8000"))
    ap.add_argument("--servicenow", default=os.getenv("ANALYSTOS_SERVICENOW_MOCK_URL") or "http://localhost:8090",
                    help="the ServiceNow instance URL as the API (not this shell) reaches it; compose: http://servicenow-mock:8090")
    ap.add_argument("--web", default=os.getenv("ANALYSTOS_WEB_URL", "http://localhost:5173"))
    ap.add_argument("--password", default=os.getenv("ANALYSTOS_DEMO_PASSWORD", "ChangeMe123!"))
    ap.add_argument("--timeout", type=float, default=1200, help="seconds to wait for the investigation")
    ap.add_argument("--wait-api", type=float, default=180, help="seconds to wait for /api/health")
    ap.add_argument("--skip-investigation", action="store_true", help="everything except the investigation (and its schedule)")
    ap.add_argument("--only", choices=("investigation", "process", "metrics"), default=None,
                    help="build one workspace: investigation (findings, dashboards) or process (process mining); default both. "
                         "metrics: only the approved semantic model of every demo workspace that exists")
    args = ap.parse_args(argv)
    build_investigation, build_process = args.only in (None, "investigation"), args.only in (None, "process")

    health = wait_for_api(args.api, args.wait_api)
    worker = (health.get("checks") or {}).get("worker") or {}
    if build_investigation and worker.get("state") == "down" and not args.skip_investigation:
        log("warning", f"no worker is polling ({worker.get('error')}); the investigation waits until one starts")
    admin = Api(args.api, "admin@analystos.local", args.password)
    analyst = Api(args.api, "analyst@analystos.local", args.password)
    approver = Api(args.api, "approver@analystos.local", args.password)
    ready: list[tuple[str, str]] = []
    if args.only == "metrics":
        for name, wid in ensure_demo_metrics(admin, approver):
            print(f"Approved metrics ready: {name}: {args.web.rstrip('/')}/w/{wid}", flush=True)
        return 0
    if build_process:  # quick (no worker needed), so it is ready while the investigation runs
        log("demo", process_demo()["workspace"]["name"])
        ready.append((process_demo()["workspace"]["name"], ensure_process_workspace(
            admin, analyst, approver, args.servicenow, timeout=args.timeout, investigate=not args.skip_investigation)))
    if build_investigation:
        log("demo", demo()["workspace"]["name"])
        wid = ensure_workspace(admin)
        ensure_source(analyst, wid, args.servicenow)
        ensure_brief(analyst, wid)
        ensure_file_and_recipe(analyst, wid)
        if not args.skip_investigation:
            rid = ensure_investigation(analyst, approver, wid, args.timeout)
            ensure_schedule(analyst, wid, rid)
        ensure_monitor(analyst, wid, evaluate=not args.skip_investigation)
        ensure_semantic_model(admin, approver, wid, demo())
        ready.insert(0, (demo()["workspace"]["name"], wid))
    shown = args.password if args.password == "ChangeMe123!" else "(as given)"
    web = args.web.rstrip("/")
    print("\n" + "\n".join(f"Demo workspace ready: {name}: {web}/w/{wid}" for name, wid in ready)
          + (f"\n  process mining: {web}/w/{ready[-1][1]}/work?tab=process" if build_process else "")
          + "\n  sign in as analyst@analystos.local (analysis), approver@analystos.local (approvals) or admin@analystos.local;"
          f" password {shown}\n", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
