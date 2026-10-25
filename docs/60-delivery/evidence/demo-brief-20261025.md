# DEMO-01 verification — 2026-10-25

Branch: `codex/investigator-demo`.

## Automated checks

- `npm run build`: pass (TypeScript and production bundle). Existing large-chunk advisory remains.
- `npm test -- --run src/test/investigationOverview.test.tsx --maxWorkers=2`: 7/7 pass.
- `npm test -- --run src/test/components.test.tsx src/test/standing.test.tsx src/test/runLineage.test.tsx src/test/workbench.test.tsx src/test/ia.test.tsx --maxWorkers=2`: 73/73 pass.
- `npx playwright test e2e/investigation-overview.spec.ts --workers=1`: 2/2 pass using locally
  installed Chrome via `PW_CHROMIUM_PATH`. The Playwright-pinned browser is not installed.
- Desktop 1440×1000: evidence link navigation, approval focus handoff, expanded limitations,
  WCAG A/AA/best-practice axe scan: pass.
- Phone 390×844 with dark theme: no horizontal page overflow, axe scan: pass.
- `git diff --check`: pass.

The first desktop scan found a pre-existing keyboard shortcut badge contrast issue. Its
background now uses the darker brand token; both final scans report no violations.

## Local live-data rehearsal

Started the existing local Postgres, API and synthetic ServiceNow containers, with the current
branch's web source served by Vite at http://127.0.0.1:5173.

- Workspace: `ws_8efcdf614840` (ServiceNow Incident Intelligence demo).
- Existing completed run: `run_5e3893278298` (30/30 execution steps).
- Current summary: two verified findings, four findings to check; the latter have void records.
- Opened the brief in a real headless Chrome browser against the live local API.
- **Explore findings** moved focus to the investigation details.
- **Open evidence** navigated to `/w/ws_8efcdf614840/outputs/findings/ins_21852c0e05bd`;
  **How it was checked** loaded.
- Browser page errors: none.
- Visually inspected the desktop summary screenshot and the full live investigation screenshot.

Local generated images (ignored, not committed): `var/demo/investigation-brief.png`,
`var/demo/investigation-live.png`, `var/demo/evidence-live.png`. Mock desktop/mobile screenshots
are generated under `web/test-results/` by the Playwright journeys.

Limits: this replays existing synthetic investigation data; no new end-to-end model investigation
or external publication was performed. No DeepAnalyze/vLLM/PySpur runtime was installed.
