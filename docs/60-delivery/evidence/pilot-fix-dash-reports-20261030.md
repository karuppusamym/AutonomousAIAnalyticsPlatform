# PILOT-FIX-03, -07, -08 and the coded-number friction: re-walk of C04, C05, C07, C09, 2026-10-30

**This is an agent re-walk on the rehearsal's seeded data, not a pilot.** It re-runs the four task cards the fixes
touch, over HTTP, as the seeded `analyst@` user, and checks the numbers against
[`pilot/08-answer-key.md`](../pilot/08-answer-key.md). No analyst time, accuracy rate or usefulness is claimed.

## Environment

| | |
|---|---|
| Code | branch `claude/pilot-fix-dash-reports-d680e5` from develop `809a53e`, plus the changes below; Windows 11 dev host |
| Stack | API (local orchestrator) on a free local port; compose Postgres 16 on 127.0.0.1:15432 and Redis. No Superset (previews only) |
| Control database | `analystos_pilotfix_dash`, a fresh copy of the rehearsal database `analystos_value05` (migration 0075) made for this walk; the rehearsal database and the shared database were not changed. Analytics: the shared `analytics` database through the gateway |
| Models | **None.** No model key was loaded (rules only), so no model call was made. Tiles and requests that need SQL generation were refused with the "no model key" reason, as in the rehearsal's rules-only pass |
| Artifacts | the rehearsal's report files for the Retail workspace were copied (not moved) into this worktree's ignored `var/artifacts` so the C09 fire could refresh them |

## Card by card

| Card | Item | Before (rehearsal) | After (this walk) | Answer key |
|---|---|---|---|---|
| C04 | new draft `art_dee6767dd830` | "Average priority" KPI (a mean of codes 1–5); no busiest-team tile | No average of priority. Tiles: Incident count 20,000; incidents per month; by assignment group name (busiest teams, named in the prompt first); by made SLA (17,120 met / 2,880 missed = 85.6%); by priority as an **ordinal bar** (P1 775 … P5 1,928). Filter controls on assignment group and made SLA | 20,000; SLA met 85.6% ✔ |
| C04 | "add a filter for assignment group" | applied (rehearsal draft had no such control) | refused in plain words: "the dashboard already has a filter for assignment_group_name" (no second identical control) | — |
| C04 | "add average resolution time in hours by priority" | applied with a model | refused: no model key (rules only); not a regression | — |
| C05 | rehearsal dashboard `art_f81c2a0510ff`, click **network** | **no other tile re-queried** | Incident count **4,138**; Average priority, Incidents per month, Incidents by CI and Average resolution time by priority all **filtered** inside their own query; the four team-target tiles say "This chart reads team targets, which has no Category field." | network 4,138 ✔ |
| C05 | drill Aug 2026 → days with the network filter, Show rows | worked without the filter carried to the trend | 31 days, sum **337**; rows list only `network`, 1 personal column hidden; "Filtered by Category" on the drill | August network 337 ✔ |
| C05 | same drill, no filter | 1,646 | 31 days, sum **1,646** | 1,646 ✔ |
| C07 | report `art_e9477b3bd9c9`, "write a two-line summary in your words" | did nothing (rules cannot write prose; the model's ops were invalid) | applied by rules: "Late Delivery Rate is 9.25%. Records with product category = Electronics have a returned rate of 10.7% versus 5.3% for product category = Sports (2.0x)." The engine's numbers guard accepted it; the note says it was drafted from verified figures with template sentences | 9.25% ✔. 10.7% is the finding's own verified figure on its discovery rows; the full-table figure is 10.1% (PILOT-FIX-06, not this branch) |
| C09 | schedule `sch_1edb9a1c95cc`, digest attached the way the new control does it (whole config read, `digest_dashboards` removed, then added back), **Run now** | dashboard digest API-only | Fire **succeeded** (11 s): report refreshed and rendered, digest `art_f9ab197eb6c9` saved; **deliveries: none** (the schedule has no approved destination, so nothing was sent) | digest shows Marketplace 0.0995, Store 0.0992, Web 0.0859 = the dashboard and the answer key ✔; lists its sources ✔ |

## What changed

1. **PILOT-FIX-03, cross-filter** (`dashboards/interact.py`). A click now re-queries every tile whose governed source can
   take the field: a chart that shows it (as before); a query over the same table as a chart that shows it (the
   predicate goes into that query's own WHERE, on the table's column, built from sqlglot nodes and `sqlbuild` literals,
   only on a column the viewer may read); a metric tile when the field is an approved dimension. The same field name
   on another table is not the same field. Tiles that cannot take it say why ("This chart reads team targets, which
   has no Category field."). Every statement still runs through `QueryGateway.execute` with the viewer's scope (row
   filters and denied columns apply; a test checks the row filter wraps a pushed-down query). Show rows carries the
   pushed filter.
2. **PILOT-FIX-07, digests on schedules** (`web/src/components/DashboardDigest.tsx`, `pages/Schedules.tsx`,
   `lib/schedules.ts`). The dashboard Summary panel has "Send a summary of this dashboard": pick a schedule, see where
   it goes (its delivery destinations, each sending only while its approval stands, or "saved under Reports; nothing is
   sent"), stop it again. The schedule form has "Send a summary of these dashboards". Both edit only
   `config.digest_dashboards` through the existing schedule edit (editor role, builder dashboards only); delivery goes
   through the schedule's approved destinations, verified with `verify_for_execution` immediately before each send.
3. **PILOT-FIX-08, report summary** (`reports/summary.py`, `reports/design.py`). "Write a short summary", "draft the
   summary", "summarise the report" draft one template sentence per verified block (KPIs, then findings, then charts;
   "two-line" → 2). No share of a total. A model may reword only through `data_story` in `always` mode; a reply with the
   wrong count, a new number, the wrong shape or no provider keeps the template draft and says so. The result is a
   `text.set`, numbers-guarded again by the engine.
4. **Friction, coded numbers** (`skills/viz.is_coded_number`, `dashboards/drafts.py`, `dashboards/questions.py`).
   Integer columns that are codes (catalog role code/dimension, semantic type code/category, or names like priority,
   status, level; at most 20 values; never count or measure roles) are groupings in a draft and ordinal axes in a
   chart; columns the prompt names are used first, text categories before coded numbers.
5. Found on the re-walk and fixed: a second identical filter control is refused (`dashboards/ops.py`).

## Tests

Python fast suite, ruff, web typecheck, vitest, build and Playwright: results in the pull request. New and changed
regression tests: `tests/unit/test_dashboard_interact.py` (C05 shape: KPI and trend over the same table filtered,
other table not filtered with its reason, pushed filter only on the source table in a join, denied column and set
operations refused, Show rows, row filter), `test_report_summary.py` (17 cases, including invalid model wording),
`test_report_docs_service.py` (schedule edit attaches a digest; viewer refused; next fire saves it, sends nothing
without a destination), `test_dashboard_conversation.py` and `test_skills_viz_dashboards.py` (coded numbers),
`test_dashboard_ops.py` (no twin control); web `dashboardDigest.test.tsx`, `dashboardExplore.test.tsx`; Playwright
`e2e/dashboard-digest.spec.ts` (attach, read where it goes, stop, re-attach, see it on the schedule screen; axe-clean)
and `dashboard-explore.spec.ts`.

## Not shown

Analyst time, accuracy or usefulness (VALUE-05). C04's model-built tiles were not re-walked (no model key).
