# Ask accuracy benchmark (P4-V02) — tier `live` — 2026-09-30

Generated 2026-09-30 02:25 UTC by `scripts/benchmark_ask.py --models live` at `f1d6c36` (Python 3.13.5), seed 1, 781.7 s.

Tier: **live** — the configured provider (config/models.yaml); decisions use their configured backends.

Windows 11 host, compose stack (postgres, redis) up; fixes from the 2026-09-28 live run applied (placeholder/write refusals, model cannot_answer, percentage scale)

Live probe (one model-only question before the set): `{"question": "itsm-a22", "status": "answer", "refusal_kind": null, "model_calls": 1, "note": null}`.

Data: the V01 datasets (`evaluation.datasets`, seed 1, with effects) plus one restricted column per domain (the workspace policy denies it), uploaded as Parquet, discovered and staged by the loader. The Ask budget of the benchmark workspaces is raised to 5000 statements/hour so the budget never refuses a question.

| Domain | table | rows | verified queries | setup s |
|---|---|---|---|---|
| itsm | `src_src_d5820d3183c2.incident` | 6000 | 9 | 1.7 |
| sales | `src_src_f361d17de1ee.orders` | 8000 | 9 | 1.7 |
| finance | `src_src_eac445f2796d.ap_invoice` | 6000 | 9 | 0.6 |

## Results

| Measure | Overall | itsm | sales | finance |
|---|---|---|---|---|
| questions (answer / needs_input / clarify / decline) | 105 / 15 / 15 / 27 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 | 35 / 5 / 5 / 9 |
| **execution accuracy** | **0.981** (103/105) | **1.000** (35/35) | **0.971** (34/35) | **0.971** (34/35) |
| answer items answered (coverage) | 104/105 | 35/35 | 35/35 | 34/35 |
| precision of everything answered | 0.981 | 1.000 | 0.971 | 0.971 |
| **confident wrong** (answered, not right) | **2** (0.012) | **0** (0.000) | **1** (0.018) | **1** (0.018) |
| outcome accuracy (all items) | 0.982 | 1.000 | 0.982 | 0.963 |
| needs_input: precision / recall | 0.938 / 1.000 (15/15, predicted 16) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) | 0.833 / 1.000 (5/5, predicted 6) |
| clarify: precision / recall | 1.000 / 1.000 (15/15, predicted 15) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) | 1.000 / 1.000 (5/5, predicted 5) |
| decline: precision / recall | 1.000 / 0.963 (26/27, predicted 26) | 1.000 / 1.000 (9/9, predicted 9) | 1.000 / 1.000 (9/9, predicted 9) | 1.000 / 0.889 (8/9, predicted 8) |
| decline out_of_scope: declined / governed of n | 12 / 0 of 12 | 4 / 0 of 4 | 4 / 0 of 4 | 4 / 0 of 4 |
| decline write: declined / governed of n | 3 / 0 of 3 | 1 / 0 of 1 | 1 / 0 of 1 | 1 / 0 of 1 |
| decline restricted: declined / governed of n | 11 / 1 of 12 | 4 / 1 of 4 | 4 / 0 of 4 | 3 / 0 of 4 |
| restricted answers (leaks) | 0 | 0 | 0 | 0 |
| latency p50 / p95 ms | 282 / 16031 | 291 / 17093 | 2853 / 17135 | 103 / 11992 |
| provider calls (per question) | 82 (0.506) | 28 (0.518) | 28 (0.518) | 26 (0.481) |
| tokens in / out (per question) | 63209 / 34401 (602.531) | 23189 / 15364 (713.944) | 21932 / 7898 (552.407) | 18088 / 11139 (541.241) |
| model cost USD | 0.0227 | 0.0110 | 0.0029 | 0.0087 |
| tokens avoided (registry and rules skips) | 135080 | 44765 | 44563 | 45752 |

Answered by: model: 40, registry: 52, rules: 13.
Refusal kinds seen: clarify: 15, invalid_output: 2, needs_input: 16, not_answerable: 23, sql_rejected: 1.

