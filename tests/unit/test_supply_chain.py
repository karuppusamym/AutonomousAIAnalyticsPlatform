"""S12-T2 supply chain: the CI workflows scan for secrets, build an SBOM per image, fail on fixable high/critical
vulnerabilities, sign by digest only from main (keyless, short-lived OIDC), rescan weekly, run the S12 release
gates after migrate and the per-model matrix nightly; images run as a numeric non-root user. No services."""
from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WF = ROOT / ".github" / "workflows"


def _wf(name: str) -> dict:
    doc = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the `on:` key as a boolean
    return doc


def _steps(job: dict) -> str:
    return "\n".join(yaml.safe_dump(s) for s in job["steps"])


def test_supply_chain_workflow_covers_secrets_sbom_scan_sign_and_weekly_rescan():
    wf = _wf("supply-chain.yml")
    assert wf["permissions"] == {"contents": "read"}  # least privilege by default
    assert any("cron" in s for s in wf["on"]["schedule"]) and "pull_request" in wf["on"]
    jobs = wf["jobs"]
    gitleaks = _steps(jobs["gitleaks"])
    assert "gitleaks" in gitleaks and "--config /repo/.gitleaks.toml" in gitleaks and "--exit-code 1" in gitleaks
    assert jobs["gitleaks"]["steps"][0]["with"]["fetch-depth"] == 0  # the whole history
    images = jobs["images"]
    names = {m["name"] for m in images["strategy"]["matrix"]["include"]}
    assert {"app", "web", "sandbox", "superset"} <= names
    steps = {s.get("name", s.get("uses", "")): s for s in images["steps"]}
    sbom = next(s for s in images["steps"] if s.get("uses", "").startswith("anchore/sbom-action"))
    assert sbom["with"]["format"] == "spdx-json"
    scan = next(s for s in images["steps"] if s.get("uses", "").startswith("anchore/scan-action"))
    assert scan["with"]["fail-build"] is True and scan["with"]["severity-cutoff"] == "high"
    sign = steps["Sign by digest and attest the SBOM (cosign keyless, OIDC)"]
    assert sign["if"] == "env.PUBLISH == 'true'" and "cosign sign --yes" in sign["run"] and "cosign attest" in sign["run"]
    assert "@${{ steps.push.outputs.digest }}" in sign["run"]  # by digest, never a mutable tag
    assert images["env"]["PUBLISH"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    assert images["permissions"]["id-token"] == "write"
    for name, job in jobs.items():  # only the image job may mint OIDC tokens or push packages
        if name != "images":
            assert "id-token" not in (job.get("permissions") or {}) and "packages" not in (job.get("permissions") or {})
    deps = _steps(jobs["dependencies"])
    assert "npm ci" in deps and "npm audit" in deps and "pip-audit" in deps
    helm = _steps(jobs["helm"])
    assert "azure/setup-helm" in helm and "test_helm_chart.py" in helm


def test_every_action_is_pinned_to_a_version():
    for path in WF.glob("*.yml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "uses:" in line:
                ref = line.split("uses:", 1)[1].strip()
                assert "@" in ref and not ref.endswith(("@main", "@master", "@latest")), (path.name, ref)


def test_gitleaks_extends_the_default_rules_with_a_narrow_allowlist():
    cfg = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert cfg["extend"]["useDefault"] is True
    allow = cfg["allowlist"]
    assert len(allow["regexes"]) <= 6 and all("sk-" not in r and r != ".*" for r in allow["regexes"])
    assert not any(p in ("'.*'", ".*", "src/", "config/") for p in allow["paths"])


def test_ci_runs_the_release_gates_after_migrate_and_nightly_runs_the_matrix():
    backend = _wf("ci.yml")["jobs"]["backend"]["steps"]
    runs = [s.get("run", "") for s in backend]
    migrate = runs.index(".venv/bin/analystos migrate")
    gate = next(i for i, r in enumerate(runs) if "release_safety,model_matrix_fake" in r)
    assert gate > migrate
    nightly = _wf("nightly-eval.yml")
    assert "model_matrix" in _steps(nightly["jobs"]["live"]) and nightly["permissions"] == {"contents": "read"}
    assert nightly["jobs"]["live"]["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"


def test_images_run_as_a_numeric_non_root_user_writable_by_group_zero():
    text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "\nUSER 10001\n" in text and "chmod -R g=u /app/var" in text and "--gid 0" in text
