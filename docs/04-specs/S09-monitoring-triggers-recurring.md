# S09 — Proactive monitoring, triggers and recurring work

**Priority:** P1 · **Closes:** FR-020, FR-021, FR-022, FR-049, FR-050, FR-054 · **ADRs:** AnalystOS 0009, 0021

## 1. Purpose
Run pre-approved analyses on schedules and events without noise, and automate the recurring analyst work
that consumes the most hours.

## 2. Baseline to reuse
`services/{schedules,monitors,notifications,deliveries,reports}.py`, `Schedule`, `ScheduleRun`, `Monitor`, `Alert`,
`Notification`, `DeliveryDestination`, `Delivery`, claim-then-execute scheduler, pinned playbook versions with "upgrade
available", the external delivery service (commit `7169481`). Donor: Atlas Kafka outbox and consumer patterns, and its
weekday/month-end DQ baselines (already ported per tracker).

## 3. Build
1. **Trigger ingress**
   * `POST /v2/triggers/webhook/{rule_id}`: HMAC-SHA256 over `timestamp.body` with a per-rule secret (`secret_ref`),
     ±5 min skew, `X-Event-Id` replay cache (7 days, in Postgres), typed payload schema per rule, and a 202 after durable enqueue.
   * Kafka (profile `events`): a consumer group per rule set, topic ACL, typed transform → trigger, commit after enqueue.
   * Freshness: a source `get_freshness()` poll (S02) compared with a contract.
2. **Anti-noise**: per-rule `debounce` (window), `cooldown`, `quiet_hours` (tz explicit), hysteresis (`enter` / `exit`
   thresholds), a `correlation_key` (for example metric+segment) that merges alerts into one incident, and per-dataset and
   per-tenant frequency caps. All of these are configuration on the `monitor` row.
3. **Rule → goal compiler**: a monitor instantiates a **pinned** playbook with the rule's frozen scope and budget. The
   monitor itself is approved once (a hash-bound approval). Changing scope or recipients needs re-approval.
4. **Recurring packs (FR-050)**: a `schedule` of a template playbook (`kpi_review`, `variance`, `funnel`, `cohort`, `dq_review`)
   + an output template (dashboard refresh, PDF/email brief, Teams card). The first run requires review. Later runs
   auto-deliver if `auto_deliver_after_n_clean_runs=3` and no verification is VOID; otherwise they route to review.
5. **Channels (FR-022, FR-054)**: email (SMTP/Graph), Microsoft Teams (incoming webhook / Bot Framework), Slack,
   generic webhook. Payloads hold only permitted aggregates and deep links. Every outbound message checks
   `approvals.verify_for_execution` against the rule's approval. Inbound Teams/Slack/email messages create Desk requests (S13).

## 4. Example rule (v1 §9.2, extended)
```yaml
rule_id: pay_fail_us
metric: payment_failure_rate@4
scope: {source: payments_ro, filters: {region: US}}
schedule: {cron: "0 8 * * *", tz: America/New_York}
condition: {enter: "observed >= baseline + 0.02 AND n >= 1000", exit: "observed < baseline + 0.01"}
baseline: {method: same_weekday_4w}
debounce: 2h
quiet_hours: "22:00-07:00"
correlation_key: [metric, processor]
on_trigger: {playbook: investigate.v1@3, limits: {max_cost_usd: 3, max_steps: 12}}
notify: {channel: teams:payments-ops, mode: request_approval}
```

## 5. Acceptance
AT-10, AT-23, AT-24; `test_hysteresis_no_flap`; `test_quiet_hours_defer`; `test_outbound_requires_approval`;
`test_recurring_auto_deliver_after_clean_runs`.

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S09-T1 | Signed webhook ingress + replay cache | 1.5d |
| S09-T2 | Kafka trigger consumer (profile) | 2d |
| S09-T3 | Debounce/cooldown/quiet hours/hysteresis/correlation | 2.5d |
| S09-T4 | Recurring pack scheduler + auto-deliver policy | 2d |
| S09-T5 | Teams, Slack, email channel adapters (in and out) | 4d |
