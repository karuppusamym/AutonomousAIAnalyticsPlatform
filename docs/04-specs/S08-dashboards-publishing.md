# S08 — Dashboards, conversational authoring and publishing

**Priority:** P0 (publish, per-tenant Superset) / P1 (patch ops, builder) · **Closes:** FR-008, FR-009, FR-014, FR-015, FR-025, FR-036, FR-048 · **ADRs:** 006

## 1. Purpose
Generate and edit dashboards through conversation with typed, versioned operations, and publish them to Superset
with correct end-user authorization, after approval.

## 2. Baseline to reuse
`artifacts/` (Artifact, ArtifactVersion), `publishing/` (BI publisher interface, Superset adapter, preview), approvals bound
to bundle hash + plan hash, rollback endpoint, `Publication`, `BiDashboardImport` (existing dashboards), `reports/` (PDF;
fix the non-Latin font), workspace dashboard/report collections, ECharts rendering in `web/`.

## 3. Chart and dashboard DSL
Keep v1 §8.1 as the engine-neutral spec, as a JSON Schema in `contracts/chart.schema.json`. Additions:
`dataset_ref` may be a `ResultHandle`, a `verified_query@v`, or a `metric@v` + dimensions. `provenance` is required.
`sensitivity` is derived as the maximum over inputs. A dashboard is `{layout grid, tabs[], filters[], charts[], text_blocks[], kpis[]}`.

## 4. Typed patch operations (FR-048)
The conversation never edits JSON directly. The `dashboard_design` purpose (rules → small LLM) maps an utterance to one or more ops,
which deterministic code validates and applies:

| Op | Params | Validation |
|---|---|---|
| `chart.set_type` | chart_id, type | type suitable for field types (temporal x for line, etc.), else a suggestion |
| `chart.set_encoding` | chart_id, x/y/series/color | fields exist in the dataset, aggregation is valid |
| `dashboard.add_filter` | field, op, value | value resolved through glossary/geography, field present in all bound charts or scoped |
| `query.extend_period` | chart_ids, compare: yoy/pop | creates a new query version, re-binds affected charts |
| `metric.bind` | metric@v, viz | metric certified or labelled experimental, access check |
| `chart.add` / `chart.remove` / `layout.move` | … | grid constraints |
| `text.set` | block_id, markdown | numbers guard |
| `publish.request` | audience, destination | goes to the approval flow, never applied directly |

Each applied batch creates `ArtifactVersion+1` with the op list as its diff. Undo is a revert to version N. Editing a
published dashboard always forks a draft (v1 §14.1).

## 5. Publishing
Lifecycle `DRAFT → PREVIEWED → VALIDATED → APPROVAL_REQUIRED → APPROVED → PUBLISHING → PUBLISHED | FAILED | ROLLED_BACK`.
* **Per-tenant Superset isolation:** one Superset database connection per tenant (a dedicated reader identity), datasets per
  workspace, Superset roles `ws_{id}_viewer|editor` managed by the publisher, and audience mapping from platform groups.
  Superset row-level security filters are generated from workspace policy where datasets are shared.
* **Embedding:** the platform mints Superset guest tokens after rechecking authorization, with RLS clauses per viewer.
* **Idempotency:** `publication.idempotency_key = sha256(artifact_id, version, destination)`. A reconcile job compares
  Superset objects to the latest publication and repairs or flags drift.
* **Exports** (CSV/XLSX/PDF) are a publish-class side effect: approval policy by classification, watermark with the
  requester and timestamp, logged.

## 6. Acceptance
AT-07, AT-08; `test_patch_ops_versioning`; `test_edit_published_forks_draft`; `test_guest_token_rechecks_auth`;
`test_cross_tenant_superset_denied`; `test_pdf_unicode` (Tamil, CJK, Arabic glyphs render).

## 7. Tasks
| ID | Task | Est |
|---|---|---|
| S08-T1 | Chart/dashboard JSON Schemas + validator | 2d |
| S08-T2 | Patch ops engine + `dashboard_design` purpose mapping | 4d |
| S08-T3 | Builder chat panel (with S11) | 3d |
| S08-T4 | Per-tenant Superset connections + roles + RLS filters | 3d |
| S08-T5 | Guest-token embedding + reconcile job | 2d |
| S08-T6 | Export approvals + watermark; PDF Unicode font | 1.5d |

## 8. As built (2026-10-27, tasks T1–T6)

