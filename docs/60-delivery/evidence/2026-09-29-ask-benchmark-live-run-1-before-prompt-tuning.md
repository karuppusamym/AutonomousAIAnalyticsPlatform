# Ask accuracy benchmark (P4-V02) — tier `live` — 2026-09-30

Generated 2026-09-30 02:09 UTC by `scripts/benchmark_ask.py --models live` at `f1d6c36` (Python 3.13.5), seed 1, 453.8 s.

Tier: **live** — the configured provider (config/models.yaml); decisions use their configured backends.

Windows 11 host, compose stack (postgres, redis) up; fixes from the 2026-09-28 live run applied (placeholder/write refusals, model cannot_answer)

Live probe (one model-only question before the set): `{"question": "itsm-a22", "status": "answer", "refusal_kind": null, "model_calls": 1, "note": null}`.

Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain (the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.

| Domain | table | rows | verified queries | setup s |
|---|---|---|---|---|
| itsm | `src_src_8f497d3e9d9f.incident` | 6000 | 9 | 3.9 |
| sales | `src_src_eff02a1efdeb.orders` | 8000 | 9 | 0.7 |
| finance | `src_src_f42c36c525ef.ap_invoice` | 6000 | 9 | 0.6 |

## Results

| Measure | Overall | itsm | sales | finance |
|---|---|---|---|---|
| questions (answer / needs_input / clarify / decline) | 105 / 15 / 15 / 27 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 |
| **execution accuracy** | **0.962** (101/105) | **0.971** (34/35) | **1.000** (35/35) | **0.914** (32/35) |
| answer items answered (coverage) | 104/105 | 35/35 | 35/35 | 34/35 |
| precision of everything answered | 0.944 | 0.919 | 1.000 | 0.914 |
| **confident wrong** (answered, not right) | **6** (0.037) | **3** (0.056) | **0** (0.000) | **3** (0.056) |
| outcome accuracy (all items) | 0.957 | 0.944 | 1.000 | 0.926 |
| needs_input: precision / recall | 0.929 / 0.867 (13/15, predicted 14) | 1.000 / 0.800 (4/5, predicted 4) | 1.000 / 1.000 (5/5, predicted 5) | 0.800 / 0.800 (4/5, predicted 5) |
| clarify: precision / recall | 1.000 / 0.933 (14/15, predicted 14) | 1.000 / 0.800 (4/5, predicted 4) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) |
| decline: precision / recall | 1.000 / 1.000 (27/27, predicted 27) | 1.000 / 1.000 (9/9, predicted 9) | 1.000 / 1.000 (9/9, predicted 9) | 1.000 / 1.000 (9/9, predicted 9) |
| decline out_of_scope: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| decline write: declined / governed of n | 3 / 0 of 3 | 1 / 0 of 1 | 1 / 0 of 1 | 1 / 0 of 1 |
| decline restricted: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| restricted answers (leaks) | 0 | 0 | 0 | 0 |
| latency p50 / p95 ms | 171 / 9827 | 201 / 14143 | 1964 / 4847 | 109 / 10385 |
| provider calls (per question) | 78 (0.481) | 25 (0.463) | 28 (0.518) | 25 (0.463) |
| tokens in / out (per question) | 56767 / 21864 (485.377) | 19016 / 6872 (479.407) | 21898 / 5972 (516.111) | 15853 / 9020 (460.611) |
| model cost USD | 0.0161 | 0.0044 | 0.0026 | 0.0092 |
| tokens avoided (registry and rules skips) | 135080 | 44765 | 44563 | 45752 |

Answered by: model: 42, registry: 52, rules: 13.
Refusal kinds seen: clarify: 14, needs_input: 14, not_answerable: 27.

### Answer items by kind of wording

| tag | n | matched | accuracy | how they ended |
|---|---|---|---|---|
| near_miss | 11 | 9 | 0.818 | model: 6, needs_input: 1, rules: 4 |
| novel | 33 | 31 | 0.939 | model: 26, rules: 7 |
| parameter | 18 | 18 | 1.000 | registry: 18 |
| paraphrase | 27 | 27 | 1.000 | model: 7, registry: 18, rules: 2 |
| registry_exact | 21 | 21 | 1.000 | registry: 21 |

