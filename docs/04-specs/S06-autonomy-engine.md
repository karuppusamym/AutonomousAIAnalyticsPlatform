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
