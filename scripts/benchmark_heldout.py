"""P4-08 held-out corpus: score AnalystOS end to end and write the dated evidence report.

  # deterministic component tier (no services; CI runs it as the non-blocking `heldout` gate)
  python scripts/benchmark_heldout.py --tier component
  # the platform tier (Postgres: upload -> staging -> gateway -> real run / recipe run / experiment)
  python scripts/benchmark_heldout.py --tier platform --report auto
  # both, with infrastructure priced at a CPU-hour rate (otherwise reported as unpriced)
  python scripts/benchmark_heldout.py --tier all --cpu-usd-per-hour 0.05 --report auto
  # re-freeze after a reviewed corpus change (bump `version` in corpus.yaml and add a changelog entry first)
  python scripts/benchmark_heldout.py --lock

`--report auto` writes docs/60-delivery/evidence/<date>-heldout-<tiers>-<models>.md (+ .json). Exit 1 when the
corpus no longer matches its lock, or with --check when a proposed threshold is missed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "src", ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY")


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def main(argv: list[str] | None = None) -> int:
    from evaluation.heldout import runner as R
    from evaluation.heldout.report import render

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=["component", "platform", "all"], default="component")
    ap.add_argument("--families", default=",".join(R.FAMILIES))
    ap.add_argument("--only", default=None, help="comma-separated task ids (smoke runs)")
    ap.add_argument("--models", choices=["off", "live"], default="off",
                    help="off removes provider keys (rule path); live uses the configured provider")
    ap.add_argument("--preset", default=None, help="settings preset for this process only (evaluation/live.py), e.g. max_quality")
    ap.add_argument("--enable-models", default="", help="comma-separated model ids to re-enable for this process only when "
                    "the stored settings disabled them (evaluation/live.py)")
    ap.add_argument("--cpu-usd-per-hour", type=float, default=None)
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    ap.add_argument("--check", action="store_true", help="exit 1 when a proposed threshold is missed")
    ap.add_argument("--lock", action="store_true", help="write corpus.lock.json from the current corpus and exit")
    args = ap.parse_args(argv)

    corpus = R.load_corpus()
    if args.lock:
        problems = R.corpus_problems(corpus)
        if problems:
            print("corpus problems: " + "; ".join(problems), file=sys.stderr)
            return 1
        R.LOCK.write_text(json.dumps(R.lock_entries(corpus), indent=1, sort_keys=True) + "\n")
        print(f"locked corpus version {corpus.version}: {len(corpus.tasks)} tasks -> {R.LOCK}")
        return 0
    if args.models == "off":
        for key in KEYS:
            os.environ.pop(key, None)
    os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")

    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    only = {t.strip() for t in args.only.split(",")} if args.only else None
    tiers = ["component", "platform"] if args.tier == "all" else [args.tier]
    from evaluation.live import fallbacks, process_preset

    with process_preset(args.preset, [m for m in args.enable_models.split(",") if m]):
        runs = {t: R.run(t, families=families, only=only, cpu_usd_per_hour=args.cpu_usd_per_hour, corpus=corpus) for t in tiers}
    found = {t: fallbacks(run_ids=[x.run_ref for x in r.results if x.run_ref]) for t, r in runs.items() if t == "platform"}
    problems = []
    for tier, r in runs.items():
        o = r.summary["overall"]
        print(f"[{tier}] tasks={o['tasks']} accepted_output_rate={_fmt(o['accepted_output_rate'])} ({o['accepted']}/{o['deliver_tasks']}) "
              f"confident_wrong={o['confident_wrong']} abstention={_fmt(o['abstention_recall'])} errors={o['errors']} "
              f"p50={_fmt(o['latency_seconds']['p50'])}s p95={_fmt(o['latency_seconds']['p95'])}s lock={'ok' if r.lock_ok else 'MISMATCH'}")
        for x in r.results:
            if x.status not in ("accepted", "correct_abstention"):
                print(f"  {x.id}: {x.status} — {x.reason[:200]}")
        for f in found.get(tier) or []:
            print(f"[{tier}] FELL BACK to {f['rung']}: {f['purpose']} ({f['cause']}) x{f['count']}")
        if not r.lock_ok:
            problems.append(f"{tier}: corpus does not match its lock: {', '.join(r.lock_mismatches)}")
        m = R.gate_metrics(r)
        for name, bound in R.PROPOSED_THRESHOLDS.items():
            v = m.get(name)
            if v is None or ("min" in bound and v < bound["min"]) or ("max" in bound and v > bound["max"]):
                problems.append(f"{tier}: {name} {v} misses the proposed {bound}")
    if args.report:
        now = datetime.now(UTC)
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{now:%Y-%m-%d}-heldout-{'-'.join(runs)}-{args.models}.md"
                if args.report == "auto" else Path(args.report))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(runs, corpus, preset=args.preset, enabled_models=args.enable_models, fallbacks=found), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps({t: R.as_dict(r) for t, r in runs.items()}, indent=1, default=str) + "\n")
        print(f"report: {path}")
    for p in problems:
        print(f"HELD-OUT: {p}", file=sys.stderr)
    lock_broken = any(not r.lock_ok for r in runs.values())
    return 1 if lock_broken or (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
