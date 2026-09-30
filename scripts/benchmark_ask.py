"""P4-V02 Ask accuracy benchmark: execution-match accuracy and refusal correctness of Ask per domain
(ITSM, sales, finance), on the V01 seeded datasets with the labelled question sets in
evaluation/ask_questions/. Every statement, gold and answer, goes through QueryGateway.execute.

  # the no-model floor: registry + rules only (needs the configured control plane + analytics plane)
  python scripts/benchmark_ask.py --models off --report auto
  # CI smoke: a fake transport writes the gold SQL (tests the harness, gateway refusals, scoring)
  python scripts/benchmark_ask.py --models fake --check
  # the measurement: the configured provider (OPENROUTER_API_KEY in the environment, never in a file)
  python scripts/benchmark_ask.py --models live --report auto

`--report auto` writes docs/60-delivery/evidence/<date>-ask-benchmark-<models>.md (+ .json). With
--check, exit 1 when a tier invariant fails (evaluation.ask.check); the pilot threshold itself is
proposed in the report and set by the owner before the pilot. The live tier asks one model-only
question first and stops without retrying when the provider does not answer (exit 3).
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

TIER_TEXT = {
    "off": "**off** — no provider key: the verified-query registry and the Ask rules (pack distributions and the "
           "catalog-built simple shapes) answer; any question they do not cover is refused (`no_api_key`, before "
           "2026-09-26 `no_model`). This is the no-model floor, not the model's accuracy.",
    "fake": "**fake** — a fake transport answers with the gold SQL (and the careless `probe_sql` for decline items). "
            "It checks the harness, the gateway refusals and the scoring; its accuracy is not a measurement.",
    "live": "**live** — the configured provider ({models}); decisions use their configured backends.",
}

SCOPE = """## How to read this

* **Execution accuracy** = answer items whose answer matched the gold result / answer items. Match: same row count,
  each gold column matched by a distinct answer column with equal values, rows equal as a multiset (order-insensitive;
  numbers within rel 1e-4 / abs 1e-6; timestamps as naive ISO; extra answer columns allowed). A scale difference
  (percent for a fraction) is a mismatch.
* **Confident wrong** = answered with numbers that are not the right answer: an answer item with a mismatching result,
  or a needs_input / clarify / decline item that was answered. This is the costly failure; refusing is not.
* **Refusal correctness**: precision and recall of each refusal class (needs_input, clarify, decline) against the labels.
  For decline items the report also counts a *governed* decline — refused by the gateway or policy
  (`sql_rejected`, `policy_denied`, `no_scope`) — since a no-model refusal (`no_api_key`, `mode_off`, ...) declines
  for the wrong reason.
* The question set, the registry and the gold SQL were frozen before any tier ran (evaluation/ask_questions/README.md).
  The registry stands for what analysts had promoted; `near_miss` items are worded close to a registry entry but
  need different SQL, which is where a token matcher can answer confidently and wrongly.
