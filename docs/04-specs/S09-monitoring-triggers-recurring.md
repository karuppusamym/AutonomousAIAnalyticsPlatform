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

## 7. As built (2026-10-27, tasks T1–T5)
* **Package** `src/analystos/triggers/` (`ingress`, `processor`, `kafka`, `noise`, `conditions`, `groups`, `rules`,
  `packs`, `channels`, `models`); API `api/routers/triggers_v2.py`; migration `0066_triggers` (revises
  `0065_tool_registry`; tables `trigger_rule`, `trigger_event`, `alert_group`, `channel_binding`, `channel_message` with RLS
  and fill trigger; `monitor.noise_state/approval_id/rule_hash`, `alert.group_id/deferred_until/notified_at`).
* **Ingress (T1).** As §3.1. Headers `X-AnalystOS-Timestamp`, `X-AnalystOS-Signature: sha256=<hex>` (the scheme
  outbound N-3 webhooks already use), `X-Event-Id`. Rule secrets live in their own namespace
  (`env:ANALYSTOS_TRIGGER_<NAME>`, 16+ characters). The replay cache is the unique `(rule_id, event_id)` of the queue
  row itself (`expires_at` = +7 days); a replay answers 200 `duplicate` so a retrying sender stops. Unknown, disabled
  and unsigned requests are all 401. A rule's `action` is `evaluate` (re-run the monitor through the gateway) or
  `observe` (the typed payload is the reading; requires the monitor's `condition.enter`).
* **Kafka (T2).** As §3.1 with the ACL as configuration: `ANALYSTOS_KAFKA_TOPIC_ACL=<tenant|*>:<glob>,...` (empty =
  nothing). A record invalid for a rule is stored as a `failed` trigger event and still committed (no poison loop);
  a database failure commits nothing. Event id: the rule's `transform.event_id` pointer, else `/event_id`, else
  `topic:partition:offset`.
* **Anti-noise (T3).** All keys on `monitor.config` (§4 shape): `condition: {enter, exit}` (hysteresis; without it
  the monitor's own verdict and its negation), `debounce` (the condition must still hold that long after it was first
  seen — a single spike never fires), `cooldown`, `quiet_hours: {window, tz, allow_critical}` (the zone is required;
  `timezone` at the top level also counts), `caps: {per_dataset_per_day, per_tenant_per_day}`, `correlation_key`.
  Conditions are parsed to an AST and only arithmetic, comparisons, and/or/not and abs/min/max run; a missing value is
  *unknown* and keeps the state. Monitors without any gate key keep the previous path unchanged. Quiet hours record
  the alert and announce it (notification + deliveries) when the window ends (`release_deferred`, housekeeping).
  A correlated alert joins the open **alert group** for its key values (table `alert_group`, API `/v2/workspaces/{id}/alert-groups`: the spec's *incident*, renamed because core code must not use the ITSM word, `tests/unit/test_domain_packs.py`) and only the group's first alert is announced.
* **Rule approval (§3.3).** A monitor with `notify: {channel: <kind>:<destination name>}` or
  `on_trigger: {playbook: key@version, limits}` requests `monitor_rule.approve` (always separate duties) whose payload
  is its kind, metric, scope, recipients (with destination hashes), notify mode and playbook/limits. Edits that change
  it re-request approval (`after_config_change`); anti-noise edits do not. Each alert delivery verifies it
  (`deliveries._authorize_rule`, also refusing a destination the rule does not name), and `start_goal` verifies it
  before starting the pinned playbook with the rule's limits and frozen scope (a refusal is recorded on the alert).
  The approval is standing for `delivery_authorization_days` once approved.
* **Packs (T4).** Schedule kind `pack`, config `{template, output: {kind}, deliver_to, question,
  auto_deliver_after_n_clean_runs}`; `version` is filled with the installed template version and pinned
  (`{key, version, source: builtin}`). The delivery decision is taken when the run's report arrives
  (`schedules._finish` -> `packs.route`); a review is `pack_delivery.review`, bound to the report's content hash and
  the destinations' hashes, consumed on approval; "clean" = delivered with no VOID verification.
* **Channels (T5).** Destination kinds `teams` and `slack` (`config.url_ref = env:ANALYSTOS_WEBHOOK_<NAME>`: the
  incoming-webhook URL is a secret, its host checked against `ANALYSTOS_CHANNEL_TEAMS_HOSTS` / `_SLACK_HOSTS` at
  every send); `ANALYSTOS_DELIVERY_EMAIL_TRANSPORT=graph` sends email through Graph `sendMail`. Cards carry severity,
  title, the monitor's aggregate message or the report counts and a deep link (`/w/{ws}/alerts|outputs/{id}`).
  Inbound: `POST /v2/channels/{slack|teams|email}/{binding_id}`, one binding per workspace channel with its own
  `env:ANALYSTOS_CHANNEL_<NAME>` secret and optional `external_key` (Slack team, Teams tenant, mailbox); the message
  becomes a Desk request (`submit(..., process=False)`), its text never an instruction. Teams outgoing webhooks carry
  no timestamp, so their replay guard is the activity id alone, kept for the replay window.
* **Not built:** the freshness poll trigger (§3.1 third bullet), rule/alert-group/binding UI, replies in the originating
  thread, Teams Bot Framework (JWT) bots, a run against a real Kafka broker or real Teams/Slack/Graph tenants.
