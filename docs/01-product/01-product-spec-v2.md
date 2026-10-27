# Product specification v2 — Autonomous AI Analytics Platform

Supersedes the product sections (1–3, 14, 16, 18) of the v1 `.docx`. The architecture is in
[`../02-architecture/01-architecture.md`](../02-architecture/01-architecture.md). Development specs are in `../04-specs/`.

## 1. Product statement

A governed analytics workforce. Business users ask questions or state goals. The platform answers
certified questions instantly, investigates open ones autonomously within budgets, produces verified
dashboards and reports, monitors KPIs proactively, and escalates to human analysts only what it cannot
answer with evidence. Every answer, definition and conversation becomes reusable, versioned context
that other applications and agents can query.

**Positioning against the v1 doc:** v1 describes *a tool analysts use*. v2 describes *a service that does
the repeatable share of analyst work and makes analysts reviewers and owners of meaning.*

## 2. Personas and what changes for them

| Persona | Today | With the platform |
|---|---|---|
| Business requester | Files a ticket, waits days | Asks in chat, Teams or email. Gets a governed answer in seconds or an investigation in minutes. Sees the evidence. |
| Data analyst | Writes the same SQL weekly; builds dashboards by hand | Owns metrics and glossary, reviews escalations and certifies answers, designs playbooks. Handles novel questions. |
| Operations / domain owner | Notices problems late | Gets proactive alerts with a first-pass root cause and an approval-gated brief |
| Data engineer | Debugs "why is this number different" | Uses the fingerprinted lineage "Why this number?", DQ monitors and connector health |
| Workspace / tenant admin | — | Models, budgets, tools, policies, approvals, audit, chargeback |
| External app / agent | — | Calls MCP or REST for certified metrics, context, findings and conversation memory |

## 3. Modes

| Mode | Trigger | Autonomy | Output |
|---|---|---|---|
| A. Ask | chat, API, MCP, Teams/email | bounded synchronous (≤ 3 queries, ≤ 30 s, ≤ $0.10 default) | answer, chart, evidence link |
| B. Investigate (goal) | user goal, escalation from Ask, Desk triage | plan → execute → verify → draft, stops at approval gates | findings, evidence graph, dashboard/report draft |
| C. Proactive | cron, monitor, freshness, webhook, Kafka | only pre-approved pinned playbooks and scopes | alert, brief, optional investigation |
| D. Assisted authoring | user edits a chart, dashboard or metric in conversation | typed patch operations only | new artifact version |
| E. Recurring work | schedule of a pinned pack (weekly KPI, month-end variance…) | as Mode C, with a fixed output template | report/dashboard refresh + narrative |

Authorization modes are orthogonal: `observe` · `analyze_and_draft` · `request_approval` · `approved_publish`.

## 4. Analyst work model — what gets automated

The **Request Desk** (S13) is the front door. Every request is classified into one of these classes, and each
class has a default handling path. The platform's value is the share of requests that end in
`auto_answered` or `auto_with_review` *and are accepted*.

| Class | Example | Default path | Human role |
|---|---|---|---|
| R1 Certified metric lookup | "Revenue by region last quarter" | Ask → semantic compiler | none (sampled QA) |
| R2 Ad-hoc slice | "Top 10 merchants by refund count in March" | Ask → validated ad hoc SQL, labelled `ad_hoc` | optional certify |
| R3 Explain a change | "Why did failures rise 18% this week?" | Investigate `investigate.v1` | review findings before share |
| R4 Recurring report | weekly ops pack, month-end variance | Recurring pack (Mode E) | approve first run, then spot check |
| R5 Monitoring | "Tell me if failure rate > baseline+2pp" | Monitor rule (Mode C) | approve the rule |
| R6 Dashboard request | "Build me a churn dashboard" | Investigate + dashboard draft (D) | approve publish |
| R7 Definition request | "What counts as an active customer?" | Context Service. If undefined, a glossary suggestion goes to the owner | owner decides |
| R8 Data request / export | "Send me the raw rows" | Policy check. Usually escalated | data owner approves |
| R9 Novel / strategic | "Should we enter market X?" | Escalate with an auto-prepared data pack | analyst leads |

