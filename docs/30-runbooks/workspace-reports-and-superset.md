# Workspace reports and Superset

## How generation works

1. An investigation selects authorized source tables and produces an analytical dataset and metric definitions. The model can propose analysis; validated SQL and statistics compute the numbers.
2. The visualization agent chooses chart types, computes previews through QueryGateway, and lays out executive and operational dashboards. It now also creates topic dashboards when enough descriptive charts exist. A topic cannot invent a metric or reuse an unverified causal claim as a descriptive measure.
3. Publication packages datasets, measures, charts and dashboards into a bundle. Governance checks current evidence and metric policy. A human approval binds the exact payload and plan hashes. The publisher verifies that approval before writing to Superset.
4. Superset receives virtual datasets, saved measures, charts, layout, filter configuration and summary text. Dashboard keys/slugs and chart identities are scoped by workspace; repeated publication updates the same objects rather than creating duplicates. Superset reads its virtual datasets using the workspace-specific BI database identity, not the shared loader identity.
5. Superset charts can read current data, while saved AnalystOS chart previews and downloadable investigation reports describe recorded investigation snapshots. A changed source can require reanalysis and re-verification; a live chart refresh does not re-establish an old causal finding.

## Reports are a separate output

AnalystOS renders reports from completed investigation records: executive, operational, statistical, exception and weekly-summary kinds. Supported formats depend on installed renderers. Reports carry findings, metrics, evidence and limitations, including stale/void verification. Generating another report does not rerun an analysis.

Superset's scheduled email/screenshot report integration is separate from these report files. It requires delivery infrastructure, recipients, a schedule and applicable approval; no email schedule was created in this change.

## Workspace-specific collections prepared on 2026-10-25

| Workspace | Topic dashboard drafts | Reports rendered |
|---|---|---|
| Operations Intelligence Demo | Activity and volume, Resolution time | Executive, operational |
| ServiceNow process mining (demo) | Activity and volume, Process waiting time, Process handovers | Executive, operational |
| ServiceNow Incident Intelligence (demo) | Activity and volume, SLA performance, Rework and reassignment | Executive, operational |
| Retail Orders (demo) | Activity and volume, Delivery reliability, Discount patterns | Executive, operational |

There are 11 topic drafts and eight rendered reports (HTML and Markdown). They reuse the latest completed investigation in each workspace. The draft summaries explicitly disclose saved snapshots and the need to review freshness. Existing Superset dashboards were not renamed or republished by this operation.

Use Outputs → Dashboards or Outputs → Reports in the relevant workspace. Lists, selection controls and search now use business titles; technical keys remain internal identities. New generated titles use `Workspace name / Topic` (or executive/operational title). Superset preserves the approved DashboardSpec title, so newly approved publications use the same label there. This is a naming convention, not a new Superset folder or tag feature. The existing workspace-scoped slug remains the stable identity.

## Review and publishing

The current Publish control retrieves the investigation's pending publication proposal; publication is for the run's reviewed bundle, not an arbitrary newly added dashboard. A completed historical run without a pending proposal needs a refreshed, reviewed publishing workflow. Do not approve an older bundle assuming it contains a newer draft.

Before publishing these snapshot-based drafts, refresh the relevant analysis, verify required findings and metric definitions, review the full generated bundle and approve it in the inbox. Nothing was externally published in this change. Existing eight Superset dashboards across four workspaces remain as before.

## Highest-value remaining improvements

- Publish one selected topic collection with its own hash-bound bundle, rather than requiring the whole run's publication proposal.
- Show a shared freshness/reporting-period indicator for saved reports and live Superset dashboards, plus owner and definition versions.
- Add further business measures through reviewed definitions: retail return rate and net sales; service SLA populations and breach polarity; process cycle time and rework. Available columns alone do not establish exclusions, denominators or revenue policy.
- Configure scheduled report delivery only after owners select recipients, timing and the reviewed dashboard/report.

Generation modules: `agents/visualization.py`, `skills/dashboard_collections.py`, `services/reports.py`; publishing: `agents/publisher.py`, `publishing/superset.py`.
