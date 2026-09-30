# Held-out evaluation (P4-08) — 2026-09-30

Generated 2026-09-30 03:30 UTC by the held-out runner (`evaluation/heldout`) at `f1d6c36` (Python 3.13.5). Corpus `evaluation/heldout/corpus.yaml` version 2 (frozen 2026-09-27, sha256 `3cfc43594ed1`). Models: **live** (a provider key is set). Settings preset **max_quality** applied to the benchmark process only (evaluation/live.py). Models re-enabled for this process only (the stored settings had them disabled): google/gemini-3.5-flash,openai/gpt-5.4-mini.

Every task ends in one status (`evaluation/heldout/runner.py`): accepted, correct_abstention, confident_wrong, incomplete, unnecessary_abstention, wrong_abstention or error. Rubrics were sealed before the first scored run; the corpus lock (`corpus.lock.json`) hashes every task's data, reference and rubric.

Proposed thresholds (non-blocking `heldout` gate, owner to confirm): accepted_output_rate ≥ 0.9, confident_wrong ≤ 0, abstention_recall ≥ 1.0, errors ≤ 0.

## Tier: platform

Corpus lock: matches. Wall time 597.8 s.

| Measure | Value |
|---|---|
| tasks (deliver / abstain) | 75 (43 / 32) |
| **accepted-output rate** (accepted / deliver tasks) | **0.907** (39/43) |
| **confident-wrong** | **0** |
| **abstention correctness** (abstain tasks ended as the rubric requires) | **1.000** |
| abstention precision (abstentions that were on abstain tasks) | 0.970 |
| unnecessary abstentions | 1 |
| task success (accepted + correct abstentions) / tasks | 0.947 |
| errors | 0 |
| latency p50 / p95 / max (s, per task) | 2.738 / 35.525 / 80.842 |
| model calls / tokens / USD | 59 / 82445 / 0.241666 |
| cost per accepted output: model USD / CPU s / wall s / infrastructure | 0.006 / 4.326 / 15.283 / unpriced |

| Domain | tasks | accepted | accepted-output rate | confident-wrong | abstention correctness | unnecessary abstentions | errors | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|
| itsm | 6 | 2 | 0.500 | 0 | 1.000 | 0 | 0 | 29.847 | 65.965 |
| sales | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 8.488 | 62.964 |
| finance | 6 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 8.489 | 49.810 |
| engineering | 15 | 9 | 1.000 | 0 | 1.000 | 0 | 0 | 1.184 | 1.726 |
| ml | 16 | 7 | 1.000 | 0 | 1.000 | 0 | 0 | 2.258 | 3.307 |
| retail | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 8.314 | 9.253 |
| logistics | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 7.294 | 7.328 |
| saas_ops | 3 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 7.233 | 7.491 |
| transfer | 4 | 1 | 0.333 | 0 | 1.000 | 1 | 0 | 5.510 | 7.604 |
| governance | 8 | 2 | 1.000 | 0 | 1.000 | 0 | 0 | 0.392 | 0.994 |
| recovery | 5 | 4 | 1.000 | 0 | 1.000 | 0 | 0 | 1.456 | 3.969 |

### Every task

