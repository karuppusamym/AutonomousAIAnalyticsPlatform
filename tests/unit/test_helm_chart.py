"""Helm chart (P4-S04): `helm lint` and `helm template` when a helm binary is available (PATH or
ANALYSTOS_HELM); the value-level checks always run. No cluster and no network needed."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "analystos"
HELM = os.environ.get("ANALYSTOS_HELM") or shutil.which("helm")
needs_helm = pytest.mark.skipif(not HELM, reason="helm binary not available (set ANALYSTOS_HELM)")


def _values(name: str = "values.yaml") -> dict:
    return yaml.safe_load((CHART / name).read_text(encoding="utf-8"))


def test_every_task_queue_has_a_worker_pool():
    queues = set(yaml.safe_load((ROOT / "config" / "task_queues.yaml").read_text(encoding="utf-8"))["queues"])
    for name in ("values.yaml", "values-ha.yaml"):
        pools = _values(name)["workers"]
        served = {q.strip() for p in pools.values() for q in p["queues"].split(",")}
        assert served == queues, name


def test_values_never_carry_credentials():
    text = "\n".join((CHART / f).read_text(encoding="utf-8") for f in ("values.yaml", "values-ha.yaml", "values-airgapped.yaml"))
    for marker in (" sk-", "\"sk-", "password:", "PASSWORD=", "postgresql://", "postgresql+psycopg://"):
        assert marker not in text, marker
    assert _values()["secrets"]["existingSecret"]


def _render(*files: str, sets: tuple[str, ...] = ()) -> list[dict]:
    args = [HELM, "template", "t", str(CHART)]
    for f in files:
        args += ["-f", str(CHART / f)]
    for s in sets:
        args += ["--set", s]
    out = subprocess.run(args, capture_output=True, text=True, timeout=60, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _kind(docs, kind):
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


@needs_helm
def test_helm_lint():
    for extra in ((), ("-f", str(CHART / "values-ha.yaml")), ("-f", str(CHART / "values-ha.yaml"), "-f", str(CHART / "values-airgapped.yaml"))):
        r = subprocess.run([HELM, "lint", str(CHART), *extra], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr


@needs_helm
def test_ha_render_has_probes_pdbs_and_one_deployment_per_pool():
    docs = _render("values-ha.yaml")
    deployments = _kind(docs, "Deployment")
    pools = _values("values-ha.yaml")["workers"]
    for name, pool in pools.items():
        dep = deployments[f"t-analystos-worker-{name}"]
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "ANALYSTOS_WORKER_QUEUES", "value": pool["queues"]} in env
    api = deployments["t-analystos-api"]["spec"]["template"]["spec"]
    container = api["containers"][0]
    assert {"readinessProbe", "livenessProbe", "startupProbe"} <= set(container)
    assert container["securityContext"]["readOnlyRootFilesystem"] is True and api["securityContext"]["runAsNonRoot"] is True
    assert api["topologySpreadConstraints"]
    pdbs = _kind(docs, "PodDisruptionBudget")
    assert {"t-analystos-api", "t-analystos-worker-analysis", "t-analystos-worker-compute", "t-analystos-web",
            "t-analystos-scheduler"} <= set(pdbs)
    assert deployments["t-analystos-scheduler"]["spec"]["replicas"] == 2
    assert "t-analystos-worker-compute" in _kind(docs, "HorizontalPodAutoscaler")
    assert not _kind(docs, "Secret")  # credentials only by reference
    job = _kind(docs, "Job")
    assert job and next(iter(job.values()))["metadata"]["annotations"]["helm.sh/hook"] == "pre-install,pre-upgrade"


@needs_helm
def test_air_gapped_render_locks_egress_and_switches_models():
    docs = _render("values-ha.yaml", "values-airgapped.yaml")
    config = _kind(docs, "ConfigMap")["t-analystos-config"]["data"]
    assert config["ANALYSTOS_AIR_GAPPED"] == "true"
    assert config["ANALYSTOS_MODELS_CONFIG"] == "/app/config/models.airgapped.yaml"
    assert config["ANALYSTOS_LOCAL_LLM_URL"].startswith("http://vllm")
    assert config["ANALYSTOS_ENV"] == "production"
    policy = _kind(docs, "NetworkPolicy")["t-analystos-egress"]  # the release-wide one; isolated pools have their own
    assert "Egress" in policy["spec"]["policyTypes"]
    cidrs = [b["ipBlock"]["cidr"] for rule in policy["spec"]["egress"] for b in rule.get("to", []) if "ipBlock" in b]
    assert cidrs == ["10.0.0.0/8"]
    image = _kind(docs, "Deployment")["t-analystos-api"]["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.startswith("registry.internal:5000/")


@needs_helm
def test_oidc_values_render_the_mapping_and_require_the_issuer():
    docs = _render(sets=("oidc.enabled=true", "oidc.issuer=https://idp.corp", "oidc.clientId=aos", "oidc.redirectUri=https://a/cb"))
    config = _kind(docs, "ConfigMap")
    assert config["t-analystos-config"]["data"]["ANALYSTOS_OIDC_MAPPING_FILE"] == "/etc/analystos/oidc.yaml"
    assert "workspace_roles" in config["t-analystos-oidc"]["data"]["oidc.yaml"]
    r = subprocess.run([HELM, "template", "t", str(CHART), "--set", "oidc.enabled=true"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "oidc.issuer is required" in r.stderr


def test_defaults_keep_governance_boundaries():
    """M1/M4/M5: no clearance comes from the IdP, the builder login is a required secret, and a default
    install never runs a sandbox child unisolated (P4-02: process isolation, refused where unavailable)."""
    from analystos.security.oidc import platform_controlled

    values = _values()
    mapping = yaml.safe_load(values["oidc"]["mapping"])
    assert not [a for a in mapping.get("attributes") or {} if platform_controlled(a)]
    assert "ANALYSTOS_ANALYTICS_BUILDER_URL" in (CHART / "values.yaml").read_text(encoding="utf-8")
    assert values["config"]["sandboxIsolation"] == "process"
    assert _values("values-airgapped.yaml")["config"]["sandboxIsolation"] != "off"
    assert values["workers"]["elt"]["replicas"] == 0  # dbt is not in the app image
    notes = (CHART / "templates" / "NOTES.txt").read_text(encoding="utf-8")  # `off` is called out, and where to check status
    assert "WITH the pod's network" in notes and "checks.sandbox" in notes


@needs_helm
def test_default_render_requires_sandbox_isolation():
    config = _kind(_render(), "ConfigMap")["t-analystos-config"]["data"]
    assert config["ANALYSTOS_SANDBOX_ISOLATION"] == "process"


def test_pool_values_match_the_settings_defaults_and_pgbouncer_is_off():
    """P4-S05: the chart's pool defaults are the application's, and the pooler is opt-in."""
    from analystos.core.config import Settings

    s, values = Settings(_env_file=None), _values()
    pools = values["database"]["pools"]
    assert pools["mode"] == s.db_pool_mode and pools["recycleSeconds"] == s.db_pool_recycle
    for plane, prefix in (("control", "db"), ("analytics", "analytics"), ("loader", "loader")):
        assert (pools[plane]["size"], pools[plane]["maxOverflow"], pools[plane]["timeoutSeconds"]) == (
            getattr(s, f"{prefix}_pool_size"), getattr(s, f"{prefix}_max_overflow"), getattr(s, f"{prefix}_pool_timeout"))
    assert values["pgbouncer"]["enabled"] is False and values["pgbouncer"]["poolMode"] == "transaction"


@needs_helm
def test_pool_settings_render_and_pgbouncer_is_opt_in():
    docs = _render()
    config = _kind(docs, "ConfigMap")["t-analystos-config"]["data"]
    assert (config["ANALYSTOS_DB_POOL_SIZE"], config["ANALYSTOS_DB_MAX_OVERFLOW"]) == ("10", "20")
    assert config["ANALYSTOS_DB_TRANSACTION_POOLER"] == "false"
    assert "t-analystos-pgbouncer" not in _kind(docs, "Deployment")

    docs = _render(sets=("pgbouncer.enabled=true", "pgbouncer.postgresHost=pg.db.svc", "pgbouncer.existingSecret=pgb",
                         "database.pools.mode=none"))
    config = _kind(docs, "ConfigMap")["t-analystos-config"]["data"]
    assert config["ANALYSTOS_DB_TRANSACTION_POOLER"] == "true" and config["ANALYSTOS_DB_POOL_MODE"] == "none"
    pod = _kind(docs, "Deployment")["t-analystos-pgbouncer"]["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert env["POOL_MODE"] == "transaction" and env["DB_HOST"] == "pg.db.svc" and env["MAX_PREPARED_STATEMENTS"] == "0"
    assert pod["containers"][0]["envFrom"] == [{"secretRef": {"name": "pgb"}}]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert "t-analystos-pgbouncer" in _kind(docs, "Service")
    r = subprocess.run([HELM, "template", "t", str(CHART), "--set", "pgbouncer.enabled=true"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "is required when pgbouncer.enabled" in r.stderr


def _merged(*files: str) -> dict:
    """Helm's values merge: maps merge key by key, a null removes the key, anything else replaces."""
    def merge(base, over):
        out = dict(base)
        for k, v in over.items():
            if v is None:
                out.pop(k, None)
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out

    values = _values()
    for f in files:
        values = merge(values, _values(f))
    return values


