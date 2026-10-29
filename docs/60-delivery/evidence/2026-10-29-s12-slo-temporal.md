# S12 load test and SLO report - 2026-10-29

Load: 20 people asking (think 5.0 s) for 120 s and 20 concurrent investigations: 2.0x the pilot targets of config/slo.yaml v1. Host: win32, 8 cores; host CPU busy during the load: mean 100%, p95 100%, max 100% (126 samples).

| SLO | Target | Client side | Server (/metrics) | Detail | Met |
|---|---|---|---|---|---|
| ask_latency | >= 95% within 3.0 s | 0.7386 | 0.7638 | p95 8989.2 ms | **no** |
| run_success | >= 95% completed or at the approval gate, p95 <= 900 s | 1.0 | None | p95 245.2 s | yes |
| availability | >= 99.5% | 1.0 | 1.0 |  | yes |

## Requests

| Kind | n | 5xx | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| ask | 306 | 0 | 2352.3 | 8989.2 | 10046.7 |
| thread | 20 | 0 | 2045.3 | 2433.1 | 2644.1 |
| run_start | 20 | 0 | 2458.4 | 3950.3 | 4076.3 |
| run_poll | 1000 | 0 | 110.6 | 294.0 | 465.3 |

Ask outcomes: {'refused:no_api_key': 85, 'answered': 221}

## Runs

```json
{
 "started": 20,
 "requested": 20,
 "finished": 20,
 "not_finished": 0,
 "not_finished_last_status": {},
 "outcomes": {
  "WAITING_USER": 20
 },
 "seconds": {
  "n": 20,
  "p50": 238.0,
  "p95": 245.2,
  "p99": 246.9,
  "max": 246.9
 }
}
```

## Notes

* topology: API + 2 worker process(es) (every queue) on a throwaway Temporal 1.25
* world: ServiceNow mock `incident` staged into one workspace in 43.1 s
* 20 people asking, each with their own analyst login (Ask budgets are per person)
* no model was called (OPENROUTER_API_KEY cleared): Ask answered from the registry and rules

## Analysis (2026-10-29, S12-T6)

Three runs on the same host and code (`scripts/load/slo_report.py`, 2x pilot: 20 people asking for 120 s, each with
their own analyst login; no model):

| Run | Investigations | Host CPU busy (mean / p95) | Ask within 3 s | Ask p50 / p95 | Runs at the gate | Availability |
|---|---|---|---|---|---|---|
| Temporal, API + 2 workers (this file) | 20 | 100% / 100% | 73.9% | 2.35 s / 8.99 s | 20/20, p95 245 s | 100% |
| lite, runs in the API process (`2026-10-29-s12-slo-lite.md`) | 20 | 75% / 100% | 44.4% | 3.97 s / 14.15 s | 20/20, p95 305 s | 100% |
| Ask alone, API only (`2026-10-29-s12-slo-ask-only.md`) | 0 | 53% / 86% | **95.5% (met)** | 0.27 s / 0.94 s | - | 100% |

Against 2026-10-28: runs on Temporal went from 0/20 to 20/20 (the compute pool fix,
`2026-10-29-s12-compute-pool-windows.md`; during this run one pool child died with `WinError 6` again, its 4
attempts failed with `ComputeChildDied`, were retried, and every run still reached the gate). Ask answers went from
62 of 356 asks (the rest `budget_exceeded`: 20 people shared one login and its 60 statements per hour) to 221 of
306 (the 85 refusals are `no_api_key`: questions only a model could answer, and no model ran).

**Ask latency still misses at 2x pilot on this host: 73.9% within 3 s against 95% (21 points short), p95 9.0 s
against 3 s (3x) on Temporal; 44% and p95 14.2 s in lite.** Why: the 20 investigations take every core of this
8-core host (the two workers' compute pools are 8 processes, plus their analysis threads and Postgres), and the API
shares those cores. With the host to itself the same Ask path meets the target (95.5%, p95 0.94 s). In lite the
investigations also run in the API process and compete with Ask for its interpreter lock, so lite is for small
installs only and cannot meet this SLO at 2x pilot.

What changed on the Ask path (governance unchanged: scope, budgets, validation, the gateway, the numbers guard all run
as before):

* tenant binding: one statement per transaction instead of two (`set_config('role', r, true)` is `SET LOCAL ROLE`);
  an Ask opens ~18 transactions;
* recording the answer as Data Thread steps no longer builds a thread view nobody reads;
* together 144 -> 118 control-plane statements per Ask; in-process with 20 concurrent askers p95 2.9 s -> 1.5 s.

**Capacity that meets it** (from these runs): run the API on cores the workers do not use. At 2x pilot the
investigations saturate 8 cores for ~4 minutes, while Ask alone needs well under half of this host. So: worker
nodes with >= 8 cores for 20 concurrent investigations (the Helm chart already deploys the API and each worker pool
separately, `analystos.io/role`), and the API with >= 2 dedicated cores (requests, not limits) and its own replica
count; Postgres on its own resources. Not yet run on Linux nodes; that run is the sign-off.
