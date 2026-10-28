# AnalystOS web UI

React 18 + TypeScript + Vite single-page app for **Context2AI AnalystOS**. Every screen talks to
the real FastAPI backend (`src/analystos/api/routers/*.py`). There are no mocks in the app itself.

## Run

```bash
cd web
npm install
npm run dev          # http://localhost:5173, proxies /api -> http://localhost:8000
                     # (override with ANALYSTOS_API_URL=http://host:port npm run dev)
npm run typecheck    # tsc --noEmit
npm test             # vitest (jsdom): parser, chart builders, tree, components, schedules/monitoring/reports,
                     # sources/crawls/catalog/explain/admin settings (increment3.test.tsx), IA/route budget,
                     # state families, command palette, theme and axe per journey (ia.test.tsx), API contract
npm run build        # typecheck + production bundle in dist/
npm run test:e2e     # Playwright journeys + axe (light and dark) against `vite preview`; API mocked by route
                     # interception (e2e/, shared fixtures in src/test/mockBackend.ts). Build first.
                     # Browsers: `npx playwright install chromium`, or PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers,
                     # or PW_CHROMIUM_PATH=/path/to/chrome when the installed build differs.
```

### Generated API client

`openapi.json` is the FastAPI schema, committed and exported without running services:

```bash
PYTHONPATH=src python scripts/export_openapi.py   # from the repository root -> web/openapi.json
npm run gen:api                                   # web/openapi.json -> src/generated/openapi.ts
```

`src/api.ts` builds every request through `apiPath`/`call`, typed by the generated `paths`: a
renamed route, a wrong path or query parameter or a changed request body fails `npm run typecheck`
(`src/test/apiContract.test.ts` pins this). Request-body types (`SourceInput`, `CrawlInput`,
`ScheduleInput`, …) are aliases of the generated schemas. Response bodies are not in the schema
because the routers declare no `response_model`, so each endpoint names a hand-maintained
response shape (the "Response shapes" section of `api.ts`). CI fails when either generated file is
stale.

## Information architecture (spec v4 §15)

`src/routes.ts` is the route manifest: `App.tsx`, the side nav and the Ctrl/Cmd-K palette are built
from it. Each screen declares its area, its audience (everyone, workspace owners, platform admins) and
the concepts whose one home it is (`owns`). Tests assert at most 20 screens (18 today), that every
concept has exactly one home and its editor renders only there (`lightIa.test.tsx`), and that analysts
and viewers see no admin screen.

| Area | Screens |
|---|---|
| Overview | Workspaces `/`, Overview `/w/:ws` (first run: a checklist from real state; after it: what needs you + Start work) |
| Data | Sources `/w/:ws/data/sources`; Catalog & definitions `/w/:ws/data/catalog?tab=catalog\|brief\|documents\|review\|metrics\|definitions\|transfer` (column curation in the catalog; the brief and readiness under `brief`) |
| Work | Work `/w/:ws/work?tab=investigations\|thread\|notebooks\|experiments\|prepare\|builds` (Data Thread `&container=run:<id>\|ask_thread:<id>&branch=`, notebook `&notebook=`, experiment `&experiment=` or `&new=predict\|forecast`, pipeline `tab=prepare&pipeline=`), Ask `/w/:ws/work/ask`, investigation `/w/:ws/work/investigations/:run`, agent console `…/:run/console` |
| Outputs | Outputs `/w/:ws/outputs?type=finding\|dashboard\|report\|dataset\|chart\|prepared\|model\|table\|other`, finding `/w/:ws/outputs/findings/:id?` |
| Operate | Approval inbox, Monitors & alerts (`?tab=monitors\|alerts\|health`: model monitors and pipeline health), Schedules (`/w/:ws/operate/*`) |
| ⚙ Settings | Members & policy `/w/:ws/settings/policy` (owners); Capability registry `/settings/registry`, Platform settings `/settings/platform`, Usage & cost `/settings/usage` (platform admins) |
| (access) | Sign in `/login` |

