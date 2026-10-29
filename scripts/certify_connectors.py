"""Run a source kind's real-engine tests and, only if they pass, write its dated certification evidence.

    python scripts/certify_connectors.py [--kinds postgres mysql sqlite duckdb csv] [--out docs/60-delivery/evidence]

A connector is certified by live evidence, never by mock tests (spec v1 §62). For each kind the
script runs that kind's integration tests against the real engine (tests/integration/
test_generic_sources.py: the compose Postgres, a throwaway MySQL container, real SQLite and DuckDB
files) and writes docs/60-delivery/evidence/connector-<kind>-YYYYMMDD.md only when every selected test
passed and none was skipped. connectors/certification.py derives the `certified` flag from that file.
Kinds with no live test here (warehouses, ServiceNow: mock only) are never written.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_FILE = "tests/integration/test_generic_sources.py"
LIVE_TESTS = {  # kind -> pytest -k expression selecting its real-engine tests (in TEST_FILE unless TEST_FILES names one)
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlite": "file_database and sqlite",
    "duckdb": "file_database and duckdb",
    "csv": "",  # real CSV/JSON/NDJSON/Excel/Parquet files: ingest, stage, gateway read under the workspace role
    "trino": "",  # the compose `federation` profile: catalogs pg + tpch + memory, cross-catalog join (TRN-004)
}
TEST_FILES = {"csv": "tests/integration/test_file_ingest_loads.py", "trino": "tests/integration/test_trino_federation.py"}
# Kinds whose only test here is a double: never certified by this script (spec v1 §62), listed so a run says so.
MOCK_ONLY = {"servicenow": "tests/integration/test_staging_servicenow_load.py (the Table-API mock)"}


def engine(kind: str) -> str:
    if kind == "sqlite":
        return f"SQLite {sqlite3.sqlite_version} (file database)"
    if kind == "duckdb":
        import duckdb

        return f"DuckDB {duckdb.__version__} (file database)"
    if kind == "csv":
        return "real CSV, JSON, NDJSON, Excel and Parquet files staged into the test PostgreSQL"
    if kind == "trino":
        try:
            import requests

            url = os.environ.get("ANALYSTOS_TEST_TRINO_URL", "http://127.0.0.1:8086")
            version = requests.get(f"{url}/v1/info", timeout=5).json()["nodeVersion"]["version"]
            return f"Trino {version} (docker image trinodb/trino:{version}; catalogs pg, tpch, memory)"
        except Exception:  # noqa: BLE001 - the version is descriptive only
            return "Trino"
    if kind == "mysql":
        return f"MySQL (docker image {os.environ.get('ANALYSTOS_TEST_MYSQL_IMAGE', 'mysql:8.4')})"
    try:
        from sqlalchemy import create_engine, text

        url = os.environ.get("ANALYSTOS_TEST_ADMIN_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos")
        with create_engine(url).connect() as c:
            return "PostgreSQL " + str(c.execute(text("SHOW server_version")).scalar()) + f" ({c.engine.url.host}:{c.engine.url.port})"
    except Exception:  # noqa: BLE001 - the version is descriptive only
        return "PostgreSQL"


def test_target(kind: str) -> str:
    target = TEST_FILES.get(kind, TEST_FILE)
    return f'{target} -k "{LIVE_TESTS[kind]}"' if LIVE_TESTS[kind] else target


def run(kind: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        xml = Path(tmp) / "junit.xml"
        select = ["-k", LIVE_TESTS[kind]] if LIVE_TESTS[kind] else []
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "integration",
               TEST_FILES.get(kind, TEST_FILE), *select, f"--junitxml={xml}"]
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        cases = []
        if xml.exists():
            for tc in ET.parse(xml).iter("testcase"):
                status = "passed"
                for child in tc:
                    if child.tag in ("failure", "error", "skipped"):
                        status = child.tag
                cases.append((tc.get("name"), status, float(tc.get("time") or 0)))
    shown = [c if " " not in c else f'"{c}"' for c in cmd[1:-1]]
    return {"cmd": " ".join(shown), "returncode": proc.returncode, "cases": cases, "tail": proc.stdout.strip().splitlines()[-3:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", nargs="*", default=list(LIVE_TESTS))
    ap.add_argument("--out", default=str(ROOT / "docs" / "60-delivery" / "evidence"))
    args = ap.parse_args()
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "config"], cwd=ROOT, capture_output=True,
                           text=True).stdout.strip()
    at = f"{head} + uncommitted changes" if dirty else head
    now =datetime.now(UTC)
    failed = 0
    for kind in args.kinds:
        if kind in MOCK_ONLY:
            print(f"{kind}: NOT CERTIFIABLE HERE (only a test double: {MOCK_ONLY[kind]}); needs a live instance")
            continue
        if kind not in LIVE_TESTS:
            print(f"{kind}: NOT CERTIFIABLE HERE (no real-engine test in this repository; needs a live system)")
            continue
        r = run(kind)
        ok = r["returncode"] == 0 and r["cases"] and all(s == "passed" for _, s, _ in r["cases"])
        print(f"{kind}: {'PASS' if ok else 'NOT CERTIFIED'} ({len(r['cases'])} tests) {' '.join(r['tail'][-1:])}")
        if not ok:
            failed += 1
            continue
        path = Path(args.out) / f"connector-{kind}-{now:%Y%m%d}.md"
        lines = ["---", f"kind: {kind}", f"date: {now:%Y-%m-%d}", f"engine: {engine(kind)}",
                 f"test: {test_target(kind)}", "result: pass", f"commit: {at}", "---",
                 f"# Connector certification evidence: {kind}", "",
                 f"Live run {now:%Y-%m-%d %H:%M} UTC at `{at}`, written by `scripts/certify_connectors.py` after every "
                 "selected real-engine test passed (none skipped).", "",
                 f"Command: `python {r['cmd']}`", "", "| Test | Result | Seconds |", "|---|---|---|"]
        lines += [f"| `{name}` | {status} | {secs:.1f} |" for name, status, secs in r["cases"]]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
