# PILOT-FIX-02 and PILOT-FIX-05: Ask re-walk of cards C02 and C06, 2026-10-30

**Agent re-walk, not a pilot.** An AI coding agent asked the card questions through `services/ask.ask_in_thread` (the
code path behind the API's Ask route) after the fixes. No analyst took part; no time saving or accuracy rate is claimed.
Problems came from the rehearsal ([`value-05-pilot-rehearsal-20261030.md`](value-05-pilot-rehearsal-20261030.md)).

## Environment

| | |
|---|---|
| Code | branch `claude/pilot-fix-ask-14df5b` from develop `809a53e`; Windows 11 dev host |
| Control database | `analystos_pilotfix_ask`, a clone of the rehearsal database `analystos_value05` (migration 0075). The shared databases were not changed |
| Analytics data | the shared `analytics` database (compose Postgres 16 on 127.0.0.1:15432), read through the gateway as `analyst@` |
| Models | Pass 1: no provider key (rules only). Pass 2: the project's `.env` loaded into the process (not read by the agent). Model calls: deepseek-v4-flash and the typesafe jev decision model only. Claude and Gemini are blocked by `blocked_families` |
| Owner step on the clone only | The Retail workspace had approved metrics but **no approved semantic model** (all four versions `proposed`, in every demo workspace). For C06, `approver@` approved Retail model version 4 on the clone (`semantic/review.decide_model`) |

## C02: SLA compliance by team for Q3 2026, then narrowed

| Turn | Answered by | Model calls | Result vs answer key |
|---|---|---|---|
| "What share of incidents met SLA for each assignment group in Q3 2026?" | model SQL (ad hoc) | 2 | Storage and Backup 92.0% (113), Network Operations 80.8% (468), no group 85.7% (119): match |
| "Only priority 1 and 2 incidents" | rules (follow-up) | **0** | Network Operations 79.8% (84), Identity and Access 80.0% (20): match. Q3 window kept, `"priority" IN (1, 2)` added |
| "just for Q2" | rules (follow-up) | 0 | Q2 2026 window replaces Q3; priority 1–2 kept; explanation names both |

* Both Q3 answers carry the note: "Q3 2026 is incomplete in this data: its records run from 1 Jul 2026 to 31 Aug 2026,
  with none in September 2026. Read it as a partial period, not a whole one." The Q2 answer has no note (Q2 is complete).
* In one model run the SQL dropped incidents without a group (`IS NOT NULL`); answers now say "Records with no
  assignment group name are left out of these groups" when that happens. In the final run the no-group row was shown.
* Before the fix the follow-up asked what to measure (rehearsal: 1 step-in). Card criteria now met: both tables use Q3
  2026, the second is P1–2 only, shares match, the no-group incidents are shown or said to be left out, and the
  incomplete quarter is pointed out.

## C06: late delivery rate by sales channel

| Run | Answer |
|---|---|
| Before approving the model (pass 2) | Ad hoc model SQL, numbers right, plus the note: "Late Delivery Rate is an approved metric, but this workspace's semantic model is not approved yet, so this answer was calculated ad hoc rather than from the approved definition." |
| After approval, rules only (pass 1) and with a model (pass 2) | **Governed**, `answered_by = semantic`, 0 model calls: "Calculated from the approved metric definition Late Delivery Rate (v1)". Marketplace 9.95%, Store 9.92%, Web 8.59%: match |
| Follow-up "exclude the marketplace channel" | Still governed: the SemanticQuery gets `sales_channel not_in [Marketplace]` and is compiled again |
| Add to dashboard → new draft "Delivery performance" | Draft `art_3c8d37076cde` created with the answer's bar chart |

The second sentence ("Put it on a dashboard I can share later.") no longer blocks the match; the answer notes that the
actions under the answer put it on a dashboard.

Two compiler limits also blocked C06 and are fixed (`semantic/compiler.py`):

1. Every dataset an investigation builds adds LEFT JOIN lookups (product category, customer region), and the compiler
   refused any dataset with a join. It now reads such a dataset from its base table alone. A LEFT JOIN never removes a
   base row, and no uncertified join reaches a governed query (ADR-0019). Fields over the joined tables are unavailable,
   with their own reason. Inner, right and nested joins are still refused.
2. The demo metrics declare a row grain (`order`, `record`), and the compiler refused every grain except `all`. It now
   accepts `record` and the base table's own entity (`order` on `orders`) when the dataset reads that one table with
   no join.

## Rules-only refusal wording

With no key, the refusals carry a business version, which the Ask page shows outside the expert view to people who are
not platform admins: "This question needs an AI model, which isn't turned on for this workspace. Ask your
administrator." The technical title and remedy (environment variable, `docker compose`) stay in the refusal for
admins and the expert view.

## Limits

* Governed answers need an approved semantic model. On the shared demo databases no workspace has one, so C06 stays ad
  hoc there (with the note above) until an owner approves the Retail model.
* Approved metrics cannot group by product category or customer region until that join is certified.
* A follow-up narrows the previous answer's table. When the rules cannot resolve the restriction, generation receives
  the previous question and SQL as data. Example: an unknown value, or a table read only through a join.
* A governed `not_in` follows SQL semantics and leaves out records with no value. A non-governed exclusion keeps them.
