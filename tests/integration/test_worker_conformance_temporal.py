"""P7-06 acceptance on the Temporal transport: the whole worker conformance suite, run against
`IsolatedTaskWorkflow` + `analystos.workers.main` processes polling `<prefix>-compute-py` / `-compute-ml` on a
Temporal dev server (tests/conformance/worker/temporal_impl.py). Skips when the dev server cannot start."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(sys.platform == "win32", reason="worker pools need POSIX select() on pipes; the conformance "
                    "suite skips every test on Windows (tests/conformance/worker/conftest.py)")
def test_the_conformance_suite_passes_on_temporal_pools():
    env = {**os.environ, "ANALYSTOS_CONFORMANCE_IMPL": "temporal", "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "-rs",
                        str(ROOT / "tests" / "conformance" / "worker" / "test_worker_conformance.py")],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=1500)
    tail = r.stdout[-3000:]
    if "Temporal dev server unavailable" in r.stdout:
        pytest.skip("Temporal dev server unavailable: " + tail[-300:])
    assert r.returncode == 0, tail + r.stderr[-2000:]
    assert " passed" in tail and "skipped" not in tail and "failed" not in tail, tail
