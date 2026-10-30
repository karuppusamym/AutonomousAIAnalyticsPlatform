# ADR-016 owner decisions taken under delegation: implementation evidence, 2026-10-30

**An AI coding agent took these decisions under the owner's delegation of 2026-10-30**
([ADR-016](../../03-decisions/adr-log.md)). This file records what was built and checked. It is an agent re-walk,
not a pilot: no analyst took part, and no time saving or accuracy rate is claimed.

## Environment

| | |
|---|---|
| Code | branch `claude/owner-decisions-2026-10-30-72c1c0` from develop `6e7eff9`; Windows 11 dev host |
| Fresh-seed database | `analystos_adr016_fresh`, created empty on the compose Postgres (127.0.0.1:15432), `analystos migrate`, then `analystos seed` twice |
| Re-walk database | `analystos_adr016`, a clone of the rehearsal database `analystos_value05`, migrated to `0076_run_leases`. The shared `analystos` database was not changed |
| API | this branch's code, `uvicorn` on 127.0.0.1:18016, local orchestrator, analytics data in the shared `analytics` database read through the gateway |
| Models | **none**: no provider key in the process, so every answer below came from rules or approved definitions. Model cost $0 |

## N-14: the dev model policy is stored by `analystos seed`

* First `analystos seed` on the fresh database printed: "stored the dev model policy as platform settings v1: preset
  pilot_low_cost (deterministic first, low-cost open models only, $2/day cap; ADR-016)".
* `platform_setting` v1 holds:
  * note "preset pilot_low_cost: dev and pilot default (ADR-016, owner delegation 2026-10-30)";
  * `disabled_models` = gpt-5.4-mini, gpt-5.4, claude-sonnet-5, claude-opus-5, gemini-3.5-flash,
    gemini-3.5-flash-lite;
  * `daily_spend_cap_usd` 2.0;
  * mode `auto` for every purpose with a rules path.
* The second `analystos seed` wrote no new version.
* Tests:
  * `tests/unit/test_pilot_low_cost_preset.py`: enabled set, every model decided, blocked families, cap, prices;
  * `tests/integration/test_platform_settings.py::test_seed_stores_the_dev_default_once_and_never_overwrites_a_saved_choice`.

## N-13: JEV price

* OpenRouter `GET /api/v1/models`, read 2026-10-30 (456 models): `typesafe/jev-router` with pricing -1 (dynamic);
  no `typesafe/jev-1.13`.
* Today's list prices of the enabled low-cost models (USD per million tokens, in / out):

  | Model | In | Out |
  |---|---|---|
  | deepseek-v4-flash-0731 | 0.119 | 0.238 |
  | deepseek-v4-flash | 0.091 | 0.182 |
  | qwen3-14b | 0.12 | 0.24 |
  | qwen3-32b | 0.08 | 0.28 |
  | gpt-oss-120b | 0.15 | 0.60 |

* JEV's provisional price = gpt-oss-120b's, $0.15 / $0.60, flagged `price_provisional: true`.
* Test `test_a_priced_jev_passes_the_approval_threshold_instead_of_falling_back`: at a $0.01 threshold the decision
  route keeps `typesafe/jev-1.13` (before: `approval_required`, missing price).

## Approved metrics in the demo workspaces

`analystos demo-seed --only metrics --api http://127.0.0.1:18016`, first run:

| Workspace | Structure | Metrics approved (all v1) |
|---|---|---|
| ServiceNow Incident Intelligence (demo) | v5 proposed by admin@, approved by approver@ | Incident count, Mean resolution hours, SLA breach rate, Reassignment count, Open backlog |
| ServiceNow process mining (demo) | v3, same | Average wait hours, Handover rate, Case count, Mean case duration hours, Rework rate |
| Retail Orders (demo) | v5, same | Order count, Late Delivery Rate, Return rate, Net sales |

* Every seeded model and new metric is proposed by admin@ and decided by approver@ (checked in `semantic_model` and
  `semantic_metric`).
* Late Delivery Rate (Retail), Average wait hours and Handover rate (process mining) were already proposed or
  approved by earlier runs with the same expression. The seed reused those rows, and its log says so ("the run's
  definition on aos_…").
* The second and third runs created nothing: "exists" for each structure, the same metric versions.
* Pilot readiness `conversation.definitions` (owner actions in the rehearsal) now passes in all three workspaces:
  5, 5 and 8 approved metrics.

## C06 re-walk: Ask answers from the approved metric

The question, asked as analyst@ in Retail: "What is our late delivery rate by sales channel? Put it on a dashboard I
can share later."

* `answered_by = semantic`, semantic model version 5.
* Explanation: "Calculated from the approved metric definition Late Delivery Rate (v1)".
* 0 model calls, $0; 2 calls avoided.
* Rows: Marketplace 9.95%, Store 9.92%, Web 8.59%. These match the answer key.
* **Add to dashboard** (promote → draft) created `art_b7e6de7982ed` with the answer's bar chart.
* Card criteria met: the rate matches the approved definition, and the chart carries the answer's numbers.

## C04 re-walk: dashboard draft from the approved incident metrics

* Ask: the C04 card text. The draft `art_f62717d90236` came back in 2.7 s from rules.
* The four KPIs are approved metrics:

  | KPI | Value | Answer key |
  |---|---|---|
  | Incident count | 20,000 | 20,000 |
  | SLA breach rate | 14.4% | 85.6% met |
  | Mean resolution hours | 53.6 | (overall, not in the key) |
  | Reassignment count | 25,754 | |

* Charts: each metric per month, plus mean resolution hours, SLA breach rate and reassignments by priority.
* Mean resolution hours by priority: P1 5.5, P2 13.9, P3 37.0, P4 71.8, P5 112.8. These match the answer key.
* Compared with the rehearsal ("Average priority" KPI, team-target tiles, no SLA tile), every tile is now about
  incidents, and SLA and resolution time are approved definitions.

Refinements, still rules only:

| Request | Result |
|---|---|
| "add average resolution time in hours by priority" | Refused: needs SQL generation, and no model key was set. The words "in hours" were not covered by the metric's names. The spec now carries the synonym "average resolution time in hours" (unit test). It is not on this clone: the seed reuses a metric with an unchanged expression. The chart "Mean resolution hours by priority" was already on the draft |
| "add a filter for assignment group" | No change: "no chart has a field 'assignment group'". The draft made a priority filter, not the one the ask named, and no "busiest teams" chart |
| "add incident count by assignment group", then the filter again | Applied (v2, v3), from approved metric tiles. The filter scopes only the chart that has the field (PILOT-FIX-03) |

Remaining C04 gaps (dashboard draft planning, not this change): "busiest teams" is not mapped to assignment group,
and the draft's filter control ignores the field the ask names.

## Checks

The fast suite, ruff, lint-imports and the web checks are reported in the PR body.
