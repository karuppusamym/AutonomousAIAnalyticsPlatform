"""Evaluation release gates (P7-07, spec v4 §13): owner thresholds in `config/eval_gates.yaml`, applied
to the metrics each suite computes. A metric past its threshold fails the gate; a live tier without a
provider key is reported `skipped` with the reason, never as passed.

`scripts/eval_gates.py` runs tiers and writes the gate result (config version and hash, metrics,
failures) that CI attaches to every build; `tests/integration/test_ask_benchmark.py` applies the Ask
tiers' gates to the runs it already makes.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
GATES_FILE = ROOT / "config" / "eval_gates.yaml"


class Bound(BaseModel):
    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _one(self) -> Bound:
        if self.min is None and self.max is None:
            raise ValueError("a threshold needs min or max")
        return self


class Tier(BaseModel):
    kind: Literal["deterministic", "live"]
    runs: str
    suite: str
    metrics: dict[str, Bound]
    blocking: bool = True  # false: a report; its failures are listed but never fail the gate result
    needs_database: bool = False  # runs in CI after `analystos migrate` (the integration step), not before


class Gates(BaseModel):
    version: int
    owner: str
    changelog: list[dict[str, Any]] = Field(default_factory=list)
    tiers: dict[str, Tier]
    digest: str = ""

    @model_validator(mode="after")
    def _versioned(self) -> Gates:
        if not any(int(c.get("version", -1)) == self.version for c in self.changelog):
            raise ValueError(f"eval gates version {self.version} has no changelog entry (thresholds are versioned)")
        return self


def load(path: Path = GATES_FILE) -> Gates:
    raw = path.read_bytes()
    gates = Gates.model_validate(yaml.safe_load(raw))
    gates.digest = hashlib.sha256(raw).hexdigest()
    return gates


def check(tier: str, metrics: dict[str, Any], gates: Gates | None = None) -> list[str]:
    """Threshold violations of one tier's metrics (empty = pass). A gated metric that is missing fails."""
    gates = gates or load()
    out = []
    for name, bound in gates.tiers[tier].metrics.items():
        value = metrics.get(name)
        if not isinstance(value, int | float) or isinstance(value, bool):
            out.append(f"{tier}: {name} missing from the suite's metrics")
            continue
        if bound.min is not None and value < bound.min - 1e-12:
            out.append(f"{tier}: {name} {value:g} < {bound.min:g}")
        if bound.max is not None and value > bound.max + 1e-12:
            out.append(f"{tier}: {name} {value:g} > {bound.max:g}")
    return out


# ------------------------------------------------------------------------------------ tier runners
def grounding_metrics(**kw: Any) -> dict[str, Any]:
    from evaluation.grounding import run

    return run(**kw).metrics()


def analytical_metrics(summary: Any) -> dict[str, Any]:
    per_method = [row["null_fdr"] for row in (summary.by_method or {}).values() if row.get("null_fdr") is not None]
    return {"precision": summary.precision, "recall": summary.recall, "fdr": summary.fdr, "null_fdr": summary.null_fdr,
            "null_fdr_per_method_max": max(per_method) if per_method else 0.0, "incomplete": len(summary.incomplete),
            "replicates": summary.replicates, "verified": summary.verified}


def analytical_component_metrics(seeds: range = range(1, 11), null_seeds: range = range(101, 106)) -> dict[str, Any]:
    from evaluation.analytical import run_component_suite

    summary, _ = run_component_suite(list(seeds), null_seeds=list(null_seeds))
    return analytical_metrics(summary)


