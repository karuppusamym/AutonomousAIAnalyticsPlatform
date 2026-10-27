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