* **T1 DSL + validator.** `contracts/dashboard_dsl.py` (`ChartDoc`, `DashboardDoc`, `KpiDoc`, `FilterDoc`, layout, tabs;
  `dataset_ref` = `result_handle` | `verified_query@v` | `metric@v` + dimensions; `provenance` required; sensitivity
  derived as the maximum over dataset and fields, never lowered). `analystos export-contracts` now writes the DSL as
  `contracts/chart.schema.json` / `dashboard.schema.json`; the v1 publish shapes moved to `chart_spec.schema.json` /
  `dashboard_spec.schema.json` (publish bundles still use them). `dashboards/validate.py`: JSON Schema first, then
  rules with a JSON path and code each (grid overflow/overlap/unplaced, unknown fields, aggregation vs. field type,
  chart-type suitability, filter scope, sensitivity below inputs, preview bound).
* **T2 ops + `dashboard_design`.** `dashboards/ops.py`: the ten ops of §4, each validated against the document;
  batches are atomic; sensitivity re-derived and the whole result re-validated; refusals carry a suggested op
  (type, aggregation, filter scope, free grid slot). `text.set` runs a numbers guard against KPI values and chart
  previews (dates and period labels are not claims). `publish.request` is returned as deferred and becomes a
  hash-bound approval. `dashboards/design.py`: ladder [cache, rules, llm_small]; rules map common phrasings, the
  small model runs only when no rule matched (or the ladder is `always`), its ops are parsed one by one and invalid
  ones dropped, and the engine still decides. `dashboards/env.py` resolves filter values through trusted glossary
  entries, a geography alias table and the profiled vocabulary (refused when unknown; `unverified` only when the
  field has no vocabulary at all) and binds metrics with an access check (certified, else labelled `experimental`).
  `dashboards/service.py`: each applied batch is `ArtifactVersion + 1` with the ops as `diff` (migration
  `0070_dashboards_publishing`, revises `0069_claims_evidence`), If-Match on the version, revert = new version, edits of a published dashboard fork
  a draft (lineage `forked_from`), edits while an approval is pending invalidate it. API `/api/v2/...`
  (`api/routers/dashboards_v2.py`; under `/api` because the web client reaches only that prefix).
* **T3 builder panel.** `web/src/components/DashboardBuilder.tsx` in Outputs › Dashboards (no new screen):
  grid whose cells pick what "it" refers to, `BuilderChatPanel` (exported for S11-T4) showing proposed ops, refusals
  with an "apply instead" suggestion, fork and approval notices; version history with op diffs, compare and revert;
  publish request/execute and exports. "Build a dashboard" starts an empty one.
* **T4 per-tenant Superset.** `publishing/superset_access.py`. *Deviation:* the spec's one connection per tenant
  is realised as one connection per workspace, each its own BI login (P4-02, strictly narrower: a tenant's
  workspaces never share a reader identity), stamped `aos_tenant` / `aos_workspace`; a connection stamped for
  another tenant or workspace is refused, never reused. Roles `ws_{slug}_viewer` (data access) and
  `ws_{slug}_editor` (+ write on charts and dashboards); `bi-sync` gives authors the editor role. Published
  dashboards get dashboard RBAC from the approved audience (`role:viewer` → both roles, `role:editor` → editor;
  `group:<idp group>` is honoured for embedding, editors only in Superset itself). Policy row filters become Superset
  RLS rules on the datasets that read the filtered asset; a filter that needs viewer attributes or columns the
  dataset lacks is `1 = 0` (fail closed) and is rendered per viewer only in guest tokens.
* **T5 embedding + reconcile.** `dashboards/access.py::guest_token` re-checks at request time: tenant/workspace
  (404 elsewhere), active user and workspace, published from a succeeded publication of the current version, the
  approved audience, then the viewer's own scope (withheld assets or denied columns refuse; row filters become the
  token's RLS clauses); grants and denials are audited. `dashboards/reconcile.py`: drift `missing` / `changed`
  (provenance stamp ≠ approved bundle hash) / `unpublished`, recorded on `publication.drift`; repair re-applies the
  approved bundle only after `governance/approvals.verify_for_reapply` (executed approval, same payload, plan and
  policy version, both people still authorized). Scheduler housekeeping flags drift at most hourly (no repair);
  `POST /api/v2/workspaces/{ws}/publications/reconcile {repair}` for editors. Idempotency key
  `sha256(artifact_id, version, destination)`; a BI failure keeps the approval usable for a reconciling retry.
* **T6 exports + fonts.** `dashboards/exports.py`: CSV/XLSX/PDF from the bounded chart previews (no re-query).
  Policy `export_approval_min_sensitivity` (default `confidential`): at or above it the export is publish-class
  (an editor requests it; approval bound to artifact, version, content hash, format and requester; single use).
  Every export is watermarked (requester, time, classification, version) in the file (CSV first line, XLSX cell +
  header/footer, PDF header, footer and a diagonal mark) and audited with its SHA-256. PDF fonts: Noto Sans Tamil and
  Noto Sans Arabic (OFL) join DejaVu and Noto CJK as fallbacks (`reports/fonts/README.md`, hashes listed); text
  shaping (Arabic joining, Tamil reordering) turns on when the optional `uharfbuzz` is installed — without it the
  glyphs print unshaped.
