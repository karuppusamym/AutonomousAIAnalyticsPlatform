# VALUE-05 pilot kit: full rehearsal, 2026-10-30

**This is an agent rehearsal, not a pilot.** An AI coding agent walked all 12 task cards of
[`pilot/02-task-cards.md`](../pilot/02-task-cards.md) through the API, and the scorecard through the UI. No analyst
took part. Every record is stored with `evidence_kind = rehearsal` and stays out of the real-pilot results. The
timings are machine wall-clock: direct read-only SQL for the manual arm, API calls plus the owner steps for the
assisted arm. They say nothing about analyst time. **No time saving, accuracy rate or usefulness is claimed.**

## Environment

| | |
|---|---|
| Code | branch `claude/value-05-pilot-kit-f38b2d` from develop `ac522d0`, plus the fixes below; Windows 11 dev host |
| Stack | API (local orchestrator) on :18000, ServiceNow mock on :18090, web production build on :4178; compose Postgres 16 on 127.0.0.1:15432 (port 5432 is held by another project) and Redis |
| Control database | `analystos_value05`, a copy of the shared dev database (it was at migration 0072), migrated to `0075_pilot_review_subjects`; the shared database was not migrated. Analytics data: the shared `analytics` database, read through the gateway |
| Models | First pass with no model key (rules only); then the project's own `.env` loaded into the API process (the agent did not read it). 46 model calls, all low-cost open models: deepseek-v4-flash (17), qwen3-14b (14), qwen3-32b (1), typesafe jev-1.13 decision model (12, $0). Total recorded model cost **$0.016**. No Claude or Gemini call |
| People | Seeded dev users: `analyst@` (cards), `approver@` (export approval), `admin@` as workspace owner (meaning definitions) |
| Manual arm | Read-only SQL as the database owner (`default_transaction_read_only=on`); values in [`pilot/08-answer-key.md`](../pilot/08-answer-key.md) |

## Card by card

Accuracy is set against the answer key's criteria. Scorecard ids are in `analystos_value05`.

| Card | Feature | Item | Outcome | Accuracy | Assisted (machine) | Steps in | Scorecard |
|---|---|---|---|---|---|---|---|
| C01 | Ask | thread `C01` | Rules rung answered; five priorities match SQL exactly | correct | 7 s | 0 | `prv_198160789e65` |
| C02 | Ask | thread `C02` | Q3 table matches SQL; follow-up "Only priority 1 and 2 incidents" not applied (asked what to measure); incomplete quarter not mentioned | partly | 17 s | 1 | `prv_5faacef451a4` |
| C03 | Investigation | `run_5bf863b5b9a9` | 6 verified findings in 5.4 min; right team (Network Operations) but **requested periods ignored**: all-history numbers, no Q2 vs Q3 comparison, falling breach rate and missing September not reported | partly | 5.4 min | 0 | `prv_a65f590e158b` |
| C04 | Dashboard | `art_ba958f6c89f1` | Before fix: **no incident tile** in an incident dashboard; filter request refused | incorrect | 36 s | 1 | `prv_94cd85ef96f4` |
| C04 | Dashboard | `art_f81c2a0510ff` | After fix: incident tiles lead, numbers match; weak without approved metrics ("Average priority" KPI, team-target tiles, no SLA tile); filter applied | partly | 20 s | 1 | `prv_efe256694e67` |
| C05 | Dashboard | `art_f81c2a0510ff` | Drill Aug 2026 → days and Show rows work (one personal column hidden); cross-filter re-queried **no other tile** | partly | 6 s | 0 | `prv_85a410f5677c` |
| C06 | Dashboard | `art_5bb4b7c1ad2b` | Rates match SQL; chart landed in a new draft. Ask used model SQL (ad hoc) although an approved Late Delivery Rate exists. Recorded through the UI | correct | 12 s | 0 | (UI) |
| C07 | Report | `art_3a349872998b` | Before fix: 3 of 4 requests did nothing ("say which" between identical names; parser error shown) | incorrect | 47 s | 3 | `prv_e3d0da16ee94` |
| C07 | Report | `art_e9477b3bd9c9` | After fix: KPI 9.25%, chart, finding added; summary set by hand passed the numbers check; PDF only after approval `apr_749d34ae34b3` | correct | 45 s | 1 | `prv_267b0f7e752f` |
| C08 | Report | `art_538915cf2eb2` | Before fix: story said "0.10 Late delivery rate (34.9% of 0.28)", a share of a sum of rates | incorrect | 2 s | 0 | `prv_943181eda844` |
| C08 | Report | `art_4b6aa0286b65` | After fix: "Marketplace has the highest Late delivery rate … 0.10" | correct | 1 s | 0 | `prv_48688dca6c01` |
| C09 | Digest | `sch_1edb9a1c95cc` | Pin by conversation, Run now (15 s) refreshed the report and built the digest; first fire carried the wrong C08 sentence, re-run correct; dashboard digest is API-only | partly | 19 s + 12 s re-run | 1 | `prv_7da10bcbd725` |
| C10 | Ask | thread `C10` | Assigned → Reassigned 9.02 h; matches SQL | correct | 13 s | 0 | `prv_0326f66819a3` |
| C11 | Investigation | `run_a60345611fb2` | 3 meaning rounds (5 owner definitions, 3 rechecks) before any test; 4 verified findings, right direction; late-vs-returned not tested; numbers are discovery-row values | partly | 15.5 min | 3 | `prv_3663b99eaeee` |
| C12 | Ask | thread `C12` | Refused: no satisfaction measure, nothing guessed | correct | 4 s | 0 | `prv_019d4d9b2f52` |

