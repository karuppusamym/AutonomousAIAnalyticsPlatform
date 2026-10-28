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

## 6. As built (2026-10-27, T1 and T3–T7; T2 was done earlier)

Migration `0069_claims_evidence` (revises `0068_desk_daily`): `insight.claim_label` (backfilled from status), `artifact.banner`;
no new table. Upgrade from empty, downgrade -1 and re-upgrade checked on Postgres 16, 2026-10-27.

* **T1 compute isolation.** The Helm part (gVisor `runtimeClassName` per pool, RuntimeClass object, per-role
  NetworkPolicies) is in S12's branch `claude/competent-curran-ad80fc`, at S12's request, to avoid two sessions editing
  the chart; S07 does the code and Docker side. The worker refuses to start when `ANALYSTOS_WORKER_REQUIRE_RUNTIME`
  (allow-listed; the chart should set it with the runtime class) does not match the kernel it sees
  (`workers/isolation.py::detect_runtime`, gVisor's fixed `/proc/version`, a Kata guest's cmdline). CPU/memory/time
  limits per task class are the existing `task_queues.yaml` budgets. Compose pools: user 10001, `read_only`, `cap_drop: ALL`,
  `no-new-privileges`, `pids_limit`, memory/CPU limits, `runtime: ${COMPUTE_RUNTIME:-runc}`, the internal network
  (compose's equivalent of `--network none` for a worker that must reach the API and Temporal). The per-execution
  container sandbox already used `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`. The
  in-process sandbox (`off`) runs only for `profile=lite AND data_class=synthetic`
  (`sandbox/isolation.py::in_process_allowed`, `run_python(data_class=)`); a development host (`ANALYSTOS_ENV`
  dev/test/local) keeps its explicit opt-in. Not verified here: a pod on a gVisor node.
* **T3 claim labels.** `evidence/claims.py`: labels decided by code from REV's verdict (`label_for`): verified
  observation → `observed`; verified association → `supported_association`, or `possible_contributor` when its
  strength is weak; failed only on undecidable checks (sample size, missing evidence, data version) → `unknown`;
  otherwise `rejected`. Drafts are `possible_contributor` (association) or `unknown` (observation). A method declares
  `spec.claim: observation` (trend, pareto, cohort_retention and the T6 pack) and `spec.causal_design: true` (none
  registered: A/B and diff-in-diff are P2). The causal lexicon runs inside the numbers guard
  (`facts.bind_finding(..., claim_label=)`) on text with group labels masked; an unlicensed causal narrative falls back
  to the template, and REV rewrites one before its verdict (the old `critic.CAUSAL` regex is now the shared lexicon).
* **T4 Evidence API v2.** `contracts.evidence.EvidenceV2`, `evidence/api_v2.py`; `GET /v2/evidence/{id}` and
  `evidence[]` on `GET /v2/runs/{id}/evidence`. Contract: [`06-evidence-api-v2.md`](../20-contracts/06-evidence-api-v2.md).
  `metric_version` is `{metric, version}` from the record's semantic dependency; `limitations` also carries the
  investigation gates' findings.
* **T5 investigation gates.** `evidence/investigation.py` (six deterministic checks), playbook step field `checks:`
  (`Gate {name, on_fail: warn|block}`, validated against the check names), run by `agents/dispatch.py` after the
  step's behaviour; results on the step output and as `investigation.check` events. `investigate.v1` → version
  1.1.0: profile [baseline], hypotheses [hypotheses], insights [segmentation, statistics: block], verify
  [alternatives, conclusions: block]. Gates are not in the plan dict, so the pinned v1 plan hashes are unchanged.
  "Mechanism" is the hypothesis rationale; "mix effects" means a method with `spec.mix_effects`
  (contribution_decomposition) was tested; "seasonality" a method with `spec.seasonality` (the T6 pack's).
* **T6 method pack.** Through the methods registry (one module + one manifest each; no name outside
  `methods/`): `seasonality` (STL with the `ml` extra, classical decomposition otherwise; Kruskal-Wallis by season
  position; check: autocorrelation at the season lag), `level_shift` (CUSUM split + 999-shuffle permutation test;
  `ruptures` is not a dependency, so CUSUM is the method; check: binary segmentation), `forecast_backtest` (ETS =
  `skills.forecast` Holt-Winters; rolling-origin backtest, K ≤ 5 folds of h ≤ 4 periods, vs the better of naive and
  seasonal naive on each fold's training data; one-sided Diebold-Mariano; 90% prediction intervals; check: (S)ARIMA
  on the same folds), `distribution_shift` (latest period vs earlier on a bounded sample: KS + PSI over ten
  quantile bins; check: k-sample Anderson-Darling). Manifests carry `inputs`, `outputs`, `assumptions`,
  `min_sample`. Funnel and cohort decomposition: the existing `cohort_retention` and `contribution_decomposition`
  methods; a funnel method is not built.
* **T7 void → banner → Desk task.** `evidence/banners.py`, hooked into `verification.void_dependents` (so event
  paths, `recheck` and the nightly sweep alike) inside a savepoint (a banner failure never undoes a void). Published
  charts presenting the finding (lineage `visualized_by`) and the dashboards holding them (`included_in`) get
  `artifact.banner`; one Request Desk task per artifact (`channel: system`, `R9`, priority high, escalated with SLA,
  assignee and a data pack of the void causes); a later void on the same artifact joins the open task
  (`request.void_added`). A new verified record of the finding clears it (`publication.banner_cleared`). The web
  shows the banner on published dashboards (`PublishedOutputs`, `DashboardPreview`). Not done: writing the banner
  into the external BI dashboard itself (Superset), which would be a new approved publication.
