# DEMO-02 — saved-evidence conversation, October 25, 2026

The old `question` feedback kind recorded a question without answering it. It now answers a bounded
set of questions deterministically from the run's saved findings and current verification records.
Supported topics: finding summary, limitations, findings needing checking, and a finding's recorded
checks (by code or a run-scoped target ID). Unsupported requests receive a scope explanation.

Answers carry finding references, a recording timestamp and `saved_evidence` mode. Both the feedback
record and run instruction history preserve the answer. The UI labels answers as snapshots of saved
evidence and links to current finding details. Questions do not replan or approve anything; existing
redirect/deeper-analysis controls remain separate. Question feedback still requires analyst access.

## Validation

- Backend unit suite `tests/unit/test_run_questions.py`: 13 cases pass, including stale, legacy,
  failed, superseded and rejected verdicts, unsupported actions and new analysis requests.
- Postgres API test `tests/integration/test_saved_questions_api.py`: pass. Saves and reloads an answer,
  denies viewers, refuses a mismatched workspace or foreign finding target, rejects empty questions,
  preserves status/plan hash/version and records no source query or model call. The test refuses
  construction of a model router for explicit saved-evidence questions.
- UI suites `investigationConversation`, `investigationOverview`, `increment4`: 38 tests pass,
  covering draft-before-send, evidence links, remounted history, save failure and existing redirects.
- Final UI pass across conversation/increment4/analystAnswer: 34/34 pass after layout and label changes.
- Final Playwright desktop and mobile dark-theme journeys: 2/2 pass, including send, refresh persistence,
  finding references, approval focus handoff, axe scans and no horizontal phone overflow.
- Desktop and mobile conversation screenshots were visually inspected.
- Python lint passes for both implementation files and both new test files.
- Production build passes; existing bundle-size advisory remains.

## Live local rehearsal

Rebuilt the local API container and used the current Vite UI with the existing synthetic ServiceNow
workspace `ws_8efcdf614840`, completed run `run_5e3893278298`.

1. Sent **Summarize the findings** through the actual feedback endpoint: HTTP 200, `saved_evidence`
   summary, two verified findings, four needing checking, two clickable references, no replan response.
2. Refreshed the browser and confirmed the answer remained.
3. Sent **What are the limitations?**: returned the limitations topic from the stored evidence.
4. Followed a cited finding to its **How it was checked** screen.
5. Browser page errors: none.

Local screenshot: `var/demo/conversation-live.png` (generated, ignored by Git).
This is bounded explanation of saved evidence, not a general-purpose LLM chat or new analytical run.

The unit and API suites also pass in one pytest invocation; the API test uses a distinct filename
(`test_saved_questions_api.py`) to avoid pytest module-name collisions with the unit suite.
