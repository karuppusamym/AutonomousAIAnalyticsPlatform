# Investigation value increment — 2026-10-25

Branch: `codex/investigator-demo`. Completes VALUE-01 through VALUE-04 tooling.
Conducting a real analyst pilot (VALUE-05) requires participating analysts and actual measurements.

## Delivered

- Four guided investigation templates: SLA change, team comparison, recurring incidents and data quality. Each captures a period, baseline, focus and decision; the server constructs the question and pins the template version.
- Reviewed business context reuses the existing versioned workspace brief. Guided runs pin a scope-filtered snapshot; suggested facts are excluded. The context agent rechecks scope before use. API run serialization exposes a fact count instead of raw pinned facts.
- Findings support persistent actions with an owner, due date, next step, team-assessed impact, success measure and unresolved questions. Updates require a revision. Completion requires a current verified finding, an owner and a resolution note. Actions record follow-up work; they do not execute operational changes.
- A pilot scorecard and [protocol](../../30-runbooks/analyst-pilot-protocol.md) separate rehearsals from real observations. Baseline times require user input. Failed and inconclusive terminal investigations are assessable; unreviewed accuracy is excluded from the accuracy denominator. Summary results are self-reported and limited to the latest 1,000 assessments, with a visible limit indicator.

New persisted concepts have migration `0055_investigation_value`, lineage, audit events and workspace permissions. No new model server or third-party runtime dependency was added.

## Verification

| Check | Result |
|---|---|
| Backend unit and PostgreSQL API tests | 17 passed |
| Relevant UI component suites | 54 passed |
| Contract/context follow-up suites | 7 passed |
| Browser journeys, including phone/dark theme and accessibility | 4 passed |
| Production frontend build | Passed; existing large-chunk advisory remains |
| OpenAPI export/check, backend lint and whitespace check | Passed |
| Local API image and migration | Rebuilt and upgraded to 0055 |

Logs are local ignored files under `var/investigation-value-*.log`.

## Live walkthrough

The existing local Operations workspace `ws_796ff841746f` was used with synthetic demo data.

- Guided SLA run `run_a8040c23d416` was created through the UI, pinning brief version 2 and ten reviewed facts. It is `WAITING_USER` at plan approval. This is not an executed new analysis.
- A proposed follow-up, **Demo: review SLA evidence with the owning team**, was saved against a currently verified finding in `run_66f63d34fc79`. It has the local Platform Admin owner and a 2026-10-26 due date. It survived reload; no numeric impact was invented.
- Both real-pilot and rehearsal assessment counts remain zero in the live database. Assessment persistence and summaries were exercised in isolated tests, not presented as analyst results.
- Four curated workspaces remain visible. Earlier OpenRouter connectivity and SQL readiness evidence is recorded separately in [demo readiness](demo-readiness-20261025.md).

Local screenshots and the live verification record are under `var/value-release-20261025/`.

## Remaining limits

Actual analyst participation, independent accuracy review and measured time benefit are outstanding. No commercial benefit is established by these tests. The optional Python sandbox remains unavailable in the local configuration; use the previously verified SQL path for the demo. Production scaling, SSO and deployment hardening retain their existing tracker status.