def _cost_gate_module() -> Any:
    spec = importlib.util.spec_from_file_location("cost_gate", ROOT / "scripts" / "cost_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cost_metrics(fixtures: Path | None = None, baseline: dict[str, Any] | None = None) -> dict[str, Any]:
    import json

    cg = _cost_gate_module()
    fixtures = fixtures or cg.FIXTURES
    measured = cg.measure(cg.load_runs(fixtures))
    baseline = baseline if baseline is not None else json.loads((fixtures / cg.BASELINE.name).read_text())
    rises, missing = [], 0
    for name, got in measured.items():
        base = baseline.get("runs", {}).get(name)
        if base is None:
            missing += 1
            continue
        rises += [got[m] / base[m] - 1 for m in ("calls", "tokens", "usd") if base.get(m) and m in got]
        rises += [float(got[m] > 0) for m in ("calls", "tokens", "usd") if base.get(m) == 0 and m in got]
    return {"max_rise": round(max(rises), 6) if rises else 0.0, "missing_baseline": missing, "runs": len(measured),
            "calls": sum(g["calls"] for g in measured.values()), "tokens": sum(g["tokens"] for g in measured.values())}


def ask_metrics(run: Any) -> dict[str, Any]:
    """Flat gate metrics of one `evaluation.ask.run` result."""
    o = run.summary["overall"]
    governed = [r["governed_recall"] for r in o["decline_reasons"].values() if r["governed_recall"] is not None]
    return {"execution_accuracy": o["execution_accuracy"], "outcome_accuracy": o["outcome_accuracy"],
            "answered_precision": o["answered_precision"], "confident_wrong": o["confident_wrong"],
            "confident_wrong_share": o["confident_wrong_share"], "restricted_leaks": o["restricted_leaks"],
            "errors": o["errors"], "problems": len(run.problems), "model_calls": o["model"]["calls"],
            "governed_decline_recall": min(governed) if governed else 0.0,
            "decline_recall": o["refusal"]["decline"]["recall"], "needs_input_recall": o["refusal"]["needs_input"]["recall"],
            "execution_accuracy_per_domain_min": min((d["execution_accuracy"] or 0.0) for d in run.summary["by_domain"].values()),
            "questions": o["questions"]}


def ml_metrics() -> dict[str, Any]:
    from evaluation.ml import run

    return run()


def heldout_metrics() -> dict[str, Any]:
    from evaluation.heldout.runner import gate_metrics, run

    return gate_metrics(run("component"))


def _ask(tier: str) -> Callable[[], dict[str, Any]]:
    def go() -> dict[str, Any]:
        from evaluation.ask import run

        return ask_metrics(run(tier))
    return go


def release_safety_metrics() -> dict[str, Any]:
    from evaluation.release_safety import run

    return run()


def _matrix(mode: str) -> Callable[[], dict[str, Any]]:
    def go() -> dict[str, Any]:
        from evaluation.model_matrix import run

        result = run(mode)  # rows travel in the gate result; `analystos ops model-matrix-import` stores them
        return {**result["metrics"], "mode": mode, "rows": result["rows"], "excluded": result["excluded"]}
    return go


RUNNERS: dict[str, Callable[[], dict[str, Any]]] = {
    "grounding": grounding_metrics,
    "analytical_component": analytical_component_metrics,
    "cost": cost_metrics,
    "ml": ml_metrics,
    "heldout": heldout_metrics,
    "ask_fake": _ask("fake"),
    "ask_off": _ask("off"),
    "ask_live": _ask("live"),
    "release_safety": release_safety_metrics,
    "model_matrix_fake": _matrix("fake"),
    "model_matrix": _matrix("live"),
}
LIVE_KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY")


def run_gates(tiers: list[str], gates: Gates | None = None,
              runners: dict[str, Callable[[], dict[str, Any]]] | None = None, *, require_live: bool = False) -> dict[str, Any]:
    """Run the named tiers and gate them. Result: {config: {version, sha256}, tiers: {name: {status,
    metrics, failures, seconds, blocking}}, skipped, reports, passed}. A deterministic tier can never pass by
    being skipped; a live tier with no provider key is skipped, and with `require_live` (a release) that fails
    too. A non-blocking tier (`blocking: false`) is a report: it runs and lists its failures, but never
    decides `passed`."""
    gates = gates or load()
    runners = RUNNERS if runners is None else runners
    out: dict[str, Any] = {"config": {"version": gates.version, "sha256": gates.digest, "owner": gates.owner}, "tiers": {}}
    for name in tiers:
        tier = gates.tiers[name]
        if tier.kind == "live" and not any(os.getenv(k) for k in LIVE_KEYS):
            out["tiers"][name] = {"status": "skipped", "reason": "live tier: no model provider key in this environment"}
            continue
        if name not in runners:
            out["tiers"][name] = {"status": "skipped", "reason": f"no runner for {name} yet ({tier.suite})"}
            continue
        started = time.perf_counter()
        try:
            metrics = runners[name]()
            failures = check(name, metrics, gates)
        except Exception as exc:  # a suite that cannot run fails its gate
            metrics, failures = {}, [f"{name}: suite failed: {type(exc).__name__}: {exc}"]
        out["tiers"][name] = {"status": "failed" if failures else "passed", "metrics": metrics, "failures": failures,
                              "seconds": round(time.perf_counter() - started, 1), "blocking": tier.blocking}
    failed = [n for n, t in out["tiers"].items() if t["status"] == "failed" and gates.tiers[n].blocking]
    out["skipped"] = [n for n, t in out["tiers"].items() if t["status"] == "skipped"]
    out["reports"] = [n for n in out["tiers"] if not gates.tiers[n].blocking]
    blocking = [n for n in out["skipped"] if gates.tiers[n].blocking and (require_live or gates.tiers[n].kind == "deterministic")]
    out["passed"] = not failed and not blocking
    return out


def deterministic_ci_tiers(gates: Gates | None = None, *, database: bool = False) -> list[str]:
    """The tiers CI gates on every change; the Ask tiers and `needs_database` tiers need Postgres (the
    integration step; `database=True` adds them)."""
    gates = gates or load()
    needs = {n for n, t in gates.tiers.items() if t.needs_database or n.startswith("ask_")}
    return [n for n, t in gates.tiers.items() if t.kind == "deterministic" and (database or n not in needs)]