| Task | family | expect | status | abstained as | seconds | why |
|---|---|---|---|---|---|---|
| HO-ITSM-01 | analysis | deliver | accepted | — | 48.199 | every planted effect verified, no false finding |
| HO-ITSM-02 | analysis | deliver | accepted | — | 71.887 | every planted effect verified, no false finding |
| HO-ITSM-03 | analysis | deliver | incomplete | — | 21.53 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-04 | analysis | deliver | incomplete | — | 30.094 | missed planted effect(s): fulfilment_by_site |
| HO-ITSM-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 29.6 | no false finding (1 true non-planted finding(s) reported) |
| HO-ITSM-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 21.684 | no false finding |
| HO-SALES-01 | analysis | deliver | accepted | — | 80.842 | every planted effect verified, no false finding |
| HO-SALES-02 | analysis | deliver | accepted | — | 9.329 | every planted effect verified, no false finding |
| HO-SALES-03 | analysis | deliver | accepted | — | 8.251 | every planted effect verified, no false finding |
| HO-SALES-04 | analysis | deliver | accepted | — | 8.724 | every planted effect verified, no false finding |
| HO-SALES-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 6.77 | no false finding |
| HO-SALES-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.212 | no false finding |
| HO-FIN-01 | analysis | deliver | accepted | — | 8.328 | every planted effect verified, no false finding |
| HO-FIN-02 | analysis | deliver | accepted | — | 8.722 | every planted effect verified, no false finding |
| HO-FIN-03 | analysis | deliver | accepted | — | 63.506 | every planted effect verified, no false finding |
| HO-FIN-04 | analysis | deliver | accepted | — | 8.65 | every planted effect verified, no false finding |
| HO-FIN-05 | analysis | abstain (no_finding) | correct_abstention | no_finding | 6.804 | no false finding |
| HO-FIN-06 | analysis | abstain (no_finding) | correct_abstention | no_finding | 6.787 | no false finding |
| HO-DE-01 | engineering | deliver | accepted | — | 1.324 | output equals the reference |
| HO-DE-02 | engineering | deliver | accepted | — | 1.502 | output equals the reference |
| HO-DE-03 | engineering | deliver | accepted | — | 1.248 | output equals the reference |
| HO-DE-04 | engineering | abstain (blocked) | correct_abstention | blocked | 0.953 | fail gate range(weight_kg) |
| HO-DE-05 | engineering | abstain (refused) | correct_abstention | refused | 0.664 | validation: node delivered (filter): column shipment_status does not exist here (have: carrier_code, loaded_at, ship_date, shipment_no, shipment_state, weight_kg) |
| HO-DE-06 | engineering | deliver | accepted | — | 1.112 | output equals the reference |
| HO-DE-07 | engineering | abstain (refused) | correct_abstention | refused | 0.788 | InvalidInput: join priced is declared many_to_one but the data is many_to_many (left keys 3/4659 rows, right keys 3/4 rows, row multiplication 1.3677); the recipe was not run |
| HO-DE-08 | engineering | deliver | accepted | — | 1.095 | output equals the reference |
| HO-ML-01 | ml | deliver | accepted | — | 2.337 | improved over the baseline on identical splits |
| HO-ML-02 | ml | deliver | accepted | — | 1.602 | improved over the baseline on identical splits |
| HO-ML-03 | ml | deliver | accepted | — | 2.397 | improved over the baseline on identical splits |
| HO-ML-04 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.772 | verdict no_improvement |
| HO-ML-05 | ml | abstain (refused) | correct_abstention | refused | 0.721 | InvalidInput: the experiment was refused before training: target_derived_features: these features alone reproduce the target (derived from it?): work_order_raised (single threshold 1.0) |
| HO-ML-06 | ml | abstain (refused) | correct_abstention | refused | 0.811 | InvalidInput: the experiment was refused before training: post_cutoff_timestamps: feature values observed after the prediction cutoff: failure_date (132 rows); post_outcome_timestamps: feature values observed at or after |
| HO-ML-07 | ml | abstain (refused) | correct_abstention | refused | 0.801 | InvalidInput: the experiment was refused before training: group_aware_split: entity ['asset_tag'] repeats across rows; a random split would put one entity in both training and holdout (declare group_keys) |
| HO-ML-08 | ml | abstain (refused) | correct_abstention | refused | 0.707 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 18 usable rows, need >= 40 (insufficient evidence is a valid outcome) |
| HO-RET-01 | analysis | deliver | accepted | — | 8.314 | every planted effect verified, no false finding |
| HO-RET-02 | analysis | deliver | accepted | — | 9.357 | every planted effect verified, no false finding |
| HO-RET-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 7.992 | no false finding |
| HO-LOG-01 | analysis | deliver | accepted | — | 7.332 | every planted effect verified, no false finding |
| HO-LOG-02 | analysis | deliver | accepted | — | 7.294 | every planted effect verified, no false finding |
| HO-LOG-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 4.596 | no false finding |
| HO-SOPS-01 | analysis | deliver | accepted | — | 7.233 | every planted effect verified, no false finding |
| HO-SOPS-02 | analysis | deliver | accepted | — | 7.52 | every planted effect verified, no false finding |
| HO-SOPS-03 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.273 | no false finding |
| HO-XFER-01 | analysis | deliver | unnecessary_abstention | no_finding | 4.869 | no verified finding |
| HO-XFER-02 | analysis | deliver | incomplete | — | 5.722 | missed planted effect(s): win_by_source |
| HO-XFER-03 | analysis | deliver | accepted | — | 7.936 | every planted effect verified, no false finding |
| HO-XFER-04 | analysis | abstain (no_finding) | correct_abstention | no_finding | 5.298 | no false finding |
| HO-DE-09 | engineering | deliver | accepted | — | 1.343 | output equals the reference |
| HO-DE-10 | engineering | deliver | accepted | — | 1.126 | output equals the reference |
| HO-DE-11 | engineering | abstain (blocked) | correct_abstention | blocked | 1.063 | fail gate accepted_values(tender_type) |
| HO-DE-12 | engineering | abstain (blocked) | correct_abstention | blocked | 1.184 | fail gate key_unique(receipt_no,line_no) |
| HO-DE-13 | engineering | deliver | accepted | — | 1.818 | output equals the reference |
| HO-DE-14 | engineering | deliver | accepted | — | 1.686 | output equals the reference |
| HO-DE-15 | engineering | abstain (refused) | correct_abstention | refused | 1.57 | InvalidInput: join with_edition is declared many_to_one but the data is many_to_many (left keys 90/3629 rows, right keys 90/91 rows, row multiplication 1.011); the recipe was not run |
| HO-ML-09 | ml | deliver | accepted | — | 2.847 | improved over the baseline on identical splits |
| HO-ML-10 | ml | deliver | accepted | — | 2.919 | improved over the baseline on identical splits |
| HO-ML-11 | ml | abstain (refused) | correct_abstention | refused | 0.896 | InvalidInput: the experiment was refused before training: feature_availability: only known after the outcome: exit_survey_score |
| HO-ML-12 | ml | abstain (no_finding) | correct_abstention | no_finding | 3.039 | verdict no_improvement |
| HO-ML-13 | ml | deliver | accepted | — | 2.178 | improved over the baseline on identical splits |
| HO-ML-14 | ml | abstain (no_finding) | correct_abstention | no_finding | 2.738 | verdict no_improvement |
| HO-ML-15 | ml | deliver | accepted | — | 4.11 | improved over the baseline on identical splits |
| HO-ML-16 | ml | abstain (refused) | correct_abstention | refused | 0.659 | InvalidInput: the experiment was refused before training: minimum_rows: insufficient data: 24 usable rows, need >= 46 (insufficient evidence is a valid outcome) |
| HO-GOV-01 | governance | abstain (denied) | correct_abstention | denied | 1.167 | SQLRejected: Table src_src_5cfea731b525.payroll_lines is not in the authorized scope. Use one of: src_src_9f24736d3ce1.loyalty_members. |
| HO-GOV-02 | governance | abstain (denied) | correct_abstention | denied | 0.63 | SQLRejected: Column src_src_9bf5a8a9b709.loyalty_members.email_address is restricted by policy and cannot be referenced. Remove it from the query. |
| HO-GOV-03 | governance | abstain (denied) | correct_abstention | denied | 0.63 | validation: node valued (derive) column points_eur: expression does not parse: Invalid expression / Unexpected token. Line 1, Col: 29.; node per_tier (aggregate) measure points_eur: column points_eur does not exist here |
| HO-GOV-04 | governance | abstain (denied) | correct_abstention | denied | 0.049 | ApprovalRequired: approval expired |
| HO-GOV-05 | governance | abstain (denied) | correct_abstention | denied | 0.039 | ApprovalRequired: payload changed after approval; a new approval is required |
| HO-GOV-06 | governance | abstain (denied) | correct_abstention | denied | 0.155 | PolicyDenied: approver no longer holds approval rights in this workspace |
| HO-GOV-07 | governance | deliver | accepted | — | 0.674 | the answer equals the reference |
| HO-GOV-08 | governance | deliver | accepted | — | 0.042 | executed once; the second attempt was refused (ApprovalRequired: approval is executed) |
| HO-REC-01 | recovery | deliver | accepted | — | 4.349 | failed without output (Forbidden: asset src_src_9ec57ee472e2.stores (node stores) is not in the authorized scope); rerun equals the reference twice |
| HO-REC-02 | recovery | deliver | accepted | — | 1.456 | the refusal names store_format; the corrected recipe equals the reference |
| HO-REC-03 | recovery | deliver | accepted | — | 0.696 | the failure named its cause and was recorded; after reconnecting the table was discovered |
| HO-REC-04 | recovery | deliver | accepted | — | 0.041 | the stale save was refused; after reloading both edits survive |
| HO-REC-05 | recovery | abstain (refused) | correct_abstention | refused | 2.448 | Conflict: src_src_28db97603c49.till_lines changed since the recipe pinned snapshot 8ecd0842a887 (now 30158aeb7dc6) |