Learning loop: every escalated request answered by a human is offered for **certification** (verified
Q→SQL, metric, glossary entry, playbook). The next similar request then moves up a class (R2 → R1).

## 5. Functional requirements v2

v1 FR-001…FR-036 are carried forward unchanged in meaning. Status per FR is in the
[review §5](../00-review/01-design-review-of-v1.md#5-functional-requirement-coverage-v1-fr--existing-code).
New requirements:

| ID | Pri | Requirement | Spec |
|---|---|---|---|
| FR-037 | P0 | Tenant → workspace hierarchy with tenant-scoped identity, data, caches, object paths and Superset connections; Postgres RLS enforced | S01 |
| FR-038 | P0 | Model configuration in the Admin UI: providers, profiles, per-purpose routing, ladders, allowlists per tenant/workspace, budgets, residency. Versioned, with rollback | S05 |
| FR-039 | P0 | Providers: OpenRouter, TypeSafe JEV (decision), Anthropic, OpenAI, Azure OpenAI, Gemini/Vertex, Bedrock, OpenAI-compatible (local/self-hosted) | S05 |
| FR-040 | P0 | Context Service API (REST + MCP): search, get, compile with receipts, access checked at read time | S03 |
| FR-041 | P0 | Conversations persisted with memory tiers (turn, session, workspace-approved, certified); summaries and accepted corrections written to the Context Ledger | S04 |
| FR-042 | P0 | Request Desk: intake from UI/API/MCP/email/Teams, triage class R1–R9, auto-answer, escalate with SLA, certify on close | S13 |
| FR-043 | P0 | Hard compute isolation (gVisor/Kata) for Python and statistics | S07, S12 |
| FR-044 | P0 | Append-only audit enforced at the DB layer; audit export (JSONL/OTel) | S01 |
| FR-045 | P1 | OKF v0.2 pack export/import with byte-stable round trip; OpenLineage and ODCS exports | S03 |
| FR-046 | P1 | Data products as tools: certified metric, saved query, dataset or playbook exposed as a typed tool (REST + MCP) with grants | S10 |
| FR-047 | P1 | External MCP servers admitted by an admin; imported tools default to `write_external` until classified; description injection screening | S10 |
| FR-048 | P1 | Typed dashboard patch operations from conversation (v1 §8.2 table), each one a new version | S08 |
| FR-049 | P1 | Signed webhook and Kafka trigger ingress with replay protection; debounce, quiet hours, hysteresis, correlation | S09 |
| FR-050 | P1 | Recurring packs (Mode E) with templates: weekly KPI, month-end variance, cohort retention, funnel, DQ review | S09 |
| FR-051 | P1 | Multi-source analysis via staged snapshots in DuckDB (or Trino where present) with declared data-movement policy | S02 |
| FR-052 | P1 | Cost attribution per request/run/workspace/tenant (LLM, warehouse, compute); budgets and chargeback export | S12 |
| FR-053 | P1 | Value dashboard: auto-resolution rate, time to answer, analyst minutes saved (estimated from a class baseline), accepted rate, confident-wrong rate | S13 |
| FR-054 | P1 | Teams/Slack/email channel adapters for Ask and alerts (approval-gated outbound) | S09, S13 |
| FR-055 | P1 | Per-model evaluation matrix: golden suite run per profile; a release gate blocks a model switch that regresses | S05, S12 |
| FR-056 | P2 | Local Text2SQL model fine-tuned with the DB-GPT-Hub recipe for air-gapped tenants, benchmarked on the golden suite | S05 |
| FR-057 | P2 | Context federation: subscribe to another tenant's or partner's published OKF packs (read-only, versioned) | S03 |
| FR-058 | P2 | Analyst skill authoring: YAML playbook editor with simulation on a fixture, signing, publish | S06, S10 |

## 6. Value and quality metrics (measured, never asserted)

| Metric | Definition | Pilot target (proposed) |
|---|---|---|
| Auto-resolution rate | accepted requests closed `auto_answered`/`auto_with_review` ÷ all closed | ≥ 50% R1–R6 by week 8 of pilot |
| Time to first answer | p50/p95 request created → first answer | p50 < 30 s (R1/R2), < 15 min (R3) |
| Analyst minutes saved | Σ(class baseline minutes − human minutes spent), with baselines from the practitioner protocol | reported, not targeted, until baselines exist |
| Accepted-answer rate | accepted ÷ answered (requester or reviewer) | ≥ 85% |
| Confident-wrong rate | answers labelled verified that are later corrected | ≤ 1% (hard gate 0 on golden suite) |
| Cost per accepted answer | Σ cost ÷ accepted | tracked by class |
| Escalation SLA met | escalations answered within SLA | ≥ 90% |

## 7. Non-functional requirements (v1 §16 carried, with additions)

| Area | Target |
|---|---|
| Ask latency | p95 < 12 s on the reference dataset (v1); p95 first SSE event < 2 s |
| Run durability | no lost accepted goal across worker kill (AT-09) |
| Tenant isolation | 0 cross-tenant reads in the negative suite; RLS-coverage CI test 100% |
| Policy | 100% of the adversarial suite denied and logged |
| Availability | 99.9% monthly (pilot), excluding source outages |
| Scale (pilot) | 200 concurrent users, 30 active investigations, 50 queries/s through the gateway (validate by load test) |
| Recovery | RPO ≤ 15 min (PITR), RTO ≤ 4 h; quarterly drill |
| Accessibility | WCAG 2.2 AA on Ask, Desk, Run, Builder, Publish |
| Residency | a model call to a provider whose region is outside workspace policy is refused (fail closed) |

## 8. Acceptance scenarios

v1 AT-01…AT-12 are carried forward unchanged. Added:

| ID | Scenario | Pass criteria |
|---|---|---|
| AT-13 | Cross-tenant RLS | With RLS on, a deliberately unfiltered ORM query from tenant A's session returns 0 rows of tenant B. The CI enumerator finds no tenant table without a policy. |
| AT-14 | Model switch | Admin switches `sql_generation` from OpenRouter/qwen to Azure/gpt-5.4 in the UI. The next call uses it, `model_call` records the new provider, and rollback restores the previous version. |
| AT-15 | Residency | Workspace policy `data_residency=eu`. A profile whose only model is US-region fails closed with a visible error. No call is made. |
| AT-16 | OKF round trip | Export pack → import into a clean tenant → export gives byte-identical files. Access-restricted entries are absent from the export of a user without access. |
| AT-17 | External agent context | An MCP client with grant `context.read` on workspace W calls `context.compile`. It gets a receipt and only W's approved entries. Calling for W2 gives 403. |
| AT-18 | Conversation memory reuse | A correction accepted in conversation 1 ("active = logged in within 30 days") is applied in conversation 2 by another user of the same workspace only after owner approval. Before approval it applies only to the author's session. |
| AT-19 | Desk auto-resolve | 20 seeded R1/R2 requests: ≥ 18 auto-answered with correct numbers versus reference SQL; the rest escalated, none answered wrongly. |
| AT-20 | Escalation learning | Analyst answers an escalated R2. They certify it. A paraphrased repeat request is answered R1 from the verified query. |
| AT-21 | Compute isolation | A sandboxed step attempting `socket.connect` or a file write outside its volume fails, and the attempt is audited. |
| AT-22 | Data tool via MCP | A certified metric exposed as `metric.payment_failure_rate` is callable over MCP with a dimension filter. The response carries `governed`, metric version and freshness. |
| AT-23 | Webhook replay | Same signed webhook ×3 plus one with an expired timestamp: one goal, one rejection audited (extends AT-10). |
| AT-24 | Recurring pack | Weekly KPI pack pinned to v3; a v4 is published; the schedule keeps v3 and shows "upgrade available". |

## 9. Out of scope (initial release)

Unchanged from v1 §1.3, plus: writing back to source systems; training foundation models (fine-tuning a
local SQL model is P2 and optional); acting as a general-purpose chat assistant outside analytics scope.
