# Analytical benchmark (P4-V01) — 2026-09-30

Generated 2026-09-30 03:25 UTC by `scripts/benchmark_analytical.py` at `f1d6c36` (Python 3.13.5). Models: **live** (config/models.yaml). Settings preset **max_quality** applied to the benchmark process only (evaluation/live.py). Models re-enabled for this process only (the stored settings had them disabled): google/gemini-3.5-flash,openai/gpt-5.4-mini.

Datasets: `evaluation.datasets` — one table per domain generated from an explicit causal graph (ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; FDR is the mean false-discovery proportion per replicate, compared with the nominal α = 0.05.

Thresholds: precision_min 0.9, recall_min 0.9, fdr_max 0.05, null_fdr_max 0.05.

## Tier: platform

| Measure | Value |
|---|---|
| replicates (with effects + global null) | 12 |
| hypotheses tested | 120 |
| verified findings | 28 (true 27, false 1) |
| **precision** of verified findings | **0.964** |
| **recall** of planted effects | **1.000** (20/20) |
| **FDR** (replicates with effects) vs α 0.05 | **0.033** |
| **FDR under the global null** (= FWER there) | **0.000** |
| null hypotheses tested | 87 |
| null hypotheses with raw p < α (before effect-size gates, BH, second method) | n/a |

| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 4 | 13 | 1.000 | 1.000 (8/8) | 0.000 | 0.000 | 25 | n/a | 109.8 |
| sales | 4 | 9 | 0.889 | 1.000 (6/6) | 0.100 | 0.000 | 31 | n/a | 105.3 |
| finance | 4 | 6 | 1.000 | 1.000 (6/6) | 0.000 | 0.000 | 31 | n/a | 66.1 |

| Method (P4-03) | tested | verified | precision | null tests | raw p<α on nulls | null FDR |
|---|---|---|---|---|---|---|
| driver_model | 0 | 4 | 1.000 | 0 | n/a | 0.000 |
| numeric_by_segment | 0 | 14 | 1.000 | 0 | n/a | 0.000 |
| pareto | 0 | 2 | 1.000 | 0 | n/a | 0.000 |
| rate_by_segment | 0 | 8 | 0.875 | 0 | n/a | 0.000 |

Missed planted effects: none.

False verified findings: sales: rate_by_segment returned by customer_segment (top None, seed 1, effects).

### Model use (model_call rows of these runs)

Provider calls 35 (errors 0), tokens in/out 31553/25477, recorded cost $0.1974; purposes answered by rules or deterministic rungs 83, cache hits 0, calls stopped for a missing price / approval 10.

| Purpose / model | calls | errors | tokens in | tokens out | USD |
|---|---|---|---|---|---|
| follow_up_generation / google/gemini-3.5-flash | 5 | 0 | 11672 | 4306 | 0.0563 |
| hypothesis_generation / google/gemini-3.5-flash | 5 | 0 | 7464 | 9854 | 0.0999 |
| insight_narrative / google/gemini-3.5-flash-lite | 3 | 0 | 4092 | 1647 | 0.0053 |
| planning / openai/gpt-5.4-mini | 7 | 0 | 2366 | 2756 | 0.0142 |
| semantic_modeling / deepseek/deepseek-v4-flash-0731 | 2 | 0 | 1392 | 2388 | 0.0008 |
| semantic_modeling / openai/gpt-5.4-mini | 4 | 0 | 2316 | 3604 | 0.0180 |
| summarization / google/gemini-3.5-flash-lite | 9 | 0 | 2251 | 922 | 0.0030 |

## Scope and limits

* Planted effects are sized clearly above the verdict layer's materiality thresholds (skills/stats.py), so recall
  here is recall for material effects; power close to the thresholds, per-method null behaviour and selection effects
  are measured by scripts/benchmark_methods.py (evaluation/method_checks.py, P4-03), not by this report.
* The component tier reads raw p-values, so it also reports test calibration (share of null hypotheses with raw p < α,
  expected ≈ α). The platform tier scores what a real run stored (verified insights and tested hypotheses).
* A run with a live or local model (`--models live`) is a separate dated report; a model changes which hypotheses are
  proposed and how findings are worded, never the statistics, BH or the second-method gate.

## Verdict

PASS — every threshold met.