def _cpu(v) -> float:
    return float(v[:-1]) / 1000 if str(v).endswith("m") else float(v)


def _mem_gi(v) -> float:
    return float(v[:-2]) / 1024 if v.endswith("Mi") else float(v[:-2])


def test_small_values_fit_one_two_cpu_four_gib_node():
    """P7-17: one API, one worker on every queue, one web pod, no PDBs, requests within 2 CPU / 4 GiB."""
    from analystos.workflows.queues import WORKLOADS, parse_workloads

    v = _merged("values-small.yaml")
    assert list(v["workers"]) == ["all"] and parse_workloads(v["workers"]["all"]["queues"]) == list(WORKLOADS)
    assert v["api"]["replicas"] == 1 and v["web"]["replicas"] == 1 and v["workers"]["all"]["replicas"] == 1
    assert v["scheduler"]["replicas"] == 0 and v["config"]["extra"]["ANALYSTOS_INPROCESS_SCHEDULER"] == "true"
    components = [v["api"], v["web"], v["workers"]["all"], v["scheduler"]]
    assert not [c for c in components if (c.get("pdb") or {}).get("enabled")]
    assert not v["pgbouncer"]["enabled"] and not v["api"]["autoscaling"]["enabled"]
    running = [c for c in components if c["replicas"]]
    cpu = sum(_cpu(c["resources"]["requests"]["cpu"]) * c["replicas"] for c in running)
    mem = sum(_mem_gi(c["resources"]["requests"]["memory"]) * c["replicas"] for c in running)
    assert cpu <= 1.0 and mem <= 2.0, (cpu, mem)  # half the node: system pods and headroom keep the rest
    assert v["config"]["supersetUrl"] == ""  # no bi profile: preview publishing


