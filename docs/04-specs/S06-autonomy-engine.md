# S06 — Autonomy engine: goals, playbooks, runs, budgets

**Priority:** P0 · **Closes:** FR-010, FR-011, FR-017, FR-019, FR-058 · **ADRs:** 002, 003

## 1. Purpose
Turn a goal into a bounded, durable, observable, resumable plan of typed steps executed by agents, which
stops at policy and approval gates and reports partial results honestly.

## 2. Baseline to reuse
`runtime/` (plan + hashes, engine: `get_state` / `execute_task` / `replan`, per-step `RunContext` with agent manifest and
budget), `workflows/` (Temporal workflow + activities, local `orchestrator.py`), `agents/` (`dispatch.py`, `generic.py`
propose → validate → execute, specialised agents), `config/agents/*.yaml` (21 manifests: supervisor, context, metadata,
profiler, data_quality, investigator, sql, critic, insight, semantic, visualization, governance, publisher, …),
`capabilities/builtin/playbooks/*.yaml` (`investigate.v1`, `prepare.v1`, `data_dictionary.v1`, `elt_build.v1`, `train.v1`,
`score.v1`), published/pinned versions (ADR-0021), `RunEvent`, `IdempotencyRecord`, `DispatchOutbox`, pause/resume/
redirect/cancel.

## 3. Build
1. **Goal API v2** (v1 §13.1 shape):
   ```
   POST /v2/goals            Idempotency-Key required
   { workspace_id, question, data_scope{source_ids[], filters}, mode: goal_autonomous|goal_supervised,
     playbook?: "investigate.v1@3", requested_outputs: [investigation, dashboard_draft, report],
     limits{max_steps, max_duration_s, max_scan_bytes, max_cost_usd, max_replans},
     approval_policy{publish: required, notify: required}, request_id? }
   → 202 {goal_id, run_id, state, events_url}
   POST /v2/runs/{id}/signals  {type: pause|resume|cancel|redirect|approve_step, payload}
   GET  /v2/runs/{id} · /events (SSE) · /evidence · /plan?version=
   ```
2. **Complexity router** (v1 §2.2): features are table count, join depth, estimated scan, ambiguity flags, policy tier
   and requested artifact. Rules first, then the `clarify_needed`/`ask_route` decision rung. Output: `low|medium|complex` → playbook
   + default limits. Users may raise the tier but never lower it below what policy requires.
3. **State machine** exactly as in architecture §5.2. Add `COMPLETED_WITH_LIMITATIONS` and `EXPIRED` if not present. Every
   transition is written as a `run_event` through the outbox with `{actor, policy_decision, correlation_id, trace_id}`.
4. **Budgets as Temporal-enforced counters.** Steps, wall clock (workflow timer), scan bytes (from S02 estimates and actuals),
   LLM USD (router spend), rows, replans and retries. Exhaustion → `COMPLETED_WITH_LIMITATIONS` with validated partials (AT-04).
   Remaining budget is streamed in events for the Autonomy Center UI.
5. **Replan contract.** `replan(reason_code ∈ {missing_dataset, empty_result, rejected_hypothesis, quality_failure,
   user_redirect, budget_downgrade}, detail)` → `plan_version+1`. Prior steps and evidence stay immutable. Approvals
   bound to the old plan hash become invalid.
6. **Parallel branches.** Independent steps run concurrently up to `min(workspace.max_parallel, source semaphore)`.
7. **Playbook authoring (FR-058, P2).** A YAML editor with schema validation, **simulation against a fixture workspace**
   (deterministic mocks for model rungs), diff against the published version, a signature on publish (S10 signing), and
   certification per workspace.
8. **Templates.** Ship packs: `investigate.v1` (RCA), `kpi_review.v1`, `variance.v1` (month-end), `funnel.v1`, `cohort.v1`,
   `dq_review.v1`, `dashboard_build.v1`. Each declares inputs, steps, outputs, default limits and approval gates.

## 4. Invariants
* Only registered, typed, policy-scoped tools execute. Data text cannot add tools.
* Exactly-once is never assumed. Step commit is compare-and-swap on `(run_id, plan_version, step_key, attempt)`, and
  side effects use idempotency keys plus reconciliation.
* A late result from a canceled run is marked `canceled` and is never published.

## 5. Acceptance
AT-03, AT-04, AT-09, AT-11, AT-12; `test_tier_cannot_be_lowered`; `test_replan_invalidates_approval`;
`test_parallel_respects_source_semaphore`; playbook simulation golden test.

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S06-T1 | Goal API v2 + idempotency + SSE contract | 2d |
| S06-T2 | Complexity router (rules + decision rung) | 2d |
| S06-T3 | Budget counters + `COMPLETED_WITH_LIMITATIONS` + remaining-budget events | 2d |
| S06-T4 | Replan reason codes + approval invalidation tests | 1d |
| S06-T5 | Template packs (6 playbooks) with fixtures | 5d |
| S06-T6 | (P2) Playbook editor + simulator + signing | 5d |

## 7. As built (2026-10-27, T1–T5)

Code: `src/analystos/autonomy/` (`goals.py`, `complexity.py`, `budget.py`, `replan.py`, `models.py`),
`api/routers/goals_v2.py`, engine hooks in `runtime/engine.py`, loops in `workflows/analysis_workflow.py` and
`workflows/orchestrator.py`, templates in `capabilities/builtin/playbooks/`, migration `0064_goals`. Contract:
[`docs/20-contracts/05-goal-api-v2.md`](../20-contracts/05-goal-api-v2.md). Deviations from §3:

