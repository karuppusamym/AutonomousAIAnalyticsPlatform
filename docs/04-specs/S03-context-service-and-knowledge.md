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
