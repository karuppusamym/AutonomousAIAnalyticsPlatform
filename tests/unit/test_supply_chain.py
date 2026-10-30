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
                if ref.startswith("./"):  # a reusable workflow of this repository: versioned with it
                    continue
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
    seed = runs.index(".venv/bin/analystos seed")  # the matrix's Ask suite runs as the seeded admin
    gate = next(i for i, r in enumerate(runs) if "release_safety,model_matrix_fake" in r)
    assert gate > seed > migrate
    nightly = _wf("nightly-eval.yml")
    assert "model_matrix" in _steps(nightly["jobs"]["live"]) and nightly["permissions"] == {"contents": "read"}
    assert nightly["jobs"]["live"]["env"]["OPENROUTER_API_KEY"] == "${{ secrets.OPENROUTER_API_KEY }}"


def test_images_run_as_a_numeric_non_root_user_writable_by_group_zero():
    text = (ROOT / "deploy" / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "\nUSER 10001\n" in text and "chmod -R g=u /app/var" in text and "--gid 0" in text


def test_release_publishes_signed_images_and_chart_then_deploys_through_a_gated_environment():
    """FND-004: a version tag builds the five images behind the scan gate, pushes and signs them by digest, pushes
    the chart to GHCR as OCI, and deploys through deploy.yml, whose Environment holds the reviewers and cluster."""
    wf = _wf("release.yml")
    assert wf["permissions"] == {"contents": "read"} and "workflow_dispatch" in wf["on"]
    assert any(t.startswith("v[0-9]") for t in wf["on"]["push"]["tags"]) and "branches" not in wf["on"]["push"]
    jobs = wf["jobs"]
    images = jobs["images"]
    assert {m["name"] for m in images["strategy"]["matrix"]["include"]} == {"app", "web", "mock", "sandbox", "superset"}
    scan = next(s for s in images["steps"] if s.get("uses", "").startswith("anchore/scan-action"))
    assert scan["with"]["fail-build"] is True and scan["with"]["only-fixed"] is True
    steps = [s.get("name", s.get("uses", "")) for s in images["steps"]]
    assert steps.index("Vulnerability scan (Grype; fixable high/critical fail)") < steps.index("Push the version and commit tags")
    body = _steps(images)
    assert '$IMAGE@${{ steps.push.outputs.digest }}' in body and "cosign attest" in body and "docker push \"$IMAGE:$VERSION\"" in body
    chart = _steps(jobs["chart"])
    assert '--version "$VERSION" --app-version "$VERSION"' in chart and 'helm push' in chart and "oci://ghcr.io/" in str(jobs["chart"]["env"])
    assert "cosign sign --yes" in chart
    for name in ("staging", "production"):
        assert jobs[name]["uses"] == "./.github/workflows/deploy.yml" and jobs[name]["secrets"] == "inherit"
        assert jobs[name]["with"]["environment"] == name and "id-token" not in jobs[name]["permissions"]
    assert "vars.PRODUCTION_DEPLOY == 'true'" in jobs["production"]["if"] and jobs["production"]["needs"][-1] == "staging"
    for name, job in jobs.items():  # only the publishing jobs mint OIDC tokens or write packages
        perms = job.get("permissions") or {}
        if name not in ("images", "chart"):
            assert "id-token" not in perms and perms.get("packages") != "write", name


def test_deploy_verifies_signatures_and_pins_digests_with_cluster_credentials_from_the_environment():
    wf = _wf("deploy.yml")
    assert set(wf["on"]) == {"workflow_call", "workflow_dispatch"} and wf["permissions"] == {"contents": "read", "packages": "read"}
    job = wf["jobs"]["deploy"]
    assert job["environment"]["name"] == "${{ inputs.environment }}"
    assert job["concurrency"]["cancel-in-progress"] is False
    body = _steps(job)
    assert "cosign verify" in body and r"release\.yml@refs/" in job["env"]["SIGNER"]
    assert 'image.tag=$VERSION@$APP_DIGEST' in body and 'webImage.tag=$VERSION@$WEB_DIGEST' in body
    assert "--atomic" in body and "helm upgrade --install" in body and "/api/health" in body
    assert "${{ secrets.KUBECONFIG }}" in body and "umask 077" in body
    text = (WF / "deploy.yml").read_text(encoding="utf-8") + (WF / "release.yml").read_text(encoding="utf-8")
    assert "kind: Config" not in text and "client-key-data" not in text  # no cluster in the repo


def test_release_and_deploy_scripts_read_inputs_from_the_environment_never_inline():
    """Security self-check 2026-10-30: an `${{ inputs.* }}` / `${{ github.event.* }}` expression inside `run:` is
    expanded into the script before bash parses it (script injection); values reach scripts through `env:`."""
    for name in ("release.yml", "deploy.yml"):
        for job in _wf(name)["jobs"].values():
            for step in job.get("steps") or []:
                script = step.get("run") or ""
                assert "${{ inputs." not in script and "${{ github.event." not in script, (name, step.get("name"))


def test_production_deploys_only_images_signed_by_a_tagged_release():
    """release.yml dispatched from a branch signs its images with the same workflow identity; production must only
    accept the identity of a tag run (docs/30-runbooks/14-release-and-deploy.md, admission control)."""
    signer = _wf("deploy.yml")["jobs"]["deploy"]["env"]["SIGNER"]
    assert signer.endswith(r"release\.yml@refs/${{ inputs.environment == 'production' && 'tags/v' || '' }}")
