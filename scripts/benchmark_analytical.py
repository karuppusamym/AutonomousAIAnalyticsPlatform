"""P4-V01 analytical benchmark: precision / recall of verified findings and FDR vs the nominal α on
seeded ITSM, sales and finance datasets with planted effects and null controls.

  # deterministic component tier (no services; what CI runs, with --check)
  python scripts/benchmark_analytical.py --tier component --check
  # the platform tier against the configured stack (staging + gateway + a real run), rule path only
  python scripts/benchmark_analytical.py --tier platform --models off --report auto
  # the same with the configured model provider (OpenRouter key, or an air-gapped local model via
  # ANALYSTOS_AIR_GAPPED=true ANALYSTOS_MODELS_CONFIG=config/models.airgapped.yaml)
  python scripts/benchmark_analytical.py --tier platform --models live --report auto
  # ... with a settings preset applied to this process only (max_quality = model first for every purpose)
  python scripts/benchmark_analytical.py --tier platform --models live --preset max_quality --report auto

`--report auto` writes docs/60-delivery/evidence/<date>-analytical-benchmark-<tier>-<models>.md (+ .json).
Exit code 1 with --check when a threshold (evaluation.analytical.THRESHOLDS) is missed.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))  # the evaluation harness lives outside the product package


LIMITS = """## Scope and limits

* Planted effects are sized clearly above the verdict layer's materiality thresholds (skills/stats.py), so recall
  here is recall for material effects; power close to the thresholds, per-method null behaviour and selection effects
  are measured by scripts/benchmark_methods.py (evaluation/method_checks.py, P4-03), not by this report.
* The component tier reads raw p-values, so it also reports test calibration (share of null hypotheses with raw p < α,
  expected ≈ α). The platform tier scores what a real run stored (verified insights and tested hypotheses).
* A run with a live or local model (`--models live`) is a separate dated report; a model changes which hypotheses are
  proposed and how findings are worded, never the statistics, BH or the second-method gate.
