# Ask accuracy benchmark (P4-V02) — tier `live` — 2026-09-30

Generated 2026-09-30 03:11 UTC by `scripts/benchmark_ask.py --models live` at `f1d6c36` (Python 3.13.5), seed 1, 728.6 s.

Tier: **live** — the configured provider (config/models.yaml); decisions use their configured backends.

Windows 11 host, compose stack (postgres, redis) up; router requests minimal reasoning for JSON purposes; fixes from the 2026-09-28 live run applied (placeholder/write refusals, model cannot_answer, percentage scale)

Live probe (one model-only question before the set): `{"question": "itsm-a22", "status": "answer", "refusal_kind": null, "model_calls": 1, "note": null}`.

Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain (the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.

| Domain | table | rows | verified queries | setup s |
|---|---|---|---|---|
| itsm | `src_src_fd3be214242a.incident` | 6000 | 9 | 1.6 |
| sales | `src_src_baf374d8d5b0.orders` | 8000 | 9 | 0.9 |
| finance | `src_src_798221cd82a6.ap_invoice` | 6000 | 9 | 1.2 |

## Results

| Measure | Overall | itsm | sales | finance |
|---|---|---|---|---|
| questions (answer / needs_input / clarify / decline) | 105 / 15 / 15 / 27 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 |
| **execution accuracy** | **0.962** (101/105) | **1.000** (35/35) | **0.914** (32/35) | **0.971** (34/35) |
| answer items answered (coverage) | 103/105 | 35/35 | 34/35 | 34/35 |
| precision of everything answered | 0.981 | 1.000 | 0.941 | 1.000 |
| **confident wrong** (answered, not right) | **2** (0.012) | **0** (0.000) | **2** (0.037) | **0** (0.000) |
| outcome accuracy (all items) | 0.975 | 1.000 | 0.944 | 0.982 |
| needs_input: precision / recall | 0.938 / 1.000 (15/15, predicted 16) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) | 0.833 / 1.000 (5/5, predicted 6) |
| clarify: precision / recall | 1.000 / 1.000 (15/15, predicted 15) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) |
| decline: precision / recall | 0.964 / 1.000 (27/27, predicted 28) | 1.000 / 1.000 (9/9, predicted 9) | 0.900 / 1.000 (9/9, predicted 10) | 1.000 / 1.000 (9/9, predicted 9) |
| decline out_of_scope: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| decline write: declined / governed of n | 3 / 0 of 3 | 1 / 0 of 1 | 1 / 0 of 1 | 1 / 0 of 1 |
| decline restricted: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| restricted answers (leaks) | 0 | 0 | 0 | 0 |
| latency p50 / p95 ms | 186 / 18605 | 152 / 17261 | 1940 / 23267 | 186 / 11398 |
| provider calls (per question) | 82 (0.506) | 25 (0.463) | 33 (0.611) | 24 (0.444) |
| tokens in / out (per question) | 66377 / 25767 (568.790) | 21580 / 8044 (548.593) | 27641 / 9837 (694.037) | 17156 / 7886 (463.741) |
| model cost USD | 0.0193 | 0.0068 | 0.0070 | 0.0055 |
| tokens avoided (registry and rules skips) | 135080 | 44765 | 44563 | 45752 |

Answered by: model: 38, registry: 52, rules: 13.
Refusal kinds seen: clarify: 15, invalid_output: 1, needs_input: 16, not_answerable: 27.

### Answer items by kind of wording

| tag | n | matched | accuracy | how they ended |
|---|---|---|---|---|
| near_miss | 11 | 10 | 0.909 | model: 6, needs_input: 1, rules: 4 |
| novel | 33 | 31 | 0.939 | model: 26, rules: 7 |
| parameter | 18 | 18 | 1.000 | registry: 18 |
| paraphrase | 27 | 26 | 0.963 | decline: 1, model: 6, registry: 18, rules: 2 |
| registry_exact | 21 | 21 | 1.000 | registry: 21 |

### Confident wrong answers

* `sales-a29` (answer) "What is the average net amount of returned orders versus kept orders?" — answered by model (no answer column matches gold column 0)
* `sales-a31` (answer) "How many orders had a discount of 20% or more?" — answered by model (no answer column matches gold column 0)

### Every question

