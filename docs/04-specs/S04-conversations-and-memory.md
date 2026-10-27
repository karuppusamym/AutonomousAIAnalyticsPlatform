# S04 — Conversations and memory

**Priority:** P0 · **Closes:** FR-003, FR-009 (with S08), FR-041 · **Depends on:** S01, S03

## 1. Purpose
Persist conversations so they can be resumed, branched, audited and *learned from*, without turning raw
data into long-term model memory. Make conversation-derived knowledge available to other apps and agents
through the Context Ledger, under review.

## 2. Baseline to reuse
`AskThread` / `AskTurn` (question, route, sql, chart, result, provenance, decisions, promotions; private to the
creator, scoped by workspace), `services/ask.py`, `services/ask_outputs.py`, follow-ups answered from prior scoped
context (commit `bdd18c9`), promotion of Ask answers to investigations (`c947372`), `Feedback`, `VerifiedQuery`.

## 3. Memory tiers
| Tier | Lifetime | Content | Who can read | Store |
|---|---|---|---|---|
| T0 Turn working memory | one turn | context bundle, candidate SQL, intermediate results | the turn | in-memory + `AskTurn.stages` |
| T1 Session state | conversation | active filters, selected sources, metric bindings, accepted clarifications, last result handles | conversation participants | `conversation_state` (new, versioned JSON) |
| T2 Personal preferences | user, TTL 180d | "I mean fiscal quarters", default region | the user only | `context_ledger_entry(trust=personal)` |
| T3 Workspace knowledge | until superseded | approved corrections, glossary, verified queries, summaries | workspace (policy) | ledger `approved/certified` |

**Promotion rule:** T1 → T2 happens automatically when the user confirms a correction. T2 → T3 needs owner approval (a
suggestion in `knowledge/suggestions.py`). Personal memory **never** changes an organization definition silently (v1 §6.5).

## 4. Build
1. **Rename and generalize.** Keep the existing tables. Expose them as `conversation` and `message` in API v2 (views or ORM
   aliases). Add `tenant_id`, `channel` (`ui|api|mcp|teams|slack|email`), `request_id` (S13), `parent_conversation_id`,
   `branch_from_message_id`.
2. **`conversation_state`** (T1): `{filters[], sources[], metric_bindings{}, time_window, clarifications[], result_handles[]}`.
   It is versioned per turn so a branch restores state exactly.
3. **Summaries.** On idle (30 min) or close, the `summarization` purpose produces a structured summary
   `{questions[], answers[{claim, evidence_ref, label}], corrections[], open_questions[]}`. The numbers guard applies:
   every number must cite a turn's computed result. It is written to the ledger as `conversation_summary`
   (trust `workspace` only if the workspace policy `share_conversation_summaries=true`, otherwise `personal`).
4. **Corrections.** Typed: `{term, meaning, scope: session|personal|workspace_proposed}`. Applying a correction
   creates T1 state immediately. "Remember this" creates T2. "Make this the team definition" creates a T3 suggestion to the owner.
5. **Retention.** Per tenant policy: messages 365d, result payloads follow handle TTL, summaries keep, legal hold
   flag per conversation. A deletion request removes messages, state, personal ledger entries and embeddings,
   and records a tombstone in the audit log.
6. **Cross-app read API.** Other apps read *summaries and approved knowledge*, never other users' raw messages:
   ```
   GET  /v2/conversations?mine=true
   GET  /v2/conversations/{id}                (participant)
   POST /v2/conversations/{id}/messages       {text, attachments?, mode?: ask|investigate|edit}
   POST /v2/conversations/{id}/branch         {from_message_id}
   GET  /v2/conversations/{id}/state
   GET  /v2/conversations/{id}/summary
   POST /v2/conversations/{id}/corrections    {term, meaning, scope}
   ```
   MCP: `conversation.start`, `conversation.say` (both create Desk requests), `conversation.summary` (participant only).
7. **Never stored as memory:** raw rows from restricted columns, credentials, full prompts containing data samples
   (prompts are kept as hashes plus a redacted payload in `ModelPayload` per existing policy).

## 5. Acceptance
AT-18; `test_branch_restores_state`; `test_summary_numbers_cite_turns`; `test_personal_memory_isolated`;
`test_retention_delete_cascades_embeddings`; follow-up regression tests from the baseline remain green.

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S04-T1 | v2 conversation API over AskThread/AskTurn + tenant/channel fields | 2d |
| S04-T2 | `conversation_state` + branch | 2d |
| S04-T3 | Summaries + ledger write + policy | 2d |
| S04-T4 | Typed corrections + promotion flow | 2d |
| S04-T5 | Retention + delete cascade + legal hold | 1.5d |
| S04-T6 | MCP conversation tools | 1d |