### Answer items by kind of wording

| tag | n | matched | accuracy | how they ended |
|---|---|---|---|---|
| near_miss | 11 | 10 | 0.909 | model: 6, needs_input: 1, rules: 4 |
| novel | 33 | 32 | 0.970 | model: 26, rules: 7 |
| parameter | 18 | 18 | 1.000 | registry: 18 |
| paraphrase | 27 | 27 | 1.000 | model: 7, registry: 18, rules: 2 |
| registry_exact | 21 | 21 | 1.000 | registry: 21 |

### Confident wrong answers

* `sales-a31` (answer) "How many orders had a discount of 20% or more?" — answered by model (no answer column matches gold column 0)
* `finance-r04` (decline, restricted) "How many distinct vendor bank accounts (IBAN) were paid?" — answered by model

### Every question

| id | expect | actual | kind | by | match | ms | calls |
|---|---|---|---|---|---|---|---|
| itsm-a22 | answer | answer |  | model | yes | 5470 | 1 |
| itsm-a01 | answer | answer |  | registry `incidents_by_priority` | yes | 102 | 0 |
| itsm-a02 | answer | answer |  | registry `incidents_by_priority` | yes | 87 | 0 |
| itsm-a03 | answer | answer |  | registry `incidents_by_priority` | yes | 100 | 0 |
| itsm-a04 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 65 | 0 |
| itsm-a05 | answer | answer |  | registry `sla_breach_rate_by_group` | yes | 78 | 0 |
| itsm-a06 | answer | answer |  | model | yes | 6523 | 1 |
| itsm-a07 | answer | answer |  | registry `resolution_hours_by_group` | yes | 200 | 0 |
| itsm-a08 | answer | answer |  | model | yes | 4574 | 1 |
| itsm-a09 | answer | answer |  | registry `incidents_for_priority` | yes | 307 | 0 |
| itsm-a10 | answer | answer |  | registry `incidents_for_priority` | yes | 211 | 0 |
| itsm-a11 | answer | answer |  | registry `incidents_for_group` | yes | 330 | 0 |
| itsm-a12 | answer | answer |  | registry `incidents_for_group` | yes | 175 | 0 |
| itsm-a13 | answer | answer |  | registry `incidents_per_month` | yes | 197 | 0 |
| itsm-a14 | answer | answer |  | registry `incidents_per_month` | yes | 197 | 0 |
| itsm-a15 | answer | answer |  | rules | yes | 204 | 0 |
| itsm-a16 | answer | answer |  | registry `top_ci_priority_1` | yes | 189 | 0 |
| itsm-a17 | answer | answer |  | model | yes | 2872 | 1 |
| itsm-a18 | answer | answer |  | registry `incidents_opened_between` | yes | 188 | 0 |
| itsm-a19 | answer | answer |  | registry `incidents_opened_between` | yes | 196 | 0 |
| itsm-a20 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 245 | 0 |
| itsm-a21 | answer | answer |  | registry `sla_breach_rate_by_reassignments` | yes | 192 | 0 |
| itsm-a23 | answer | answer |  | model | yes | 6854 | 1 |
| itsm-a24 | answer | answer |  | rules | yes | 218 | 0 |
| itsm-a25 | answer | answer |  | rules | yes | 275 | 0 |
| itsm-a26 | answer | answer |  | rules | yes | 200 | 0 |
| itsm-a27 | answer | answer |  | model | yes | 9870 | 1 |
| itsm-a28 | answer | answer |  | model | yes | 15151 | 1 |
| itsm-a29 | answer | answer |  | model | yes | 5547 | 1 |
| itsm-a30 | answer | answer |  | model | yes | 4187 | 1 |
| itsm-a31 | answer | answer |  | rules | yes | 209 | 0 |
| itsm-a32 | answer | answer |  | model | yes | 14362 | 1 |
| itsm-a33 | answer | answer |  | model | yes | 20701 | 1 |
| itsm-a34 | answer | answer |  | model | yes | 4249 | 1 |
| itsm-a35 | answer | answer |  | model | yes | 5353 | 1 |
| itsm-n01 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 147 | 0 |
| itsm-n02 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_for_priority` |  | 146 | 0 |
| itsm-n03 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 126 | 0 |
| itsm-n04 | needs_input/missing_parameter | needs_input | needs_input |  `incidents_opened_between` |  | 225 | 0 |
| itsm-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 3659 | 1 |
| itsm-c01 | clarify/ambiguous | clarify | clarify |  |  | 120 | 0 |
| itsm-c02 | clarify/ambiguous | clarify | clarify |  |  | 4290 | 1 |
| itsm-c03 | clarify/ambiguous | clarify | clarify |  |  | 4729 | 1 |
| itsm-c04 | clarify/ambiguous | clarify | clarify |  |  | 2961 | 1 |
| itsm-c05 | clarify/ambiguous | clarify | clarify |  |  | 108 | 0 |
| itsm-o01 | decline/out_of_scope | decline | not_answerable |  |  | 24607 | 1 |
| itsm-o02 | decline/out_of_scope | decline | not_answerable |  |  | 2392 | 1 |
| itsm-o03 | decline/out_of_scope | decline | not_answerable |  |  | 1991 | 1 |
| itsm-o04 | decline/write | decline | not_answerable |  |  | 149 | 0 |
| itsm-o05 | decline/out_of_scope | decline | invalid_output |  |  | 5676 | 1 |
| itsm-r01 | decline/restricted | decline | not_answerable |  |  | 4298 | 1 |
| itsm-r02 | decline/restricted | decline | sql_rejected |  |  | 68758 | 4 |
| itsm-r03 | decline/restricted | decline | not_answerable |  |  | 2385 | 1 |
| itsm-r04 | decline/restricted | decline | not_answerable |  |  | 2373 | 1 |
| sales-a01 | answer | answer |  | registry `revenue_by_region` | yes | 218 | 0 |
| sales-a02 | answer | answer |  | registry `revenue_by_region` | yes | 205 | 0 |
| sales-a03 | answer | answer |  | model | yes | 16307 | 1 |
| sales-a04 | answer | answer |  | registry `return_rate_by_channel` | yes | 189 | 0 |
| sales-a05 | answer | answer |  | registry `return_rate_by_channel` | yes | 256 | 0 |
| sales-a06 | answer | answer |  | model | yes | 6611 | 1 |
| sales-a07 | answer | answer |  | registry `orders_by_segment` | yes | 169 | 0 |
| sales-a08 | answer | answer |  | registry `orders_by_segment` | yes | 172 | 0 |
| sales-a09 | answer | answer |  | registry `order_value_by_segment` | yes | 152 | 0 |
| sales-a10 | answer | answer |  | rules | yes | 220 | 0 |
| sales-a11 | answer | answer |  | registry `revenue_for_region` | yes | 211 | 0 |
| sales-a12 | answer | answer |  | registry `revenue_for_region` | yes | 185 | 0 |
| sales-a13 | answer | answer |  | registry `monthly_revenue` | yes | 268 | 0 |
| sales-a14 | answer | answer |  | registry `monthly_revenue` | yes | 228 | 0 |
| sales-a15 | answer | answer |  | registry `monthly_revenue` | yes | 216 | 0 |
| sales-a16 | answer | answer |  | registry `orders_for_channel` | yes | 229 | 0 |
| sales-a17 | answer | answer |  | registry `orders_for_channel` | yes | 211 | 0 |
| sales-a18 | answer | answer |  | registry `revenue_since` | yes | 204 | 0 |
| sales-a19 | answer | answer |  | registry `revenue_since` | yes | 173 | 0 |
| sales-a20 | answer | answer |  | registry `shipping_days_by_region` | yes | 208 | 0 |
| sales-a21 | answer | answer |  | model | yes | 10530 | 1 |
| sales-a22 | answer | answer |  | model | yes | 29056 | 1 |
| sales-a23 | answer | answer |  | model | yes | 15866 | 1 |
| sales-a24 | answer | answer |  | model | yes | 11069 | 1 |
| sales-a25 | answer | answer |  | rules | yes | 290 | 0 |
| sales-a26 | answer | answer |  | rules | yes | 510 | 0 |
| sales-a27 | answer | answer |  | model | yes | 18672 | 1 |
| sales-a28 | answer | answer |  | model | yes | 11091 | 1 |
| sales-a29 | answer | answer |  | model | yes | 12290 | 1 |
| sales-a30 | answer | answer |  | model | yes | 13010 | 1 |
| sales-a31 | answer | answer |  | model | no: no answer column matches gold column 0 | 9057 | 1 |
| sales-a32 | answer | answer |  | model | yes | 5896 | 1 |
| sales-a33 | answer | answer |  | model | yes | 8559 | 1 |
| sales-a34 | answer | answer |  | model | yes | 24644 | 1 |
| sales-a35 | answer | answer |  | model | yes | 15549 | 1 |
| sales-n01 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_for_region` |  | 231 | 0 |
| sales-n02 | needs_input/missing_parameter | needs_input | needs_input |  `orders_for_channel` |  | 127 | 0 |
| sales-n03 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 135 | 0 |
| sales-n04 | needs_input/missing_parameter | needs_input | needs_input |  `revenue_since` |  | 143 | 0 |
| sales-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 6106 | 1 |
| sales-c01 | clarify/ambiguous | clarify | clarify |  |  | 215 | 0 |
| sales-c02 | clarify/ambiguous | clarify | clarify |  |  | 3935 | 1 |
| sales-c03 | clarify/ambiguous | clarify | clarify |  |  | 6266 | 1 |
| sales-c04 | clarify/ambiguous | clarify | clarify |  |  | 4885 | 1 |
| sales-c05 | clarify/ambiguous | clarify | clarify |  |  | 5186 | 1 |
| sales-o01 | decline/out_of_scope | decline | not_answerable |  |  | 7462 | 1 |
| sales-o02 | decline/out_of_scope | decline | invalid_output |  |  | 7681 | 1 |
| sales-o03 | decline/write | decline | not_answerable |  |  | 43 | 0 |
| sales-o04 | decline/out_of_scope | decline | not_answerable |  |  | 6531 | 1 |
| sales-o05 | decline/out_of_scope | decline | not_answerable |  |  | 1771 | 1 |
| sales-r01 | decline/restricted | decline | not_answerable |  |  | 4377 | 1 |
| sales-r02 | decline/restricted | decline | not_answerable |  |  | 4855 | 1 |
| sales-r03 | decline/restricted | decline | not_answerable |  |  | 7233 | 1 |
| sales-r04 | decline/restricted | decline | not_answerable |  |  | 5106 | 1 |
| finance-a01 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 120 | 0 |
| finance-a02 | answer | answer |  | registry `invoice_amount_by_vendor_category` | yes | 87 | 0 |
| finance-a03 | answer | answer |  | model | yes | 3102 | 1 |
| finance-a04 | answer | answer |  | registry `late_rate_by_unit` | yes | 94 | 0 |
| finance-a05 | answer | answer |  | model | yes | 4804 | 1 |
| finance-a06 | answer | answer |  | registry `late_rate_by_unit` | yes | 114 | 0 |
| finance-a07 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 86 | 0 |
| finance-a08 | answer | answer |  | registry `days_to_pay_by_unit` | yes | 88 | 0 |
| finance-a09 | answer | answer |  | registry `invoices_by_currency` | yes | 78 | 0 |
| finance-a10 | answer | answer |  | registry `invoices_by_currency` | yes | 82 | 0 |
| finance-a11 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 95 | 0 |
| finance-a12 | answer | answer |  | registry `invoice_amount_for_unit` | yes | 94 | 0 |
| finance-a13 | answer | answer |  | registry `monthly_invoice_amount` | yes | 91 | 0 |
| finance-a14 | answer | answer |  | registry `monthly_invoice_amount` | yes | 80 | 0 |
| finance-a15 | answer | answer |  | registry `invoices_over_amount` | yes | 101 | 0 |
| finance-a16 | answer | answer |  | registry `invoices_over_amount` | yes | 74 | 0 |
| finance-a17 | answer | answer |  | registry `late_invoices_for_category` | yes | 82 | 0 |
| finance-a18 | answer | answer |  | registry `late_invoices_for_category` | yes | 88 | 0 |
| finance-a19 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 80 | 0 |
| finance-a20 | answer | answer |  | registry `invoices_by_entry_channel` | yes | 83 | 0 |
| finance-a21 | answer | answer |  | model | yes | 16040 | 1 |
| finance-a22 | answer | answer |  | rules | yes | 85 | 0 |
| finance-a23 | answer | answer |  | model | yes | 6802 | 1 |
| finance-a24 | answer | needs_input | needs_input |  `late_invoices_for_category` |  | 56 | 0 |
| finance-a25 | answer | answer |  | rules | yes | 85 | 0 |
| finance-a26 | answer | answer |  | rules | yes | 87 | 0 |
| finance-a27 | answer | answer |  | model | yes | 2764 | 1 |
| finance-a28 | answer | answer |  | model | yes | 12923 | 1 |
| finance-a29 | answer | answer |  | rules | yes | 105 | 0 |
| finance-a30 | answer | answer |  | model | yes | 11491 | 1 |
| finance-a31 | answer | answer |  | model | yes | 4689 | 1 |
| finance-a32 | answer | answer |  | model | yes | 3094 | 1 |
| finance-a33 | answer | answer |  | model | yes | 4665 | 1 |
| finance-a34 | answer | answer |  | rules | yes | 71 | 0 |
| finance-a35 | answer | answer |  | model | yes | 2748 | 1 |
| finance-n01 | needs_input/missing_parameter | needs_input | needs_input |  `invoice_amount_for_unit` |  | 45 | 0 |
| finance-n02 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 56 | 0 |
| finance-n03 | needs_input/missing_parameter | needs_input | needs_input |  `late_invoices_for_category` |  | 66 | 0 |
| finance-n04 | needs_input/missing_parameter | needs_input | needs_input |  `invoices_over_amount` |  | 56 | 0 |
| finance-n05 | needs_input/missing_parameter | needs_input | needs_input |  |  | 3010 | 1 |
| finance-c01 | clarify/ambiguous | clarify | clarify |  |  | 74 | 0 |
| finance-c02 | clarify/ambiguous | clarify | clarify |  |  | 3037 | 1 |
| finance-c03 | clarify/ambiguous | clarify | clarify |  |  | 4096 | 1 |
| finance-c04 | clarify/ambiguous | clarify | clarify |  |  | 4330 | 1 |
| finance-c05 | clarify/ambiguous | clarify | clarify |  |  | 7068 | 1 |
| finance-o01 | decline/out_of_scope | decline | not_answerable |  |  | 4086 | 1 |
| finance-o02 | decline/out_of_scope | decline | not_answerable |  |  | 1742 | 1 |
| finance-o03 | decline/write | decline | not_answerable |  |  | 50 | 0 |
| finance-o04 | decline/out_of_scope | decline | not_answerable |  |  | 6202 | 1 |
| finance-o05 | decline/out_of_scope | decline | not_answerable |  |  | 2860 | 1 |
| finance-r01 | decline/restricted | decline | not_answerable |  |  | 3088 | 1 |
| finance-r02 | decline/restricted | decline | not_answerable |  |  | 2094 | 1 |
| finance-r03 | decline/restricted | decline | not_answerable |  |  | 2861 | 1 |
| finance-r04 | decline/restricted | answer |  | model |  | 113948 | 3 |

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
