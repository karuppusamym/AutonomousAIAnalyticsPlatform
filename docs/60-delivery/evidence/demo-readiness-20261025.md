# Demo cleanup and OpenRouter readiness — October 25, 2026

The workspace list contains four retained populated demos: Operations Intelligence, ServiceNow
Incident Intelligence, ServiceNow process mining and Retail Orders. The other 454 generated
benchmark/evaluation/duplicate test workspaces were archived, not purged. Cleanup manifests are
saved locally under `var/workspace-cleanup-20261025/`.

## Issues found and fixed

1. A new API image resolved unbounded `sqlglot>=30.0` to 30.24.0. That release removed the
   `infer_schema` argument used by the SQL gateway's qualifier. An actual model-generated Ask
   question reached the gateway and failed with HTTP 500. The dependency is now pinned to 30.20.0,
   the version exercised by the existing local security and lineage suites. Gateway validation
   remains unchanged; no checks were weakened to accommodate the new library.
2. The notification list still included archived workspaces (238 retained notifications versus
   11 on current workspaces). Visibility now filters out archived/disabled workspaces before
   pagination. Mark-as-read uses the same visibility filter and cannot modify another user's
   private or inaccessible-workspace notifications.

## Checks completed

- Live OpenRouter probe through the app router: success, `qwen/qwen3-14b`, approximately 2.9 seconds,
  reported cost $0.0000027. Credential presence verified without exposing the key. No provider cooldown.
- The decision provider has no health-probe route and was not live-certified by that chat probe.
- Gateway validator, dialect, corpus and SQL-lineage suites pass under SQLGlot 30.20.0.
- Workspace archival and notification visibility tests: four pass. Targeted Python lint passes.
- Real browser login and eight screens: workspace list, overview, sources, catalog, work, outputs,
  approval inbox and investigation. No page errors or danger alerts.
- No enabled schedule was found on an archived workspace.

## Rebuilt-image results

- Rebuilt and restarted the API. Installed SQLGlot version confirmed as 30.20.0.
- Actual OpenRouter-generated analytical question in Operations Intelligence:
  `deepseek/deepseek-v4-flash-0731`, 12 result rows, about 8.6 seconds. The question groups incidents
  by assignment group and computes average resolution hours. Response `status=answered`,
  `answered_by=model`; SQL was validated and executed by the gateway.
- Retail Orders, Incident Intelligence and process-mining workspaces each answered a scoped grouped
  count question through the deterministic path, returning 2, 5 and 13 rows respectively.
- Browser Ask screen: submitted the Operations question, rendered the 12-row table, no danger alerts
  or page errors. Screenshot `var/demo-readiness-20261025/ask-live.png`.
- Notification API now returns only notifications attached to the four current workspaces; the
  238 archived-workspace notifications are retained but hidden.
- Additional gateway service and skill integration unit suites pass.

## Fresh investigation and approval boundary

Created `run_385d980e0a6d` in Operations Intelligence with the objective of investigating SLA drivers,
assignment groups, priorities and resolution time. It moved from PLANNING to WAITING_USER, with a
pending `execute_plan` proposal `apr_2029d1b1ec23`, no run error and no executed analysis tasks yet.
That plan is ready for a person to review in the demo. It was not approved on the user's behalf.
This check validates creation/planning/approval gating, not a fresh completed full analysis run.

Local URL: http://127.0.0.1:5173/w/ws_796ff841746f/work/investigations/run_385d980e0a6d

Raw local evidence: `var/demo-readiness-20261025/` (health, model probe and browser results;
API credentials are not saved there).

## Known environment limitation

The optional isolated Python sandbox is unavailable in this Docker configuration. Code steps
are refused; the service reports degraded health with an informational sandbox explanation.
SQL-based investigation and Ask use the governed query gateway and are unaffected by this optional
backend. Sandbox protections were not disabled to hide that limitation.