## 7. As built (2026-10-27, T1–T6)
Code: `src/analystos/conversations/` (`service`, `state`, `corrections`, `summaries`, `retention`, `ledger`, `models`),
`api/routers/conversations.py` (prefix `/v2`), MCP tools in `mcp/server.py`, migration `0061_conversations`.
Tests: `tests/unit/test_conversations.py`, `tests/integration/test_conversations.py` (Postgres, RLS, strict tenancy).

Deviations and decisions:
* **Conversation = `ask_thread`, message = `ask_turn`** through the service layer (no SQL views). `tenant_id` is the
  existing trigger-filled column (shown from the workspace); new columns `channel`, `request_id`,
  `parent_conversation_id`, `branch_from_message_id`, `legal_hold`, `closed_at`, `summarized_at`.
* **Participants** are the owner only (no sharing yet). v2 has no admin bypass to messages or state; a platform admin
  may file a deletion request. Another member, workspace or tenant gets the same 404.
* **Branches inherit by reference.** A branch shows its parent's messages up to the branch point (`inherited: true`),
  continues the numbering, and its follow-ups build on the branch-point answer. State versions carry `at_seq`; a branch
  copies the latest version with `at_seq <= N` (corrections made after N but before N+1 are included).
  `mode=edit` = branch just before the edited message, then ask there. `mode=investigate` starts a run and records a
  message with status `delegated` and the run id. Attachments are references (`message` | `artifact`), never content.
* **Ledger interface.** The S03 `context_ledger_entry` table is not on `main` yet. S04 writes through
  `conversations/ledger.py` (`ContextLedger.upsert/remove/remove_for_conversation`); the default `InterimLedger` stores
  rows in `conversation_memory_entry`, which has the S03 columns plus `conversation_id` (for deletion). **S03 to do:**
  install its writer with `set_ledger(...)` (or move these rows into `context_ledger_entry`) — the S04 callers do
  not change. Owner rows stay in `conversation_summary` / `conversation_correction`.
* **Corrections.** T2 personal memory is `conversation_correction(scope=personal)` (180-day TTL), projected with
  trust `personal`, owner-only. "Make this the team definition" proposes a `knowledge_suggestion` (kind `definition`,
  subject `term:<key>`, origin `conversation.correction`); the existing knowledge review (workspace editor or owner)
  approves it, and a hook in `knowledge/suggestions.review` marks the correction `approved` (ledger trust `approved`).
  Promotion is `POST …/corrections/{id}/promote {to}` (session → personal → workspace_proposed). Memory precedence is
  session > personal > approved team; a differing meaning is reported as an override and named in the Ask stages,
  never applied silently. The generation prompt receives clarifications as labelled data
  (`conversation_clarifications`).
* **Summaries.** Rules build one claim per answered message from its stored result; the `summarization` purpose may
  reword claims only through `model_gate` (in `auto` mode the rules summary is used, so no call). Every claim passes
  the numbers guard against the cited message's result; failing model claims are dropped and listed in
  `guard.dropped`. The idle sweep (30 min, scheduler housekeeping) uses rules only. The model sees questions,
  explanations and single-row aggregates, never result rows. Policy flag `share_conversation_summaries`
  (workspace policy, default false) selects trust `workspace` over `personal`.
* **Retention.** `tenant.settings.conversations = {messages_days: 365, results_days: 30}` (API
  `/v2/tenants/{id}/conversation-retention`, tenant admin). There is no result-handle store yet (S02-T3), so
  `results_days` clears the rows inside stored answers and keeps query id and result hash. Personal memory expires
  after 180 days. The sweep runs from scheduler housekeeping at most hourly, per tenant binding; legal holds are
  skipped. Legal hold is set by a tenant admin or auditor (or platform admin) and blocks deletion (409).
* **Deletion cascade** removes messages, their citation sets, state versions, summaries, session and personal
  corrections, and every memory entry with its embedding; the audit tombstone holds counts only. Kept on purpose:
  a team correction under review (detached), the Desk request record (its link is cleared), model-call and decision
  receipts (hashes and redacted payloads, as before). **Not yet removed:** analysis steps a user ingested from an Ask
  thread (`analysis_step` with container `ask_thread`).
* **MCP.** `conversation.start` creates a conversation (channel `mcp`) and submits the first message as a Desk request
  bound to it; `conversation.say` submits a Desk request in an existing conversation of the same client;
  `conversation.summary` returns the latest summary (refreshed when messages arrived). Grants: start/say need an
  analyst grant (like `request.create`), summary a viewer grant. A Desk duplicate link may decline a `say` (no message).
* Not built: Teams/Slack/email intake (the channel values are accepted), sharing a conversation with more participants,
  UI screens.