"""


def _ints(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        if not part.strip():
            continue
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def render(results: dict, args: argparse.Namespace, problems: list[str]) -> str:
    from evaluation.analytical import ALPHA, THRESHOLDS
    from evaluation.live import fallback_label, fallback_lines

    now = datetime.now(UTC)
    rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    lines = [f"# Analytical benchmark (P4-V01) — {now:%Y-%m-%d}", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC by `scripts/benchmark_analytical.py` at `{rev or 'unknown'}` "
             f"(Python {platform.python_version()}). Models: **{args.models}**"
             + (f" ({os.environ.get('ANALYSTOS_MODELS_CONFIG', 'config/models.yaml')}"
                f"{', air-gapped' if os.environ.get('ANALYSTOS_AIR_GAPPED', '').lower() == 'true' else ''})" if args.models == "live" else
                " (no provider key: every model purpose takes its rule path)") + "."
             + (f" Settings preset **{args.preset}** applied to the benchmark process only (evaluation/live.py)."
                if args.preset else "")
             + (f" Models re-enabled for this process only (the stored settings had them disabled): {args.enable_models}."
                if args.enable_models else "") + "", "",
             "Datasets: `evaluation.datasets` — one table per domain generated from an explicit causal graph "
             "(ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns "
             "each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted "
             "effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; "
             f"FDR is the mean false-discovery proportion per replicate, compared with the nominal α = {ALPHA}.", "",
             "Thresholds: " + ", ".join(f"{k} {v}" for k, v in THRESHOLDS.items()) + ".", ""]
    for tier, (summary, _scores) in results.items():
        s = summary
        found = (getattr(args, "usage", {}).get(tier) or {}).get("fallbacks")
        lines += [f"## Tier: {tier}{fallback_label(found)}", "",
                  "| Measure | Value |", "|---|---|",
                  f"| replicates (with effects + global null) | {s.replicates} |",
                  f"| hypotheses tested | {s.tested} |",
                  f"| verified findings | {s.verified} (true {s.true_positive}, false {s.false_positive}) |",
                  f"| **precision** of verified findings | **{_fmt(s.precision)}** |",
                  f"| **recall** of planted effects | **{_fmt(s.recall)}** ({s.planted_found}/{s.planted_total}) |",
                  f"| **FDR** (replicates with effects) vs α {ALPHA} | **{_fmt(s.fdr)}** |",
                  f"| **FDR under the global null** (= FWER there) | **{_fmt(s.null_fdr)}** |",
                  f"| null hypotheses tested | {s.null_tests} |",
                  f"| null hypotheses with raw p < α (before effect-size gates, BH, second method) | {_fmt(s.null_raw_rate)} |",
                  "", "| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for d, row in s.by_domain.items():
            lines.append(f"| {d} | {row['replicates']} | {row['verified']} | {_fmt(row['precision'])} | {_fmt(row['recall'])} "
                         f"({row['planted_found']}/{row['planted_total']}) | {_fmt(row['fdr'])} | {_fmt(row['null_fdr'])} | "
                         f"{row['null_tests']} | {_fmt(row['null_raw_rate'])} | {row['seconds']} |")
        if s.by_method:
            lines += ["", "| Method (P4-03) | tested | verified | precision | null tests | raw p<α on nulls | null FDR |",
                      "|---|---|---|---|---|---|---|"]
            lines += [f"| {m} | {r['tested']} | {r['verified']} | {_fmt(r['precision'])} | {r['null_tests']} | "
                      f"{_fmt(r['null_raw_rate'])} | {_fmt(r['null_fdr'])} |" for m, r in s.by_method.items()]
        lines += ["", "Missed planted effects: " + ("; ".join(s.missed) if s.missed else "none") + ".",
                  "", "False verified findings: " + ("; ".join(s.false_findings) if s.false_findings else "none") + "."]
        if s.incomplete:
            lines += ["", "Runs that did not complete: " + "; ".join(s.incomplete) + "."]
        usage = getattr(args, "usage", {}).get(tier)
        if usage is not None:
            lines += ["", "### Model use (model_call rows of these runs)", "",
                      f"Provider calls {usage['calls']} (errors {usage['errors']}), tokens in/out {usage['input_tokens']}/"
                      f"{usage['output_tokens']}, recorded cost ${usage['cost_usd']:.4f}; purposes answered by rules or "
                      f"deterministic rungs {usage['skipped']}, cache hits {usage['cache_hits']}, calls stopped for a missing "
                      f"price / approval {usage['approval_required']}.", ""]
            if usage["by_purpose"]:
                lines += ["| Purpose / model | calls | errors | tokens in | tokens out | USD |", "|---|---|---|---|---|---|"]
                lines += [f"| {k} | {v['calls']} | {v['errors']} | {v['input_tokens']} | {v['output_tokens']} | {v['cost_usd']:.4f} |"
                          for k, v in sorted(usage["by_purpose"].items())]
            lines += [""] + fallback_lines(found)
        lines.append("")
    lines += LIMITS.splitlines() + ["", "## Verdict", "",
                                     "PASS — every threshold met." if not problems else "FAIL — " + "; ".join(problems), ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=["component", "platform", "all"], default="component")
    ap.add_argument("--seeds", default=None, help="seeds with planted effects (component default 1-10, platform 1)")
    ap.add_argument("--null-seeds", default=None, help="global-null seeds (component default 101-105, platform 101)")
    ap.add_argument("--domains", default="itsm,sales,finance")
    ap.add_argument("--models", choices=["off", "live"], default="off",
                    help="platform tier: off removes provider keys (rule path); live uses the configured provider")
    ap.add_argument("--preset", default=None, help="settings preset for this process only (evaluation/live.py), e.g. max_quality")
    ap.add_argument("--enable-models", default="", help="comma-separated model ids to re-enable for this process only when "
                    "the stored settings disabled them (evaluation/live.py)")
    ap.add_argument("--check", action="store_true", help="exit 1 when a threshold is missed")
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    args = ap.parse_args()

    if args.models == "off":
        for key in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"):
            os.environ.pop(key, None)
    os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")

    from evaluation.analytical import as_dict, check, run_component_suite, run_platform_suite
    from evaluation.live import model_usage, process_preset

    domains = tuple(d.strip() for d in args.domains.split(",") if d.strip())
    results = {}
    args.usage = {}
    if args.tier in ("component", "all"):
        results["component"] = run_component_suite(_ints(args.seeds or "1-10"), null_seeds=_ints(args.null_seeds or "101-105"),
                                                   domains=domains)
    if args.tier in ("platform", "all"):
        with process_preset(args.preset, [m for m in args.enable_models.split(",") if m]):
            results["platform"] = run_platform_suite(_ints(args.seeds or "1"), null_seeds=_ints(args.null_seeds or "101"),
                                                     domains=domains)
        args.usage["platform"] = model_usage(run_ids=[sc.run_id for sc in results["platform"][1] if sc.run_id])
    problems = [p for summary, _ in results.values() for p in check(summary)]
    for tier, (s, _) in results.items():
        print(f"[{tier}] replicates={s.replicates} verified={s.verified} precision={_fmt(s.precision)} "
              f"recall={_fmt(s.recall)} ({s.planted_found}/{s.planted_total}) FDR={_fmt(s.fdr)} null_FDR={_fmt(s.null_fdr)} "
              f"null_raw_p<α={_fmt(s.null_raw_rate)}")
        if tier in args.usage:
            u = args.usage[tier]
            print(f"[{tier}] model calls={u['calls']} errors={u['errors']} cost=${u['cost_usd']:.4f} skipped={u['skipped']} "
                  f"approval_required={u['approval_required']}")
            for f in u.get("fallbacks") or []:
                print(f"[{tier}] FELL BACK to {f['rung']}: {f['purpose']} ({f['cause']}) x{f['count']}")
    if args.report:
        tiers = "-".join(results)
        path = (ROOT / "docs" / "60-delivery" / "evidence" /
                f"{datetime.now(UTC):%Y-%m-%d}-analytical-benchmark-{tiers}-{args.models}{'-' + args.preset if args.preset else ''}.md") if args.report == "auto" else Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(results, args, problems), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps({t: {**as_dict(s, sc), "model_usage": args.usage.get(t)}
                                                         for t, (s, sc) in results.items()}, indent=1, default=str))
        print(f"report: {path}")
    for p in problems:
        print(f"THRESHOLD MISSED: {p}", file=sys.stderr)
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