* **T1.** `GET /v2/runs/{id}` etc. are not under a workspace path; the run is authorized in its own workspace
  (`get_run_for`). The goal is a new table (`goal`) next to the run; `analysis_run.origin = {"type": "goal",
  "goal_id", "mode", "requested_outputs", "publish"}`. `approval_policy` accepts only `required` (approvals are
  never waived through the API). `approve_step` needs approval rights, the other signals analyst rights.
  `redirect` goes through run feedback (rule/LLM interpretation into filters), not straight to `replan`.
* **T2.** The decision rung is `clarify_needed` only (through the DecisionService when a router is
  configured, else its rule); `ask_route` is not consulted at goal intake, because the Ask path owns the
  verified-query match it needs. "Never below what policy requires": the floor is the policy tier (masked,
  sensitive or restricted columns; unattended publication) plus any `clarify_needed` escalation; a request
  below the rules tier but at or above the floor is honoured. A template that matches raises the tier to its
  lowest tier; a template that is disabled in the workspace or not certified for an autonomous run falls back
  to `playbook.investigate` with the reason recorded.
* **T3.** Budgets are enforced in `get_state`, the activity both orchestrators call every iteration (so the
  same counters hold under Temporal and the local runner), plus at the call: the router's per-run cost check
  is tightened by `max_cost_usd`, and `RunContext.check_query_budget` refuses a statement once `max_scan_bytes`
  is used. Temporal's wait timer is capped by the remaining wall clock (`workflow.patched("budget-deadline")`).
  Steps, replans and retries are allowances (no further step/replan/retry once used); duration, cost, scan
  and rows stop the run when reached. A step refused by a budget (`budget_exceeded`) is a stop, not a failure.
  Scan bytes count only what `autonomy.budget.record_scan` is told: the gateway does not report estimates or
  actuals yet (S02 §3.5 owns that), so `max_scan_bytes` is wired but inert until S02 calls it. `EXPIRED` is
  used when the wall clock runs out while the run waits for a person; a local-runtime run parked
  `WAITING_USER` is re-checked only when it is re-driven (signal, sweep, restart).
* **§3.3 state machine** is not renamed to architecture §5.2's states: only `COMPLETED_WITH_LIMITATIONS` and
  `EXPIRED` were added to the existing run states; transitions keep their `run.status` events (the new goal and replan events carry an actor;
  `run.status` events do not yet carry `policy_decision`, `correlation_id` or `trace_id`).
* **T4.** Superseded plans are copied to `run_plan_version` before a replan changes anything. A plan
  approval gate that is still waiting when the run is replanned requests a new approval for the new hash
  (before this change it would have been skipped, letting the plan run unapproved). A replan after the plan
  was approved does not re-ask for plan approval; side-effect approvals downstream are re-requested by their
  gates. `max_replans` refuses further replans (429).
* **T5.** Six templates beside `investigate.v1`: `kpi_review.v1`, `variance.v1`, `funnel.v1`, `cohort.v1`,
  `dq_review.v1`, `dashboard_build.v1`. They reuse the certified agents; what differs is the step set, the
  investigator's `params.prefer_methods` (ranked first, nothing dropped), declared inputs/outputs, default
  limits, approval gates and replan boundaries. They are `tested`, not certified, and off until a workspace
  enables them (like `data_dictionary.v1`): an autonomous goal never runs them. Golden fixtures:
  `tests/fixtures/playbooks/templates.yaml`.
* **Not built (at the time of T1–T5):** §3.6 parallel branches bounded by a source semaphore
  (`test_parallel_respects_source_semaphore`; since built, tracker row S06-PAR), T6 (P2; built below).

## 8. As built (2026-10-29, T6)

Code: `autonomy/playbook_editor.py` (YAML in and out, check, options), `autonomy/simulator.py` +
`autonomy/simulation_fixtures.yaml`, `autonomy/playbook_signing.py`, `api/routers/playbook_editor.py`,
hooks in `services/definitions.py` (tester, publish, update, promote, `require_runnable`) and `services/pins.py`,
generic payload signing in `toolreg/signing.py` (`sign_payload` / `verify_payload`), migration
`0073_playbook_signature` (`definition.signature`). Web: `web/src/components/PlaybookEditor.tsx` in Work →
Workflows, shown only with expert tools on. Deviations from §3.7:

* **Editor.** Drafts are the existing workspace `playbook` definitions (ADR-0021 versions, one draft per key,
  If-Match revisions); the editor adds YAML (`yaml.safe_load`, one mapping, 256 KiB), a check that lists every
  problem (contract + registry, the same validator a pack load uses), and the diff against the newest published
  version or the installed manifest of that id. It is a text area, not a schema-aware code editor.
* **Simulator.** Fixtures are named runs (autonomy level, origin, limits, approver decisions, mock failures,
  expansion counts, replans, enable/disable overrides), not a seeded workspace: enablement and grants are the real
  workspace's, layered with the fixture's overrides. Every agent is a deterministic mock (no model rung runs at
  all, so "deterministic mocks for model rungs" holds trivially); cost is the agents' declared `budget.usd`, an
  upper bound. Golden test: `test_simulation_golden_investigate_v1` against `tests/fixtures/playbooks/simulation_golden.yaml`.
* **Signing.** Signing is approval-bound rather than "on publish" alone: publish requires a passing simulation
  (the playbook tester) and an approved `playbook.sign` approval (separation of duties always); publish verifies
  it (`verify_for_execution`), signs `(purpose, tenant, key, version, content hash, approval)` with the S10 tenant
  key, and consumes the approval. Any edit voids the sign-off. Runs, schedule pins and promotion verify the
  signature. Certification per workspace is unchanged (publishing certifies the composition in its workspace;
  agents are still checked one by one).
* **Behaviour change.** A workspace playbook published before T6 is unsigned and no longer runs (refused with the
  reason); publish a new version through simulation and signing. Drafts still run in a workspace marked
  `environment: dev` (ADR-0021), unsigned.

