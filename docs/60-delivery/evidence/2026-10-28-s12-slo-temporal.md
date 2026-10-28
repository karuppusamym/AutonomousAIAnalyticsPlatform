# S12 load test and SLO report - 2026-10-28

Load: 20 people asking (think 5.0 s) for 120 s and 20 concurrent investigations: 2.0x the pilot targets of config/slo.yaml v1. Host: win32, 8 cores.

| SLO | Target | Client side | Server (/metrics) | Detail | Met |
|---|---|---|---|---|---|
| ask_latency | >= 95% within 3.0 s | 0.8118 | 0.8218 | p95 6333.0 ms | **no** |
| run_success | >= 95% completed or at the approval gate, p95 <= 900 s | 0.0 | None | p95 None s | **no** |
| availability | >= 99.5% | 1.0 | 1.0 |  | yes |

## Requests

| Kind | n | 5xx | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| ask | 356 | 0 | 668.7 | 6333.0 | 10196.0 |
| thread | 20 | 0 | 1038.3 | 3343.3 | 4146.7 |
| run_start | 20 | 0 | 3995.6 | 4591.3 | 5546.0 |
| run_poll | 6700 | 0 | 22.3 | 80.6 | 233.7 |

Ask outcomes: {'refused': 296, 'answered': 60}

## Runs

```json
{
 "started": 20,
 "requested": 20,
 "finished": 0,
 "not_finished": 20,
 "not_finished_last_status": {
  "RUNNING": 20
 },
 "outcomes": {},
 "seconds": {
  "n": 0,
  "p50": null,
  "p95": null,
  "p99": null,
  "max": null
 }
}
```

## Notes

* topology: API + 2 worker process(es) (every queue) on a throwaway Temporal 1.25
* world: ServiceNow mock `incident` staged into one workspace in 18.2 s
* no model was called (OPENROUTER_API_KEY cleared): Ask answered from the registry and rules
