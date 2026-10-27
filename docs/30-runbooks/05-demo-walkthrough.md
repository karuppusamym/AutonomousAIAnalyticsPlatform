# Runbook 05 — Demo walkthrough (10–15 minutes)

A click-by-click script for showing AnalystOS live: what to click, what to say, and what to do when a step is
slow or fails. It assumes the demo stack from `scripts/demo.ps1` (Windows) or `scripts/demo.sh`, which
brings everything up with Docker Compose and seeds a ready workspace with `analystos demo-seed`.

Evidence that this path works end to end, and what was and was not proven:
[`docs/60-delivery/evidence/2026-09-27-demo-readiness.md`](../60-delivery/evidence/2026-09-27-demo-readiness.md).

## 1. The night before

1. Docker Desktop running (WSL 2 backend), with at least **8 GB of memory** for the full stack
   (Settings → Resources). The full stack is Postgres, Redis, Temporal, Superset, the API, a worker, a scheduler,
   the web UI and the ServiceNow mock.
2. These host ports must be free: 5173 (web), 8000 (API), 8088 (Superset), 5432 (Postgres), 6379 (Redis),
   7233 (Temporal), 8090 (ServiceNow mock). A local Postgres service on 5432 is the usual clash: stop it
   (`Stop-Service postgresql*`) or use the lite path in §6.
3. From the repository folder, in PowerShell:

   ```powershell
   .\scripts\demo.ps1          # full stack; first build takes 10–20 minutes, later starts about 2 minutes
   ```

   If script execution is blocked: `powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1`.
   The script creates `.env` from `.env.example` if there is none (never commit `.env`), builds and starts the
   stack, waits for the API, the web UI and Superset, runs `analystos migrate`, `seed` and `demo-seed`, and prints
   the URLs, the logins and a one-line health summary. Every step is idempotent: run it again at any time.
4. Open the printed workspace link and click through §3 once, so browser caches and Superset are warm.
5. Keep two browser windows: a normal one signed in as the **analyst**, and a private one signed in as the
   **approver**. Keep a third tab on Superset (`http://localhost:8088`, `admin` / `admin`).

| What | Where | Sign in |
|---|---|---|
| Web UI | http://localhost:5173 | `analyst@analystos.local` / `ChangeMe123!` (runs work) |
| | | `approver@analystos.local` / `ChangeMe123!` (approves publication and KPIs) |
| | | `admin@analystos.local` / `ChangeMe123!` (administration) |
| Superset | http://localhost:8088 | `admin` / `admin` |
| API docs | http://localhost:8000/docs | |
| Health | http://localhost:8000/api/health | every dependency as `up`, `down` or `off` |

What `demo-seed` prepared in the workspace **ServiceNow Incident Intelligence (demo)**: the ServiceNow source
(the synthetic mock) with `incident` and `change_request` loaded; a filled workspace brief; one completed,
verified investigation with two dashboards published to Superset; a small CSV (`team_targets.csv`) with a
recipe (`team_capacity`); a weekly re-analysis schedule; and a volume-drift monitor. The investigation also
proposed seven KPI definitions, which wait in the approval inbox: they are part of the story in step 6.

**Do the demo in the seeded workspace.** It is created with the policy *require approved metrics* off, so a run
can publish KPIs it proposed itself (the v1 §62 scenario). A workspace you create live keeps the default (on): its
first Explain run verifies its findings and then stops at `publish_request`, because its KPIs are not approved yet.
If you do create one live, present that as the governance step it is: approve the proposed KPIs as the approver
(**Operate → Approval inbox → Approve a KPI definition**) and start the run again, or turn the policy off under
**Settings → Members & policy → Advanced**. Do not change the platform default.

## 2. If something is down: the status banner

The UI polls `/api/health` every 30 seconds. When a dependency the demo needs is down, a banner under the top
bar says which one and what it means, instead of pages spinning:

| Banner | Meaning | Fix (in a terminal, from the repository folder) |
|---|---|---|
| **API:** not responding | the API container is down or restarting | `docker compose ps`; `docker compose restart api` |
| **Worker:** no worker is running | runs stay queued (shown within about 2 minutes of the worker stopping) | `docker compose restart worker` |
| **Temporal:** unreachable | runs cannot start | `docker compose restart temporal`, then `worker` |
| **Superset:** not reachable | new publications go to the in-platform preview | `docker compose restart superset` (about 1 minute) |
| **Redis:** unreachable | model calls are refused (spend caps fail closed) | `docker compose restart redis` |

`docker compose` needs the same env files as the script; the short form is
`docker compose --env-file .env --env-file deploy/compose/demo.env --env-file deploy/compose/standard.env --env-file deploy/compose/bi.env --profile demo --profile standard --profile bi <command>`,
or simply re-run `.\scripts\demo.ps1`, which restarts whatever is missing.

