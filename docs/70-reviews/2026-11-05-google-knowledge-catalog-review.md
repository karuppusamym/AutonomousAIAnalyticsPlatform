# Google Cloud Knowledge Catalog — what to learn for our context layer

**Date:** 2026-11-05 · **Scope:** the Knowledge Catalog docs (formerly Dataplex Universal Catalog, renamed 2026-04-10),
read from the [overview](https://docs.cloud.google.com/knowledge-catalog/docs/introduction) and ~20 sub-pages listed
under Sources, compared against this repository at `8b6458b`. **Specs touched:** S03 (context), S07 (verification),
S09 (monitoring), S10 (registry/MCP).

## Decision

Knowledge Catalog is a context layer for AI agents that also works as a catalog, which is what our S03 Context Service
does. We already match its core ideas: reviewed glossary and metrics, verified queries with typed parameters,
value-free query-history mining, relationship discovery, a review queue for model drafts, an MCP server, an OKF
interchange format and a pull change feed. On governance we go further than Google does: access is filtered before
ranking, a compile is refused rather than trimmed, approvals are bound to hashes, and drafts never reach a prompt.

Google does better in five areas, and all five can be built inside our rules:

1. **A change to knowledge is tested before it is approved.** Google runs a set of test questions with and without the
   proposed metadata and shows the difference. Our golden questions exist but nothing calls them when a reviewer
   approves a change. **(P0)**
2. **One call returns the context for named assets.** `LookupContext` returns schema, profile, quality, join paths and
   terms for up to 10 assets in YAML or JSON. Our compile starts from an objective. An external agent cannot ask
   "tell me about these 3 tables" in one call. **(P0)**
3. **Failed questions turn into knowledge drafts.** Google mines agent conversations for things the agent could not
   answer. We learn only from corrections people make on purpose. **(P1)**
4. **Usage, quality and lineage feed trust signals.** This covers how often an asset is queried, quality rules a person
   declared, and PII followed through column lineage. **(P1)**
5. **Agents can call the catalog by API.** Search accepts filters, change events can be pushed, and data products carry
   contracts. **(P1/P2)**

## What Knowledge Catalog is (short)

| Pillar | Main features |
|---|---|
| **Aggregation** | Automatic crawl of GCP stores; connectors for Oracle, Postgres, MySQL, SQL Server, Snowflake Horizon, Databricks Unity, dbt, SAP BDC; Looker LookML/dashboards; unstructured discovery of PDFs and images in Cloud Storage; column-level lineage and OpenLineage |
| **Enrichment** | Model-written descriptions, sample queries and join graphs ("data insights"), with custom steward instructions; glossaries; *aspects* (typed custom metadata); profiling; auto data quality rules; **Knowledge Studio enrichment tasks**: an agent reads schemas, query logs, documents (GCS, Drive, SharePoint, GitHub, Confluence, Jira) and agent chat logs, then proposes a batch reviewed against an evaluation set |
| **Retrieval** | Hybrid semantic and keyword search with structured predicates; `LookupContext` API (≤10 assets, `context_budget`, YAML/JSON/XML); remote and local MCP servers; **context sets** (bounded per-domain containers with a progressive-disclosure index); data products (assets + access groups + contract + docs, self-service access requests); data domains |
| **Governance** | Change requests for glossary and entries (single and batch); IAM per container; metadata change feeds to Pub/Sub filtered by entry, aspect and change type; audit logs; token-usage metrics |

## Where we already match or lead

| Knowledge Catalog | Ours | Note |
|---|---|---|
| Golden queries / query templates (typed `@params`, `sampleValues`, dialect) | `registries/verified_queries.py`: deterministic match, typed parameter extraction from profiled vocabulary, run through the gateway, declines when a parameter is missing | At parity; ours runs before any model call (ladder L1) |
| Relationships from query logs and the LLM | `skills/relationships.py` (declared → name heuristic → same-name key, **validated with SQL**), `skills/query_history.py` | Ours validates joins; theirs only infers them |
| Query-log mining | `skills/query_history.py`, value-free (`table, column, operator class`) | Ours is stricter on privacy; keep it that way |
| Enrichment proposals with confidence, diff, edit, approve, reject, supersede | `knowledge/suggestions.py` (`{value, confidence, provenance}` per field, batches, supersede, negative memory), `ReviewQueue.tsx` | At parity, except the evaluation gate (P0-1) |
| Curated descriptions never overwritten by generators | Crawler invariants in `knowledge/drafts.py` and `services/crawler.py` (curated kept, tags only tighten) | Google stores generated text as "overlay" entries for the same reason |
| Agent self-learning documents | `knowledge/learning.py` (accepted findings, approved KPIs, corrections → drafts) | Partial (see P1-3) |
| Context budgets | `context/compiler.py` and `context/retrieval.compile_bundle` (receipts, omissions, refuse rather than trim) | Ours refuses a mandatory section that does not fit; theirs trims |
| ACL-aware retrieval | `context/retrieval.py`: access filter before ranking | At parity |
| MCP server | `mcp/server.py` (grants, quotas, audit at the gate) | At parity; theirs has a separate lineage MCP |
| Change feed | `GET /v2/context/changes` (cursor), `/feed`, federation subscriptions | Pull only (P1-6) |
| Docs as knowledge | `knowledge/documents.py` (md/txt/pdf, screened, redacted, drafts) | Upload only (P2-10) |
| Column lineage, OpenLineage | `evidence/lineage/{sql,dbt}.py`, `evidence/openlineage.py` | Not yet joined to PII tags (P1-5) |
| Domains | `services/context_grounding.domains_for`, domain-keyword review rule | Flat, no hierarchy (P2-8) |

## Recommendations

Each item says what Google does, what we have, what to build, and how it stays inside CLAUDE.md rule 3
("models propose, code decides").

### P0-1 — Test a knowledge change against golden questions before it is approved

**Google.** Every enrichment run scores its proposals against an evaluation set (`.jsonl` of question → SQL). The run page
shows *baseline pass rate*, *projected pass rate* and the **delta**, with a per-case table (*Fixed / Still failing / Newly
failing* plus a "why"). Auto-publish is allowed only when overall confidence ≥ 0.85, no proposal fails a validity check
(for example malformed SQL), and accuracy drops by ≤ 5 points. Deletions always need a person. Runs with more than 250
changes are split into batches approved in order. A newer run locks the older run's pending proposals.

**Ours.** Owners write golden questions (`api/routers/context_grounding.py` `POST /golden`) and can run
`POST /golden/evaluate`. The evaluation checks *grounding* (expected columns, or an expected clarification) against the
current knowledge only. `knowledge/suggestions.review` never runs it, so a reviewer approves a batch without knowing
whether answers improve or get worse.

**Build.**
* `knowledge/suggestions.preview_effect(batch)`: apply the selected drafts to a **shadow pack revision** (not committed),
  run the golden suite twice (current and shadow) with `use_model=false` so it is deterministic and costs nothing, and
  return `{baseline, projected, delta, cases:[{id, before, after, change}]}`.
* Extend a golden question with an optional `expected_query` (a verified-query id plus parameters) and compare the
  **result fingerprint** (S07). The test then covers "the right number", not only "the right columns".
* In ReviewQueue, show the delta on the batch card, and list *newly failing* cases first. If a batch makes any case newly
  fail, approving it needs a written override reason, which is stored in the review block and the audit.
* Optional and off by default: an auto-approve policy for low-risk kinds only (column descriptions on assets without
  PII tags, landing at trust `workspace`, never `certified`, never deletions). The conditions are deterministic:
  confidence ≥ policy, no validity failure, delta ≥ 0. Certification stays human, as S03 §6 requires.

**Proof.** A unit test with a fixture pack where one draft fixes a golden case and another breaks one: the preview reports
+1/−1 and approving the breaking draft without a reason is refused. One live run recorded in the register.

### P0-2 — `context.lookup`: context keyed by asset, for external agents

**Google.** `lookupContext(resources[≤10], format=yaml|json|xml, context_budget)` returns one text block: schema
(partitioning, routines), business descriptions and linked terms, a *guidelines* field written for agents, profile
(distribution, distinct counts, null ratio, samples), quality results, and **join paths among the requested assets**.
The block is filtered by the caller's IAM. When trimming to the budget, it keeps the important parts first
(descriptions, lineage).

**Ours.** `compile_bundle` is driven by a purpose and an objective over ledger kinds. Asset facts (profile,
relationships, quality, verified queries, PII tags) live in crawler and catalog tables. An MCP client that already
knows which tables it wants has to make several calls and join the results itself.

**Build.**
* `POST /v2/context/lookup {workspace, assets[≤10], format: yaml|json, budget_chars}` and the MCP tool
  `context.lookup` (an S10 grant), built in `context/retrieval.py` from `SourceAsset`, the profile, `skills/relationships`
  (validated joins between the requested assets only), quality outcomes, linked glossary and metric entries, verified
  queries that read the asset, and a curated `guidelines` field (new, human-edited, in the table's OKF document).
* Fill the block in a fixed order (identity and keys → curated description and guidelines → joins → terms and metrics
  → quality → profile → usage) and record what was left out as omissions. Profile *values* are included only for
  columns without a PII or restricted tag, and only from vocabulary the crawler already profiled.
* The same scope filter as `access_for`. An asset outside scope returns nothing and is not counted.

**Proof.** A contract test of the YAML shape (deterministic, sorted). A scope test: a restricted table is absent from the
block and its omission count is 0. A budget test: a block cut down to the budget still has keys and joins.

### P1-3 — Mine failed questions into drafts that report a knowledge gap

**Google.** Enrichment reads agent chat traces. When users keep asking something the agent cannot answer ("what does
status code 42 mean?"), it proposes a `learning` document.

**Ours.** `knowledge/learning.py` turns *explicit* feedback into drafts. The Ask outcomes `clarify`, decline and
low-confidence (`services/ask.py`), plus repeated rephrasing, are never aggregated.

**Build.** A `@platform_loop` job (tenancy rule 5) groups the last N days of Ask outcomes per workspace by the
normalised unresolved term. It uses the already-redacted question text (`llm/redaction.py`) and never
stores result values. For terms seen in ≥ k distinct conversations, it proposes a `Note` draft of kind `knowledge_gap`
with counts and example question ids, routed to the asset owner. The rules rung (term frequency) works with models
`off`. A model may draft the *wording* of the note through a new `config/models.yaml` purpose with
`model_gate`/`record_skip`. The draft goes through the normal review queue and is never knowledge on its own.

### P1-4 — Usage as a ranking and trust signal

**Google.** The context names the usual filters and joins ("90% of queries filter `status='COMPLETED'` and join
`dim_promotions`"). The relationship tab shows *origin* (query history or insights) and a sample join query.

**Ours.** `skills/query_history.py` already computes value-free patterns and `knowledge/crawl_docs.py` writes
`Query Patterns` documents, but `context/retrieval.py` does not rank on usage, and the relationship origin is not shown
in the UI.

**Build.** Add a bounded `usage` factor (distinct users and runs over 30 days, from audited `query_execution`) to the
retrieval rerank (`trust × freshness × in_scope × usage`). Add common filter columns by operator class to the lookup
block (P0-2), without literals. Show origin and confidence on the Catalog relationship view with the validated join SQL.
Extend `context/benchmark.py` with one configuration that uses usage, so the gain is measured and not assumed.

### P1-5 — Follow PII along column lineage, and report impact before a change

**Google.** A tutorial uses column lineage to show that `user_email` leaked from a restricted table into an analytics
table open to the whole organisation. Impact analysis lists what sits downstream of a change.

**Ours.** We have column lineage (`evidence/lineage/sql.py`, dbt), crawler PII tags that only tighten, and a graph
projection. They are not joined.

**Build.** A deterministic check: when a column with a PII tag reaches a column whose asset lacks the tag (through a
recipe, dbt model or published dataset), raise a finding and a review draft that *adds* the tag. Adding a tag is the
only automatic direction, which matches the crawler invariant. Add an impact report on a schema-change crawl diff:
metrics, verified queries, dashboards, schedules and monitors that read the changed column. Pause dependent schedules
only through an approval (rule 6).

### P1-6 — Push change events with filters

**Google.** Metadata change feeds go to Pub/Sub, scoped and filtered by entry type, aspect type and change type. Example
use: when an inventory table's schema changes, pause the report pipeline and alert the owner.

**Ours.** Consumers poll a cursor (`/v2/context/changes`). Webhook delivery already exists (`services/deliveries.py`,
`triggers/`).

**Build.** A `context_subscription` with filters (`kinds`, `change: create|update|deprecate`, `tag_added: pii`,
`schema_changed`) delivered through the existing webhook path. Creating one is a standing side effect, so it is an
approval bound to its filter and destination hash (rule 6). The new table needs RLS and a line in
`test_tenancy_rls_coverage.py` (rule 5). Internal consumers: re-profile and re-check data quality on `schema_changed`
(S09).

### P1-7 — Declared data-quality rules, suggested from the profile

**Google.** Rule types: range, not-null, set, regex, uniqueness, statistic range, SQL row-level and aggregate, each with a
pass threshold and a dimension. Rules are *recommended from the profile scan* and can be kept as code (Terraform).

**Ours.** `skills/quality.py` runs fixed checks (null rate, constant column, duplicate key, orphan reference).
`recipes/gates.py` has the DataPilot rule types for recipe outputs. Grounding has a `quality_rule` kind. Monitors
re-profile for data-quality regressions. Nothing lets an owner *declare* a rule on a catalog asset.

**Build.** A `QualityRule` contract that reuses the `recipes/gates.py` types and adds a threshold and a dimension. It
attaches to an asset or column and is rendered to SQL by `skills/sqlbuild` through the gateway. Deterministic
recommendations from the stored profile: observed null rate → not-null with a threshold, p01..p99 → range,
low-cardinality vocabulary → set. They arrive as review drafts, the owner approves, and monitors run the rules. Pass
rates become a trust signal in retrieval and lookup. Rules export to OKF so they can be kept as code.

### P2-8 — Context sets: a bounded, named slice for one agent or domain

**Google.** A context set is a container (assets, concepts, documents, query templates, learnings) with a fixed root
index document and a *progressive-disclosure* layout. The agent reads the index first, then tries verified queries,
then table context, then concepts. Access is granted per set.

**Ours.** The workspace is the boundary. Domains exist only as keywords and binding scopes.

**Build (light).** No new container type. Add (a) a generated `index.md` per workspace pack and per domain that lists
what exists, so agents read the index first and fewer tokens are spent; and (b) optional `domains` / `assets` scoping on
an MCP grant (S10), so an external agent sees one domain's slice. Defer a domain hierarchy until a customer needs one.

### P2-9 — Data products with a contract and an access request

**Google.** A data product = assets + access groups (Reader/Analyst mapped to groups) + a contract (refresh cadence,
quality thresholds) + docs. Consumers request access in the product, and approval grants IAM automatically, with an
audit record.

**Ours.** `toolreg/publish.py` publishes datasets and verified queries as signed `data_product` manifests. ODCS export
exists (`evidence/odcs.py`). There is no contract block that monitors check, and no request flow for consumers.

**Build.** A `contract` block in the manifest (freshness SLA, `QualityRule` refs from P1-7) checked by a monitor, with a
breach shown on the product. An access request is an approval bound to (product, version, requester, role). A human
approves and code applies the workspace role or MCP grant (JEV may escalate risk, never grant).

### P2-10 — Read-only knowledge sources beyond upload

**Google.** Knowledge signals come from GCS, Drive, SharePoint, GitHub, Confluence and Jira.

**Build.** Start with **GitHub** (README, dbt `schema.yml` and docs, `.sql`) and **Confluence** (one space). Fetch through
`tools/http.py` (SSRF-safe) with `secret_ref` credentials. Every file goes through the same screening as
`knowledge/documents.py` (redaction, instruction-line markers, size caps) and arrives as drafts (rule 7).

### P2-11 — Spreadsheet round trip for stewards

**Google.** Glossaries and entry links import and export through Google Sheets and JSON.

**Build.** Glossary and term-to-column links as CSV/XLSX export and import. An import always lands as suggestions with
provenance `import:csv` (S03 §6), and the field confidence is 1.0 only after a reviewer approves. This is cheap and
it is what stewards ask for.

## What not to copy

* **Literal values from query logs.** Google's examples quote filter values (`status = 'COMPLETED'`). Our value-free
  rule (spec v3 §6.3) is stricter. Include a literal only when it is profiled vocabulary on a column with no PII tag.
* **Last writer wins on change requests.** Google applies the most recently approved change whatever the pending
  order. Our `expected_revision` and pack revisions are safer. Keep them.
* **Trimming to the budget.** Google fits "most important first". We refuse when a mandatory section does not fit
  (S03 §4). Keep refusing for mandatory sections, and trim with omissions only for optional ones (P0-2).
* **Automatic access grants without a person.** Approval → automatic IAM is fine. An automatic *decision* is not.
* **GCP-only plumbing** (Pub/Sub, multi-region lineage fan-out, VPC-SC). Use our webhook, outbox and RLS equivalents.

## Suggested order

| # | Item | Spec / task to open | Size |
|---|---|---|---|
| 1 | P0-1 evaluation-gated review | S03 (new T: golden delta on review) | M |
| 2 | P0-2 `context.lookup` + MCP tool | S03 §5, S10 grant | M |
| 3 | P1-4 usage in rank + relationship origin UI | S03 §4 | S |
| 4 | P1-5 PII along lineage + impact report | S07 / S09 | M |
| 5 | P1-7 declared quality rules from the profile | S09 | M |
| 6 | P1-3 knowledge-gap mining | S03 / S04 | S |
| 7 | P1-6 filtered push subscriptions | S03 §5 | S |
| 8 | P2-8…11 | S10, S03 | S–M each |

## Sources (read 2026-11-05)

Overview `introduction` · `about-context` · `context-sets` · `manage-context-sets` · `knowledge-studio` ·
`about-enrichment-tasks` · `create-enrichment-tasks` · `manage-enrichment-runs` · `retrieve-data-context` ·
`mcp-overview` · `data-insights-structured-data` · `data-relationships` · `data-products-overview` ·
`data-domains-overview` · `about-governance-workflows` · `metadata-overview` · `auto-data-quality-overview` ·
`data-profiling-overview` · `metadata-change-feeds-overview` · `about-search` · `lineage-use-cases-pii-leakage` ·
`build-agent-to-enrich-metadata`. All under `https://docs.cloud.google.com/knowledge-catalog/docs/`. Pages not read
include connector setup, IAM and role lists, Terraform data-quality as code, and lineage ingestion control.

## Limits of this review

The comparison of our side comes from reading the code and docstrings at `8b6458b`. Nothing was run. "Not built" means
not found by searching `src/` and `web/src`. A feature may exist under a name this search did not try.
