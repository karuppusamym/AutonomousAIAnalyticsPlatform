# S13 — Analyst Request Desk

**Priority:** P0 · **Closes:** FR-042, FR-053 · **ADRs:** 011 · **Depends on:** S04, S06, S03

## 1. Purpose
Make analyst workload explicit, automate the repeatable share, route the rest to people with context
already prepared, and learn from every human answer. This is how the platform *reduces analyst load
measurably* rather than by assertion.

## 2. Data model (new package `desk/`)
```sql
CREATE TABLE request (
  id TEXT PRIMARY KEY, tenant_id UUID NOT NULL, workspace_id TEXT NOT NULL,
  requester_id TEXT NOT NULL, channel TEXT NOT NULL,          -- ui|api|mcp|teams|slack|email
  text TEXT NOT NULL, normalized_intent JSONB,                 -- metrics, dims, period, outcome
  class TEXT,                                                  -- R1..R9 (product spec §4)
  state TEXT NOT NULL,         -- new|triaged|auto_answering|auto_answered|auto_with_review|
                               -- in_investigation|escalated|answered|accepted|reopened|declined
  priority TEXT NOT NULL DEFAULT 'normal', sla_due_at TIMESTAMPTZ,
  assignee_id TEXT, conversation_id TEXT, run_ids TEXT[] DEFAULT '{}',
  answer_ref JSONB,            -- turn/evidence/artifact refs
  resolution TEXT,             -- auto|human|declined|duplicate
  human_minutes INT, cost_usd NUMERIC(10,4),
  duplicate_of TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), closed_at TIMESTAMPTZ
);
CREATE TABLE request_event (...);   -- typed timeline, outbox-published
CREATE TABLE class_baseline (tenant_id, class, baseline_minutes, source, measured_at);  -- from practitioner protocol
```

## 3. Flow
1. **Intake.** Any channel creates a `request` (and a conversation). Dedupe: if an open request in the same workspace has
   similar text (embedding cosine ≥ 0.92 and the same normalized intent) within 7 days, link it as a duplicate and follow it.
2. **Triage** (`ask_route` + `clarify_needed` ladder: rules → JEV → small LLM). Output: class, normalized intent, missing
   info, policy flags (R8 export → data owner). If blocking ambiguity exists, ask one clarifying question.
3. **Auto path.** R1/R2 run Ask. R3/R6 start an investigation (S06). R4/R5 propose a schedule or monitor (needs approval). R7 runs a
   context lookup, or a glossary suggestion to the owner.
   * `auto_answered`: governed answer, or verified evidence with no flags.
   * `auto_with_review`: an ad hoc answer in a workspace with `review_ad_hoc=true`, or one that touches sensitive classes. It goes to the
     analyst queue *with the answer prepared*. The analyst approves or edits it in about a minute.
4. **Escalation** (R9, refusals, low-coverage investigations, requester rejects): assign by workspace rota or skills,
   SLA by priority, attach the **auto-prepared data pack** (context receipts, candidate SQL, partial evidence).
5. **Close.** The requester accepts or reopens. An analyst answer offers **Certify** → verified query, metric suggestion,
   glossary entry or playbook (S03 ledger). A certified pattern lets the next similar request auto-resolve (AT-20).
6. **Metrics (FR-053).** Computed nightly into `desk_daily(tenant, workspace, class, n, auto_rate, accepted_rate, p50/p95 TTA,
   human_minutes, est_minutes_saved = Σ(baseline − human_minutes), cost)`. Shown on the Value screen and exportable.

## 4. API
```
POST /v2/requests                {workspace_id, text, channel?, priority?}  → 201 {request, conversation_id}
GET  /v2/requests?view=mine|queue|all&state=&class=
GET  /v2/requests/{id}           (timeline, answer, evidence links)
POST /v2/requests/{id}/actions   {type: accept|reopen|assign|escalate|answer|decline|certify|mark_duplicate, payload}
GET  /v2/value?workspace=&from=&to=
```
MCP: `request.create`, `request.status`.

## 5. Acceptance
AT-19, AT-20; `test_dedupe_links`; `test_sla_breach_alert`; `test_auto_with_review_requires_analyst`;
`test_value_metrics_math` (fixture with known baselines).

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S13-T1 | Models, migrations, state machine, events | 2d |
| S13-T2 | Intake from UI/API/MCP + dedupe | 2d |
| S13-T3 | Triage ladder + class routing to Ask/Run/Schedule/Context | 3d |
| S13-T4 | Escalation queue, SLA, assignment, data pack | 2d |
| S13-T5 | Certify-on-close into ledger | 1.5d |
| S13-T6 | Value metrics job + API | 1.5d |

## 7. As built: S13-T5 certify-on-close (2026-10-27)
* Action `certify` on `POST /api/requests/{id}/actions` (`desk/certify.py`), payload `{as, …}`. Analysts only. The
  request must be `answered` or `accepted`, with an analyst's answer (`via` human or auto_with_review).
  * `verified_query` promotes the analyst's successful Ask or console statement (`query_id`, or the first `query`
    evidence item of the answer), with the request text as its phrasing (`registries/verified_queries.promote`, which
    validates it for the analyst). Ledger: `certified`, `source=desk`. The next similar request triages R1, and the
    Ask registry rung answers it without a model (AT-20).
  * `glossary` writes a `context_entry` (`origin=desk_certified`; mapped columns must be readable by the analyst).
    Ledger: `approved`.
  * `metric` writes a knowledge suggestion for the owner's review queue. Ledger: `suggested`. Only the semantic
    layer's own approval certifies a metric, never the desk.
  * `playbook` keeps the steps on the request (`answer_ref.playbook`). Ledger: kind `playbook`, `approved`.
* Text that reads like an instruction to a model is refused (422). Each certification is appended to
  `answer_ref.certified` and written to the timeline (`certified`, event `request.certified`) and the audit trail.
  It is projected into the ledger in the same transaction.

## 8. As built: value rollup, export, R4/R5 drafts, screens (2026-10-27)
* `desk_daily(workspace, day, class, n, duplicates, closed, accepted, auto_resolved, auto_resolution_rate, accepted_rate,
  tta_p50/p95, human_minutes, est_minutes_saved, unbaselined, cost)`, keyed by the day requests were created; class `all`
  is the day total. Tenant comes from the workspace (RLS). Migration `0066_desk_daily`.
* Nightly: `desk/rollup.maybe_rollup_nightly` (`@platform_loop`, advisory lock + `desk.value_rollup` audit marker, once
  per 20 h) picks workspaces with requests in the last 14 days and calls `rollup_workspace` (`@owned_by("workspace")`),
  which recomputes those days. The scheduler calls it as `nightly_desk_value`; its housekeeping pass also runs
  `sweep_sla_all` (same pattern) so SLA breaches are flagged without anyone opening the queue.
* API: `GET /api/value` adds `daily` (rollup, class `all`) and `rollup_computed_at`; `POST /api/value/rollup` (analyst)
  recomputes a range now; `GET /api/value/export` is the rollup as CSV (aggregates only, no request text);
  `GET /api/value/baselines`. Ranges are at most 366 days.
* R4/R5 escalate with `data_pack.proposal`: a draft schedule (cadence → cron) or monitor (threshold → op/value, baseline
  → `metric_drift`). The desk never creates either; the analyst submits it through the approval-gated APIs.
* Screens (S11-T1, S11-T7): Work → Request desk (`/w/:wsId/work/desk/:requestId?`), Overview → Value (`/w/:wsId/value`).
