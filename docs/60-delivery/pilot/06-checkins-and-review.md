# Daily check-in and end-of-pilot review

## Daily check-in (15 minutes, coordinator + analysts; approver and technical owner when needed)

1. **Done since yesterday** (2 min): cards finished per analyst; the coordinator ticks them on the timing sheet.
2. **Blockers** (5 min): anything that stopped a card: logins, an error, a refusal, waiting for an approval or a definition.
   The technical owner takes platform problems; the owner takes definitions. Never work around a blocker with another login.
3. **Surprises** (5 min): a number that looked wrong, an answer that ignored part of the question, a step that took
   longer than expected. Write each as one line in the issues list with the card and the item id.
4. **Today** (3 min): who does which cards, and in which order (manual first or platform first).

Coordinator after the check-in (10 min): compare yesterday's scorecard entries with the timing sheet (minutes,
what was assessed, assessment type **Real analyst pilot observation**); ask the reviewer to rate yesterday's results.

Stop the pilot the same day if a governance problem appears (someone read, exported or delivered something they should
not have, or an approval was skipped). Fix, then re-run the affected cards.

## End-of-pilot review (60–90 minutes, everyone; owner decides)

Prepare (coordinator): the scorecard's **Real analyst pilot** panel and its per-feature table, the timing sheet,
the reviewer's ratings, the issues list. Check there are at least 20 paired tasks with a rating.

1. **Coverage** (5 min): paired tasks per card and per feature; missing or abandoned ones and why.
2. **Results against the thresholds** (20 min), from [01-pilot-plan.md](01-pilot-plan.md):
   median time saved, rated correct (platform vs manual), wrong delivered numbers, honest refusals, governance, per feature.
   Read failures before averages.
3. **Analysts' view** (15 min): usefulness, would use again, what they would and would not trust it with.
4. **Issues** (15 min): each issue with its card; agree which are bugs, which are missing features, which are training.
5. **Decision** (10 min): the owner states one of: extend to real work, extend with conditions, repeat after fixes, stop.
   With the reasons and what would change it.
6. **Write-up** (after): a dated file `docs/60-delivery/evidence/<date>-value-05-real-pilot.md` with every task and
   rating, the per-feature table, disagreements between analyst and reviewer, deviations from this kit, and the decision.
   VALUE-05 closes only with that file, and only with what it shows.
