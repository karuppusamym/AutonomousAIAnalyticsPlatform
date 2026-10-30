# Observation form

One form per card per analyst. It holds the same fields as the [timing sheet](03-timing-sheet.csv) and as the app's
**Pilot scorecard → Record an assessment**, so it can be typed in without re-reading notes.

## Field map

The first block is sheet-only context. Every other row is exactly one scorecard field.

| Timing sheet column | Form / app label | API field (`POST /api/workspaces/{id}/pilot-reviews`) | Values |
|---|---|---|---|
| `participant` | (sheet only) | — the signed-in reviewer is recorded as `reviewer_id` | pseudonym, e.g. A1 |
| `card` | (sheet only; start the task text with it) | — | C01 … C12 |
| `date`, `order` | (sheet only) | — | `manual_first` or `assisted_first` |
| `manual_started_utc` … `assisted_finished_utc` | (sheet only; check the minutes) | — | ISO times |
| `scorecard_id` | (sheet only; filled after saving) | `id` in the response | `prv_…` |
| `evidence_kind` | Assessment type | `evidence_kind` | `real_pilot` (Real analyst pilot observation) or `rehearsal` |
| `subject_type` | What was assessed | `subject_type` | `investigation`, `ask`, `dashboard`, `report`, `digest` |
| `subject_ref` | Investigation / Ask conversation / Dashboard / Report / Scheduled digest assessed | `run_id` for an investigation, otherwise `subject_id` | id chosen from the list |
| `task` | Task measured | `task` | "C04 Draft and refine a weekly operations dashboard" |
| `baseline_minutes` | Manual baseline (minutes) | `baseline_minutes` | > 0 |
| `assisted_minutes` | Assisted time including review (minutes) | `assisted_minutes` | ≥ 0, includes checking |
| `review_minutes` | Of which checking and correcting (minutes) | `review_minutes` | optional, ≤ assisted |
| `interventions` | Times you had to step in | `interventions` | whole number ≥ 0 |
| `accuracy` | Accuracy assessment | `accuracy` | `correct`, `partly_correct`, `incorrect`, `not_reviewed` (set by the reviewer) |
| `usefulness` | Usefulness (1 low, 5 high) | `usefulness` | 1–5 |
| `would_use_again` | Would you use it again? | `would_use_again` | `true` / `false` |
| `notes` | Review notes and corrections needed | `notes` | what went wrong, what you corrected, anything odd |

## Form

```
Participant: ____   Card: C__   Date: ________   Order: manual first / assisted first
Assessment type:     [ ] Real analyst pilot observation   [ ] Demo rehearsal
What was assessed:   [ ] Investigation [ ] Ask conversation [ ] Dashboard by conversation [ ] Report by conversation [ ] Scheduled digest
Item assessed (id or title): ______________________________________
Task measured: C__ ________________________________________________

Manual run     started ____:____  finished ____:____   = ______ minutes
Assisted run   started ____:____  finished ____:____   = ______ minutes
   of which checking and correcting: ______ minutes
   times I had to step in: ______   (rephrased / answered a question / corrected / re-ran / asked someone)

Usefulness (1-5): ___     Would you use it again?  [ ] Yes  [ ] No
Accuracy (reviewer):  [ ] Correct  [ ] Partly correct  [ ] Incorrect  [ ] Not reviewed yet
Notes (what was wrong, what you fixed, what confused you):
__________________________________________________________________
Saved in the scorecard as: prv_____________
```
