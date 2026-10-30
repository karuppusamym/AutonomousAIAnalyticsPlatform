# Analyst pilot: one-page plan for the owner

**Question the pilot answers:** with the same data and the same task, does a business analyst using the platform
reach a correct, usable result in clearly less of their own time than doing it the usual way? And does that hold
for each feature, not only on average?

**Not claimed yet:** nothing. Only agent rehearsals exist. A result counts once real analysts have recorded real
measurements as **Real analyst pilot observation** in the scorecard.

## Shape

| | |
|---|---|
| Length | 2–3 weeks: a short preparation, two working weeks of task cards, an optional third week of own questions |
| People | 3–6 analysts who do this kind of work today, plus the roles below |
| Work | The 12 task cards in [02-task-cards.md](02-task-cards.md), on the three demo workspaces (ServiceNow incidents, Retail orders, ServiceNow process mining) |
| Pairing | Every card is done twice by the same analyst: the usual way (manual baseline) and with the platform (assisted). Half the analysts do the manual run first and half the assisted run first, card by card, so learning the task does not favour one side |
| Volume | 3 analysts × 12 cards = 36 paired tasks; the evaluation plan needs at least **20** before any time or quality claim |

## Roles

| Role | Who | Does |
|---|---|---|
| Owner (sponsor) | You | Chooses the analysts, confirms the thresholds below before day one, takes the final decision |
| Coordinator | You or a delegate | Hands out cards, keeps the timing sheet, runs the daily check-in, checks each scorecard entry against the sheet |
| Analysts (3–6) | Your team | Do the cards both ways, time themselves, rate the result, record it in the scorecard |
| Approver | Someone who is not an analyst in the pilot | Approves report exports, publishing and scheduled delivery, as in production |
| Reviewer | A person who did not do the task | Checks each result against the answer key ([08-answer-key.md](08-answer-key.md)) and sets "rated correct". Hide which side produced the result where practical |
| Technical owner | Platform admin | Keeps the platform running, creates one login per person, sets the model choice ([07-data-and-privacy.md](07-data-and-privacy.md)), gives analysts the manual-baseline data access |

## Before day one (owner and technical owner)

1. **Settings → Members & policy → Pilot readiness** (as a workspace owner) for each demo workspace. Close what you can; write down any gap you accept and why. On
   2026-10-30 the gaps were: no named owners, ServiceNow connector not certified against a live instance (the demo uses
   a mock, so this is accepted for demo data), no single sign-on (local logins), and **no approved metric or verified
   query** in the ServiceNow and process-mining workspaces. Approve 3–5 key incident metrics (volume, SLA compliance,
   resolution hours) before the pilot: without them, dashboards by conversation are drafted from table names and are weaker.
2. One login per analyst (never a shared login: the per-person Ask budget applies). Analysts get the analyst role.
3. Turn on low-cost models (or decide to run on rules only, knowing that open questions are then refused).
4. Give analysts their usual tool for the manual baseline: a read-only SQL login or spreadsheet extracts of the same tables.
5. The coordinator walks one card end to end and records it as a **rehearsal** (never as a real observation).

## Success measures (thresholds from the [evaluation plan](../05-evaluation-plan.md) §3–4)

| Measure | How it is read | Threshold |
|---|---|---|
| Time | Median of (manual − assisted) ÷ manual per paired task; assisted includes checking and correcting | **At least 50% less** analyst time |
| Quality | Share of results the reviewer rated correct, assisted vs manual | **At least 90%** correct, and **not lower** than the manual baseline |
| Numbers | Every number in a delivered report, digest or dashboard matches the source for the stated period | **100%**: one wrong delivered number fails that task |
| Honest refusals | Cards with no answer in the data (C12) | **Zero** confident answers |
| Governance | Reads, exports or deliveries without the required role or approval | **Zero** |
| Per feature | The four measures above, separately for Ask, investigations, dashboards, reports and digests (the scorecard splits them) | Reported per feature; a feature below threshold is reported as such, not averaged away |
| Usefulness, would use again | Analysts' own ratings | Not a gate in the evaluation plan. Proposed signal, for you to confirm or change before day one: average ≥ 3.5 of 5 and ≥ 60% "would use again" |

## What counts as failure

* Any governance violation: stop, fix, and re-run the affected cards.
* A wrong number reaches a delivered report, digest or dashboard and the analyst accepted it: that task fails. Two or more in the pilot: the quality claim fails.
* Median time saved below 50%, or correctness below 90% or below the manual baseline: the value claim is **not supported**.
* Fewer than 20 paired tasks with a reviewer's rating: the pilot is **inconclusive**, not a success.
* Failed, refused and abandoned attempts are recorded like any other; leaving them out is a protocol breach.

## After the pilot

The end review ([06-checkins-and-review.md](06-checkins-and-review.md)) produces a dated results file under
`docs/60-delivery/evidence/` with every task, the per-feature table, disagreements and deviations. Only then may
VALUE-05 be closed, and only with what the numbers show.
