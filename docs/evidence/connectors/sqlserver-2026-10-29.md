---
kind: sqlserver
date: 2026-10-29
engine: Microsoft SQL Server 2022 (RTM-CU26) (KB5093420) - 16.0.4265.3 (X64) (127.0.0.1)
test: analystos certify-connector
result: pass
dialect: tsql
commit: 2338c90
---
# Connector certification: sqlserver

Live run 2026-10-29 05:58 UTC against Microsoft SQL Server 2022 (RTM-CU26) (KB5093420) - 16.0.4265.3 (X64) (127.0.0.1), written by `analystos certify-connector` (S02-T6). The DSN reference is not recorded.

| Check | Result | Seconds | Detail |
|---|---|---|---|
| validator | pass | 0.00 | 30 hostile statements refused in tsql; a safe one accepted |
| read_only | pass | 0.70 | CREATE / INSERT / UPDATE / DELETE refused for the least-privilege reader login (no read-only session on SQL Server: the identity is the guard) |
| timeout | pass | 1.59 | stopped after 1.3s with a 1s driver timeout; the next statement ran |
| cancel | pass | 0.45 | interrupted after 0.5s |
| row_cap | pass | 0.12 | 1000-row statement returned cap + 1 = 11 rows |
| metadata | pass | 2.47 | 2 tables, 8 columns (e.g. cert.items), 2 key and 1 foreign-key columns |
| type_mapping | pass | 2.03 | int, bigint, decimal, float, bit, nvarchar, date, datetime2, uniqueidentifier, varbinary, NULL map to JSON-safe values and typed Parquet columns |
| estimate | pass | 0.00 | no plan estimate for tsql: the gateway's estimate is the row cap (basis row_cap), so a scan budget cannot stop a statement before it runs on this kind; row cap and driver timeout bound it |
| identity | pass | 0.27 | SESSION_CONTEXT app.user / app.user_email set read-only per statement for two users in turn; none left on the next connection (a security policy can read them) |
