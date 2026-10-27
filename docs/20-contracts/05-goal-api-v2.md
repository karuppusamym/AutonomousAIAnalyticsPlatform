# Goal API v2 and the run event stream (S06-T1)

Implemented in `src/analystos/api/routers/goals_v2.py` over `src/analystos/autonomy/`. Request and
response models: `contracts/goals.py` (`GoalIn`, `RunLimits`, `SignalIn`, `ComplexityDecision`).
Tests: `tests/integration/test_goals_v2.py` (Postgres, strict tenancy), `tests/unit/test_autonomy_engine.py`.

## POST /v2/goals

`Idempotency-Key` is **required** (422 without it). The goal row, its run, the dispatch-outbox row and the
idempotency record commit in one transaction. The same key and body returns the same `goal_id` and `run_id`
with `Idempotent-Replayed: true`; the same key with another body is 409 `conflict`; a key whose first request
is still running is 409 and retryable. Keys are scoped by principal, workspace and operation (`goal.create`)
and kept 7 days.

```json
{ "workspace_id": "ws_…", "question": "Explain the month-end variance in volume versus the prior month",
  "data_scope": {"source_ids": ["src_…"], "filters": [{"column": "priority", "op": "=", "value": "1"}]},
  "mode": "goal_supervised",            // or goal_autonomous
  "playbook": "variance.v1",            // optional: "<template>.v<major>[@<definition version>]", a capability id or a workspace playbook key
  "requested_outputs": ["investigation"],   // investigation | dashboard_draft | report
  "limits": {"max_steps": 20, "max_duration_s": 1800, "max_scan_bytes": 10000000000, "max_cost_usd": 0.5,
             "max_replans": 1, "max_rows": 1000000, "max_retries": 4},
  "approval_policy": {"publish": "required", "notify": "required"},   // the only accepted values
  "tier": "complex",                    // optional: raises the router's tier; never below the policy floor
  "request_id": "client-correlation-id" }
```

`202 {goal_id, run_id, state, events_url, tier, playbook, limits}` and `Location: /v2/runs/{run_id}`.

* `mode`: `goal_supervised` = autonomy 2 (the plan waits for approval); `goal_autonomous` = autonomy 3, capped
  by the workspace level. A `clarify_needed` escalation makes the run supervised.
* `limits` are the router's defaults (tier defaults, overridden by the chosen playbook's `default_limits`)
  tightened by the request: a request can lower a limit, never raise it. Workspace policy caps still apply.
* `data_scope.filters` are `contracts.analysis.Filter`s over columns in the caller's authorized scope; they
  become the run's constraints and are part of the plan hash.
* `requested_outputs` containing `dashboard_draft` lets a playbook propose publication (`origin.publish =
  propose`); otherwise publication steps are skipped. Publication itself always needs a hash-bound approval.

## Complexity router

Rules over deterministic features (table count, validated join depth, catalog row counts, ambiguity flags,
masked/sensitive/restricted columns, unattended publication, requested artifacts) give `low | medium |
complex`; the `clarify_needed` decision rung can only escalate (floor ≥ medium, supervised). A template is
chosen when one of its `router.intents` matches and it serves the tier; a matched template raises the tier to
its lowest tier; a template that is disabled in the workspace, or not certified for an autonomous run, is
passed over for `playbook.investigate` (the reason is in `complexity.playbook_reason`).

## POST /v2/runs/{id}/signals

`{type, payload}`: `pause`, `resume`, `cancel` (analyst on the run); `redirect` (`payload.text`, interpreted
like run feedback, replans with `user_redirect`); `approve_step` (`payload.step` or `payload.approval_id`,
optional `approve: false` and `reason`; needs approval rights: approver or owner; separation of duties applies).
Returns `{run_id, signal, state, result}`. A signal on a terminal run is 409.

## GET /v2/runs/{id} · /evidence · /plan?version=

* run: state, control, steps, plan version and hash, the complexity decision, `budget` `{limits, used,
  remaining, exhausted}`, and `limitations` once a budget stopped the run.
* evidence: findings with verification standing, statement counts and rows by status, the data manifest.
* plan: the current plan with its tasks, or a superseded version from `run_plan_version` (immutable: plan,
  hash, every task's status and output keys, and the `reason_code`, detail and actor of the replan that
  replaced it). Unknown version: 404.

## GET /v2/runs/{id}/events — Server-Sent Events

One frame per persisted `run_event`, in id order:

```
id: 1234
event: run.budget
data: {"v": 2, "id": 1234, "run_id": "run_…", "type": "run.budget", "payload": {…}, "actor": null, "created_at": "…"}
```

* Resume with `Last-Event-ID` (or `?after_id=`): frames with a larger id only.
* `: keep-alive` comments every 15 s while idle.
* The stream ends with `event: end` / `data: {"status": "<terminal state>"}` once the run is terminal, or with
  `event: expired|revoked` / `data: {"reason"}` when the caller's token expires or access is lost (the caller is
  re-authorized before every batch).
* Event types of interest: `goal.created`, `run.status`, `run.replanned` (`plan_version`, `plan_hash`,
  `reason_code`, `trigger`, `tasks_reset`, `tasks_removed`, `tasks_dropped`, `approvals_invalidated`),
  `run.budget` (after every step of a goal run: `{task, limits, used, remaining, exhausted}`), `run.limited`
  (`{outcome, reason, limits, used, completed_steps, not_run, verified_findings, unverified_findings}`),
  `agent.*`, `approval.*`, `goal.signal`.

## Run states

Terminal: `COMPLETED`, `COMPLETED_WITH_LIMITATIONS` (a budget ran out; work not started is skipped, approvals
not yet acted on are invalidated, verified findings stand, unverified ones stay drafts), `EXPIRED` (the wall
clock ran out while the run waited for a person), `FAILED`, `REJECTED`, `CANCELLED`.

## Replan reason codes

`missing_dataset`, `empty_result`, `rejected_hypothesis`, `quality_failure`, `user_redirect`, `budget_downgrade`.
A playbook may declare a `replan_boundary` for a code; otherwise `user_redirect`, `rejected_hypothesis`,
`missing_dataset`, `empty_result` and `quality_failure` reset the `redirect` boundary, and `budget_downgrade`
resets nothing (it drops optional steps that have not started). Every replan bumps `plan_version`, changes the
plan hash and invalidates the run's pending and approved approvals; a plan gate that is still waiting asks
again for the new hash. `max_replans` bounds replans (429 `budget_exceeded` past it).
