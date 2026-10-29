# S03 — Context Service, Context Ledger and OKF interchange

**Priority:** P0 (service + ledger API) / P1 (OKF CI, federation) · **Closes:** FR-004, FR-024, FR-040, FR-045, FR-057 · **ADRs:** 004, 007, 008

## 1. Purpose
Context is the product's long-lived asset. The service must (a) give every agent the right, permitted,
versioned context within a token budget, (b) let other applications and agents read the same context, and
(c) export and import it in open formats for review and portability.

**The answer to "OKF for export, or used by the agent?":**

| Job | Mechanism | Why |
|---|---|---|
| Agent reads context at runtime (ours or external) | **Context Service** `compile/search/get` (in-process, REST, MCP) | per-reader access, freshness, budgets, receipts, injection screening |
| Review a change to definitions | **OKF pack** in Git (pull request) | human-diffable, offline |
| Move context between environments or tenants | **OKF pack** export → import | portable, versioned |
| Share with partner systems (Atlas, Data catalogs) | **OKF pack** + **OpenLineage** + **ODCS** | open standards |
| Prove how a number was computed | **Attested computation** record + evidence | reproducible |
| Store of record | **Postgres** knowledge/semantic/ledger tables | transactions, RLS |

## 2. Baseline to reuse
`context/compiler.py::compile_context` (relevance gates, budgets, receipts, omissions, injection screening),
`context/service.py`, `context/export.py`, `knowledge/okf.py` (pinned OKF v0.2 profile, conformance self-check),
`knowledge/{bundle,store,index,entries,suggestions,studio,providers,attested}.py`, `knowledge/{dbt_manifest,superset_meta,crawl_docs,documents}.py`,
`semantic/` (metrics, relationships, compiler), `services/crawler.py`, `mcp/server.py` resources
(`knowledge`, `metrics`, `findings`, `datasets`). Docs: `docs/10-architecture/okf-profile.md` in AnalystOS.

## 3. Context Ledger (new, small)
A single typed index over everything reusable, so other apps and agents have one place to look. Existing
tables remain the owners. The ledger is a projection plus a few new kinds.

```sql
CREATE TABLE context_ledger_entry (
  id TEXT PRIMARY KEY, tenant_id UUID NOT NULL, workspace_id TEXT NOT NULL,
  kind TEXT NOT NULL,          -- glossary|metric|relationship|verified_query|finding|decision|
                               -- conversation_summary|correction|playbook|dataset|document_section
  ref_table TEXT NOT NULL, ref_id TEXT NOT NULL, version INT NOT NULL,
  trust TEXT NOT NULL,         -- certified|approved|workspace|personal|suggested
  visibility TEXT NOT NULL,    -- workspace|tenant|exported
  title TEXT NOT NULL, summary TEXT NOT NULL,  -- no raw result rows, ever
  classification TEXT NOT NULL, valid_from TIMESTAMPTZ NOT NULL, valid_to TIMESTAMPTZ,
  fingerprint TEXT,            -- verification fingerprint where applicable (S07)
  embedding VECTOR(768), embedding_model TEXT,
  owner_id TEXT, source TEXT,  -- crawler|human|conversation|import:okf|mcp
  UNIQUE (tenant_id, ref_table, ref_id, version)
);
```
Writers: the semantic and knowledge services on approve/publish, verification on verify/void, S04 on
summary/correction acceptance, S07 on finding verification. Each is written in the same transaction through an outbox
consumer, so the ledger never runs ahead of the owner.

**Trust precedence in retrieval:** certified > approved > workspace > personal (author only) > suggested
(never in model inputs unless the caller asks). Conflicting certified definitions **block** (v1 §6.2.4).

## 4. Retrieval
Hybrid: exact name/alias → glossary synonyms → lineage proximity → pgvector similarity, reranked by
`trust × freshness × in_scope × authorization`. Access is filtered **before** ranking, so inaccessible items never appear.
`compile()` returns `ContextBundle{sections, receipts[], omissions[], identity_hash, budget_used}`. The identity
hash enters the run's plan hash, so a context change voids dependent verifications (S07).

Close the 2026-10-25 review's P0 item: the **reviewed brief / pinned context** is a first-class compiler section, included
in the cache identity and receipts. If a required definition does not fit the budget, the compiler refuses visibly. It never silently trims.