@needs_helm
def test_small_render_has_one_worker_and_no_pdbs():
    docs = _render("values-small.yaml")
    deployments = _kind(docs, "Deployment")
    assert [n for n in deployments if "-worker-" in n] == ["t-analystos-worker-all"]
    env = deployments["t-analystos-worker-all"]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "ANALYSTOS_WORKER_QUEUES", "value": "all"} in env
    assert not _kind(docs, "PodDisruptionBudget") and not _kind(docs, "HorizontalPodAutoscaler")


def test_isolated_pools_are_opt_in_in_the_default_values():
    """P7-06: compute-py / compute-ml are declared isolated and deploy nothing until replicas > 0."""
    pools = _values()["workers"]
    assert {n for n, p in pools.items() if p.get("isolated")} == {"compute-py", "compute-ml"}
    assert all(pools[n]["replicas"] == 0 for n in ("compute-py", "compute-ml"))


@needs_helm
def test_isolated_pools_get_no_credentials_and_their_own_network_policy():
    default = _render()
    assert not [n for n in _kind(default, "Deployment") if n.endswith(("compute-py", "compute-ml"))]
    assert "t-analystos-isolated-pools" not in _kind(default, "NetworkPolicy")
    assert _kind(default, "ConfigMap")["t-analystos-config"]["data"]["ANALYSTOS_ISOLATED_POOLS"] == ""

    docs = _render("values-ha.yaml")
    config = _kind(docs, "ConfigMap")["t-analystos-config"]["data"]
    assert set(config["ANALYSTOS_ISOLATED_POOLS"].split(",")) == {"compute-py", "compute-ml"}
    assert config["ANALYSTOS_WORKER_ARTIFACT_URL"] == "http://t-analystos-api:8000"
    for pool in ("compute-py", "compute-ml"):
        dep = _kind(docs, "Deployment")[f"t-analystos-worker-{pool}"]
        pod = dep["spec"]["template"]
        container = pod["spec"]["containers"][0]
        assert pod["metadata"]["labels"]["analystos.io/isolated"] == "true"
        assert "envFrom" not in container  # neither the ConfigMap nor the Secret
        assert {e["name"] for e in container["env"]} == {
            "ANALYSTOS_WORKER_QUEUES", "ANALYSTOS_WORKER_ARTIFACT_URL", "ANALYSTOS_TEMPORAL_ADDRESS",
            "ANALYSTOS_TEMPORAL_NAMESPACE", "ANALYSTOS_TEMPORAL_QUEUE_PREFIX", "ANALYSTOS_ENV"}
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert [v["name"] for v in pod["spec"]["volumes"]] == ["tmp"]
    policies = _kind(docs, "NetworkPolicy")
    iso = policies["t-analystos-isolated-pools"]["spec"]
    assert iso["podSelector"]["matchLabels"]["analystos.io/isolated"] == "true" and iso["ingress"] == []
    peers = [rule["to"] for rule in iso["egress"]]
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/instance": "t", "app.kubernetes.io/component": "api"}}} in peers[1]
    assert peers[2] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "temporal"}}}]
    assert not [b for rule in iso["egress"] for b in rule["to"] if "ipBlock" in b]
    release = policies["t-analystos-egress"]["spec"]["podSelector"]
    assert {"key": "analystos.io/isolated", "operator": "NotIn", "values": ["true"]} in release["matchExpressions"]
    # an isolated pool without a Temporal egress rule does not render
    r = subprocess.run([HELM, "template", "t", str(CHART), "--set", "workers.compute-py.replicas=1"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0 and "isolatedPools.temporalEgress" in r.stderr
