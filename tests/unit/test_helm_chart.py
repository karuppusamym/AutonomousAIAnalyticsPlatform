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
    text = "\n".join((CHART / f).read_text(encoding="utf-8") for f in ("values.yaml", "values-ha.yaml", "values-airgapped.yaml", "values-kind.yaml"))
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
            "ANALYSTOS_TEMPORAL_NAMESPACE", "ANALYSTOS_TEMPORAL_QUEUE_PREFIX", "ANALYSTOS_ENV",
            "ANALYSTOS_METRICS_PORT", "ANALYSTOS_METRICS_DIR"}  # S12: a scrape port, no credential
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



# ------------------------------------------------------------------ S12: roles, KEDA, gVisor, ESO, SCC, monitoring
S12_FULL = ("keda.enabled=true", "gvisor.enabled=true", "gvisor.createRuntimeClass=true", "openshift.enabled=true",
            "networkPolicy.perRole=true", "networkPolicy.ingressFrom[0].namespaceSelector.matchLabels.ingress=yes",
            "networkPolicy.monitoringFrom[0].namespaceSelector.matchLabels.monitoring=yes",
            "networkPolicy.egress.platform[0].ipBlock.cidr=10.1.0.0/16",
            "networkPolicy.egress.models[0].ipBlock.cidr=10.9.0.0/24",
            "networkPolicy.egress.sources[0].ipBlock.cidr=10.2.0.0/16",
            "monitoring.podMonitor.enabled=true", "monitoring.prometheusRule.enabled=true",
            "monitoring.grafanaDashboards.enabled=true", "monitoring.otel.enabled=true",
            "monitoring.otel.endpoint=http://otel:4318", "externalSecrets.enabled=true",
            "externalSecrets.secretStoreRef.name=vault", "externalSecrets.dataFrom[0].extract.key=analystos/prod",
            "migrations.databaseUrlKey=ANALYSTOS_MIGRATOR_DATABASE_URL")


def test_s12_values_name_every_worker_role():
    pools = _values()["workers"]
    assert pools["analysis"]["role"] == "query" and pools["compute"]["role"] == "compute"
    assert pools["compute"]["gvisor"] and pools["compute-py"]["gvisor"] and pools["compute-ml"]["gvisor"]
    assert pools["analysis"]["keda"]["enabled"] and pools["compute"]["keda"]["enabled"]
    v = _values()
    assert v["keda"]["enabled"] is False and v["gvisor"]["enabled"] is False and v["openshift"]["enabled"] is False
    assert v["externalSecrets"]["enabled"] is False and v["networkPolicy"]["perRole"] is False  # all opt-in
    roles = v["networkPolicy"]["roles"]
    assert "models" not in roles["compute"] and "sources" not in roles["compute"]  # statistics never reach a model or source


@needs_helm
def test_helm_lint_with_every_s12_feature_on():
    args = [HELM, "lint", str(CHART), "-f", str(CHART / "values-ha.yaml")]
    for s in S12_FULL:
        args += ["--set", s]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


