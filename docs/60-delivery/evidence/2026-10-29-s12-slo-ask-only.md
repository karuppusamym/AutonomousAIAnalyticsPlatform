# S12 load test and SLO report - 2026-10-29

Load: 20 people asking (think 5.0 s) for 120 s and 0 concurrent investigations: 2.0x the pilot targets of config/slo.yaml v1. Host: win32, 8 cores; host CPU busy during the load: mean 53%, p95 86%, max 100% (63 samples).

| SLO | Target | Client side | Server (/metrics) | Detail | Met |
|---|---|---|---|---|---|
| ask_latency | >= 95% within 3.0 s | 0.9545 | 0.9565 | p95 937.6 ms | yes |
| run_success | >= 95% completed or at the approval gate, p95 <= 900 s | None | None | p95 None s | not measured |
| availability | >= 99.5% | 1.0 | 1.0 |  | yes |

## Requests

| Kind | n | 5xx | p50 ms | p95 ms | p99 ms |
|---|---|---|---|---|---|
| ask | 440 | 0 | 270.0 | 937.6 | 4039.5 |
| thread | 20 | 0 | 399.7 | 465.6 | 476.4 |
| run_start | 0 | 0 | None | None | None |
| run_poll | 0 | 0 | None | None | None |

Ask outcomes: {'refused:no_api_key': 122, 'answered': 318}

## Runs

```json
{
 "started": 0,
 "requested": 0,
 "finished": 0,
 "not_finished": 0,
 "not_finished_last_status": {},
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

* topology: lite (runs execute inside the API process, local orchestrator)
* world: ServiceNow mock `incident` staged into one workspace in 9.5 s
* 20 people asking, each with their own analyst login (Ask budgets are per person)
* no model was called (OPENROUTER_API_KEY cleared): Ask answered from the registry and rules

## Analysis

See the comparison of the lite, Temporal and Ask-alone runs in `2026-10-29-s12-slo-temporal.md` (Analysis).
