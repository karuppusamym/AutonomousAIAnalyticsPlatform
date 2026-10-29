"""The dated P4-08 evidence report (markdown) for one or more tiers of a held-out run. Shared by
scripts/benchmark_heldout.py and tests/integration/test_heldout_platform.py."""
from __future__ import annotations

import os
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evaluation.heldout.runner import PROPOSED_THRESHOLDS, STATUSES

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY")


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def _metric_rows(m: dict) -> list[str]:
    cost = m["cost_per_accepted"]
    return [
        f"| tasks (deliver / abstain) | {m['tasks']} ({m['deliver_tasks']} / {m['abstain_tasks']}) |",
        f"| **accepted-output rate** (accepted / deliver tasks) | **{_fmt(m['accepted_output_rate'])}** ({m['accepted']}/{m['deliver_tasks']}) |",
        f"| **confident-wrong** | **{m['confident_wrong']}** |",
        f"| **abstention correctness** (abstain tasks ended as the rubric requires) | **{_fmt(m['abstention_recall'])}** |",
        f"| abstention precision (abstentions that were on abstain tasks) | {_fmt(m['abstention_precision'])} |",
        f"| unnecessary abstentions | {m['unnecessary_abstentions']} |",
        f"| task success (accepted + correct abstentions) / tasks | {_fmt(m['task_success_rate'])} |",
        f"| errors | {m['errors']} |",
        f"| latency p50 / p95 / max (s, per task) | {_fmt(m['latency_seconds']['p50'])} / {_fmt(m['latency_seconds']['p95'])} / "
        f"{_fmt(m['latency_seconds']['max'])} |",
        f"| model calls / tokens / USD | {m['model']['calls']} / {m['model']['tokens']} / {m['model']['usd']} |",
        f"| cost per accepted output: model USD / CPU s / wall s / infrastructure | {_fmt(cost['model_usd'])} / "
        f"{_fmt(cost['cpu_seconds'])} / {_fmt(cost['wall_seconds'])} / {cost['infrastructure_usd']} |",
    ]


def coverage(corpus: Any) -> str:
    """The corpus's size per family and its analysis domains, against the evaluation plan's first-release target."""
    fam = {f: sum(t.family == f for t in corpus.tasks) for f in dict.fromkeys(t.family for t in corpus.tasks)}
    abstain = sum(t.expect == "abstain" for t in corpus.tasks)
    domains = sorted({t.domain for t in corpus.tasks if t.family == "analysis"})
    return (f"* Coverage: {len(corpus.tasks)} tasks (" + ", ".join(f"{n} {f}" for f, n in fam.items()) + f"; {abstain} expect an "
            "abstention) against the evaluation plan's first-release target of at least 60 (20 analyst, 15 engineering, 15 ML, "
            f"10 unsupported). Analysis domains: {', '.join(domains)}. The plan's 10 tasks per domain are not reached for any "
            "single domain, so per-domain rates are small-sample; the suite cannot claim capability beyond these tasks.")


def render(runs: dict, corpus: Any, *, preset: str | None = None, enabled_models: str = "",
           fallbacks: dict[str, list[dict]] | None = None) -> str:
    """`fallbacks` = per tier, the purposes its runs answered by rules for want of a model route (evaluation.live)."""
    from evaluation.live import fallback_label, fallback_lines

    now = datetime.now(UTC)
    rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    live = any(os.environ.get(k) for k in KEYS)
    lines = [f"# Held-out evaluation (P4-08) — {now:%Y-%m-%d}", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC by the held-out runner (`evaluation/heldout`) at `{rev or 'unknown'}` "
             f"(Python {platform.python_version()}). Corpus `evaluation/heldout/corpus.yaml` version {corpus.version} "
             f"(frozen {corpus.frozen}, sha256 `{corpus.digest[:12]}`). Models: "
             + ("**live** (a provider key is set)." if live else "**off** (no provider key: every model purpose takes its rule path).")
             + (f" Settings preset **{preset}** applied to the benchmark process only (evaluation/live.py)." if preset else "")
             + (f" Models re-enabled for this process only (the stored settings had them disabled): {enabled_models}." if enabled_models else ""),
             "", "Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, "
             "incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; "
             "the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.", "",
             "Proposed thresholds (non-blocking `heldout` gate, owner to confirm): "
             + ", ".join(f"{k} {'≥' if 'min' in v else '≤'} {v.get('min', v.get('max'))}" for k, v in PROPOSED_THRESHOLDS.items()) + ".", ""]
    for tier, r in runs.items():
        found = (fallbacks or {}).get(tier)
        lines += [f"## Tier: {tier}{fallback_label(found)}", "", *fallback_lines(found), f"Corpus lock: {'matches' if r.lock_ok else 'MISMATCH ' + ', '.join(r.lock_mismatches)}. "
                  f"Wall time {r.seconds} s.", "", "| Measure | Value |", "|---|---|", *_metric_rows(r.summary["overall"]), "",
                  "| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for d, m in r.summary["by_domain"].items():
            lines.append(f"| {d} | {m['tasks']} | {m['accepted']} | {_fmt(m['accepted_output_rate'])} | {m['confident_wrong']} | "
                         f"{_fmt(m['abstention_recall'])} | {m['unnecessary_abstentions']} | {m['errors']} | "
                         f"{_fmt(m['latency_seconds']['p50'])} | {_fmt(m['latency_seconds']['p95'])} |")
        lines += ["", "### Every task", "", "| Task | family | expect | status | abstained as | seconds | why |", "|---|---|---|---|---|---|---|"]
        for x in r.results:
            expect = x.expect + (f" ({x.abstain_kind})" if x.abstain_kind else "")
            lines.append(f"| {x.id} | {x.family} | {expect} | {x.status} | {x.abstained_as or '—'} | {x.seconds} | "
                         f"{x.reason.replace('|', '/')[:220]} |")
        fails = [x for x in r.results if x.status not in ("accepted", "correct_abstention")]
        lines += ["", f"Failures and non-accepted outcomes ({len(fails)}): "
                  + ("; ".join(f"{x.id} {x.status}" for x in fails) if fails else "none") + ".",
                  "Abstentions: " + ("; ".join(f"{x.id} {x.abstained_as} ({x.status})" for x in r.results if x.produced == "abstained")
                                     or "none") + ".", ""]
        counts = r.summary["overall"]["statuses"]
        lines += ["Status counts: " + ", ".join(f"{s} {counts[s]}" for s in STATUSES) + ".", ""]
    lines += ["## Scope and limits", "", coverage(corpus),
              "* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects "
              "are sized above the materiality thresholds.",
              "* The component tier has no model in its path; the platform tier ran with "
              + ("a live provider." if live else "no provider key, so it measures the rule path (`off`). A live-model run is a separate dated report."),
              "* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.",
              "* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it "
              "needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.",
              ""]
    return "\n".join(lines)