## 5. External interfaces
```
GET  /v2/context/search?q=&kinds=&workspace=      → [{id, kind, title, summary, trust, version}]
GET  /v2/context/entries/{id}                      → full entry (body from owner table)
POST /v2/context/compile {purpose, objective, workspace, budget_tokens}
                                                   → {bundle, receipts, identity_hash}
GET  /v2/context/changes?since=cursor              → change feed for downstream caches/agents
POST /v2/context/export  {workspace, kinds, format: okf|openlineage|odcs} → 202 + artifact handle
POST /v2/context/import  (OKF pack, multipart)     → suggestions (never auto-certified)
```
MCP tools (S10 grants): `context.search`, `context.get`, `context.compile`, `context.changes`.
MCP resources: `analystos://{ws}/{kind}/{id}` (existing scheme, extended to ledger kinds).

## 6. OKF interchange rules
* Pinned profile: OKF v0.2 at the upstream revision recorded in `contracts/okf_profile_pin.json` (inherited).
  Bump it only with a new ADR and a fixture update.
* Export is deterministic (sorted keys, stable ids, LF line endings, no timestamps except `valid_from`). A CI test checks
  `export → import(clean tenant) → export` is byte-identical (AT-16).
* Export respects the caller's access. Restricted entries are omitted and listed in `log.md` as counts only.
* An import always lands as **suggestions** with `source=import:okf` and provenance. Certification requires a human.
* Federation (FR-057, P2): a workspace may subscribe to a published pack URL or tenant. Entries arrive as
  `trust=approved` (read-only) and refresh on version change.

## 7. Acceptance
AT-16, AT-17, AT-18 (with S04); `test_context_conflict_blocks`; `test_pinned_brief_reaches_planner`;
`test_suggested_never_in_model_input`; ledger-owner consistency test (every ledger row resolves; every approved owner row has a ledger row).

## 8. Tasks
| ID | Task | Est |
|---|---|---|
| S03-T1 | Ledger table + projection consumers for existing kinds | 3d |
| S03-T2 | Hybrid retrieval with access pre-filter + rerank | 2d |
| S03-T3 | Pinned brief section in compiler (review P0) + refusal on overflow | 2d |
| S03-T4 | REST `/v2/context/*` + change feed | 2d |
| S03-T5 | MCP context tools + grants | 1d |
| S03-T6 | OKF round-trip CI + access-filtered export | 1.5d |
| S03-T7 | Federation subscription (P2) | 3d |

## 9. As built (2026-10-27, tasks T1–T6; 2026-10-29, task T7)
* **Table.** `context_ledger_entry` follows the codebase: ids are `String(40)` (`cle_…`) and `tenant_id` is filled by
  the tenancy trigger from the workspace (ADR-009), so the unique key is `(workspace_id, ref_table, ref_id, version)`.
  `embedding` is the platform's 256-dimension local hashing embedding (`context/embeddings.py`; pgvector or `real[]`
  per `ANALYSTOS_VECTOR_BACKEND`), not `VECTOR(768)`. Added columns: `aliases`, `refs` (the `schema.table[.column]` an
  entry depends on, used by the access filter and lineage proximity), `content_hash` and `provenance`. The change feed
  is its own table, `context_ledger_change` (`seq`, `entry_id`, `op` added|changed|retired, `version`). Migration
  `0061_context_ledger` installs RLS, the policy and the fill trigger for both tables.
