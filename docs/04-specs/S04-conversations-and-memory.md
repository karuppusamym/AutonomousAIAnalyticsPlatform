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
