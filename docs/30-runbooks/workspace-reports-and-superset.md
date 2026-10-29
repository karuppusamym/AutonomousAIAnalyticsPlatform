# Workspace reports and Superset

## Find published dashboards quickly

Open **Outputs → Published dashboards & reports**. This searchable collection spans all investigations
in the current workspace and shows the newest record for each published BI object. **Open in Superset**
opens the BI dashboard; **Details & evidence** selects its originating investigation and artifact.
Drafts remain in the normal outputs list. Superset uses a separate login session: sign in to the BI
tool in the same browser, then reopen the dashboard link. Publication does not rerun analysis;
check the evidence and source freshness before relying on an older finding.

## How generation works

When starting from an Ask answer, **Investigate why** first opens an editable business question.
Replace a sample request with the outcome you want explained, including the period and comparison,
then choose **Start investigation**. Sample rows and the Ask SQL filters are not automatically the
new run's scope. Check the proposed analysis and any clarification requests; canceling the editor
does not start a run.

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

**Request publication** retrieves the investigation's pending publication proposal for the run's whole reviewed bundle; to publish some of its dashboards on their own, see below. A completed historical run without a pending proposal needs a refreshed, reviewed publishing workflow. Do not approve an older bundle assuming it contains a newer draft.

Before publishing these snapshot-based drafts, refresh the relevant analysis, verify required findings and metric definitions, review the full generated bundle and approve it in the inbox. Nothing was externally published in this change. Existing eight Superset dashboards across four workspaces remain as before.

## Publishing one collection on its own (BI-VIEWS-02, 2026-10-29)

Open a generated dashboard in **Outputs → Dashboards**. Under **Publish → Publish on its own**, tick the dashboards to
publish together (the open one is ticked) and choose **Ask for approval**. The approver sees "Publish chosen dashboards
on their own" in the inbox with exactly that selection. Once approved, **Publish now** checks once more that the
selection, its charts and KPIs, the investigation plan and the policy are unchanged, then publishes only those
dashboards. Another selection needs its own approval. **Request publication** still opens the investigation's own
proposal for all of its dashboards.

## Grouping dashboards already in Superset

**Outputs → Dashboards → Group published dashboards under this workspace's name** lists the dashboards AnalystOS
published to Superset for this workspace and the `Workspace / title` each would get. Tick the ones to rename and ask
for approval; after an approver says yes, **Rename now** re-reads the titles from Superset and renames only if nobody
changed them there since. Only titles change. Hand-made dashboards and other workspaces' dashboards are never listed.

## Highest-value remaining improvements

- Show a shared freshness/reporting-period indicator for saved reports and live Superset dashboards, plus owner and definition versions.
- Add further business measures through reviewed definitions: retail return rate and net sales; service SLA populations and breach polarity; process cycle time and rework. Available columns alone do not establish exclusions, denominators or revenue policy.
- Configure scheduled report delivery only after owners select recipients, timing and the reviewed dashboard/report.

Generation modules: `agents/visualization.py`, `skills/dashboard_collections.py`, `services/reports.py`; publishing: `agents/publisher.py`, `publishing/superset.py`.

## Single-reviewer publishing (updated 2026-10-25)

The existing **The person who asks cannot approve** switch now governs KPI approvals as well as publication approvals. With separation of duties off, a requester with approval rights can explicitly approve their own KPI or publication. With it on, a different reviewer is required. This does not disable approval, definition validation, role checks, expiry or payload/plan/policy verification. Structural model review, relationship acceptance and external delivery-destination authorization retain their separate-reviewer rules.

Retail run `run_7896fbdad581` had already received publication approval `apr_5034c738fd80`, but its publish step failed at bundled KPI self-review. After the rule was made consistent with the disabled switch, the exact unchanged approved bundle was reverified and retried. Publication `pub_d685edb69e1c` succeeded: executive dashboard 16, operational 17, activity 28 and delivery 29. No new approval was fabricated or granted as a different user. All 12 published chart data requests succeeded. Earlier statements that all KPI approvals always need a different person describe the previous implementation.