| id | expect | actual | kind | by | match | ms | calls |
|---|---|---|---|---|---|---|---|
| itsm-a22 | answer | answer |  | model | yes | 12076 | 1 |
| itsm-a01 | answer | answer |  | registry `incidents_by_priority` | yes | 96 | 0 |
| itsm-a02 | answer | answer |  | registry `incidents_by_priority` | yes | 98 | 0 |
| itsm-a03 | answer | answer |  | registry `incidents_by_priority` | yes | 85 | 0 |
| itsm-a04 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 98 | 0 |
| itsm-a05 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 197 | 0 |
| itsm-a06 | answer | answer |  | model | yes | 17698 | 1 |
| itsm-a07 | answer | answer |  | registry `resolution_hours_by_group` | yes | 97 | 0 |
| itsm-a08 | answer | answer |  | model | yes | 36919 | 1 |
| itsm-a09 | answer | answer |  | registry `incidents_for_priority` | yes | 99 | 0 |
| itsm-a10 | answer | answer |  | registry `incidents_for_priority` | yes | 82 | 0 |
| itsm-a11 | answer | answer |  | registry `incidents_for_group` | yes | 91 | 0 |
| itsm-a12 | answer | answer |  | registry `incidents_for_group` | yes | 88 | 0 |
| itsm-a13 | answer | answer |  | registry `incidents_per_month` | yes | 92 | 0 |
| itsm-a14 | answer | answer |  | registry `incidents_per_month` | yes | 85 | 0 |
| itsm-a15 | answer | answer |  | rules | yes | 88 | 0 |
| itsm-a16 | answer | answer |  | registry `top_ci_priority_1` | yes | 138 | 0 |
| itsm-a17 | answer | answer |  | model | yes | 3503 | 1 |
| itsm-a18 | answer | answer |  | registry `incidents_opened_between` | yes | 166 | 0 |
| itsm-a19 | answer | answer |  | registry `incidents_opened_between` | yes | 88 | 0 |
| itsm-a20 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 99 | 0 |
| itsm-a21 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 80 | 0 |
| itsm-a23 | answer | answer |  | model | yes | 8148 | 1 |
| itsm-a24 | answer | answer |  | rules | yes | 96 | 0 |
| itsm-a25 | answer | answer |  | rules | yes | 91 | 0 |
| itsm-a26 | answer | answer |  | rules | yes | 91 | 0 |
| itsm-a27 | answer | answer |  | model | yes | 5138 | 1 |
| itsm-a28 | answer | answer |  | model | yes | 10919 | 1 |
| itsm-a29 | answer | answer |  | model | yes | 6221 | 1 |
| itsm-a30 | answer | answer |  | model | yes | 7320 | 1 |
| itsm-a31 | answer | answer |  | rules | yes | 101 | 0 |
| itsm-a32 | answer | answer |  | model | yes | 9136 | 1 |
| itsm-a33 | answer | answer |  | model | yes | 19130 | 1 |
| itsm-a34 | answer | answer |  | model | yes | 17026 | 1 |
| itsm-a35 | answer | answer |  | model | yes | 16006 | 1 |
| itsm-n01 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 76 | 0 |
| itsm-n02 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 69 | 0 |
| itsm-n03 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 95 | 0 |
| itsm-n04 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 62 | 0 |
| itsm-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 4781 | 1 |
| itsm-c01 | clarify/ambiguous | clarify | clarify |  |  | 65 | 0 |
| itsm-c02 | clarify/ambiguous | clarify | clarify |  |  | 4347 | 1 |
| itsm-c03 | clarify/ambiguous | clarify | clarify |  |  | 3269 | 1 |
| itsm-c04 | clarify/ambiguous | clarify | clarify |  |  | 2956 | 1 |
| itsm-c05 | clarify/ambiguous | clarify | clarify |  |  | 63 | 0 |
| itsm-o01 | decline/out_of_scope | decline | not_answerable |  |  | 2862 | 1 |
| itsm-o02 | decline/out_of_scope | decline | not_answerable |  |  | 7765 | 1 |
| itsm-o03 | decline/out_of_scope | decline | not_answerable |  |  | 5354 | 1 |
| itsm-o04 | decline/write | decline | not_answerable |  |  | 58 | 0 |
| itsm-o05 | decline/out_of_scope | decline | not_answerable |  |  | 3582 | 1 |
| itsm-r01 | decline/restricted | decline | not_answerable |  |  | 7142 | 1 |
| itsm-r02 | decline/restricted | decline | not_answerable |  |  | 1300 | 1 |
| itsm-r03 | decline/restricted | decline | not_answerable |  |  | 8339 | 1 |
| itsm-r04 | decline/restricted | decline | not_answerable |  |  | 5411 | 1 |
| sales-a01 | answer | answer |  | registry `revenue_by_region` | yes | 127 | 0 |
| sales-a02 | answer | answer |  | registry `revenue_by_region` | yes | 118 | 0 |
| sales-a03 | answer | answer |  | model | yes | 9133 | 1 |
| sales-a04 | answer | answer |  | registry `return_rate_by_channel` | yes | 115 | 0 |
| sales-a05 | answer | answer |  | registry `return_rate_by_channel` | yes | 149 | 0 |
| sales-a06 | answer | decline | invalid_output |  |  | 50791 | 3 |
| sales-a07 | answer | answer |  | registry `orders_by_segment` | yes | 107 | 0 |
| sales-a08 | answer | answer |  | registry `orders_by_segment` | yes | 99 | 0 |
| sales-a09 | answer | answer |  | registry `order_value_by_segment` | yes | 95 | 0 |
| sales-a10 | answer | answer |  | rules | yes | 106 | 0 |
| sales-a11 | answer | answer |  | registry `revenue_for_region` | yes | 98 | 0 |
| sales-a12 | answer | answer |  | registry `revenue_for_region` | yes | 110 | 0 |
| sales-a13 | answer | answer |  | registry `monthly_revenue` | yes | 98 | 0 |
| sales-a14 | answer | answer |  | registry `monthly_revenue` | yes | 93 | 0 |
| sales-a15 | answer | answer |  | registry `monthly_revenue` | yes | 104 | 0 |
| sales-a16 | answer | answer |  | registry `orders_for_channel` | yes | 108 | 0 |
| sales-a17 | answer | answer |  | registry `orders_for_channel` | yes | 120 | 0 |
| sales-a18 | answer | answer |  | registry `revenue_since` | yes | 102 | 0 |
| sales-a19 | answer | answer |  | registry `revenue_since` | yes | 129 | 0 |
| sales-a20 | answer | answer |  | registry `shipping_days_by_region` | yes | 102 | 0 |
| sales-a21 | answer | answer |  | model | yes | 23624 | 2 |
| sales-a22 | answer | answer |  | model | yes | 10984 | 1 |
| sales-a23 | answer | answer |  | model | yes | 23075 | 2 |
| sales-a24 | answer | answer |  | model | yes | 14533 | 1 |
| sales-a25 | answer | answer |  | rules | yes | 183 | 0 |
| sales-a26 | answer | answer |  | rules | yes | 142 | 0 |
| sales-a27 | answer | answer |  | model | yes | 31233 | 1 |
| sales-a28 | answer | answer |  | model | yes | 20127 | 2 |
| sales-a29 | answer | answer |  | model | no: no answer column matches gold column 0 | 9360 | 1 |
| sales-a30 | answer | answer |  | model | yes | 10700 | 1 |
| sales-a31 | answer | answer |  | model | no: no answer column matches gold column 0 | 7334 | 1 |
| sales-a32 | answer | answer |  | model | yes | 9323 | 1 |
| sales-a33 | answer | answer |  | model | yes | 10862 | 1 |
| sales-a34 | answer | answer |  | model | yes | 10639 | 1 |
| sales-a35 | answer | answer |  | model | yes | 18653 | 1 |
| sales-n01 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_for_region` |  | 116 | 0 |
| sales-n02 | needs_input/missing_parameter | needs_input | needs_input |  `orders_for_channel` |  | 110 | 0 |
| sales-n03 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 95 | 0 |
| sales-n04 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 101 | 0 |
| sales-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 8003 | 1 |
| sales-c01 | clarify/ambiguous | clarify | clarify |  |  | 115 | 0 |
| sales-c02 | clarify/ambiguous | clarify | clarify |  |  | 2098 | 1 |
| sales-c03 | clarify/ambiguous | clarify | clarify |  |  | 1894 | 1 |
| sales-c04 | clarify/ambiguous | clarify | clarify |  |  | 1986 | 1 |
| sales-c05 | clarify/ambiguous | clarify | clarify |  |  | 2057 | 1 |
| sales-o01 | decline/out_of_scope | decline | not_answerable |  |  | 4576 | 1 |
| sales-o02 | decline/out_of_scope | decline | not_answerable |  |  | 5783 | 1 |
| sales-o03 | decline/write | decline | not_answerable |  |  | 77 | 0 |
| sales-o04 | decline/out_of_scope | decline | not_answerable |  |  | 6207 | 1 |
| sales-o05 | decline/out_of_scope | decline | not_answerable |  |  | 2514 | 1 |
| sales-r01 | decline/restricted | decline | not_answerable |  |  | 4793 | 1 |
| sales-r02 | decline/restricted | decline | not_answerable |  |  | 6057 | 1 |
| sales-r03 | decline/restricted | decline | not_answerable |  |  | 5648 | 1 |
| sales-r04 | decline/restricted | decline | not_answerable |  |  | 6017 | 1 |
| finance-a01 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 187 | 0 |
| finance-a02 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 157 | 0 |
| finance-a03 | answer | answer |  | model | yes | 7656 | 1 |
| finance-a04 | answer | answer |  | registry `late_rate_by_unit` | yes | 170 | 0 |
| finance-a05 | answer | answer |  | model | yes | 6533 | 1 |
| finance-a06 | answer | answer |  | registry `late_rate_by_unit` | yes | 145 | 0 |
| finance-a07 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 156 | 0 |
| finance-a08 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 180 | 0 |
| finance-a09 | answer | answer |  | registry `invoices_by_currency` | yes | 173 | 0 |
| finance-a10 | answer | answer |  | registry `invoices_by_currency` | yes | 161 | 0 |
| finance-a11 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 202 | 0 |
| finance-a12 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 173 | 0 |
| finance-a13 | answer | answer |  | registry `monthly_invoice_amount` | yes | 148 | 0 |
| finance-a14 | answer | answer |  | registry `monthly_invoice_amount` | yes | 161 | 0 |
| finance-a15 | answer | answer |  | registry `invoices_over_amount` | yes | 270 | 0 |
| finance-a16 | answer | answer |  | registry `invoices_over_amount` | yes | 179 | 0 |
| finance-a17 | answer | answer |  | registry `late_invoices_for_category` | yes | 164 | 0 |
| finance-a18 | answer | answer |  | registry `late_invoices_for_category` | yes | 167 | 0 |
| finance-a19 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 184 | 0 |
| finance-a20 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 176 | 0 |
| finance-a21 | answer | answer |  | model | yes | 11578 | 1 |
| finance-a22 | answer | answer |  | rules | yes | 138 | 0 |
| finance-a23 | answer | answer |  | model | yes | 10738 | 1 |
| finance-a24 | answer | needs_input | needs_input |  `late_invoices_for_category` |  | 49 | 0 |
| finance-a25 | answer | answer |  | rules | yes | 72 | 0 |
| finance-a26 | answer | answer |  | rules | yes | 80 | 0 |
| finance-a27 | answer | answer |  | model | yes | 18739 | 1 |
| finance-a28 | answer | answer |  | model | yes | 4039 | 1 |
| finance-a29 | answer | answer |  | rules | yes | 77 | 0 |
| finance-a30 | answer | answer |  | model | yes | 2876 | 1 |
| finance-a31 | answer | answer |  | model | yes | 11301 | 1 |
| finance-a32 | answer | answer |  | model | yes | 2980 | 1 |
| finance-a33 | answer | answer |  | model | yes | 5825 | 1 |
| finance-a34 | answer | answer |  | rules | yes | 101 | 0 |
| finance-a35 | answer | answer |  | model | yes | 4308 | 1 |
| finance-n01 | needs_input/missing_parameter | needs_input | needs_input |  `invoice_amount_for_unit` |  | 61 | 0 |
| finance-n02 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 70 | 0 |
| finance-n03 | needs_input/missing_parameter | needs_input | needs_input |  `late_invoices_for_category` |  | 67 | 0 |
| finance-n04 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 58 | 0 |
| finance-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 5653 | 1 |
| finance-c01 | clarify/ambiguous | clarify | clarify |  |  | 48 | 0 |
| finance-c02 | clarify/ambiguous | clarify | clarify |  |  | 5562 | 1 |
| finance-c03 | clarify/ambiguous | clarify | clarify |  |  | 13866 | 1 |
| finance-c04 | clarify/ambiguous | clarify | clarify |  |  | 2920 | 1 |
| finance-c05 | clarify/ambiguous | clarify | clarify |  |  | 4149 | 1 |
| finance-o01 | decline/out_of_scope | decline | not_answerable |  |  | 2932 | 1 |
| finance-o02 | decline/out_of_scope | decline | not_answerable |  |  | 5764 | 1 |
| finance-o03 | decline/write | decline | not_answerable |  |  | 51 | 0 |
| finance-o04 | decline/out_of_scope | decline | not_answerable |  |  | 5979 | 1 |
| finance-o05 | decline/out_of_scope | decline | not_answerable |  |  | 4727 | 1 |
| finance-r01 | decline/restricted | decline | not_answerable |  |  | 2076 | 1 |
| finance-r02 | decline/restricted | decline | not_answerable |  |  | 3070 | 1 |
| finance-r03 | decline/restricted | decline | not_answerable |  |  | 5597 | 1 |
| finance-r04 | decline/restricted | decline | not_answerable |  |  | 5854 | 1 |

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