Failures and non-accepted outcomes (4): HO-ITSM-03 incomplete; HO-ITSM-04 incomplete; HO-XFER-01 unnecessary_abstention; HO-XFER-02 incomplete.
Abstentions: HO-ITSM-05 no_finding (correct_abstention); HO-ITSM-06 no_finding (correct_abstention); HO-SALES-05 no_finding (correct_abstention); HO-SALES-06 no_finding (correct_abstention); HO-FIN-05 no_finding (correct_abstention); HO-FIN-06 no_finding (correct_abstention); HO-DE-04 blocked (correct_abstention); HO-DE-05 refused (correct_abstention); HO-DE-07 refused (correct_abstention); HO-ML-04 no_finding (correct_abstention); HO-ML-05 refused (correct_abstention); HO-ML-06 refused (correct_abstention); HO-ML-07 refused (correct_abstention); HO-ML-08 refused (correct_abstention); HO-RET-03 no_finding (correct_abstention); HO-LOG-03 no_finding (correct_abstention); HO-SOPS-03 no_finding (correct_abstention); HO-XFER-01 no_finding (unnecessary_abstention); HO-XFER-04 no_finding (correct_abstention); HO-DE-11 blocked (correct_abstention); HO-DE-12 blocked (correct_abstention); HO-DE-15 refused (correct_abstention); HO-ML-11 refused (correct_abstention); HO-ML-12 no_finding (correct_abstention); HO-ML-14 no_finding (correct_abstention); HO-ML-16 refused (correct_abstention); HO-GOV-01 denied (correct_abstention); HO-GOV-02 denied (correct_abstention); HO-GOV-03 denied (correct_abstention); HO-GOV-04 denied (correct_abstention); HO-GOV-05 denied (correct_abstention); HO-GOV-06 denied (correct_abstention); HO-REC-05 refused (correct_abstention).

Status counts: accepted 39, correct_abstention 32, confident_wrong 0, incomplete 3, unnecessary_abstention 1, wrong_abstention 0, error 0.

## Scope and limits

* Coverage: 75 tasks (31 analysis, 15 engineering, 16 ml, 8 governance, 5 recovery; 32 expect an abstention) against the evaluation plan's first-release target of at least 60 (20 analyst, 15 engineering, 15 ML, 10 unsupported). Analysis domains: finance, itsm, logistics, retail, saas_ops, sales, transfer. The plan's 10 tasks per domain are not reached for any single domain, so per-domain rates are small-sample; the suite cannot claim capability beyond these tasks.
* Synthetic data with planted effects gives known truth; it is not a claim about production data. Planted effects are sized above the materiality thresholds.
* The component tier has no model in its path; the platform tier ran with a live provider.
* Infrastructure cost is reported as CPU and wall seconds; it is priced only when `--cpu-usd-per-hour` is given.
* The paired practitioner baseline (`docs/60-delivery/06-practitioner-baseline-protocol.md`) was **not run**: it needs qualified human analysts and a blinded scorer. No human-effort or time-saving claim follows from this report.