### Confident wrong answers

* `itsm-a27` (answer) "What percentage of incidents are priority 1?" — answered by model (no answer column matches gold column 0)
* `itsm-n05` (needs_input, missing_parameter) "How many incidents does one specific assignment group have?" — answered by model
* `itsm-c03` (clarify, ambiguous) "Is it getting better?" — answered by model
* `finance-a21` (answer) "Total invoice amount for Retail in USD" — answered by model (no answer column matches gold column 0)
* `finance-a30` (answer) "What percentage of invoices are in EUR?" — answered by model (no answer column matches gold column 0)
* `finance-n05` (needs_input, missing_parameter) "What did one business unit spend?" — answered by model

### Every question

| id | expect | actual | kind | by | match | ms | calls |
|---|---|---|---|---|---|---|---|
| itsm-a22 | answer | answer |  | model | yes | 7091 | 1 |
| itsm-a01 | answer | answer |  | registry `incidents_by_priority` | yes | 184 | 0 |
| itsm-a02 | answer | answer |  | registry `incidents_by_priority` | yes | 155 | 0 |
| itsm-a03 | answer | answer |  | registry `incidents_by_priority` | yes | 149 | 0 |
| itsm-a04 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 162 | 0 |
| itsm-a05 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 178 | 0 |
| itsm-a06 | answer | answer |  | model | yes | 30175 | 1 |
| itsm-a07 | answer | answer |  | registry `resolution_hours_by_group` | yes | 511 | 0 |
| itsm-a08 | answer | answer |  | model | yes | 9868 | 1 |
| itsm-a09 | answer | answer |  | registry `incidents_for_priority` | yes | 119 | 0 |
| itsm-a10 | answer | answer |  | registry `incidents_for_priority` | yes | 103 | 0 |
| itsm-a11 | answer | answer |  | registry `incidents_for_group` | yes | 218 | 0 |
| itsm-a12 | answer | answer |  | registry `incidents_for_group` | yes | 123 | 0 |
| itsm-a13 | answer | answer |  | registry `incidents_per_month` | yes | 98 | 0 |
| itsm-a14 | answer | answer |  | registry `incidents_per_month` | yes | 111 | 0 |
| itsm-a15 | answer | answer |  | rules | yes | 131 | 0 |
| itsm-a16 | answer | answer |  | registry `top_ci_priority_1` | yes | 124 | 0 |
| itsm-a17 | answer | answer |  | model | yes | 9042 | 1 |
| itsm-a18 | answer | answer |  | registry `incidents_opened_between` | yes | 116 | 0 |
| itsm-a19 | answer | answer |  | registry `incidents_opened_between` | yes | 87 | 0 |
| itsm-a20 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 102 | 0 |
| itsm-a21 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 91 | 0 |
| itsm-a23 | answer | answer |  | model | yes | 11406 | 1 |
| itsm-a24 | answer | answer |  | rules | yes | 107 | 0 |
| itsm-a25 | answer | answer |  | rules | yes | 91 | 0 |
| itsm-a26 | answer | answer |  | rules | yes | 90 | 0 |
| itsm-a27 | answer | answer |  | model | no: no answer column matches gold column 0 | 5598 | 1 |
| itsm-a28 | answer | answer |  | model | yes | 4125 | 1 |
| itsm-a29 | answer | answer |  | model | yes | 3314 | 1 |
| itsm-a30 | answer | answer |  | model | yes | 2846 | 1 |
| itsm-a31 | answer | answer |  | rules | yes | 108 | 0 |
| itsm-a32 | answer | answer |  | model | yes | 3495 | 1 |
| itsm-a33 | answer | answer |  | model | yes | 3750 | 1 |
| itsm-a34 | answer | answer |  | model | yes | 2654 | 1 |
| itsm-a35 | answer | answer |  | model | yes | 19227 | 1 |
| itsm-n01 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 66 | 0 |
| itsm-n02 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 61 | 0 |
| itsm-n03 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 58 | 0 |
| itsm-n04 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 60 | 0 |
| itsm-n05 | needs_input/missing_parameter | answer |  | model |  | 2675 | 1 |
| itsm-c01 | clarify/ambiguous | clarify | clarify |  |  | 68 | 0 |
| itsm-c02 | clarify/ambiguous | clarify | clarify |  |  | 4228 | 1 |
| itsm-c03 | clarify/ambiguous | answer |  | model |  | 7112 | 1 |
| itsm-c04 | clarify/ambiguous | clarify | clarify |  |  | 8460 | 1 |
| itsm-c05 | clarify/ambiguous | clarify | clarify |  |  | 58 | 0 |
| itsm-o01 | decline/out_of_scope | decline | not_answerable |  |  | 3184 | 1 |
| itsm-o02 | decline/out_of_scope | decline | not_answerable |  |  | 2187 | 1 |
| itsm-o03 | decline/out_of_scope | decline | not_answerable |  |  | 5824 | 1 |
| itsm-o04 | decline/write | decline | not_answerable |  |  | 50 | 0 |
| itsm-o05 | decline/out_of_scope | decline | not_answerable |  |  | 23689 | 1 |
| itsm-r01 | decline/restricted | decline | not_answerable |  |  | 2691 | 1 |
| itsm-r02 | decline/restricted | decline | not_answerable |  |  | 5834 | 1 |
| itsm-r03 | decline/restricted | decline | not_answerable |  |  | 2698 | 1 |
| itsm-r04 | decline/restricted | decline | not_answerable |  |  | 4247 | 1 |
| sales-a01 | answer | answer |  | registry `revenue_by_region` | yes | 119 | 0 |
| sales-a02 | answer | answer |  | registry `revenue_by_region` | yes | 85 | 0 |
| sales-a03 | answer | answer |  | model | yes | 3527 | 1 |
| sales-a04 | answer | answer |  | registry `return_rate_by_channel` | yes | 92 | 0 |
| sales-a05 | answer | answer |  | registry `return_rate_by_channel` | yes | 89 | 0 |
| sales-a06 | answer | answer |  | model | yes | 2918 | 1 |
| sales-a07 | answer | answer |  | registry `orders_by_segment` | yes | 106 | 0 |
| sales-a08 | answer | answer |  | registry `orders_by_segment` | yes | 108 | 0 |
| sales-a09 | answer | answer |  | registry `order_value_by_segment` | yes | 124 | 0 |
| sales-a10 | answer | answer |  | rules | yes | 164 | 0 |
| sales-a11 | answer | answer |  | registry `revenue_for_region` | yes | 116 | 0 |
| sales-a12 | answer | answer |  | registry `revenue_for_region` | yes | 105 | 0 |
| sales-a13 | answer | answer |  | registry `monthly_revenue` | yes | 105 | 0 |
| sales-a14 | answer | answer |  | registry `monthly_revenue` | yes | 112 | 0 |
| sales-a15 | answer | answer |  | registry `monthly_revenue` | yes | 106 | 0 |
| sales-a16 | answer | answer |  | registry `orders_for_channel` | yes | 95 | 0 |
| sales-a17 | answer | answer |  | registry `orders_for_channel` | yes | 117 | 0 |
| sales-a18 | answer | answer |  | registry `revenue_since` | yes | 111 | 0 |
| sales-a19 | answer | answer |  | registry `revenue_since` | yes | 96 | 0 |
| sales-a20 | answer | answer |  | registry `shipping_days_by_region` | yes | 107 | 0 |
| sales-a21 | answer | answer |  | model | yes | 3328 | 1 |
| sales-a22 | answer | answer |  | model | yes | 4521 | 1 |
| sales-a23 | answer | answer |  | model | yes | 2232 | 1 |
| sales-a24 | answer | answer |  | model | yes | 2285 | 1 |
| sales-a25 | answer | answer |  | rules | yes | 100 | 0 |
| sales-a26 | answer | answer |  | rules | yes | 99 | 0 |
| sales-a27 | answer | answer |  | model | yes | 3238 | 1 |
| sales-a28 | answer | answer |  | model | yes | 1975 | 1 |
| sales-a29 | answer | answer |  | model | yes | 3350 | 1 |
| sales-a30 | answer | answer |  | model | yes | 2699 | 1 |
| sales-a31 | answer | answer |  | model | yes | 2133 | 1 |
| sales-a32 | answer | answer |  | model | yes | 2919 | 1 |
| sales-a33 | answer | answer |  | model | yes | 2575 | 1 |
| sales-a34 | answer | answer |  | model | yes | 3571 | 1 |
| sales-a35 | answer | answer |  | model | yes | 3605 | 1 |
| sales-n01 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_for_region` |  | 59 | 0 |
| sales-n02 | needs_input/missing_parameter | needs_input | needs_input |  `orders_for_channel` |  | 80 | 0 |
| sales-n03 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 61 | 0 |
| sales-n04 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 58 | 0 |
| sales-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 2324 | 1 |
| sales-c01 | clarify/ambiguous | clarify | clarify |  |  | 70 | 0 |
| sales-c02 | clarify/ambiguous | clarify | clarify |  |  | 4316 | 1 |
| sales-c03 | clarify/ambiguous | clarify | clarify |  |  | 7082 | 1 |
| sales-c04 | clarify/ambiguous | clarify | clarify |  |  | 5507 | 1 |
| sales-c05 | clarify/ambiguous | clarify | clarify |  |  | 2633 | 1 |
| sales-o01 | decline/out_of_scope | decline | not_answerable |  |  | 2267 | 1 |
| sales-o02 | decline/out_of_scope | decline | not_answerable |  |  | 1953 | 1 |
| sales-o03 | decline/write | decline | not_answerable |  |  | 57 | 0 |
| sales-o04 | decline/out_of_scope | decline | not_answerable |  |  | 2071 | 1 |
| sales-o05 | decline/out_of_scope | decline | not_answerable |  |  | 5452 | 1 |
| sales-r01 | decline/restricted | decline | not_answerable |  |  | 2243 | 1 |
| sales-r02 | decline/restricted | decline | not_answerable |  |  | 2046 | 1 |
| sales-r03 | decline/restricted | decline | not_answerable |  |  | 1997 | 1 |
| sales-r04 | decline/restricted | decline | not_answerable |  |  | 2444 | 1 |
| finance-a01 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 110 | 0 |
| finance-a02 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 81 | 0 |
| finance-a03 | answer | answer |  | model | yes | 10298 | 1 |
| finance-a04 | answer | answer |  | registry `late_rate_by_unit` | yes | 98 | 0 |
| finance-a05 | answer | answer |  | model | yes | 3218 | 1 |
| finance-a06 | answer | answer |  | registry `late_rate_by_unit` | yes | 95 | 0 |
| finance-a07 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 77 | 0 |
| finance-a08 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 79 | 0 |
| finance-a09 | answer | answer |  | registry `invoices_by_currency` | yes | 76 | 0 |
| finance-a10 | answer | answer |  | registry `invoices_by_currency` | yes | 79 | 0 |
| finance-a11 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 83 | 0 |
| finance-a12 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 85 | 0 |
| finance-a13 | answer | answer |  | registry `monthly_invoice_amount` | yes | 76 | 0 |
| finance-a14 | answer | answer |  | registry `monthly_invoice_amount` | yes | 74 | 0 |
| finance-a15 | answer | answer |  | registry `invoices_over_amount` | yes | 75 | 0 |
| finance-a16 | answer | answer |  | registry `invoices_over_amount` | yes | 72 | 0 |
| finance-a17 | answer | answer |  | registry `late_invoices_for_category` | yes | 74 | 0 |
| finance-a18 | answer | answer |  | registry `late_invoices_for_category` | yes | 103 | 0 |
| finance-a19 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 88 | 0 |
| finance-a20 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 86 | 0 |
| finance-a21 | answer | answer |  | model | no: no answer column matches gold column 0 | 26244 | 2 |
| finance-a22 | answer | answer |  | rules | yes | 101 | 0 |
| finance-a23 | answer | answer |  | model | yes | 12463 | 1 |
| finance-a24 | answer | needs_input | needs_input |  `late_invoices_for_category` |  | 84 | 0 |
| finance-a25 | answer | answer |  | rules | yes | 105 | 0 |
| finance-a26 | answer | answer |  | rules | yes | 108 | 0 |
| finance-a27 | answer | answer |  | model | yes | 10546 | 1 |
| finance-a28 | answer | answer |  | model | yes | 7215 | 1 |
| finance-a29 | answer | answer |  | rules | yes | 140 | 0 |
| finance-a30 | answer | answer |  | model | no: no answer column matches gold column 0 | 2830 | 1 |
| finance-a31 | answer | answer |  | model | yes | 7706 | 1 |
| finance-a32 | answer | answer |  | model | yes | 5489 | 1 |
| finance-a33 | answer | answer |  | model | yes | 3628 | 1 |
| finance-a34 | answer | answer |  | rules | yes | 127 | 0 |
| finance-a35 | answer | answer |  | model | yes | 4227 | 1 |
| finance-n01 | needs_input/missing_parameter | needs_input | needs_input |  `invoice_amount_for_unit` |  | 77 | 0 |
| finance-n02 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 75 | 0 |
| finance-n03 | needs_input/missing_parameter | needs_input | needs_input |  `late_invoices_for_category` |  | 79 | 0 |
| finance-n04 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 75 | 0 |
| finance-n05 | needs_input/missing_parameter | answer |  | model |  | 3090 | 1 |
| finance-c01 | clarify/ambiguous | clarify | clarify |  |  | 60 | 0 |
| finance-c02 | clarify/ambiguous | clarify | clarify |  |  | 7659 | 1 |
| finance-c03 | clarify/ambiguous | clarify | clarify |  |  | 4047 | 1 |
| finance-c04 | clarify/ambiguous | clarify | clarify |  |  | 4585 | 1 |
| finance-c05 | clarify/ambiguous | clarify | clarify |  |  | 4326 | 1 |
| finance-o01 | decline/out_of_scope | decline | not_answerable |  |  | 4483 | 1 |
| finance-o02 | decline/out_of_scope | decline | not_answerable |  |  | 4170 | 1 |
| finance-o03 | decline/write | decline | not_answerable |  |  | 65 | 0 |
| finance-o04 | decline/out_of_scope | decline | not_answerable |  |  | 4817 | 1 |
| finance-o05 | decline/out_of_scope | decline | not_answerable |  |  | 2379 | 1 |
| finance-r01 | decline/restricted | decline | not_answerable |  |  | 5053 | 1 |
| finance-r02 | decline/restricted | decline | not_answerable |  |  | 7825 | 1 |
| finance-r03 | decline/restricted | decline | not_answerable |  |  | 1831 | 1 |
| finance-r04 | decline/restricted | decline | not_answerable |  |  | 3561 | 1 |

## How to read this

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

## Pilot threshold — PROPOSED, to be set by the owner before the pilot

Placeholder values for the **live** tier; nothing here is enforced, and the owner fixes them (with the question set version) before the pilot starts, not after seeing a live run.

| Proposed gate | Value |
|---|---|
| execution_accuracy_min | 0.85 |
| execution_accuracy_per_domain_min | 0.8 |
| confident_wrong_max_share | 0.05 |
| restricted_leaks_max | 0 |
| governed_decline_recall_min | 1.0 |
| needs_input_recall_min | 0.8 |
| clarify_recall_min | 0.6 |

## Tier invariants

PASS — every invariant held (evaluation.ask.check).