* **Writers (T1).** Writers make no outbox calls. A session hook (`context/ledger.py`) marks `(workspace, owner table)`
  when an owner row is flushed. Just before the transaction commits, it reconciles those pairs in the same
  transaction (a savepoint on Postgres, so a projection error never loses the owner's write). Bulk Core writes
  bypass the hook. `analystos context reconcile` and `POST /api/v2/context/reconcile` catch up and report
  `check_consistency` (dangling, missing, stale, unresolved). Projected owners and their trust:

  | Owner | Ledger kind | Trust |
  |---|---|---|
  | `context_entry` (term/definition/note/rule, metric, correction, negative) | glossary, metric, correction | `approved` for a reviewed mapping or a desk certification, `workspace` if trusted, else `suggested` |
  | `semantic_metric` (latest approved / proposed version) | metric | `certified` / `suggested` |
  | `verified_query` (active) | verified_query | `certified` |
  | `insight` (verified, not stale) | finding | `approved` |
  | `semantic_relationship_candidate` (accepted / pending) | relationship | `approved` / `suggested` |
  | `source_asset` (selected, active) | dataset | `approved` if reviewed, else `workspace` |
  | `knowledge_document` (the workspace's own and imported packs) | glossary, metric, finding, correction, document_section | `approved` for a verified trust tier, else `workspace`; `suggested` for drafts |
  | `knowledge_suggestion` (pending, OKF imports included) | from the draft | `suggested` |
  | `request` (a desk answer certified as a playbook, S13-T5) | playbook | `approved` |

  Not projected yet: `decision` (no owner table holds business decisions; the `decision` table logs model decisions)
  and S04's conversation summaries and corrections (S04 is not built; `context_entry` kinds `correction` and
  `conversation_summary` are mapped for when it writes them). The reader supports `personal` trust (author only),
  but no owner produces it yet.
* **Access pre-filter (T2).** Applied before ranking: RLS (tenant), workspace membership (404 otherwise), and trust
  (`personal` for its author only; `suggested` only with `include_suggested`, never in `compile`). Data scope comes
  from `resolve_scope`. An entry is withheld everywhere (search, get, compile, changes, export, MCP resources) if it
  references a catalogued table outside the caller's scope, or a column the policy denies them (PII without
  clearance, restricted columns, attribute rules). A reference to a table the workspace does not catalogue reveals
  no data, so it does not withhold the entry. Ranking: exact name or alias, name mentioned in the query, synonym term
  overlap, summary overlap, lineage proximity (shares a table with an exact or named match) and cosine similarity;
  then × trust weight × freshness (age of the version) × in-scope. Candidates are capped at 5000 per workspace and
  scored in-process.
* **Compile.** Returns `ContextBundle{sections, receipts, omissions, identity_hash, budget_used}`, with `budget_tokens`
  × 3.6 chars as the budget. Certified entries of the same kind and name with different fingerprints raise
  `ContextConflict` (409). Text that reads like an instruction is omitted as `screened`. With `run_id`, the run's
  reviewed brief is the mandatory first section; if it alone exceeds the budget the compile is refused
  (`ContextOverBudget`, 422). The bundle and `CompiledContext.summary()` both return the identity hash, so
  model-call records carry it. Folding it into the run's plan hash is left to S07.
* **Pinned brief (T3).** `compile_context(..., pinned=, pinned_receipts=)` owns the reviewed brief as section
  `reviewed_business_context`, the key prompts and verification already read. The compiler screens the facts again
  and receipts every fact it sends. The brief is part of the compiled-context cache key and the identity hash, and is
  rendered inside `<untrusted_context>` of the cached preamble. A brief that does not fit refuses the compile with
  `details.section = reviewed_business_context` instead of being trimmed.
* **REST (T4).** Served at `/api/v2/context/*`, because `/api/context/search` is already the v1 search.
  `POST /export` builds the pack synchronously and returns 202 with an export artifact handle. Only the exporting
  user can download it (`GET /api/v2/context/exports/{id}`), because it reflects their access. `openlineage` and
  `odcs` answer 422 (`unsupported_capability`) for now. `POST /reconcile` (owner) is an addition.
* **MCP (T5).** Tools `context.search`, `context.get`, `context.compile` and `context.changes` (viewer grants); the
  grant `context.read` covers all four. Resources `analystos://{ws}/{ledger kind}/{entry id}` list and read the
  client's permitted, non-suggested entries.
* **OKF (T6).** One document per exportable entry under `<kind dir>/<slug>.md`, with frontmatter
  `analystos.ledger{kind, trust, classification, aliases, refs, fingerprint?, valid_from}` and the summary as body,
  plus `index.md` and `log.md` (counts of withheld entries per kind, nothing else). The zip is reproducible (sorted
  names, fixed timestamps). Exported: certified, approved and workspace entries, plus content that itself came from
  a pack (`source=import:okf`), forwarded with the trust its producer declared. The receiving tenant's review state
  is not part of the content; that is what makes `export → import (clean tenant) → export` byte-identical. Import
  queues each document as a knowledge suggestion (`origin=import:okf`, with the pack digest and path as provenance),
  so it stays `suggested` in the ledger until a person approves it in the review queue. The round trip runs in the
  integration suite (`tests/integration/test_context_service.py::test_at16_…`), not as a separate CI job.
