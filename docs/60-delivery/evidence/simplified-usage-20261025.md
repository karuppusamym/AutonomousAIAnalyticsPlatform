# Simplified usage and functional UI — 2026-10-25

Scope: USAGE-01, following GROUND-01 through GROUND-07. Branch `codex/investigator-demo`.

## Implemented

- Expandable five-step usage guide on the overview and context studio: data, meaning, question, review, evidence. Explains hypotheses, supported/rejected/inconclusive tests and verified findings.
- Context preview → **Investigate this question** → existing Start work preflight, with the question preserved. Clears unrelated saved template/context/source selections for this handoff. Starting and plan approval remain separate decisions.
- Question/model edits and mapping saves invalidate the old preview; a response from an older question is discarded using a request sequence.
- Rule inputs preserve text, numbers and boolean values. In particular boolean false no longer becomes the text “false”.
- Read-only users receive a role explanation; analysis/evaluation buttons remain disabled. Server permissions remain authoritative.
- Phone catalog section selector makes every section reachable. Owner evaluations are expandable; the top-bar Guide includes current context terminology.
- Accessibility check exposed insufficient header hover contrast (4.32:1); hover now uses the darker brand colour, and the browser accessibility checks pass.

## Checks

- Production frontend build passed: `var/usability-final-build.log`.
- 53 targeted UI tests passed across context grounding, investigation value, information architecture and Guide: `var/usability-ui.log`.
- Five mock-backend Chromium browser journeys passed: `var/usability-browser.log`. Includes question handoff, persistent mapping lifecycle, mobile section changes, accessibility, dark theme, overview and value workflows.
- One real-API Chromium browser journey passed: `var/usability-live-browser.log`; `web/e2e-live/08-context-usage.live.spec.ts`. Signs in through the form to workspace `ws_796ff841746f`, resolves the existing SLA question, opens the populated investigation preflight, closes it, and switches mobile sections. No page errors. No route mocks, source writes, workspace creation, new investigation or approval. The journey may consume a configured model call.
- Final mobile screenshot visually inspected: readable layout, section selector, no horizontal overflow. Local screenshots are under `web/test-results/` and `web/test-results-live/` (ignored).
- Named-file diff reviewed; whitespace check passed.

The initial browser invocation lacked its expected bundled Chromium; rerun with the installed Chrome executable passed. A test override initially omitted the `/api` prefix; corrected before the final passing run. No test suppression was added.

## Still pending

This pass does not claim completion of every platform roadmap item. Actual analyst pilot measurements (VALUE-05), practitioner baseline, company IdP and external connector certifications require people or environments not supplied. Golden grounding checks do not measure numerical-answer accuracy. Optional isolated Python execution remains unavailable locally. The build retains an existing large-bundle advisory; no production load certification is claimed.

Instructions: [Everyday usage](../../30-runbooks/everyday-usage.md). Detailed domain ownership: [mapping runbook](../../30-runbooks/context-mapping-and-domain-onboarding.md).
