# Runbook 10 — Trino federation (TRN-004)

Spec v3 §7.2: a cross-source run may execute on a federating engine. With Trino, the join happens inside the
cluster, and AnalystOS still checks every table against its own source's scope first. Tracker row N-7.
Evidence: [`docs/60-delivery/evidence/trn004-trino-federation-20261029.md`](../60-delivery/evidence/trn004-trino-federation-20261029.md).

## 1. The model

* **One AnalystOS source per Trino catalog.** It is a source of kind `trino` with `config.catalog` set, for
  example `pg` or `tpch`. Its assets are `schema.table` names inside that catalog, discovered through the
  connector like any SQL source. Three-part names stay refused by the validator, so a statement can only reach
  another catalog through another source that is in the scope.
* **Queried in place, opt-in.** Register the source with `execution_mode: pushdown`. Without it, the default is
  `staged`, which extracts into the analytics DB. The gateway validates in the `trino` dialect, adds the row cap
  (`LIMIT cap + 1`), and runs the statement on Trino.
* **Read-only by transaction.** Every connection the connector or the gateway opens is an
  `engines/trino.ReadOnlyConnection`. Each of its transactions starts with `START TRANSACTION READ ONLY`, and it
  never runs a statement outside a transaction. Trino refuses any write with `READ_ONLY_VIOLATION`, even when
  the Trino user could write. Still give that user read-only catalog rules (defense in depth).
* **Timeout and cancel.** The timeout is the `query_max_execution_time` session property, sent with every
  request. `QueryGateway.cancel(query_id)` cancels the statement through its cursor (`USER_CANCELED`).
* **Federation.** Use `execute(..., federated=True)`, or `run_sql_for(scope, federated=True)`. When every source
  in the scope is a `trino` source, the federated dialect is `trino` (`validator.federation_dialect`).
  * The statement is validated against each source's assets, columns and denied columns.
  * The gateway checks that all sources share one cluster and one identity: host, port, user, scheme and
    `secret_ref`.
  * Each table is then prefixed with its source's catalog (`engines/trino.qualify_catalogs`), and the statement
    runs once on Trino.
  * The audit row keeps the catalog-qualified SQL. Lineage records `reads` for each table and `from_source` for
    each source.
  * A scope that mixes Trino with other kinds keeps the DuckDB path (legs pulled through the gateway).
  * Moving data between sources still needs the workspace policy `data_movement: staged_ok`.
* **Scan budget.** `EXPLAIN (FORMAT JSON)` gives the estimate: the scan nodes' output bytes, summed
  (`gateway/cost.trino_estimate`). A scan without statistics makes the plan unreadable, so a budget then fails
  closed.
* **Analysis compiler.** `skills/sqlbuild` emits `trino`.
  * Day of week is `EXTRACT(DOW ..) % 7`, so 0 is Sunday, as on postgres.
  * Durations use `TO_UNIXTIME`.
  * Samples and row partitions hash with `LOWER(TO_HEX(MD5(TO_UTF8(..))))`, the same digits as postgres.
  * A grouped median is refused because Trino has no exact percentile; medians come from the row sample.
  * Date and time filter values become typed literals.

## 2. Local stack (compose profile `federation`)

```bash
docker compose --profile federation up -d trino-source-pg trino
```

| Container | Image | Host port | What |
|---|---|---|---|
| `analystos-trino` | `trinodb/trino:476` | `127.0.0.1:${ANALYSTOS_TRINO_PORT:-8086}` | catalogs `pg`, `tpch`, `memory` (`deploy/trino/catalog/`) |
| `analystos-trino-source-pg` | `pgvector/pgvector:pg16` | `127.0.0.1:${ANALYSTOS_TRINO_PG_PORT:-15434}` | demo database `trino_demo` (`deploy/trino/source-pg/01-sales.sql`), Trino logs in as SELECT-only `trino_ro` |

* `pg.sales.customer.nation_key` joins `tpch.tiny.nation.nationkey`.
* `memory` is writable on purpose: the tests prove the platform's sessions cannot write to it while the same
  user can.
* Both ports are bound to loopback. Trino runs without authentication here, so this setup is for development only.
* The demo database is not the control plane.
* On Windows, check `netsh interface ipv4 show excludedportrange protocol=tcp` before you choose a port.

Register the two sources (API or seed), for example:

```json
{"kind": "trino", "name": "Sales (Trino pg)", "execution_mode": "pushdown",
 "config": {"host": "127.0.0.1", "port": 8086, "catalog": "pg", "username": "analystos", "schemas": ["sales"]}}
{"kind": "trino", "name": "TPC-H (Trino)", "execution_mode": "pushdown",
 "config": {"host": "127.0.0.1", "port": 8086, "catalog": "tpch", "username": "analystos", "schemas": ["tiny"]}}
```

For password authentication, set `http_scheme: https` and `secret_ref: env:ANALYSTOS_SOURCE_SECRET_<NAME>`. The
driver refuses Basic authentication over http.

Stop the stack:

```bash
docker compose --profile federation stop trino trino-source-pg
```

Remove it, including the demo data volume:

```bash
docker compose --profile federation down -v trino trino-source-pg
```

## 3. Certify and test

```bash
ANALYSTOS_CERT_TRINO_DSN=trino://analystos_cert@127.0.0.1:8086/pg analystos certify-connector --kind trino --dsn-ref env:ANALYSTOS_CERT_TRINO_DSN
pytest -q -m integration tests/integration/test_trino_federation.py   # needs the profile and a data-plane Postgres
python scripts/certify_connectors.py --kinds trino                     # runs the file above, writes connector-trino-YYYYMMDD.md
```

* The kit runs nine checks: the trino validator suite, `read_only`, `timeout`, `cancel`, `row_cap`, `metadata`,
  `type_mapping`, `estimate` and `identity`. `identity` passes as not applicable, because the kind declares no
  session identity.
* The kit writes `docs/evidence/connectors/trino-YYYY-MM-DD.md`.
* The integration tests use `ANALYSTOS_TEST_TRINO_URL` (default `http://127.0.0.1:8086`) and skip when Trino or
  its demo catalogs are missing.
* On a Windows host, point `ANALYSTOS_TEST_DATABASE_URL` and `ANALYSTOS_TEST_ADMIN_URL` at a Postgres you own.
  The demo Postgres on 15434 works.

## 4. Limits

* JSON values come back as text.
* The Trino PostgreSQL connector exposes no primary keys, foreign keys or comments, so discovery has none of them.
* Row-level identity (`capabilities.session_identity`) is not wired. The configured Trino user is the identity.
* `explain()` plan summaries remain postgres-only. Cost estimates work.
* Sources on different Trino clusters, or with different identities, cannot share one federated statement.