**Start work** (Overview, Work) lists Explain, Compare, Forecast, Predict, Prepare data and Monitor
as the server decides them (`GET /api/workspaces/{ws}/capabilities`, P4-04); a disabled kind shows
every reason with its remediation and has no start button. Explain and Compare show the readiness
assessment check by check before starting; Predict and Forecast open the ML spec form in Work →
Experiments. Wave-2 panels: the Data Thread (steps, versions, pins, branches, merge), notebooks,
governed ML (Outputs → Models & scoring), pipelines (Prepare data; Outputs → Managed tables); every
side effect is a two-step approval (`components/ApprovalStepper.tsx`). Hashes, JEV, rungs, REV, OKF and Ossie appear only under *Technical details*.

Old URLs (`/admin`, `/operate/*`, `/w/:ws/runs/…`, `/insights/…`, `/investigate/…`, `/knowledge/…`,
`/build/…`, `/ask`, `/sources`, `/catalog`, `/studio`, `/reports`, `/schedules`, `/monitoring`,
`/governance`) redirect to their new home with the query string kept; an old `?tab=` whose content
moved (Build's builds/KPIs/dashboards, Knowledge's graph) lands on the new tab (`LEGACY_REDIRECTS`).
Links are built with `to.*` from `routes.ts`, never by hand.

Design system: tokens on `:root` (light) with dark tokens under `prefers-color-scheme` unless the user
pinned light, and under `[data-theme=dark]`; the top-bar toggle cycles system → light → dark and is
remembered in `localStorage`. State families: `<Value>` renders an absent number as an explicit
"unknown" (never 0, used in KPI, cost and token displays), `<StateView kind=…>` covers loading, empty,
refused, failed, not-entitled, stale and unknown.

Seeded users (development only): `admin@analystos.local`, `analyst@analystos.local`,
`approver@analystos.local`, all with password `ChangeMe123!`.

Optional live smoke test, which renders every screen against a running API:

```bash
ANALYSTOS_LIVE_API=http://localhost:8000 npx vitest run src/test/live.smoke.test.tsx
```

### Docker

`Dockerfile` does a multi-stage build (node:22 build, then nginx). `nginx.conf` serves `dist/` with
SPA fallback and proxies `/api/` to `http://api:8000` (the compose service). Proxying is
SSE-friendly: `proxy_buffering off`, no cache, and a 1 h read timeout. It also allows 210 MB request
bodies for uploads. `compose.yaml` publishes the app on port 5173 (`web` service).

## Screens → endpoints

