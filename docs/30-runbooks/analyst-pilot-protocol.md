# Controlled analyst pilot

## Purpose and participants

Determine whether governed investigations help service-operations analysts reach defensible decisions
with less effort. Recruit three to five analysts from one business team. The product owner names
the business sponsor, technical owner and a person who can independently review the answers.
Participant selection is pending the user; this document does not invite or contact anyone.

Use synthetic data for rehearsals. Use approved, representative team data for real pilot observations.
The same source permissions, query gateway, budgets and approval controls apply throughout.

## Before the pilot

1. Review the workspace's source selection and intended use. Confirm that all reviewers have the
   appropriate workspace membership and that sensitive fields remain restricted.
2. In **Business context we can reuse**, review definitions of SLA breach, elapsed/business hours,
   priority, team attribution, reopened incidents, duplicate records, reporting periods and exclusions.
   Keep unresolved assumptions as suggestions, not facts. Review join cardinality and metric
   definitions in **Catalog & definitions**.
3. Choose ten representative questions and record the decision each supports. Agree the data window
   and comparison baseline before either the manual or assisted attempt.
4. Have a reviewer independently establish an expected result or an acceptable evidence-based
   conclusion. Include an intentionally unanswerable question to test honest limitation reporting.
5. Agree the pilot's success criteria before collecting results. Proposed starting thresholds:
   at least 30 paired task assessments from at least three analysts; at least 90% rated correct
   among reviewed answers; at least 25% reduction in total recorded time across matched tasks;
   at least 70% willing to use again; zero unauthorized writes or unlabelled stale conclusions.
   These are proposed decision criteria, not achieved results or statistical certification.

## Suggested task set

| Task | Guided template | Evidence to assess |
|---|---|---|
| Explain a breach-rate increase between two periods | Explain an SLA increase | Baseline, denominator, exclusions and available periods |
| Distinguish rising workload from rising breach rate | Explain an SLA increase | Counts and rates shown separately |
| Compare resolution time across teams | Compare team performance | Priority mix, group size and fair comparison caveats |
| Examine an apparent underperforming team | Compare team performance | Alternative explanations, association versus causation |
| Identify applications with repeated incidents | Find recurring incidents | Entity mapping and record-level evidence |
| Separate duplicate tickets from repeat incidents | Find recurring incidents | Row grain, entity keys and deduplication assumptions |
| Investigate reassignment and SLA outcomes | Explain an SLA increase | Supporting tests, unresolved questions, no causal overclaim |
| Check missing or impossible timestamps | Check data quality | Measured null/invalid counts and affected conclusions |
| Check whether joins distort the denominator | Check data quality | Key/cardinality checks and fan-out refusal |
| Ask for a conclusion unsupported by available data | Any appropriate template | Explicit limitations or refusal, no invented numbers |

## Each paired task

1. Record a manual baseline using the agreed scope and data. Include the time needed to check the
   answer and prepare its evidence. Do not estimate the baseline after seeing the assisted result.
2. Start the matching guided investigation. Enter its period, comparison, focus/exclusions and
   the business decision it should support. Review the plan when approval is required.
3. Include all assisted work time: prompting, waiting, reviewing sources, correcting mistakes and
   checking findings. Follow-up questions about saved evidence do not constitute fresh analysis.
4. Have the reviewer assess accuracy: **correct**, **partly correct**, **incorrect**, or **not reviewed**.
   A correctly identified evidence gap may be the right answer; confident speculation is not.
5. If an action is appropriate, record its owner, due date, intended outcome measure and remaining
   questions. Keep financial estimates labelled as team estimates. An action record does not
   execute operational changes. Completion needs current verified evidence and a resolution note.
6. In the workspace's **Pilot scorecard**, record the assessment as **Real analyst pilot observation**.
   Use **Demo rehearsal** for synthetic or illustrative practice. Notes should explain corrections,
   evidence gaps and review method; do not include unnecessary sensitive data.

## Interpreting the scorecard

- Time saved is the sum of paired manual baselines minus assisted times. Negative values remain
  visible. The time-saving percentage uses the total baseline, rather than averaging percentages.
- Rated-correct percentage excludes **not reviewed** from its denominator and shows the number
  reviewed. It describes human ratings, not automatic proof of correctness.
- Usefulness is the mean of 1–5 ratings. Willingness to reuse is the fraction answering yes.
- Distinct reviewer and investigation counts are shown. Each reviewer has one editable assessment
  per investigation and assessment type, so corrections do not silently add duplicate votes.
- Rehearsal results never contribute to the real pilot summary. All measurements are self-reported;
  small samples and selection bias still require judgment. No fabricated pilot data is seeded.
- The current view caps the review list at the most recent 1,000 and labels that limit if reached.
- Include failed, cancelled, rejected and completed-but-inconclusive investigations in the review
  sample. Selecting only runs with verified findings would bias the pilot toward successes. A run
  that is merely awaiting its initial execution-plan approval is not yet an assessable task.

## Finish the pilot

Review all incorrect, partial and unreviewed answers. Compare results to the criteria agreed before
the pilot, document limitations, and choose: continue iterating, run a larger controlled pilot, or stop.
Publish benefits only after the business owner accepts the evidence. The software and this protocol
prepare the pilot; they do not substitute for participation by real analysts.