* **Tests.** `tests/unit/test_dashboard_{dsl,ops,bundle,exports}.py`, `tests/unit/test_superset_access.py`
  (incl. `test_pdf_unicode`, the publisher half of `test_cross_tenant_superset_denied`),
  `tests/integration/test_dashboards_publishing.py` (`test_patch_ops_versioning`, `test_edit_published_forks_draft`,
  `test_guest_token_rechecks_auth`, `test_cross_tenant_superset_denied`, publish/reconcile/export flows, migration),
  `web/src/test/dashboardBuilder.test.tsx`, `web/src/test/embeddedDashboard.test.tsx` (in-memory Superset), and live
  `tests/integration/test_superset_embedding_live.py` (AT-07/AT-08 against Superset 4.1.1, 2026-10-28).
* **Embedding as built.** `deploy/superset`: `EMBEDDED_SUPERSET`, guest role `aos_embed_guest` (7 read-only
  permissions, no data access of its own, no `explore_json`; `guest_role.py` resets it on boot), `frame-ancestors`
  from `SUPERSET_EMBED_ORIGINS`. `aos_security.py` makes a guest's chart-data request the saved chart (its columns,
  metrics, WHERE/HAVING, row limit), because Superset 4.1.1 skips that check for guests. Each embed config's allowed
  domains = `ANALYSTOS_SUPERSET_EMBED_DOMAINS` or the `web_url` origin, never empty. A guest holds only the guest
  role, so workspace-role RLS rules do not apply to it; the token's clauses do. `viewer_rls` refuses unless every
  published asset is in the viewer's current scope, and a dataset on the live dashboard that the publication does
  not name gets `1 = 0`. The builder shows a published dashboard through `EmbeddedDashboard.tsx`; each SDK token
  refresh goes back through the recheck.
* **Not built / open.** Metric-bound charts publish only against an approved semantic model (the fixture flows use
  verified queries and result handles); previews of new charts come from a question since CONV-01 (§9); group audiences are not Superset roles; the platform endpoint → live Superset →
  browser chain has not run as one journey (each half is proven live or against the fake).

## 9. As built — conversational dashboards (2026-10-29, CONV-01…03)

* **A chart from a question (CONV-01).** New op `chart.from_question {question, type?, title?}`. The engine stays pure:
  `OpEnv.answer_question` (`dashboards/questions.py`) runs the question through `agents/sql_agent.ask` unchanged, so
  the semantic compiler, a verified query, the Ask rules or model-written SQL decide, and every statement passes the
  gateway validator and `QueryGateway.execute`. The answer becomes a `ChartDoc`: fields typed from the result, the type
  from `skills/viz.recommend_chart` (a type the person named wins only when it suits the fields), `dataset_ref` =
  `metric@v` + dimensions for a plain governed metric, else the gateway execution (`result_handle` = query id), a
  preview of at most `MAX_PREVIEW_ROWS` rows and provenance (assets, query id, `created_via: conversation`). A
  model-written answer is labelled `ad_hoc` ("new calculation" in the UI). A single governed value is a `KpiDoc`. A
  refusal, a clarify or an empty result is a refused op whose suggestion carries the clarifying reply and examples
  the workspace can answer; the dashboard is unchanged. The rules rung reads "add / plot / chart / show me …" (and a
  "show/how many/what is" sentence no edit rule matched) as this op, so no model is needed to ask for a chart.
* **A draft from a prompt (CONV-02).** `dashboards/drafts.py` + `service.create_draft`, `POST
  /api/v2/workspaces/{ws}/dashboards/draft {prompt}`. Topic words (expanded through trusted glossary synonyms) are
  matched against the approved metrics the caller may see (`planner_catalog`, so masked metrics are absent) and the
  brief's entities; tiles are questions the semantic rules rung answers exactly (`<metric>`, `monthly <metric>`,
  `<metric> by <dimension>`), or without a metric layer the Ask rules (`how many <entity>`, `<entity> per month`,
  `count of <entity> by <column>`). Two charts per metric at most while others have tiles. Filters are controls
  without a preset value (`FilterDoc.value = null`, `resolved.via = control`) on the categorical fields the built
  charts share; the bundle publishes them as native filters and never as a WHERE. Purpose `dashboard_plan`
  ([cache, rules, llm_small], escalation never) may only reorder known tile ids; `auto` skips it when the topic
  matched (recorded as a saving). Each tile is applied on its own, so failures are listed with their reason; a prompt
  that matches nothing gets a clarifying reply and no dashboard. Version 1's diff holds the origin (prompt, grounding,
  built and failed tiles).