| Screen | Route | Endpoints |
|---|---|---|
| Login | `/login` | `POST /api/auth/login`, `GET /api/auth/me` (validates a stored token) |
| Workspaces | `/` | `GET/POST /api/workspaces` (name, description, objective, autonomy 0–4 with explanations) |
| Overview | `/w/:ws` | `GET /api/workspaces/{id}`, `…/sources`, `…/analysis`, `…/catalog` (first-run checklist), `…/approvals?status=pending`, `…/alerts?status=open`, `…/insights` (void findings), `…/semantic/relationships/candidates?status=pending`; **Start work** → `GET /api/capabilities?workspace_id=`, `POST …/analysis {objective, source_ids?}` |
| Sources (Data) | `/w/:ws/data/sources` | `GET /api/source-kinds` (Add-source form generated per kind: grouped by category, required/optional fields, `secret_ref` only, driver/install hint, pushdown vs staged, admin-disabled kinds greyed out), `POST …/sources` (file kinds may upload via `POST …/uploads` then `config.path`), `POST …/sources/{id}/discover`, `PUT …/sources/{id}/selection`, `GET …/assets`, `PUT /api/assets/{asset}/columns/{col}/tags`, `GET …/relationships`; per source **Crawl** `POST …/sources/{id}/crawl {mode, include, exclude, profile?, enrich?}`, history `GET …/crawls?source_id=`, running crawls polled every 2 s via `GET /api/crawls/{id}`; detail shows schema drift (new / changed with added, removed, retyped columns / missing / deprecated / rename candidates) and the stage log |
| Catalog & definitions (Data) | `/w/:ws/data/catalog` | `GET …/catalog?q=&domain=&role=&include_deprecated=` (role, domain, grain, confidence, description with origin badge source/rule/model/user, reviewed, lifecycle; expandable columns with role, unit, tags + `tags_origin`, PII category, glossary term), `PATCH /api/assets/{id}/metadata` (editors/owners: business name, description, reviewed) |
| Work | `/w/:ws/work?tab=` | `GET …/analysis`; Prepare data: `POST …/uploads` → `POST …/sources/{id}/ingest`, `GET …/recipes`, `POST …/recipes/{id}/runs {mode: preview\|materialize}`, `POST …/recipes/{id}/publish`, `GET …/recipe-runs`; dbt builds as before |
| Investigation | `/w/:ws/work/investigations/:run` | `GET …/analysis/{run}/why` (numbers panel); `GET …/analysis/{run}` (incl. `origin` badge: scheduled / alert investigation, and a **What changed since the previous run** panel from `summary.changes` with KPI deltas and a link to `summary.report_artifact_id`); live **SSE** `GET …/analysis/{run}/events` (fetch + ReadableStream with a bearer header; reconnects with `?after_id=`); `POST …/analysis/{run}/pause|resume|cancel`; `POST …/feedback`; `POST /api/approvals/{id}/approve|reject`; `GET /api/agent-runs/{task}` (task details); `POST /api/publications/{id}/rollback` |
| Agent console | `/w/:ws/work/investigations/:run/console` | `GET …/analysis/{run}/console` (messages, tool calls, model calls with **JEV decision** highlight for `provider == "typesafe"`, queries, cost) |
| Finding (Outputs) | `/w/:ws/outputs/findings/:id?` | `GET …/insights` (with `verification_state`: void badge + cause), `GET /api/insights/{id}/why[?number=]` (Why this number? drawer), `GET /api/insights/{id}` (how it was checked, queries with SQL + result preview, experiments, lineage) |
| Outputs | `/w/:ws/outputs?type=&artifact=` | `GET …/recipe-runs` (prepared data), dashboards: `POST /api/artifacts/{id}/publish` (proposal → inbox); `GET …/artifacts`, `GET /api/artifacts/{id}` (versions + lineage); dashboard preview uses `GET …/artifacts?type=chart&run_id=` for chart previews |
| Schedules (Operate) | `/w/:ws/operate/schedules?schedule=` | `GET/POST …/schedules` (list includes `recent_runs`), `GET …/schedules/{id}` (pin status: upgrade available + diff), `POST …/schedules/{id}/upgrade` (If-Match + `upgrade_hash`), `PATCH/DELETE /api/schedules/{id}` (edit, enable toggle, delete), `POST /api/schedules/{id}/run` (run now). Form: kind-specific config (reanalysis / dataset_refresh / report / monitor / crawl: sources, mode, include/exclude), cron presets (weekly Mon 07:00, daily 06:00, monthly 1st 07:00) plus raw cron, IANA time zone; next run shown in the schedule's zone and local time; server validation errors (e.g. < 15 min interval) shown verbatim |
| Monitors & alerts (Operate) | `/w/:ws/operate/monitoring?tab=monitors\|alerts&alert=` | `GET/POST …/monitors` (kinds: threshold, drift, change point, forecast deviation {z, history, seasonal_periods?}, data quality), `PATCH /api/monitors/{id}` (enabled, auto_investigate), `POST /api/monitors/{id}/evaluate`, `GET /api/monitors/{id}/series` (line chart, latest point marked, drift baseline median / threshold line), `GET …/artifacts?type=metric` (metric picker), `GET …/assets` (data-quality assets); `GET …/alerts?status=`, `POST /api/alerts/{id}/acknowledge\|resolve\|investigate` (JEV triage `data.triage.p_material` shown) |
| Reports (an Outputs filter) | `/w/:ws/outputs?type=report&artifact=` | `GET …/artifacts?type=report`, `POST …/reports {run_id?, kind, formats}`, `GET /api/artifacts/{id}/download?format=` (fetch with bearer → blob → object URL; HTML **Preview** renders in `<iframe sandbox="" srcdoc>`, never in the page DOM) |
| Approval inbox (Operate) | `/w/:ws/operate/approvals` | `GET …/approvals?status=pending` (inbox across runs), `POST /api/approvals/{id}/approve\|reject` |
| Notifications (top bar) | all pages | `GET /api/notifications?unread=true` polled every 30 s for the badge, `GET /api/notifications` when the dropdown opens, `POST /api/notifications/read {ids}`; a click follows `link {type, id}` (alert → Monitoring, report artifact → Reports, other artifact → Studio, run → Run view, schedule → Schedules) |
| Ask (Work) | `/w/:ws/work/ask` | `POST …/ask` (SQL, explanation, repair attempts, result, chart hint), `POST …/query` (SQL console; `sql_rejected` shown as a gateway rejection), **Explain** `POST …/query/explain` (deterministic explanation + gateway verdict, nothing executed) |
| Members & policy (Settings, owners) | `/w/:ws/settings/policy` | `PUT …/policy` (form for the common fields; JSON under Advanced), `PATCH /api/workspaces/{id}` (autonomy, under Advanced), `POST/DELETE …/members`, `GET …/audit` (the one audit view; admins can switch to `GET /api/admin/audit`) |
| Capability registry / Platform settings / Usage & cost (Settings, admins) | `/settings/registry`, `/operate/settings`, `/operate/usage` | `GET/PATCH /api/agents`, `GET/PATCH /api/tools`, `GET /api/skills`, `GET /api/admin/models` (incl. effective mode per purpose), `GET /api/admin/usage`, `GET /api/admin/audit`; admin-only **Settings** `GET/PUT /api/admin/settings` (per-purpose off/auto/always with the rule-path flag, feature flags, numeric limits with schema ranges, enabled source kinds, disabled models, cacheable purposes; a note is required; `purpose_modes` is always sent as the complete map because the server replaces it wholesale), `POST /api/admin/settings/preset` (confirmed), `GET /api/admin/settings/history` + `POST …/rollback`; **Token savings** `GET /api/admin/token-savings?days=`; **Prompts** `GET /api/admin/prompts` |

