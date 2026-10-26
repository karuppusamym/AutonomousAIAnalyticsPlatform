# Single-reviewer publishing — 2026-10-25

## User request and cause

The user explicitly requested permission to publish after reporting that separation of duties was off. Retail workspace `ws_9a4b56958e2e` had `separation_of_duties=false`, `publish_requires_approval=true` and Superset allowed. Publication approval `apr_5034c738fd80` was already approved by the human requester. Run `run_7896fbdad581` completed its analysis but the publish task failed because metric approval was hard-coded to reject self-review even with the workspace switch off.

## Change

KPI approvals now follow the existing workspace separation-of-duties flag through both the generic inbox and the semantic metric endpoint. When off, an authorized requester can explicitly review their own metric. When on, self-review remains blocked. Mandatory independent structural-model, relationship and delivery-destination reviews remain unchanged. Role, scope, definition validation, current policy version, approval expiry, payload/plan hashes and publication gating remain enforced. No workspace policy values or reviewer identities were changed. The policy UI hint explains the scope of the switch.

## Verified publication

Before retry, the runtime verified that the current bundle matched the human-approved payload and that the approval was unexpired, bound to the same plan/policy and still authorized. The existing publisher executed the retry, including explicit bundled KPI approval in the recorded human approver's name under the configured policy. It did not create or impersonate a second reviewer. The failed task was recorded completed with an audit entry; the run summary reflects publication success.

Publication `pub_d685edb69e1c` succeeded to Superset with four workspace-labelled dashboards:

- Executive overview: dashboard 16.
- Operations deep-dive: dashboard 17.
- Activity and volume: dashboard 28.
- Delivery reliability: dashboard 29.

Live checks confirm all four are published, have the retail workspace title prefix and all 12 chart data endpoint requests return successfully. Local ignored evidence: `var/publish-current-approval.log`, `publish-ready.log`, `publish-retry-result.log`, `published-retail-verification.log`.

## Tests

- 20 Postgres integration tests passed across semantic KPI approval, governance and the analyst review workflow. Explicit tests cover self-review through both APIs with separation off; existing strict-mode scenarios now explicitly enable separation. Structural/relationship review still denies self-review.
- 44 delivery-authorization and approval-view unit tests passed; mandatory independent destination authorization remains covered.
- 36 UI/contract tests and production frontend build passed. The existing large-bundle advisory remains.
- Targeted Ruff and whitespace checks passed.

The old semantic publication test assumed publication must fail before proposing unapproved KPIs and ignored the current plan-approval step. It now follows the existing staged plan/publication gates and reviews proposed KPIs independently before testing reuse and publication. No production gate was removed to satisfy it.

This changes KPI approval behavior according to the user's requested single-reviewer mode. It does not establish a new interpretation of the business question or new analysis: the published bundle is the exact previously reviewed investigation output.