## 3. The walkthrough

Timings are from the live evidence run on a loaded machine: an investigation reaches the approval step in
45–90 seconds and finishes 10–30 seconds after the approval.

### Step 1 — The workspace (1 min)

* **Click:** sign in as the analyst → the workspace card **ServiceNow Incident Intelligence (demo)**.
* **Say:** "A workspace is one business problem: its data, its brief, its people and its policy. The home page
  shows what needs me — here, KPI definitions waiting for an approver."
* **If slow:** the banner (§2) tells you which service is missing; carry on talking while it recovers.
* **Optional (first-time audience):** **Guide** (top bar) → **Tours** → *The two-minute tour*. It points at the
  real screens in order; **What you can do** lists every capability by role with **Open** / **Show me**.

### Step 2 — Governed data (1.5 min)

* **Click:** **Data → Sources**. Show the ServiceNow source (staged) and the CSV file source. Click **Columns** on
  `incident` to show the column list and tags.
* **Click:** **Data → Catalog & definitions → Brief & readiness**.
* **Say:** "Only the tables selected here are in scope. Every query — from agents, from Ask, from dashboards —
  goes through one gateway that parses it, checks it against this scope, runs it read-only with row caps and
  audits it. Restricted and PII columns are refused even through `SELECT *`. The brief records what the business
  told us — the question, the audience, what counts as out of scope — as reviewed facts the planner uses."
* **If slow:** skip to step 3; the brief is optional colour.

### Step 2b — What the data means, measured (2 min)

* **Click:** **Data → Catalog & definitions → Catalog** → **Columns** on *Incident*. Each column shows what was
  measured: how complete it is, distinct values, range or the full list of codes; **Profile** opens the
  distribution, top values, rows per month and value formats. Point at `number` ("nearly unique: 15 values repeat")
  and `opened_at` ("some rows are dated after today"): the crawler found the data-quality defects on its own.
* **Click:** **Definitions** (opens on **Suggested model**): facts and dimensions, each table's key and how it is
  known, the time column, how the tables join, the gaps to settle and candidate metrics — built by rules, no model.
  **Measure keys and joins** runs the checks through the gateway; **Propose for approval** sends it to an approver.
