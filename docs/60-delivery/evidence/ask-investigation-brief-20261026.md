# Ask-to-investigation question editor — 2026-10-26

## Problem and change

Previously, Investigate why immediately called Ask promotion without an objective. The server used
`Investigate why: <original question>`, including for requests such as Show 5 sample orders with
related details. That label implied a focused explanation even though the run selected broader
workspace data and did not inherit the sample query's filters.

`web/src/pages/Ask.tsx` now opens an inline investigation form with the original question as editable
text. Guidance asks for an outcome, period and comparison. Scope text states that samples are examples
and the analysis does not automatically reuse the answer's SQL filters. The form explains supported,
rejected and inconclusive hypotheses, and applicable publication approval. A trimmed objective of
10–2000 characters enables Start investigation. Cancel closes the form without a promotion call;
reopening during the mounted answer preserves the edit. The existing API's objective field and
Ask-origin lineage are reused; backend execution and permission policies are unchanged.

## Verification

- 33 tests pass across `ask.test.tsx` and `downstream.test.tsx`, including no immediate promotion,
  exact edited objective submission, whitespace blocking, cancellation and draft preservation.
- Production build passes with the existing bundle-size advisory.
- Production-bundle Playwright journey Ask → promote to monitor → investigate passes, including
  the new Start investigation step and navigation to the investigation page.
- Live Retail thread `ask_1dd26dcc7c4a`, Question 7: opened Investigate why on the existing related-order
  sample, edited the objective, inspected the scope text and canceled. No new live run was started.
  Local screenshot: `var/ask-investigation-question-browser.jpg` (ignored runtime evidence).

## Limits

This is a question editor, not an automatic analytical interpretation of sample records. Including a
population or period in prose still goes through existing context resolution, plan and SQL validation;
the editor does not guarantee execution will resolve every requested restriction. The UI draft is
not persisted after leaving the mounted answer. API clients retain the existing default objective
behavior when omitting the optional objective field.