Rules-only pass (no model key): C01 answered; C02, C10 and C12 were refused with "No model provider key is set" before
any data check. Rules-only mode answers only catalog-shaped questions. This matters for the owner's model choice
([`pilot/07-data-and-privacy.md`](../pilot/07-data-and-privacy.md)).

## What worked

* Every number the platform showed for Ask (C01, C02 first question, C06, C10), dashboard tiles (C04 after the fix),
  report KPIs and charts (C07) matched direct SQL exactly.
* The honest refusal (C12) and the approval gates (C07 export needed the approver; C09 delivered nowhere without an
  approved destination) behaved as designed.
* Drill-down with row detail hid the personal column; findings were verified and carried their caveats into the report.
* The new scorecard flow worked end to end: conversational subjects, review minutes, step-ins, the per-feature split, and
  real-pilot vs rehearsal separation (the real-pilot panel stays empty).

## Bugs and friction found

Fixed in this branch (each with a regression test):

1. **Dashboard draft ignored the table the prompt named** (C04). Glossary synonyms put "change" and "team" in the topic, all
   three tables tied, and alphabetical order picked team targets and change requests. Words the person typed now outweigh
   synonyms, and typed words naming a table's columns break ties (`dashboards/drafts.py`).
2. **"Add a filter for X" became a chart question and was refused** (C04). New rule for filter controls: "add a filter for X",
   "add X as a filter", "let viewers filter by X" (`dashboards/design.py`).
3. **Report builder could not choose between identical names** (C07): one KPI listed five times, the same run twice, and
   "say which" cannot be answered by name. Identical names now use the newest and say so; duplicates are listed once. The
   model's invalid ops are reported in plain words, with the parser detail in the log (`reports/design.py`).
4. **Data story gave a share of a total for a rate** (C08, and the C09 digest built from it). Rates, averages and ratios now get
   "has the highest …" without a share (`reports/story.py`).
5. Scorecard (this branch's own UI): dashboards were listed by internal name, and the per-feature table overflowed its
   panel (now a keyboard-focusable scroll region; the browser journey's accessibility check caught the first attempt).

Open, too big for this branch (new tracker rows PILOT-FIX-01…08):

| Row | Card | Problem |
|---|---|---|
| PILOT-FIX-01 | C03 | Guided templates pass period and baseline as free text; the run analysed all history and never compared the two periods |
| PILOT-FIX-02 | C02 | An Ask follow-up that only narrows ("only priority 1 and 2") is not applied to the previous question |
| PILOT-FIX-03 | C05 | Cross-filter skips every tile whose chart does not show the field, even tiles from the same table |
| PILOT-FIX-04 | C11 | Meaning review does not converge: each recheck asks about new terms (3 rounds, about 11 minutes of owner time); data that does not exist (return reasons) blocks instead of becoming a stated limitation |
| PILOT-FIX-05 | C06 | Ask wrote ad-hoc SQL although an approved metric named in the question exists |
| PILOT-FIX-06 | C03, C07, C11 | Finding sentences read as all records but are measured on the discovery rows (about 70%); the caveat says so, the sentence does not, so an analyst's full-table check disagrees |
| PILOT-FIX-07 | C09 | No screen attaches a dashboard digest to a schedule (`config.digest_dashboards` is API-only) |
| PILOT-FIX-08 | C07 | "Write a short summary" does nothing: rules cannot write prose and the model's proposal was invalid; the analyst has to type the summary |

Smaller friction, noted for the coordinator: the rules-only refusal shows deployment instructions (environment variable,
`docker compose`) to the analyst; readiness remediation lines show API paths on the owner page; the draft dashboard turns a
coded integer (priority) into an "Average priority" KPI; and the guided investigation needs a source choice when a
workspace has two sources. Pilot readiness lives on the owner-only Members & policy page, so analysts do not see it.

## Kit changes made from the rehearsal

* Cards use the on-screen labels; C02 and C03 now also require pointing out an incomplete period, and the answer key
  records the data facts (Q3 2026 holds only July–August, 5 future-dated incidents, `returned` is capitalised).
* C07 tells the analyst to set the summary text themselves; C09 uses the report-pin path the UI offers.
* The plan asks the owner to approve 3–5 incident metrics before the pilot (readiness now checks this), and to decide on
  models up front.

## Readiness after the rehearsal (`GET /api/workspaces/{id}/pilot-readiness`)

All three demo workspaces are **not ready**. Accepted for a demo-data pilot: ServiceNow connector not certified against a
live instance (mock), no single sign-on. Owner actions: name business and technical owners; approve incident metrics in the
ServiceNow and process-mining workspaces (`conversation.definitions` missing); per-workspace rehearsal coverage
(`pilot.rehearsal`) is complete for none of them by itself, because the cards spread the features across workspaces.

## What this does not show

Analyst time, analyst accuracy, usefulness or willingness to use it again. Those need the owner's analysts (VALUE-05).