* **Click:** **Review queue** → the glossary questions ("What do the Priority codes 1–5 mean?", "What does SLA
  mean?") → **Edit & accept** one with a definition.
* **Say:** "Everything an agent reads about this data is measured or confirmed by a person. The platform asks
  what it cannot know — what a code means — instead of guessing, and a rejected suggestion never comes back."
* **Optional:** **Import & export** → **Download context** (OKF, JSON or Markdown; never secrets or rows) and
  **What the agents see** → *Writing SQL for Ask* → **Preview**: the exact prompt context, its token count and
  how much of it is cached between calls.

### Step 3 — Start a live investigation (3 min)

* **Click:** **Start work** (Overview) → **Explain** → keep the objective pre-filled from the workspace →
  **Data: ServiceNow** → point at **Before it starts** (what it reads, the spend cap, what needs a person) and
  **Is the data ready?** (*ready*: every required check passes, including the reviewed grain from the brief) →
  **Start investigation**.
* **Show:** the **Hypotheses** tab filling in (the status counts move Proposed → Testing → Supported / Rejected /
  Inconclusive; click one to filter), then **Live events**.
* **Say:** "Models propose, code decides. Hypotheses come from a closed analysis vocabulary; each one compiles to
  SQL through the gateway and is tested with real statistics, corrected for multiple comparisons. Nothing a
  model writes reaches a number, a query or an approval without passing that deterministic path. With no model
  key at all — as here — every step takes its deterministic rung and says so."
* **Fallback (slow or failed run):** open **Work → Investigations** and the completed run that `demo-seed` made;
  everything below works on it. Say: "Here's one I ran before the session."

### Step 4 — Redirect it (1 min)

* **Click:** on the running investigation, in **Redirect the investigation**, type
  "Exclude inquiry-category incidents; they are requests, not operational failures." → **Send**.
* **Say:** "A person can steer at any time. The request becomes a filter, the plan is re-versioned and hashed,
  and any approval bound to the old plan is invalidated."
* **Fallback:** if the run already finished, show the same box on the completed run and explain it; don't wait.

### Step 5 — Why should I trust a finding? (2 min)

* **Click:** a **Supported** card → **Why trust this** (the checks: method fit, sample size, significance after
  correction, effect size, representativeness, wording) → **Why this number?** (the receipt: query, data
  fingerprint, method).
* **Click:** **Agent console** → the **Queries** tab (every SQL statement, hashed) and **Model calls** (each
  decision with the rung that answered it: rules, a small model or JEV).
* **Say:** "Every verified finding was re-run from its evidence query and checked by an independent method. If
  the data, the query or the semantics change, the verdict turns VOID by itself."

### Step 6 — Approval before anything leaves the platform (2 min)

* **Click (analyst):** wait for **Approvals → Publish dashboards** on the run (status *waiting for you*). Click
  **Approve** as the analyst: it is refused ("role 'editor' cannot approve").
* **Click (approver window):** **Operate → Approval inbox** → *… Publish dashboards* → **Approve**. The run
  finishes publishing within about 30 seconds; a second investigation in the same workspace updates the same two
  dashboards rather than creating new ones.
* **Say:** "Publishing is a side effect, so it needs a person with the approver role. The approval is bound to
  the hash of the exact bundle and the plan; if either changes, it must be approved again. The same inbox holds
  the KPI definitions this run proposed: approving one makes it a governed metric that Ask and dashboards reuse."
* **Optional:** approve one of the **Approve a KPI definition** items.
* **Fallback:** if the live run is not at the approval yet, show the approved publication on the seeded run.

### Step 7 — The dashboards (1.5 min)

* **Click:** on the run's **Result summary → Publication**, open **executive** and **operational**
  (Superset tab, `admin` / `admin`).
* **Say:** "Two dashboards, built from validated KPIs and charts chosen by rule. Superset reads the data through
  a per-workspace read-only login, and every tile traces back to its query and table (**Outputs** → a dashboard →
  lineage)."
* **Fallback (Superset down or slow):** the banner says "Superset is not reachable"; new publications then go to
  the **in-platform preview**, and the run's messages say so. Show **Outputs** → *executive dashboard* instead.
  Restart Superset in the background (§2).

### Step 8 — Keep watching (1.5 min)

* **Click:** **Operate → Monitors & alerts** → *Weekly incident volume drift* → **Evaluate now**.
* **Click:** **Operate → Schedules** → *Weekly incident review* → **Run now** (a re-analysis that re-tests
  every previous finding, diffs against the last run and writes a report; about a minute).
* **Say:** "Findings are not a one-off: schedules re-run the analysis on fresh data and report what is new,
  persisting, changed or resolved; monitors raise alerts, triaged by rules first, and can open an investigation
  automatically."
* **Fallback:** don't wait for the re-analysis; show the schedule's next run and move on.

### Step 9 — Prepare data and ask (1.5 min)

* **Click:** **Work → Prepare data** → recipe **team_capacity** → preview (filter, aggregate, quality gates).
* **Click:** **Work → Ask** → a **Try asking** chip (built from the tables, answered by rules, no model), e.g.
  *Which assignment group has the most incidents?*. Show the labels (*Ad hoc analysis*, *Built from the catalog ·
  no model*, *Validated by the query gateway*), the table and **Why these numbers?**. (The chips under **SQL
  console** fill in read-only SQL for **Explain** / **Run** instead.)
* **Click:** switch to **Step by step** → ask "incidents by priority and category" → the answer cites its steps
  (click *step 2*); each step shows its SQL, the facts computed in code and its checks. **Open as a Data Thread**:
  every step is already recorded there, with versions, re-run and fork.
* **Say:** "The same governance covers data preparation and quick questions: recipes compile to SQL with
  quality gates and lineage; Ask answers through the same gateway."

### Step 10 (optional) — Process and task mining (2 min)

`demo-seed` also builds **ServiceNow process mining (demo)** (`--only process` builds just it): the ServiceNow
source with the task activity log `u_task_activity` (every created, assigned, reassigned, work started, on hold,
resolved, reopened, closed or cancelled step of incidents, changes and catalog tasks, last six months) and the
records it describes, and three saved process analyses (one per task type) in **Outputs**.

* **Click:** that workspace → **Work → Process**. The log is detected with its mapping pre-filled (case
  `task_sys_id`, activity, time `activity_at`, resource `assignment_group`; declared by the ITSM pack). Choose
  **Segment → Change requests** → **Analyze**.
* **Show:** the highlights; the process map (thicker = more often, orange = back to an earlier step); the most
  common paths with the expected one marked; **Conformance**: "'Authorized' skipped in … completed cases" (mostly
  emergency changes); **Cancellations**: after *Assessed* or, for database upgrades, after *Scheduled*.
* **Then:** **Segment → Incidents** → **Analyze**: **Handovers** shows Network Operations ⇄ Cloud Platform /
  Security Operations ping-pong and *Reassigned* as the top rework; **Catalog tasks**: *On hold → Resumed* is the
  biggest wait (End User Computing waiting for stock).
* **Say:** "Every event was read through the same governed gateway (see *How this was computed*: the query ids);
  the analysis is deterministic, and a saved one is a versioned output with lineage to its queries."

### Close (30 s)

"Governed data access, reproducible findings, people approving side effects, and a platform that tells you
plainly when a part of it is down."

## 4. Reset

* **Between rehearsals (keep data):** nothing to do; extra investigations and workspaces are harmless. Re-run
  `.\scripts\demo.ps1` (or `docker compose ... exec -T api analystos demo-seed`) to make sure every seeded piece is
  there.
* **Clean slate (deletes all demo data, about 3 minutes plus a first investigation):**

  ```powershell
  .\scripts\demo.ps1 -Reset
  ```

  This runs `docker compose down -v` (removes the Postgres volume, including Superset's metadata) and starts
  again from nothing.
* **Stop everything:** `.\scripts\demo.ps1 -Down` (data is kept).

## 5. Lite with Docker (no Temporal, no Superset)

`.\scripts\demo.ps1 -Lite` starts Postgres, the API (the local orchestrator runs investigations inside the API
process), the web UI and the ServiceNow mock. Publication goes to the in-platform preview; everything else in §3
works. Use it on a laptop with little memory, or when the full stack will not start.

## 6. Windows without Docker (lite, from source)

For a machine where Docker is not available. Tested here on Linux with the Windows-only gaps simulated
(`resource`, `fcntl`, `os.getuid`, `SIGKILL` and friends removed before import); not run on real Windows.

1. **Postgres 16 with pgvector.** Install PostgreSQL 16 (EDB installer). pgvector is not in the installer: build it
   from the *x64 Native Tools Command Prompt for VS* (as in the pgvector README):

   ```bat
   set "PGROOT=C:\Program Files\PostgreSQL\16"
   git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git
   cd pgvector
   nmake /F Makefile.win
   nmake /F Makefile.win install
   ```

   If that is not possible, run only Postgres in Docker (`docker compose up -d postgres`) and continue at step 3.
2. **Databases and roles.** As the `postgres` superuser:

   ```powershell
   psql -U postgres -c "CREATE ROLE analystos LOGIN SUPERUSER PASSWORD 'analystos'"
   psql -U postgres -c "CREATE DATABASE analystos OWNER analystos"
   psql -U postgres -f deploy\postgres\01-init.sql
   psql -U postgres -d analystos -c "CREATE EXTENSION IF NOT EXISTS vector"
   psql -U postgres -d analytics -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC; REVOKE ALL ON SCHEMA public FROM analystos_reader;"
   ```

3. **Python 3.11 and the app** (PowerShell, repository folder):

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\pip install -e ".[reports]"
   $env:ANALYSTOS_PROFILE = "lite"          # local orchestrator, in-process scheduler, no Redis/Temporal/Superset
   $env:ANALYSTOS_SOURCE_SECRET_SERVICENOW = "admin"        # the mock source's development credential
   .venv\Scripts\analystos migrate
   .venv\Scripts\analystos seed
   ```

4. **Run** (three terminals; set the two `$env:` lines in each):

   ```powershell
   .venv\Scripts\uvicorn analystos.connectors.servicenow_mock:app --port 8090   # demo source
   .venv\Scripts\uvicorn analystos.api.app:app --port 8000                      # API
   cd web; npm install; npm run dev                                             # UI on http://localhost:5173
   ```

5. **Seed:** `.venv\Scripts\analystos demo-seed` (defaults: API `http://localhost:8000`, mock
   `http://localhost:8090`). It prints the workspace link.

Differences from the Docker stack: no Superset (publication goes to the preview), no worker (runs execute in the
API process and resume after a restart), and the Python sandbox is unavailable on Windows, so Python code steps in
notebooks are refused with the reason; investigations, Ask, recipes, schedules and monitors do not use it.

## 7. Running the live proofs yourself

With the stack up (full profile) and a Python 3.11 venv on the host (`pip install -e ".[dev]"`):

```powershell
$env:ANALYSTOS_SOURCE_SECRET_SERVICENOW = "admin"
.venv\Scripts\python scripts\e2e_demo.py        # v1 §62: ServiceNow → findings → dashboards → approval → Superset
.venv\Scripts\python scripts\e2e_phase3.py      # schedules, reports, monitors (uses the workspace of the latest e2e report)
.venv\Scripts\python scripts\e2e_increment3.py  # any database, crawler, token economy
```

Each writes a dated report under `docs/60-delivery/evidence/`. Without `OPENROUTER_API_KEY`, the phase 3 script
checks that alert triage was decided by rules and recorded (JEV cannot be proven without a key); with a key it
checks that JEV triaged the alert.
