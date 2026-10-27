# S07 — Analysis, verification and evidence

**Priority:** P0 (isolation, claim labels) / P1 (forecast pack) · **Closes:** FR-018, FR-031, FR-033, FR-043 · **ADRs:** 012, AnalystOS 0002, 0008, 0020

## 1. Purpose
Every number is computed by deterministic code, every claim is labelled and verified, and every verdict
voids itself when its inputs change.

## 2. Baseline to reuse
Closed analysis vocabulary (`contracts/analysis`, ADR-0002), `skills/sqlbuild`, `skills/` statistics (SciPy, statsmodels,
scikit-learn), BH correction, the numbers guard, REV verification (re-run the evidence query with a hash match plus an independent
second method), `VerificationRecord`, `VerificationDependency`, `VerificationSweep`, fingerprints (ADR-0020: query, data,
semantics, method, context, model call, policy), "Why this number?", `evidence/` (attested computations, OpenLineage),
`methods/` registry, `sandbox/` (rlimits, not a boundary), `workers/` compute protocol (ADR-0022), `ml/` (governed classical ML).

## 3. Build
1. **Compute isolation (ADR-012, FR-043).** `worker:compute` pods use `runtimeClassName: gvisor`, a read-only root, a non-root user,
   no service account token, NetworkPolicy deny-all, an ephemeral `emptyDir` of 2 GiB, and CPU/mem/time limits per task class.
   Inputs and outputs travel only as presigned object URLs. Docker dev: `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`.
   The in-process sandbox is allowed only when `profile=lite AND data_class=synthetic`. Fix the Windows `AF_UNIX` dispatch issue
   (the conformance test `test_ml_on_isolated_pool` fails on win32), or mark it POSIX-only like the other sandbox tests.
2. **Claim labels (v1 §7.2).** Every insight carries `label ∈ {observed, supported_association, possible_contributor,
   rejected, unknown}`. The narrative generator cannot use causal verbs unless `label=supported_association` *and* the
   method is in the `causal_design` set (A/B, diff-in-diff, which are P2). Enforced by a lexicon check in the numbers guard.
3. **Evidence object** (v1 §7.3) as the API shape of the existing `Insight` + `VerificationRecord` + citations:
   `{evidence_id, run_id, claim_id, label, description, dataset_versions, metric_version, query_execution_ids[],
   method, effect{value, unit, ci?}, sample{n, coverage}, limitations[], review_status, fingerprint, void_reason?}`.
4. **Investigation workflow checks** (v1 §7.2) as playbook gates: baseline (window, time zone, denominator, freshness,
   completeness) → segmentation (materiality, mix effects, contribution and coverage) → hypotheses (mechanism and testable evidence) →
   statistics (test choice by type, minimum n, multiple-testing correction) → alternatives (seasonality, traffic mix, only with
   validated data) → conclusions.
5. **Method packs.** P1: time series (STL seasonality, change-point detection with `ruptures` if its license is acceptable, otherwise
   CUSUM), forecasting (ETS/ARIMA via statsmodels with a rolling-origin backtest and prediction intervals), distribution shift
   (KS, PSI), funnel and cohort decomposition. Each method is registered with input/output schema, assumptions and minimum sample.
6. **Void sweep.** When a fingerprint component changes (metric version, data snapshot, policy version, context identity), a
   sweep marks dependents `VOID`, and published artifacts referencing them show a banner and raise a Desk task (S13).

## 4. Acceptance
AT-03, AT-11, AT-21; `test_causal_language_blocked`; `test_forecast_backtest_reported`; `test_void_on_metric_version_change`
propagates to publication banner; adversarial: a data row containing instructions never changes the plan (AT-06).

## 5. Tasks
| ID | Task | Est |
|---|---|---|
| S07-T1 | gVisor compute role + Helm runtimeClass + dev docker flags | 2d |
| S07-T2 | Fix/mark Windows compute dispatch test | 0.5d |
| S07-T3 | Claim labels + causal-language guard | 1.5d |
| S07-T4 | Evidence API v2 shape | 1d |
| S07-T5 | Investigation gates in `investigate.v1` | 2d |
| S07-T6 | Time-series + forecast method pack | 4d |
| S07-T7 | Void sweep → publication banner + Desk task | 1.5d |
