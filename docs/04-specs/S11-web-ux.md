# S11 — Web UX

**Priority:** P0 (Desk, Ask, Run) / P1 (Builder, Autonomy Center, Model admin) · **Stack:** React 18 + Vite + ECharts (kept; v1's Next.js is not adopted)

## 1. Baseline
18 pages in `web/src/pages` (Ask, Studio, Runs, RunView, Insights, Catalog, Sources, Approvals, Governance, Monitoring,
Schedules, Reports, Registry, Admin, AdminSettings, WorkspaceHome, Workspaces, Login), generated OpenAPI client
(`web/src/generated/openapi.ts`), context export screen, knowledge studio.

## 2. Information architecture (v2)
| Area | Screen | New or changed |
|---|---|---|
| Home | Workspace home: my requests, alerts, recent answers, pinned dashboards | **changed**: Desk widgets |
| **Desk** | Request queue (requester view and analyst view), request detail with timeline, SLA, certify-on-close | **new** (S13) |
| Ask | Chat with a right-hand trust panel (SQL, metric version, freshness, evidence, `governed`/`ad_hoc`), branch, corrections chips | changed: branches, corrections, state chip bar |
| Investigate | Run view: goal header, live plan DAG, budget meters, hypothesis/evidence tree, pause/redirect/approve | changed: budget meters, plan versions |
| Build | Dashboard builder: grid + chat panel issuing typed ops, op history, version diff | **new** (S08) |
| Publish | Audience, classification, preview as viewer, approval status, rollback | changed |
| Autonomy Center | Runs, schedules, monitors, queues, budgets, exceptions, noise stats | **new** (merges Runs, Schedules, Monitoring) |
| Context | Knowledge studio, glossary, metrics, ledger browser, OKF export/import, context receipts | changed: ledger + receipts |
| Tools | Registry: data products, agents, playbooks, external MCP admission | changed |
| Admin | Tenants, workspaces, roles, **Models** (providers, purposes, profiles, budgets, evaluation matrix), policies, audit, spend | **Models** new |
| Value | Value dashboard (S13 metrics) | **new** |

## 3. UX invariants (v1 §14.1, kept)
* Show "running" versus "validated". No fabricated completion.
* Every claim offers "View evidence" with metric version and data period.
* Branching never erases prior results.
* Automation bounds, remaining budget, current plan and blockers are always visible during a run.
* Editing a published dashboard creates a draft.
* Unresolved assumptions are listed and correctable, and a correction triggers a new versioned run.
Added:
* Every answer shows **who/what answered** (rule, verified query, model + rung) and its cost.
* Personal versus team memory is visibly distinct (chip color and label).

## 4. Quality bars
WCAG 2.2 AA (axe in CI + manual pass on Desk, Ask, Run, Build, Publish); keyboard-complete; works at 1280 px
and on tablet; SSE reconnect with `Last-Event-ID`; all strings in an i18n catalog (English first).

## 5. Tasks
| ID | Task | Est |
|---|---|---|
| S11-T1 | Desk queue + detail (requester and analyst views) | 4d |
| S11-T2 | Ask: branches, corrections, state chips, answered-by + cost | 3d |
| S11-T3 | Run view budget meters + plan versions | 2d |
| S11-T4 | Builder (with S08-T3) | — |
| S11-T5 | Autonomy Center (merge pages) | 3d |
| S11-T6 | Admin › Models | (S05-T4) |
| S11-T7 | Value dashboard | 2d |
| S11-T8 | axe CI + a11y fixes | 2d |