## Layout

```
src/
  api.ts                client typed by the generated OpenAPI paths, ApiError, token store, SSE subscription
  generated/openapi.ts  generated from ../openapi.json by `npm run gen:api` (do not edit)
  routes.ts             route manifest (journeys, screens, legacy redirects, `to.*` link builders)
  auth.tsx              auth context (401 anywhere -> logout)
  App.tsx, main.tsx     routes rendered from the manifest
  lib/theme.ts          light/dark/system preference (data-theme on <html>, localStorage with try/catch)
  lib/sse.ts            text/event-stream parser + fetch reader
  lib/charts.ts         ECharts option builders per chart_type (kpi, line, bar, histogram, heatmap,
                        table, pie, treemap, stacked_bar, scatter); light/dark palettes
  lib/tree.ts           Question -> Hypotheses (nested follow-ups) -> Findings
  lib/lineage.ts        layered lineage (upstream -> downstream)
  lib/status.ts         one status -> tone map used by every badge (+ alert severity tones); autonomy level texts
  lib/schedules.ts      cron presets/description/validation, schedule form -> kind-specific config, zone formatting
  lib/monitors.ts       monitor form -> config (incl. forecast_deviation), chart overlays (drift baseline, threshold)
  lib/sourceKinds.ts    source-kind catalog -> add-source form (grouping, typed config, validation)
  lib/crawls.ts         crawl form -> body, stats/drift summaries, stage progress
  lib/settings.ts       admin settings diff -> PUT patch (replaced maps sent whole), limits validation, preset effect
  lib/notifications.ts  notification link -> route
  components/           Layout, ui primitives, Chart, DashboardPreview, InvestigationTree,
                        ApprovalsPanel, LineageGraph, Markdown (safe subset), ErrorBoundary,
                        NotificationBell, ChangesPanel, CrawlPanel, CommandPalette (Ctrl/Cmd-K)
  pages/                one file per screen (Admin.tsx serves the three Operate platform screens)
  test/                 vitest suites; mockBackend.ts is the in-memory API shared with e2e/
e2e/                    Playwright journeys + axe (playwright.config.ts)
```

