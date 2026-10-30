# PILOT-FIX-04: C11 meaning step re-walked, 2026-10-30

**An agent re-walk on seeded data, not a pilot.** It replays card C11's meaning step through the new code with the
rehearsal's own recorded model answers. No analyst or owner took part, and owner minutes are estimated (see below).

## Setup

| | |
|---|---|
| Code | branch `claude/pilot-fix-meaning-601196` from develop `809a53e` |
| Data | `analystos_pilotfix_meaning`, a copy of the rehearsal database `analystos_value05` (migration 0075) with the five owner definitions C11 created removed, so the Retail workspace is as it was before the card |
| Question | the C11 objective of `run_a60345611fb2`: "Why are some orders returned, and are late deliveries part of the reason? Compare categories, sales channels and regions; say what the data cannot show." |
| Model | none called. The four `glossary_suggestion` answers recorded for `run_a60345611fb2` (qwen/qwen3-14b) were replayed in order; the re-walk used two of them. $0 |
| Path | a new run `run_a6213c83eefa`; the hypotheses step calls `services/context_grounding.resolve` (the production path) and waits on `MeaningRequired`; the owner (`admin@`) saves definitions through `POST /grounding/bindings`; the analyst rechecks through `POST /grounding/runs/{id}/recheck`, which records the round |
| Script | `rewalk_c11.py` in the session scratchpad (not committed); same flow as `tests/integration/test_meaning_review_rounds.py` |

## Result

| | Rehearsal (before) | Re-walk (after) |
|---|---|---|
| Meaning rounds before testing | 3 | **1** |
| Owner definitions | 5 (sales channel, region, shipping lead time, reasons for returns, category) | **1** (region) |
| Rechecks | 3 | **1** |
| Owner minutes | about 11 (measured) | **about 2, estimated** at the rehearsal's own pace (9.4 waiting minutes for 5 definitions, about 1.9 per definition); machine time 5.4 s |
| Missing return reasons | blocked the run until the owner wrote "not recorded in this data" | **recorded limitation**: "No field in the selected data records reasons. Findings can show what goes with the outcome, not the reasons themselves." |

Round 1 asked one question, all at once: the model's "Region names (geographic attribute has 4 distinct values but no
labels)". It resolved without asking: *Sales channel* (the retail glossary term, matched to the one field of that name),
*Region*, *Category* and *Returned* (catalog fields), *Return rate* (glossary, matched with model help). The model's
"Explicit reasons for returns (no 'return_reason' column)" became the limitation, not a question.

Round 2 asked nothing. The recorded answer raised *shipping lead time* and proposed a *return reason* metric with no
fields; the first was not caused by the owner's answer, so it is recorded as "not reviewed by an owner" and not asked; the
second is the same missing-reason limitation. The run went on with the owner's *Region* definition pinned
(`bind_2c855a518ee0` revision 1) and the round recorded on the run (`Meaning review round 1`, asked keys, binding
revisions, owner minutes). In the run's tenant scope, `meaning_review.finding_caveats` returns the limitation as
"Data limitation: …", which every finding of the run, and so every report built from it, carries.

## Why it converged

1. Plain fields the question names resolve from the catalog or the glossary, never by asking (rounds 1 and 3 of the
   rehearsal asked about sales channel, region and category).
2. A "why" field no selected table holds (reason, cause, satisfaction …) is a limitation; a model's claim that data is
   absent is accepted only when the catalog agrees.
3. A recheck asks only what is still open or what an answer changed; other new model questions are recorded, not asked.
4. Owner definitions now match the plural the question uses: before, `resolve_known` did not match an owner's
   "Category" or "Late delivery" to a question saying "categories" or "late deliveries", so such an answer could not
   close the question.

## What this does not show

Real owner time, a model call under the new prompt (`open_terms`), or the findings of the re-walked run (only the meaning
step ran). Late deliveries were not asked about here because the recorded answers never raised them; with the new prompt
a model is asked to list such derivable terms in round 1.