* **An Ask answer into a draft (CONV-03).** Ask promote target `draft {title? | dashboard_id?}` builds the recommended
  chart from the stored answer (no re-query; the evidence check of `promote` applies) and creates a draft or applies
  `chart.add` to an existing builder dashboard (a published one forks a draft). The approval-bound direct publish
  (`dashboard`) is unchanged.
* **UI.** Outputs › Dashboards › Build a dashboard: "What should the dashboard show?" (`DraftFromPrompt`), the result
  lists tiles not built; starting empty is still there. Builder chat shows how each new chart was answered, and a
  clarifying reply with questions to try. Ask: "Add to dashboard" asks where (new draft, existing draft, or the BI
  tool with approval). Query ids, metric versions and planning detail appear only with expert tools on.
* **Not built.** The draft has no text summary (CONV-05 adds narrative); a draft of a workspace with no metric layer
  depends on catalog semantic types and profiles; drill-down and cross-filtering are CONV-06 (§10).

## 10. As built — drill-down and cross-filtering in the preview (2026-10-29, CONV-06)

* **A view, not a version.** Clicks, chips and drill paths are browser-session state. `POST /api/v2/dashboards/{id}/view
  {filters: [{field, op: eq|in, value, source}], base_version, refresh_saved}` never writes the document. "Apply as
  dashboard filter" posts the server-built `dashboard.add_filter` op to `/ops` (an edit: version + 1, editors only).
* **Never trust the client.** The browser sends names and values only. The field must be a field of a chart on this
  dashboard (exact, or the same last segment: `orders.priority` ≡ `priority`) and categorical (time is drilled, measures
  are refused); the value resolves through `dashboards/env.py` like a typed filter (vocabulary, glossary, geography), or
  the request is a 422. At most 5 filters, 50 values each.
* **Every re-query governed, as the viewer.** `dashboards/interact.py` rebuilds each affected tile from where it was
  built. A `metric@v` chart or KPI compiles a `SemanticQuery` with the filter as a `SemanticFilter` on the metric's
  approved dimension, so a metric chart is filtered by a dimension it does not show. A verified query or gateway
  execution (`bundle.Resolver.sql`) is wrapped `SELECT * FROM (…) f WHERE …` with sqlglot nodes and `skills/sqlbuild`
  literals, exactly like a published dashboard filter. The scope is `resolve_scope(viewer, minimum_role="viewer")` and
  every statement goes through `QueryGateway.execute` (purposes `dashboard.cross_filter|drill|detail`), so denied
  columns, row filters, ABAC and budgets apply. The document's saved value filters apply to every re-query. Tiles
  without the field come back `not_filtered` with the reason; the clicked chart is `source`.
* **Drill-down.** A time axis grouped by `DATE_TRUNC` re-grains one level per click (year → quarter → month → day; week →
  day) inside the clicked member's window. A metric chart walks the metric's approved non-time dimensions in order
  (the semantic model's drill path). `GET …/explore` lists per chart what a click filters by and drills into (no query).
* **Rows behind a selection.** `POST …/drill {detail: true}`: the dataset's FROM and WHERE plus the member predicates on
  the expressions the chart groups by, projecting only columns the viewer may read (denied ones are counted, not
  named), at most 100 rows. Joins and set operations are refused, because the rows' grain would not be the table's.
* **Bounds.** At most 24 statements per request; identical statements run once; at most 4 statements per dashboard at a
  time in a process (a bounded map of semaphores; a tile that waits over 15 s is `busy`); inline previews ≤
  `MAX_PREVIEW_ROWS`; the gateway's result cache answers repeats. The client debounces 250 ms, aborts the older request
  and keeps an LRU of 20 answered views.
* **UI.** Builder grid: chips "Filtered by Priority = 1 - Critical ×", Clear all, Apply as dashboard filter; per tile
  Filtered / Not filtered / Your selection / Dashboard filter; keyboard lists ("Filter the dashboard by Priority",
  "Drill into day…"), breadcrumbs back and a "Show rows" panel. Cells are labelled groups with a Select toggle for the
  chat focus. Query ids and cache hits appear only with expert tools on.
* **Not built.** Embedded Superset dashboards keep Superset's native filters. SQL Server time axes (DATEADD/DATEDIFF)
  are not re-grained. Time members are drilled, not cross-filtered.
