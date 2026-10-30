# PILOT-FIX-01 / PILOT-FIX-06 re-walk, 2026-10-30

Agent re-walk of cards C03 and C11 on the seeded demo data. **Rules only, no model call.** Branch
`claude/pilot-fix-investigate-aba52d`. API (local orchestrator) on :18391 over `analystos_pilotfix_inv`, a clone of the
rehearsal database `analystos_value05`. Analytics data is the shared `analytics` database, read through the gateway.
Reference values are direct read-only SQL.

## C03: guided "Explain an SLA increase", Q3 2026 vs previous period (`run_c76dec934e98`, 116 s, 0 step-ins)

* The run stored `constraints.period`: 2026 Q3 (2026-07-01..2026-09-30) vs 2026 Q2, with `incident.opened_at` as the period column.
  Every test read Q3 only; the comparison tests read Q2 and Q3. Before this fix the run read all history.
* Data facts stated: "the requested period 2026 Q3 is incomplete: the data ends on 2026-08-31 … (no rows in 2026-09)", and
  "5 rows are dated after today (2026-11-01 to 2027-01-08)". Windows hold 3,421 (Q3) and 5,029 (Q2) incidents. The answer key gives the same.
* Requested comparison, all rows: "Missed SLA was 14.2% in 2026 Q2 (5,029 records) and 13.5% in 2026 Q3 (3,421 records), a
  decrease; the run's test did not find the difference statistically significant (H-1)". Answer key: 14.2% to 13.5%, same.
  The within-group split by assignment group (H-2) was not significant either.
* Findings (PILOT-FIX-06):

| Finding | Sentence now (all rows) | Discovery figures (kept in evidence) | Direct SQL |
|---|---|---|---|
| I-2 reassignment 3+ | 25.4% vs 9.7% (count = 1), n 3,421 | 26.0% vs 9.3% (count = 0), n 2,372 | 25.4% (523) vs 9.7% (1,143) |
| I-1 after hours | median 52.3 h vs 32.9 h, n 3,320 | 52.0 vs 33.3, n 2,307 | 52.3 vs 32.9 (1,115 / 2,205) |
| I-3 Payments Gateway share of P1 | 35.9%, not confirmed; the sentence says it is discovery rows only | 35.9% | 33.6% (50 incidents) |

## C11: custom question in Retail (`run_e5059aad5b9a`, 83 s)

Meaning review passed on the owner definitions recorded in the rehearsal. Of four verified findings, two were confirmed on
the held-out rows and now quote all rows. One example: West late-delivery rate 14.8% vs South 6.8%, n 6,000 (discovery 15.0% vs 7.3%, n 4,200).
The other two end with the discovery-rows note. I did not check C11's numbers against SQL in this pass.

## Checks

* `pytest -m "not integration"`: all pass except `test_sse_stream.py::test_loop_stays_responsive_while_the_database_is_slow`.
  That is a 0.1 s latency limit (0.115 s measured while this re-walk ran); it passes on develop.
* ruff clean. Web: `tsc --noEmit` and `investigationValue` / `investigationOverview` tests pass.
* `scripts/eval_gates.py`: grounding, analytical_component, cost and heldout pass. `ml` fails with a Windows
  `BrokenProcessPool`, and fails the same way on unchanged develop 809a53e on this host.

Not shown: a run with models, and the guided source choice on a live two-source workspace (unit-tested only).
