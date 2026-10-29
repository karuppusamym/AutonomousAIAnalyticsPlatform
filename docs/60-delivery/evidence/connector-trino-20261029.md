---
kind: trino
date: 2026-10-29
engine: Trino 476 (docker image trinodb/trino:476; catalogs pg, tpch, memory)
test: tests/integration/test_trino_federation.py
result: pass
commit: 57f907a
---
# Connector certification evidence: trino

Live run 2026-10-29 06:31 UTC at `57f907a`, written by `scripts/certify_connectors.py` after every selected real-engine test passed (none skipped).

Command: `python -m pytest -q -p no:cacheprovider -m integration tests/integration/test_trino_federation.py`

| Test | Result | Seconds |
|---|---|---|
| `test_trino_source_kind_discovery_and_read_only_by_transaction` | passed | 2.7 |
| `test_trino_pushdown_through_the_gateway` | passed | 12.2 |
| `test_cross_catalog_join_runs_on_trino_through_the_gateway` | passed | 1.1 |
| `test_timeout_and_cancel_on_trino` | passed | 3.8 |
| `test_analysis_compiler_sql_runs_on_trino` | passed | 3.3 |
| `test_scan_estimate_from_the_trino_plan_and_budget` | passed | 0.7 |