Design notes: CSS variables with separately tuned light and dark themes (`prefers-color-scheme`, or pinned by the
theme toggle); text and status colours meet WCAG AA contrast in both (checked by axe in Playwright).
Every status value uses the same colour everywhere: green = done/verified, blue = running,
amber = waiting/pending, red = failed/rejected, grey = inert/superseded. JEV decision calls are
violet. The layout is responsive: the nav becomes a drawer under 860 px and the dashboard grid
stacks under 760 px. Charts fall back to a table view when canvas is unavailable, and each chart
has a "Table view" disclosure.

## Known gaps

The next interaction design is [Workspace workbench](../docs/10-architecture/02-workbench-ux.md),
with implementation tracked under P4-07, P5-03 and P6-03. The five areas, Start work, the first-run
checklist and the evidence drawer have shipped (P7-18); the specialist ML and pipeline views arrive
as panels with P5-03 and P6-03.

- There is no user-management screen. `POST /api/users` exists, but the spec does not require one here.
- Hypothesis editing (`PATCH /api/hypotheses/{id}`) is in the client but has no UI yet.
  Feedback and redirect cover the steering flow.
- Context/glossary endpoints (`/api/workspaces/{id}/context`, `/api/context/search`) are not surfaced.
- The dashboard preview renders chart previews as the agents computed them. Native filters are shown
  but do not re-query.
- Schedules: `dataset_refresh`/`reanalysis` `source_ids` can only be chosen for dataset refresh; re-analysis
  uses the workspace sources. `baseline_run_id` (set by the backend) is shown only indirectly via "previous run".
  Schedule ownership is not displayed (the list returns `owner_id` only).
- Catalog: the glossary term is shown (with its match reason) but not linked, because there is no glossary screen yet.
  Column descriptions and business names are read-only in the UI (the curation endpoint covers tables only).
- Admin settings: `routing_overrides` and `profile_models` are shown but edited only through the API.
- Monitors: name/config cannot be edited after creation in the UI (only enable / auto-investigate);
  `sql_expression` monitors and `dataset_artifact_id` are API-only. Data-quality monitors have no chart.
- Notifications are polled (30 s), not pushed; there is no "load more" beyond the API's 100 most recent.
- Publication rollback is available from the run summary, using `summary.publication.publication_id`.
  There is no separate publications list endpoint.
# Lite workspace

Open a workspace and select **Lite workspace** in the header, or visit
`/w/<workspace-id>/lite`. This alternate shell starts with the existing Ask
conversation and keeps Data, Metadata, Investigations, Outputs, and Publish in
a compact activity rail. History and Evidence & SQL are expandable; Workflow
opens a guide beside the current tool. **Full interface** restores the standard
navigation. The choice lasts for the browser tab's session.

The Lite frame opens with three panels. Drag either divider to resize its side
panel, or focus a divider and use the arrow keys (Home/End select its limits).
Double-click a divider to restore its default width. Navigation and Workflow
can be collapsed independently; Focus mode hides both, and Three panels restores
them without clearing the current draft. Narrow screens stack the side panels
around the working area instead of squeezing the conversation.

Lite uses the existing authenticated routes, APIs, and publication approvals.
Tools switch within the shell; this version does not interpret database setup
or publishing commands from chat. Superset publishing still uses the existing
dashboard and approval workflow and requires its normal backend configuration.
