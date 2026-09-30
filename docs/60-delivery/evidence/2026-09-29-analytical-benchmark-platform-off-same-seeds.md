# Analytical benchmark (P4-V01) — 2026-09-30

Generated 2026-09-30 03:27 UTC by `scripts/benchmark_analytical.py` at `f1d6c36` (Python 3.13.5). Models: **off** (no provider key: every model purpose takes its rule path).

Datasets: `evaluation.datasets` — one table per domain generated from an explicit causal graph (ITSM incidents with 4 planted effects, sales orders with 3, accounts-payable invoices with 3; two null columns each, drawn independently of everything). Global-null replicates regenerate the same tables with every planted effect set to zero. A verified finding is *true* when its outcome and segment share an ancestor in the graph; FDR is the mean false-discovery proportion per replicate, compared with the nominal α = 0.05.

Thresholds: precision_min 0.9, recall_min 0.9, fdr_max 0.05, null_fdr_max 0.05.

## Tier: platform

| Measure | Value |
|---|---|
| replicates (with effects + global null) | 12 |
| hypotheses tested | 116 |
| verified findings | 24 (true 24, false 0) |
| **precision** of verified findings | **1.000** |
| **recall** of planted effects | **1.000** (20/20) |
| **FDR** (replicates with effects) vs α 0.05 | **0.000** |
| **FDR under the global null** (= FWER there) | **0.000** |
| null hypotheses tested | 88 |
| null hypotheses with raw p < α (before effect-size gates, BH, second method) | n/a |

| Domain | replicates | verified | precision | recall | FDR | null FDR | null tests | raw p<α on nulls | seconds |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 4 | 10 | 1.000 | 1.000 (8/8) | 0.000 | 0.000 | 28 | n/a | 40.4 |
| sales | 4 | 8 | 1.000 | 1.000 (6/6) | 0.000 | 0.000 | 28 | n/a | 29.4 |
| finance | 4 | 6 | 1.000 | 1.000 (6/6) | 0.000 | 0.000 | 32 | n/a | 24.1 |

| Method (P4-03) | tested | verified | precision | null tests | raw p<α on nulls | null FDR |
|---|---|---|---|---|---|---|
| driver_model | 0 | 4 | 1.000 | 0 | n/a | 0.000 |
| numeric_by_segment | 0 | 12 | 1.000 | 0 | n/a | 0.000 |
| pareto | 0 | 2 | 1.000 | 0 | n/a | 0.000 |
| rate_by_segment | 0 | 6 | 1.000 | 0 | n/a | 0.000 |

Missed planted effects: none.

False verified findings: none.

### Model use (model_call rows of these runs)

Provider calls 0 (errors 0), tokens in/out 0/0, recorded cost $0.0000; purposes answered by rules or deterministic rungs 106, cache hits 0, calls stopped for a missing price / approval 0.


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