* **Federation (T7).** Decisions where §6 left a choice:
  * **Source.** A subscription follows another *workspace's ledger* through its change feed. The source may be in the
    same tenant or in another tenant the subscribing user belongs to. "Subscribe to a published pack URL" is not
    built: fetching packs from the network is a new outbound path, and an OKF pack already lands through `POST /import`
    as suggestions.
  * **Who decides what is shared.** The subscriber is a person (`subject_id`), not a workspace. The creator must be an
    owner of both workspaces, and their access to the source is the filter (`retrieval.Access`: scope, denied columns),
    re-checked on every sync. An entry is shared when:
    * its trust is certified, approved or workspace (never `personal` or `suggested`);
    * its classification is `internal`. An entry the source tags confidential or restricted stays home, because the
      subscriber workspace's readers are not governed by the source's column policy;
    * it is not itself a federated copy. Federation is one hop, so mutual subscriptions cannot echo.
  * **Storage.** `context_subscription` (cursor, status `active|suspended|revoked`, optional `kinds`) and
    `context_federated_entry` (the copies, keyed by the source's `ref_table:ref_id`), both owned by the subscriber
    workspace with RLS and the fill trigger (migration `0074_context_federation`). The source workspace id has no
    foreign key because it may be in another tenant. `context_federated_entry` is a ledger owner table:
    `ledger._federated` projects each live copy into the subscriber's ledger as `approved`, `source=federation`,
    with the source workspace, entry and version in `provenance`. A new source version opens a new ledger version
    ("refresh on version change"), and a removed copy retires its entry. Copies are read-only: only the sync writes them.
  * **Delivery.** `feed()` turns the source's `context_ledger_change` rows after the cursor into `upsert` items (the
    full entry with its version) and `remove` items (key and version). A version that stops being shareable is sent
    as a `remove` when its previous version was shared. A sync reads a batch as the source tenant and applies it as
    the subscriber's tenant in a separate transaction, where the cursor also advances. That gives at-least-once
    delivery. The apply is idempotent:
    * an upsert applies only when its version is newer than the copy's;
    * a remove applies only when its version is not older;
    * removed copies stay as tombstones, so a replay cannot bring them back.

    After the feed, the sync re-checks every live copy against the source's current state. That catches access lost
    without a ledger change, and entries `ledger.forget` erased (their change rows go with them). `POST …/resync`
    replays from cursor 0, for example after the subject gains access.
  * **Tenancy.** `sync_subscription` is `@owned_by("context_subscription", "subscription_id")`. Each source read
    enters `system_scope()` only to re-bind to the source workspace's tenant (`as_owner_of`), so the read runs under
    that tenant's RLS. The subject must be visible there (home tenant or tenant membership) and still be a source
    owner. Otherwise the subscription is suspended, its copies are retired, and the suspension is audited.
    `federation.sync_due` is the scheduler housekeeping `@platform_loop`: it selects active subscriptions not synced
    for 10 minutes, at most 50 per pass. Bounds:
    * 500 changes per batch, 20 batches per sync;
    * 5000 live copies per subscriber workspace (the retrieval candidate cap);
    * 20 subscriptions per workspace.
  * **Context is data.** Feed items are checked for shape and length (kind, trust, classification, string lists) and
    never interpreted. A copy's text is screened like any other entry when a compile puts it in front of a model.
  * **REST.** All under `/api/v2/context`:
    * `GET subscriptions?workspace=`;
    * `POST subscriptions` (201, first sync included);
    * `POST subscriptions/{id}/sync` and `POST subscriptions/{id}/resync`;
    * `DELETE subscriptions/{id}` (revoke: copies leave the ledger, history kept);
    * `GET feed?workspace=&since=&kinds=`, the same feed for an external subscriber, with the caller's access and
      tenant applied.

    Create, resync, revoke and suspend are audited (`context.subscription.*`). Like an export, subscribing moves
    context within the platform, so it needs no approval.
  * **Not built.** Pack-URL subscriptions; an MCP tool for the feed; a UI; lineage edges from a copy to its source
    entry (the ledger `provenance` names it); UI events for sync results.
