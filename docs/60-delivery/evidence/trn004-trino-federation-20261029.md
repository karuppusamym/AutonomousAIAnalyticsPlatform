# TRN-004 — Trino federation, live evidence (2026-10-29)

Tracker row N-7 (TRN-004 half). Code at `57f907a` (`claude/n7-trino-federation-a6ce5f`).
Runbook: [`docs/30-runbooks/10-trino-federation.md`](../../30-runbooks/10-trino-federation.md).

## Environment

| Part | What |
|---|---|
| Trino | `trinodb/trino:476`, container `analystos-trino`, `127.0.0.1:8086`, compose profile `federation` |
| Catalog `pg` | PostgreSQL connector to demo database `trino_demo` (container `analystos-trino-source-pg`, pgvector pg16, `127.0.0.1:15434`), SELECT-only login `trino_ro` |
| Catalog `tpch` | generated TPC-H (`tiny`, `sf1000` used for the long-running checks) |
| Catalog `memory` | writable on purpose, to prove the platform's sessions cannot write |
| Control plane | throwaway databases on the same demo Postgres (not the compose control plane; port 5432 belongs to another stack on this host) |
| Host | Windows 11, Python 3.12, trino client 0.340.0, sqlglot 30.20 |

## 1. Live federated query through `QueryGateway.execute(federated=True)` (06:32 UTC)

Two sources of kind `trino`, `execution_mode: pushdown`: `pg` (schemas `sales`) and `tpch` (schemas `tiny`). They
use the same cluster and the same user. The scope denies `sales.customer.email`.

Statement, as the caller wrote it (`schema.table` names only):

```sql
SELECT r.name AS region, COUNT(DISTINCT c.id) AS customers, SUM(o.amount) AS revenue
FROM sales.orders o JOIN sales.customer c ON c.id = o.customer_id
JOIN tiny.nation n ON n.nationkey = c.nation_key JOIN tiny.region r ON r.regionkey = n.regionkey
GROUP BY r.name ORDER BY revenue DESC
```

Result (1.37 s, audit row `status=ok`, `row_count=3`):

| region | customers | revenue |
|---|---|---|
| ASIA | 1 | 300.0 |
| EUROPE | 3 | 195.25 |
| AMERICA | 1 | 80.25 |

The statement Trino ran, from the audit row: validated in the trino dialect, then catalog-qualified, with the row
cap applied:

```sql
SELECT "r"."name" AS "region", COUNT(DISTINCT "c"."id") AS "customers", SUM("o"."amount") AS "revenue"
FROM "pg"."sales"."orders" AS "o" JOIN "pg"."sales"."customer" AS "c" ON "c"."id" = "o"."customer_id"
JOIN "tpch"."tiny"."nation" AS "n" ON "n"."nationkey" = "c"."nation_key"
JOIN "tpch"."tiny"."region" AS "r" ON "r"."regionkey" = "n"."regionkey"
GROUP BY "r"."name" ORDER BY "revenue" DESC LIMIT 10001
```

Referenced assets: `sales.customer`, `sales.orders`, `tiny.nation`, `tiny.region`. Each asset is bound to its own
source, and lineage records `reads` and `from_source`.

Refusals in the same scope:

| Statement | Gateway answer |
|---|---|
| `SELECT c.email, n.name FROM sales.customer c JOIN tiny.nation n ON …` (federated) | `Column sales.customer.email is restricted by policy and cannot be referenced.` |
| `SELECT name FROM pg.sales.customer` | `Three-part names (pg.sales.customer) are not allowed. Reference tables as schema.table.` |

Plan estimate for a `pg` join, from Trino's `EXPLAIN (FORMAT JSON)`: about 6 rows and 108 bytes scanned
(`basis: explain`).

## 2. Integration tests (`tests/integration/test_trino_federation.py`, 6/6)

Written by `scripts/certify_connectors.py --kinds trino`:
[`connector-trino-20261029.md`](connector-trino-20261029.md).

* **Discovery** through the read-only connection finds `sales.customer` and `sales.orders` with their types.
* **Read-only by transaction.**
  * The same Trino user creates a table in `memory` in autocommit, so the user itself can write.
  * The connector's sessions and the gateway engine are refused (`READ_ONLY_VIOLATION`), and the table stays empty.
* **Pushdown through the gateway:**
  * A join and a row cap (`max_rows=2` returns 2 rows with `truncated=True`).
  * Refused: a denied column, the denied column through `*`, a catalog hop, a `TABLE(system.query(...))`
    pass-through, and a table outside the scope.
* **Cross-catalog join.**
  * Revenue by region (AMERICA 80.25, ASIA 300.0, EUROPE 195.25), with catalog-qualified SQL in the audit.
  * Refused: a denied column inside the join, a discovered table that is not in the scope, and the same
    statement without `federated=True` (`Cross-source`).
* **Timeout and cancel.**
  * With `timeout_seconds=1`, an `sf1000.lineitem` scan raises `QueryTimeout` in under 10 s.
  * `gateway.cancel(query_id)` from another thread raises `RunCancelled`, and the audit status is `error`.
* **Analysis compiler.**
  * `day_of_week` on Trino returns 0–5 for the six demo dates (0 = Sunday, as on postgres).
  * A compiled `trend` spec returns 4 months.
* **Scan budget.** `gateway.estimate` returns `basis=explain` with 6 rows. With `max_scan_bytes=1`, a single-source
  query and a federated query both raise `BudgetExceeded` before execution.
* **Clean skip.** With `ANALYSTOS_TEST_TRINO_URL` pointing at a closed port, all six tests skip with "Trino is not
  reachable".

## 3. Certification kit (`analystos certify-connector --kind trino`, 9/9)

[`docs/evidence/connectors/trino-2026-10-29.md`](../../evidence/connectors/trino-2026-10-29.md):

| Check | Result |
|---|---|
| validator | 28 hostile trino statements refused |
| read_only | CREATE TABLE / DELETE refused (`READ_ONLY_VIOLATION`) |
| timeout | stopped after 1.2 s |
| cancel | 1.1 s |
| row_cap | 11 rows for a cap of 10 |
| metadata | discovered tables with typed columns |
| type_mapping | JSON-safe values and Parquet types |
| estimate | plan-based |
| identity | not applicable |

`connectors/certification.py` now reports `trino` as `certified`.

## 4. Fast suite and lint

* `ruff check src tests`: clean.
* `pytest -m "not integration"`: all pass except `test_sse_stream.py::test_loop_stays_responsive_while_the_database_is_slow`.
  That test is a timing check that does not touch these paths, and it passed on 2 of 3 runs in isolation on this
  host.
* New unit tests: `test_trino_engine.py` (17).

## Limits (stated, not hidden)

* A federated statement must use one Trino cluster and one identity; otherwise it is refused. Mixed Trino and
  non-Trino scopes keep the DuckDB legs path.
* The Trino PostgreSQL connector exposes no primary keys, foreign keys or comments.
* JSON comes back as text.
* There is no grouped median, because Trino has no exact percentile; medians come from the row sample.
* No session identity is passed to the source.
* `explain()` plan summaries stay postgres-only.
* Ask's deterministic rules path does not cover trino tables yet (`agents/ask_rules.SUPPORTED_DIALECTS`).
* No authentication on the dev cluster. It is bound to loopback.
