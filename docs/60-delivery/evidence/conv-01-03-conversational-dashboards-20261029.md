# CONV-01…03 live run — conversational dashboards (2026-10-29)

**What ran.** `tests/integration/test_dashboard_conversation.py` against the compose Postgres (`analystos-postgres-1`,
migrated and seeded by the test's `control_db` fixture: `analystos seed` users analyst / approver), with
`ANALYSTOS_EVIDENCE_OUT` set. Windows 11 dev host, branch `claude/conv-01-03-dashboard-chat`. **No model was called**:
the provider key was blanked for the module, so only deterministic rungs could answer.

**Setup (in the test).** A workspace over one staged table (`incidents.parquet`, 60 rows: priority, category, opened_at,
resolution_hours) through a `csv` source. The analyst proposed the semantic model (dataset `incidents`, primary key
`number`, dimensions priority / category, time dimension opened_at); the approver approved it. Two metrics, proposed by
the analyst and approved by the approver: `resolution_time = AVG(resolution_hours)` and `incident_count = COUNT(number)`.

## Results

| Step | Request | Outcome |
|---|---|---|
| CONV-02 | `POST /api/v2/workspaces/{ws}/dashboards/draft` "Build an incident operations dashboard" | 201, draft `art_4f1e34078763` v1, grounded on the approved metrics, ranked by rules. KPIs: incident count **60**, resolution time **11.7 h** (both equal pandas over the file). Charts (6): resolution time per month (line, gateway execution), resolution time by priority (bar, `resolution_time@1`), incident count per month (line), incident count by priority (bar), resolution time by category (bar), incident count by category (bar). Filter controls: category, priority (no preset value). Failed tiles: none |
| CONV-02 | "a weather forecast dashboard" | 200 `clarify`, no dashboard created |
| CONV-01 | builder chat on an empty dashboard: "add resolution time by priority" | `applied`, v2, rules rung (no model); chart `c_resolution_time_by_priority`, bar, `dataset_ref = metric resolution_time@1 [incidents.priority]`, source the staged table; rows 1 - Critical **10.85**, 2 - High **11.7**, 3 - Moderate **12.55** (equal pandas `groupby(priority).mean()`) |
| CONV-01 | "add weather by city" | `refused` with a clarifying reply and the examples "Incident count by priority", "Resolution time by priority"; version unchanged (2). With no model key the generation rung was unavailable: "SQL generation unavailable: no API key for provider 'openrouter'" |
| CONV-03 | Ask "resolution time by category" → promote `draft {title}` | new draft `art_6d57d423692f` with a bar chart bound to `resolution_time@1`, `provenance.turn_id` = the Ask turn |
| CONV-03 | promote `draft {dashboard_id: art_4f1e34078763}` | `added`, the prompt-built draft is now v2 |
| Publish | `POST /api/v2/dashboards/art_4f1e34078763/publish-request {destination: preview}` | 202, approval `apr_e108786bcd7f` pending, risk medium, payload hash set: the metric-bound charts compiled into the publish bundle |

Command (from the worktree, Windows):

```
set PYTHONPATH=<worktree>\src;<worktree>
set ANALYSTOS_EVIDENCE_OUT=<file>.json
.venv\Scripts\python -m pytest -q -m integration tests/integration/test_dashboard_conversation.py
```

Result: 1 passed.

**Host note.** Docker Desktop 4.51 would not start its engine: stale AF_UNIX socket files
(`%LOCALAPPDATA%\Docker\run\dockerInference`, `%LOCALAPPDATA%\docker-secrets-engine\engine.sock`) could not be removed
("The file cannot be accessed by the system"), and each failed start left a new one. Moving both folders aside
(`*.stale-*`) and starting Docker Desktop once fixed it. Only `analystos-postgres-1` and `analystos-redis-1` were
started again afterwards.

**Not covered here.** A model-ranked plan (`dashboard_plan` in `always` mode) and a model-written chart (ad hoc label)
ran only in unit tests with a fake router; the browser journey ran against the mock backend
(`web/e2e/dashboard-draft.spec.ts`), not this database.
