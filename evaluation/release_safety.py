"""Release-safety gate (spec S12 §3, blocking in CI): the negative suites, counted as gate metrics.

  sql_safety       the adversarial SQL corpus (every dialect), the validator rules and the injection suite:
                   every hostile statement refused, every benign one accepted (100%)
  cross_tenant     RLS coverage (metadata and live database), tenant-scoped caches and background work, and the
                   two-tenant integration suite: 0 leaks (a failing test is a leak)
  publish_integrity  approvals bound to payload hashes, preview and Superset publishing: 100%

Each group runs as its own pytest process with a JUnit report; a test that is skipped or did not run counts in
`not_run` and fails the gate, so a missing database can never pass it (the cross-tenant suite needs Postgres).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GROUPS: dict[str, list[str]] = {
    "sql_safety": ["tests/unit/test_gateway_corpus.py", "tests/unit/test_gateway_validator.py",
                   "tests/unit/test_security_injection.py"],
    "cross_tenant": ["tests/unit/test_tenancy_rls_coverage.py", "tests/unit/test_cache_tenant_isolation.py",
                     "tests/unit/test_tenancy_background.py", "tests/integration/test_tenancy_rls.py"],
    "publish_integrity": ["tests/unit/test_approval_view.py", "tests/unit/test_publishing_preview.py",
                          "tests/unit/test_publishing_superset.py"],
}


def run_group(files: list[str], *, timeout: int = 900) -> dict[str, int]:
    """Run pytest on `files`; counts of tests, passed, failed (failures + errors) and skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "integration or not integration",
               f"--junitxml={report}", *files]
        subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
        if not report.exists():
            return {"tests": 0, "passed": 0, "failed": 0, "skipped": 0}
        root = ET.parse(report).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        tests = sum(int(s.get("tests", 0)) for s in suites)
        failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
        skipped = sum(int(s.get("skipped", 0)) for s in suites)
        return {"tests": tests, "passed": tests - failed - skipped, "failed": failed, "skipped": skipped}


def run(groups: dict[str, list[str]] | None = None, runner: Any = run_group) -> dict[str, Any]:
    groups = groups or GROUPS
    counts = {name: runner(files) for name, files in groups.items()}

    def share(name: str) -> float:
        c = counts.get(name) or {}
        return round(c.get("passed", 0) / c["tests"], 6) if c.get("tests") else 0.0

    return {
        "sql_safety_pass_share": share("sql_safety"),
        "sql_safety_cases": counts.get("sql_safety", {}).get("tests", 0),
        "cross_tenant_leaks": counts.get("cross_tenant", {}).get("failed", 0),
        "cross_tenant_cases": counts.get("cross_tenant", {}).get("passed", 0),
        "publish_integrity_share": share("publish_integrity"),
        "publish_integrity_cases": counts.get("publish_integrity", {}).get("tests", 0),
        "not_run": sum(c.get("skipped", 0) for c in counts.values())
        + sum(1 for c in counts.values() if not c.get("tests")),
        "groups": counts,
    }
