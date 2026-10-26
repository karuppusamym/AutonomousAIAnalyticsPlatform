# Workspace reporting collections — 2026-10-25

## Scope and evidence

Read-only inspection confirms Superset connected and eight existing published dashboards, two in each of four active workspaces. Each workspace has its own BI database identity, stable workspace-prefixed object identities and reader grants. Existing dashboard titles were generic executive/operational names without a clear workspace label.

The visualization agent now prefixes generated executive/operational titles with the workspace display name. `skills/dashboard_collections.py` adds topic dashboards from available descriptive charts with existing measures; it excludes chart errors, simulated values and charts carrying causal insight codes, and requires at least two usable charts per topic. Topics keep stable keys for reconcile/update. The existing Superset adapter preserves each approved title.

## Prepared local artifacts

11 draft topic dashboards and eight historical-run reports (HTML/Markdown) were prepared in the four active workspaces. Role checks, artifact versions, chart-to-dashboard lineage, existing dashboard/report events and audit records are retained. Dashboard summaries disclose reused chart snapshots and require freshness review. No workspace or old publication was deleted or externally renamed. Reports preserve the completed run's timestamp and verification caveats.

Retail drafts include Activity and volume (`art_4a54c17370e9`), Delivery reliability (`art_fd18f9509d04`) and Discount patterns (`art_c13e8f29a54f`). Local machine-readable inventory: ignored `var/workspace-bi-prepared.json`. Initial read-only inventories: `workspace-bi-inspection.log`, `workspace-bi-dashboards.log`, `bi-chart-inventory.log`.

## Validation

- 114 unit tests passed: topic generation, chart/layout skills, Superset/preview publishing adapters, chart builders and report narrative (`var/workspace-bi-unit.log`).
- Three local full-flow integration tests passed with the expanded dashboard set, including preview publication and deterministic fallback (`var/workspace-bi-integration.log`). Test expectation now requires executive, operational and activity topic dashboards rather than exactly two dashboards.
- A real-API browser journey opens the retail Delivery reliability draft, shows its saved chart previews and snapshot warning (`web/e2e-live/10-workspace-dashboard.live.spec.ts`, `var/workspace-bi-browser.log`). No browser API mocks. The initial heading selector matched both card and summary headings; narrowed to the card heading before the passing run.
- Targeted Ruff and whitespace checks pass. Local API deployed healthy with the new modules. A source-only image layer followed the existing application image, keeping dependencies unchanged.

## Limits

These are additional local draft views over existing chart snapshots, not new analytical findings or newly verified metrics. They do not claim that old snapshots are current after source refresh. The eight existing Superset dashboards remain unchanged; external publication needs an exact reviewed bundle and existing human approval. The current Publish UI retrieves a pending whole-run proposal; publishing selected topic drafts independently remains future work. No email recipients, schedules or deliveries were created.

See [workspace report/Superset guide](../../30-runbooks/workspace-reports-and-superset.md) for the pipeline, collection inventory, publishing constraints and remaining priorities.