"""


def _fmt(x) -> str:
    return "n/a" if x is None else (f"{x:.3f}" if isinstance(x, float) else str(x))


def _pr(m: dict) -> str:
    return f"{_fmt(m['precision'])} / {_fmt(m['recall'])} ({m['correct']}/{m['expected']}, predicted {m['predicted']})"


def render(r, args: argparse.Namespace) -> str:
    from evaluation.ask import PROPOSED_THRESHOLDS

    now = datetime.now(UTC)
    rev = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    models = os.environ.get("ANALYSTOS_MODELS_CONFIG", "config/models.yaml")
    lines = [f"# Ask accuracy benchmark (P4-V02) — tier `{r.tier}` — {now:%Y-%m-%d}", "",
             f"Generated {now:%Y-%m-%d %H:%M} UTC by `scripts/benchmark_ask.py --models {r.tier}` at `{rev or 'unknown'}` "
             f"(Python {platform.python_version()}), seed {r.seed}, {r.seconds} s.", "",
             "Tier: " + TIER_TEXT[r.tier].format(models=models), ""]
    if getattr(args, "run_note", None):
        lines += [args.run_note, ""]
    if r.probe is not None:
        lines += [f"Live probe (one model-only question before the set): `{json.dumps(r.probe)}`.", ""]
    if not r.outcomes:
        lines += ["## Not run", "", "; ".join(r.problems) or "no questions selected", ""]
        return "\n".join(lines)
    lines += ["Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain "
              "(the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget "
              "of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.", "",
              "| Domain | table | rows | verified queries | setup s |", "|---|---|---|---|---|"]
    for d, s in r.setup.items():
        lines.append(f"| {d} | `{s['table']}` | {s['rows']} | {s['registry']} | {s['setup_seconds']} |")
    o = r.summary["overall"]
    lines += ["", "## Results", "",
              "| Measure | Overall | " + " | ".join(r.summary["by_domain"]) + " |",
              "|---|---|" + "---|" * len(r.summary["by_domain"])]

    def row(label, fn):
        lines.append(f"| {label} | {fn(o)} | " + " | ".join(fn(m) for m in r.summary["by_domain"].values()) + " |")

    row("questions (answer / needs_input / clarify / decline)",
        lambda m: " / ".join(str(m["by_expect"].get(k, 0)) for k in ("answer", "needs_input", "clarify", "decline")))
    row("**execution accuracy**", lambda m: f"**{_fmt(m['execution_accuracy'])}** ({m['answer_items_matched']}/{m['answer_items']})")
    row("answer items answered (coverage)", lambda m: f"{m['answer_items_answered']}/{m['answer_items']}")
    row("precision of everything answered", lambda m: _fmt(m["answered_precision"]))
    row("**confident wrong** (answered, not right)", lambda m: f"**{m['confident_wrong']}** ({_fmt(m['confident_wrong_share'])})")
    row("outcome accuracy (all items)", lambda m: _fmt(m["outcome_accuracy"]))
    for kind in ("needs_input", "clarify", "decline"):
        row(f"{kind}: precision / recall", lambda m, k=kind: _pr(m["refusal"][k]))
    for reason in ("out_of_scope", "write", "restricted"):
        row(f"decline {reason}: declined / governed of n",
            lambda m, x=reason: f"{m['decline_reasons'][x]['declined']} / {m['decline_reasons'][x]['governed']} of {m['decline_reasons'][x]['n']}")
    row("restricted answers (leaks)", lambda m: str(m["restricted_leaks"]))
    row("latency p50 / p95 ms", lambda m: f"{_fmt(m['latency_ms']['p50'])} / {_fmt(m['latency_ms']['p95'])}")
    row("provider calls (per question)", lambda m: f"{m['model']['calls']} ({_fmt(m['model']['calls_per_question'])})")
    row("tokens in / out (per question)",
        lambda m: f"{m['model']['input_tokens']} / {m['model']['output_tokens']} ({_fmt(m['model']['tokens_per_question'])})")
    row("model cost USD", lambda m: f"{m['model']['cost_usd']:.4f}")
    row("tokens avoided (registry and rules skips)", lambda m: str(m["model"]["tokens_saved"]))
    lines += ["", "Answered by: " + ", ".join(f"{k}: {v}" for k, v in o["answered_by"].items()) + ".",
              "Refusal kinds seen: " + ", ".join(f"{k}: {v}" for k, v in sorted(o["refusal_kinds"].items())) + ".", "",
              "### Answer items by kind of wording", "", "| tag | n | matched | accuracy | how they ended |", "|---|---|---|---|---|"]
    for tag, t in o["by_tag"].items():
        lines.append(f"| {tag} | {t['n']} | {t['matched']} | {_fmt(t['accuracy'])} | "
                     + ", ".join(f"{k}: {v}" for k, v in sorted(t["answered_by"].items())) + " |")
    wrong = [x for x in r.outcomes if x.confident_wrong]
    lines += ["", "### Confident wrong answers", ""]
    lines += [f"* `{x.id}` ({x.expect}{', ' + x.reason if x.reason else ''}) \"{x.question}\" — answered by {x.answered_by}"
              f"{' `' + x.verified_query + '`' if x.verified_query else ''}{' (' + x.match_note + ')' if x.match_note else ''}"
              for x in wrong] or ["None."]
    lines += ["", "### Every question", "", "| id | expect | actual | kind | by | match | ms | calls |", "|---|---|---|---|---|---|---|---|"]
    for x in r.outcomes:
        lines.append(f"| {x.id} | {x.expect}{'/' + x.reason if x.reason else ''} | {x.actual} | {x.refusal_kind or ''} | "
                     f"{x.answered_by or ''}{' `' + x.verified_query + '`' if x.verified_query else ''} | {'' if x.match is None else ('yes' if x.match else 'no: ' + x.match_note)} | "
                     f"{x.latency_ms} | {x.model_calls} |")
    lines += ["", SCOPE, "## Pilot threshold — PROPOSED, to be set by the owner before the pilot", "",
              "Placeholder values for the **live** tier; nothing here is enforced, and the owner fixes them (with the "
              "question set version) before the pilot starts, not after seeing a live run.", "",
              "| Proposed gate | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in PROPOSED_THRESHOLDS.items()]
    lines += ["", "## Tier invariants", "", ("PASS — " + "every invariant held (evaluation.ask.check).") if not r.problems
              else "FAIL — " + "; ".join(r.problems), ""]
    if r.tier != "live":
        lines += ["## Live tier", "", args.live_note or "Not run in this report.", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", choices=["off", "fake", "live"], default="off")
    ap.add_argument("--domains", default="itsm,sales,finance")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="questions per domain (stratified), for a smoke run")
    ap.add_argument("--check", action="store_true", help="exit 1 when a tier invariant fails")
    ap.add_argument("--report", default=None, help="markdown path, or `auto` for docs/60-delivery/evidence/")
    ap.add_argument("--live-note", default=None, help="what to say about the live tier in an off/fake report")
    ap.add_argument("--run-note", default=None, help="a sentence on where this was run (environment, stack)")
    args = ap.parse_args()
    os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")

    from evaluation.ask import as_dict, run

    r = run(args.models, domains=tuple(d.strip() for d in args.domains.split(",") if d.strip()), seed=args.seed, limit=args.limit)
    if r.outcomes:
        o = r.summary["overall"]
        print(f"[{r.tier}] questions={o['questions']} execution_accuracy={_fmt(o['execution_accuracy'])} "
              f"({o['answer_items_matched']}/{o['answer_items']}) confident_wrong={o['confident_wrong']} "
              f"needs_input P/R={_fmt(o['refusal']['needs_input']['precision'])}/{_fmt(o['refusal']['needs_input']['recall'])} "
              f"clarify P/R={_fmt(o['refusal']['clarify']['precision'])}/{_fmt(o['refusal']['clarify']['recall'])} "
              f"decline P/R={_fmt(o['refusal']['decline']['precision'])}/{_fmt(o['refusal']['decline']['recall'])} "
              f"leaks={o['restricted_leaks']} calls={o['model']['calls']} p50={_fmt(o['latency_ms']['p50'])}ms ({r.seconds}s)")
    if args.report:
        path = (ROOT / "docs" / "60-delivery" / "evidence" / f"{datetime.now(UTC):%Y-%m-%d}-ask-benchmark-{r.tier}.md"
                if args.report == "auto" else Path(args.report))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(r, args), encoding="utf-8")
        path.with_suffix(".json").write_text(json.dumps(as_dict(r), indent=1, default=str), encoding="utf-8")
        print(f"report: {path}")
    for p in r.problems:
        print(f"INVARIANT FAILED: {p}", file=sys.stderr)
    if not r.outcomes and r.tier == "live":
        return 3
    return 1 if (args.check and r.problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
