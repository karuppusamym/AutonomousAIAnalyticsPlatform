# Analytical benchmark (P4-V01) — 2026-09-30

Generated 2026-09-30 02:59 UTC by `scripts/benchmark_analytical.py` at `f1d6c36` (Python 3.13.5). Models: **live** (config/models.yaml). Settings preset **max_quality** applied to the benchmark process only (evaluation/live.py). Models re-enabled for this process only (the stored settings had them disabled): google/gemini-3.5-flash,openai/gpt-5.4-mini.

Datasets: `evaluation.datasets` — one table per domain generated from an explicit causal graph (ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; FDR is the mean false-discovery proportion per replicate, compared with the nominal α = 0.05.

Thresholds: precision_min 0.9, recall_min 0.9, fdr_max 0.05, null_fdr_max 0.05.

## Tier: platform

| Measure | Value |
|---|---|
| replicates (with effects + global null) | 12 |
| hypotheses tested | 126 |
| verified findings | 25 (true 24, false 1) |
| **precision** of verified findings | **0.960** |
| **recall** of planted effects | **0.950** (19/20) |
| **FDR** (replicates with effects) vs α 0.05 | **0.033** |
| **FDR under the global null** (= FWER there) | **0.000** |
| null hypotheses tested | 91 |
| null hypotheses with raw p < α (before effect-size gates, BH, second method) | n/a |

| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 4 | 10 | 1.000 | 1.000 (8/8) | 0.000 | 0.000 | 27 | n/a | 222.6 |
| sales | 4 | 9 | 0.889 | 1.000 (6/6) | 0.100 | 0.000 | 32 | n/a | 199.7 |
| finance | 4 | 6 | 1.000 | 0.833 (5/6) | 0.000 | 0.000 | 32 | n/a | 167.1 |

| Method (P4-03) | tested | verified | precision | null tests | raw p<α on nulls | null FDR |
|---|---|---|---|---|---|---|
| correlation | 0 | 1 | n/a | 0 | n/a | 0.000 |
| driver_model | 0 | 4 | 1.000 | 0 | n/a | 0.000 |
| numeric_by_segment | 0 | 13 | 1.000 | 0 | n/a | 0.000 |
| pareto | 0 | 2 | 1.000 | 0 | n/a | 0.000 |
| rate_by_segment | 0 | 6 | 0.833 | 0 | n/a | 0.000 |

Missed planted effects: finance:finance_late_by_unit (seed 2).

False verified findings: sales: rate_by_segment returned by customer_segment (top None, seed 1, effects).

### Model use (model_call rows of these runs)

Provider calls 59 (errors 3), tokens in/out 54240/53089, recorded cost $0.3826; purposes answered by rules or deterministic rungs 53, cache hits 0, calls stopped for a missing price / approval 14.

| Purpose / model | calls | errors | tokens in | tokens out | USD |
|---|---|---|---|---|---|
| follow_up_generation / google/gemini-3.5-flash | 8 | 0 | 17342 | 6572 | 0.0852 |
| hypothesis_generation / google/gemini-3.5-flash | 12 | 0 | 17824 | 23100 | 0.2346 |
| insight_narrative / google/gemini-3.5-flash-lite | 5 | 0 | 4766 | 1826 | 0.0060 |
| planning / openai/gpt-5.4-mini | 12 | 0 | 4042 | 4566 | 0.0236 |
| semantic_modeling / deepseek/deepseek-v4-flash-0731 | 3 | 3 | 2095 | 7500 | 0.0028 |
| semantic_modeling / openai/gpt-5.4-mini | 8 | 0 | 4399 | 5289 | 0.0271 |
| semantic_modeling / openai/gpt-oss-120b | 3 | 0 | 1827 | 3427 | 0.0007 |
| summarization / google/gemini-3.5-flash-lite | 8 | 0 | 1945 | 809 | 0.0026 |

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