@needs_helm
def test_keda_scales_named_queues_instead_of_the_hpa():
    docs = _render("values-ha.yaml", sets=("keda.enabled=true",))
    scaled = _kind(docs, "ScaledObject")
    assert set(scaled) == {"t-analystos-worker-analysis", "t-analystos-worker-compute"}
    trig = scaled["t-analystos-worker-compute"]["spec"]["triggers"][0]
    assert trig["type"] == "temporal" and trig["metadata"]["taskQueue"] == "analystos-compute"
    assert trig["metadata"]["endpoint"] == "temporal-frontend.temporal.svc:7233"
    hpas = _kind(docs, "HorizontalPodAutoscaler")
    assert "t-analystos-worker-compute" not in hpas and "t-analystos-worker-analysis" not in hpas
    assert "replicas" not in _kind(docs, "Deployment")["t-analystos-worker-compute"]["spec"]
    r = subprocess.run([HELM, "template", "t", str(CHART), "-f", str(CHART / "values-small.yaml"), "--set", "keda.enabled=true",
                        "--set", "workers.all.keda.enabled=true"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "KEDA needs named queues" in r.stderr


@needs_helm
def test_gvisor_openshift_and_the_migrator_login():
    docs = _render("values-ha.yaml", sets=S12_FULL)
    deps = _kind(docs, "Deployment")
    for pool in ("compute", "compute-py", "compute-ml"):
        assert deps[f"t-analystos-worker-{pool}"]["spec"]["template"]["spec"]["runtimeClassName"] == "gvisor"
    assert "runtimeClassName" not in deps["t-analystos-worker-analysis"]["spec"]["template"]["spec"]
    assert _kind(docs, "RuntimeClass")["gvisor"]["handler"] == "runsc"
    for name, dep in deps.items():
        psc = dep["spec"]["template"]["spec"]["securityContext"]
        assert psc["runAsNonRoot"] is True and not {"runAsUser", "runAsGroup", "fsGroup"} & set(psc), name
    job = next(iter(_kind(docs, "Job").values()))["spec"]["template"]["spec"]["containers"][0]
    assert job["env"] == [{"name": "ANALYSTOS_DATABASE_URL", "valueFrom": {"secretKeyRef": {
        "name": "analystos-secrets", "key": "ANALYSTOS_MIGRATOR_DATABASE_URL"}}}]
    default = _render()  # without openshift the fixed UID stays
    assert _kind(default, "Deployment")["t-analystos-api"]["spec"]["template"]["spec"]["securityContext"]["runAsUser"] == 10001



def _env(dep: dict) -> dict:
    return {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", [])}


@needs_helm
def test_isolated_gvisor_pools_require_the_runtime_they_are_given():
    """S07-T1: an isolated pool under the gVisor class refuses to start on any other kernel
    (ANALYSTOS_WORKER_REQUIRE_RUNTIME -> workers/isolation.py::check_runtime, exit 78)."""
    deps = _kind(_render("values-ha.yaml", sets=S12_FULL), "Deployment")
    for pool in ("compute-py", "compute-ml"):
        assert _env(deps[f"t-analystos-worker-{pool}"])["ANALYSTOS_WORKER_REQUIRE_RUNTIME"] == "gvisor"
    without = _kind(_render("values-ha.yaml"), "Deployment")  # gVisor off: no class, no check
    assert "ANALYSTOS_WORKER_REQUIRE_RUNTIME" not in _env(without["t-analystos-worker-compute-py"])
    custom = _kind(_render("values-ha.yaml", sets=(*S12_FULL, "workers.compute-ml.runtimeClassName=kata-qemu",
                                                   "workers.compute-ml.requireRuntime=kata",
                                                   "workers.compute-py.requireRuntime=")), "Deployment")
    ml = custom["t-analystos-worker-compute-ml"]
    assert ml["spec"]["template"]["spec"]["runtimeClassName"] == "kata-qemu"
    assert _env(ml)["ANALYSTOS_WORKER_REQUIRE_RUNTIME"] == "kata"
    assert "ANALYSTOS_WORKER_REQUIRE_RUNTIME" not in _env(custom["t-analystos-worker-compute-py"])  # "" turns it off



@needs_helm
def test_the_migration_hook_brings_its_own_service_account_and_config():
    """A first install runs the pre-install Job before any release object exists (found on kind, 2026-10-29): the Job
    may only reference hooks of the same events with a lower weight, never the release's ServiceAccount or ConfigMap."""
    for docs in (_render(), _render("values-ha.yaml", sets=(*S12_FULL, "models.customConfig=x: 1"))):
        hooks = {(d["kind"], d["metadata"]["name"]): d for d in docs
                 if "pre-install" in (d["metadata"].get("annotations") or {}).get("helm.sh/hook", "")}
        job = next(d for (kind, _), d in hooks.items() if kind == "Job")
        spec = job["spec"]["template"]["spec"]
        weight = int(job["metadata"]["annotations"]["helm.sh/hook-weight"])
        refs = [("ServiceAccount", spec["serviceAccountName"])]
        refs += [("ConfigMap", e["configMapRef"]["name"]) for e in spec["containers"][0]["envFrom"] if "configMapRef" in e]
        refs += [("ConfigMap", v["configMap"]["name"]) for v in spec["volumes"] if "configMap" in v]
        for ref in refs:
            assert ref in hooks, ref
            assert int(hooks[ref]["metadata"]["annotations"]["helm.sh/hook-weight"]) < weight
            assert "hook-succeeded" in hooks[ref]["metadata"]["annotations"]["helm.sh/hook-delete-policy"]
        config = _kind(docs, "ConfigMap")
        assert config["t-analystos-migrate"]["data"] == config["t-analystos-config"]["data"]

@needs_helm
def test_kind_values_render_the_gvisor_proof_install():
    """deploy/kind/proof.sh: the compute roles under the chart's own RuntimeClass, images never pulled."""
    r = subprocess.run([HELM, "lint", str(CHART), "-f", str(CHART / "values-kind.yaml")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    docs = _render("values-kind.yaml")
    assert _kind(docs, "RuntimeClass")["gvisor"]["handler"] == "runsc"
    deps = _kind(docs, "Deployment")
    for pool in ("compute", "compute-py"):
        spec = deps[f"t-analystos-worker-{pool}"]["spec"]["template"]["spec"]
        assert spec["runtimeClassName"] == "gvisor" and spec["containers"][0]["imagePullPolicy"] == "Never"
    assert _env(deps["t-analystos-worker-compute-py"])["ANALYSTOS_WORKER_REQUIRE_RUNTIME"] == "gvisor"
    assert "runtimeClassName" not in deps["t-analystos-worker-analysis"]["spec"]["template"]["spec"]
    assert _values("values-kind.yaml")["env"] == "dev"  # never a production overlay

@needs_helm
def test_external_secrets_sync_the_secret_before_the_migration():
    docs = _render(sets=S12_FULL)
    es = _kind(docs, "ExternalSecret")["analystos-secrets"]
    assert es["apiVersion"] == "external-secrets.io/v1" and es["spec"]["secretStoreRef"]["name"] == "vault"
    assert es["metadata"]["annotations"]["helm.sh/hook-weight"] == "-10" and es["spec"]["target"]["creationPolicy"] == "Orphan"
    assert not _kind(docs, "Secret")
    r = subprocess.run([HELM, "template", "t", str(CHART), "--set", "externalSecrets.enabled=true"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0 and "secretStoreRef.name is required" in r.stderr


@needs_helm
def test_per_role_network_policies_limit_each_role():
    docs = _render("values-ha.yaml", sets=S12_FULL)
    pol = _kind(docs, "NetworkPolicy")
    assert "t-analystos-egress" not in pol  # replaced by the per-role ones
    assert {"t-analystos-role-api", "t-analystos-role-web", "t-analystos-role-query", "t-analystos-role-compute",
            "t-analystos-role-publish", "t-analystos-role-scheduler", "t-analystos-role-ops"} <= set(pol)

    def cidrs(name):
        return {b["ipBlock"]["cidr"] for rule in pol[name]["spec"]["egress"] for b in rule.get("to", []) if "ipBlock" in b}

    assert cidrs("t-analystos-role-query") == {"10.1.0.0/16", "10.9.0.0/24", "10.2.0.0/16"}
    assert cidrs("t-analystos-role-compute") == {"10.1.0.0/16"}  # no model host, no source
    assert cidrs("t-analystos-role-web") == set()
    ingress = pol["t-analystos-role-compute"]["spec"]["ingress"]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 9464}]  # metrics only
    # every pod of the release (but the isolated pools) carries a role the policies select
    for name, dep in _kind(docs, "Deployment").items():
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert labels.get("analystos.io/role") or labels.get("analystos.io/isolated") or "pgbouncer" in name, name
    r = subprocess.run([HELM, "template", "t", str(CHART), "--set", "networkPolicy.enabled=true", "--set",
                        "networkPolicy.perRole=true"], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "needs networkPolicy.ingressFrom" in r.stderr


@needs_helm
def test_monitoring_objects_and_ops_cronjobs():
    docs = _render("values-ha.yaml", sets=S12_FULL)
    monitors = _kind(docs, "PodMonitor")
    assert monitors["t-analystos-api"]["spec"]["podMetricsEndpoints"][0]["port"] == "http"
    assert monitors["t-analystos-workers"]["spec"]["selector"]["matchLabels"]["analystos.io/metrics"] == "true"
    groups = next(iter(_kind(docs, "PrometheusRule").values()))["spec"]["groups"]
    alerts = {r["alert"]: r for g in groups for r in g["rules"] if "alert" in r}
    assert {"AnalystOSAskLatencyBudgetBurnFast", "AnalystOSRunSuccessBudgetBurnSlow", "AnalystOSAvailabilityBudgetBurnFast",
            "AnalystOSQueueBacklog", "AnalystOSModelProviderErrors", "AnalystOSSpendNearBudget",
            "AnalystOSAuditChainVerifyFailed", "AnalystOSRlsCoverageFailed"} <= set(alerts)
    assert alerts["AnalystOSAuditChainVerifyFailed"]["labels"]["severity"] == "page"
    assert alerts["AnalystOSRlsCoverageFailed"]["labels"]["severity"] == "page"
    assert alerts["AnalystOSQueueBacklog"]["for"] == "10m" and "> 100" in alerts["AnalystOSQueueBacklog"]["expr"]
    assert "> 0.05" in alerts["AnalystOSModelProviderErrors"]["expr"] and "> 0.8" in alerts["AnalystOSSpendNearBudget"]["expr"]
    rec = next(r["expr"] for g in groups for r in g["rules"] if r.get("record") == "analystos:ask_latency_bad:ratio_rate5m")
    assert 'le="3"' in rec
    dashboards = {n: c for n, c in _kind(docs, "ConfigMap").items() if "dashboard" in n}
    assert len(dashboards) == 3 and all(c["metadata"]["labels"]["grafana_dashboard"] == "1" for c in dashboards.values())
    config = _kind(docs, "ConfigMap")["t-analystos-config"]["data"]
    assert config["ANALYSTOS_OTEL_ENABLED"] == "true" and config["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel:4318"
    assert config["ANALYSTOS_METRICS_PORT"] == "9464"
    jobs = _kind(docs, "CronJob")
    cmds = {n: j["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1] for n, j in jobs.items()}
    assert cmds == {"t-analystos-audit-verify": "analystos audit verify", "t-analystos-rls-check": "analystos ops rls-check"}
    assert all(j["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0 for j in jobs.values())
    worker = _kind(docs, "Deployment")["t-analystos-worker-analysis"]["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "metrics", "containerPort": 9464} in worker["ports"]
