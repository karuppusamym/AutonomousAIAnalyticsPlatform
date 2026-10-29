"""Seed the SQL Server certification fixture (compose profile `mssql`, container `analystos-mssql`).

    docker compose --env-file ~/.analystos/mssql.env --profile mssql up -d mssql
    python scripts/mssql_cert_seed.py          # reads the variables below from the environment
    analystos certify-connector --kind sqlserver --dsn-ref env:ANALYSTOS_CERT_MSSQL_DSN --schemas cert

Creates database `analystos_cert` with `cert.items` / `cert.other` and the least-privilege login `aos_cert_ro`
(db_datareader only: SQL Server has no read-only session, so the identity is the guard). Idempotent.

    ANALYSTOS_MSSQL_SA_PASSWORD       SA password (admin; used here only, never by the gateway)
    ANALYSTOS_MSSQL_CERT_RO_PASSWORD  password for aos_cert_ro
    ANALYSTOS_MSSQL_HOST / _PORT      default 127.0.0.1:14330
"""
from __future__ import annotations

import os
import sys

DATABASE = "analystos_cert"
READER = "aos_cert_ro"


def main() -> int:
    import pymssql

    sa = os.environ.get("ANALYSTOS_MSSQL_SA_PASSWORD")
    ro = os.environ.get("ANALYSTOS_MSSQL_CERT_RO_PASSWORD")
    if not sa or not ro:
        print("set ANALYSTOS_MSSQL_SA_PASSWORD and ANALYSTOS_MSSQL_CERT_RO_PASSWORD", file=sys.stderr)
        return 2
    host = os.environ.get("ANALYSTOS_MSSQL_HOST", "127.0.0.1")
    port = int(os.environ.get("ANALYSTOS_MSSQL_PORT", "14330"))
    conn = pymssql.connect(server=host, port=port, user="sa", password=sa, autocommit=True, login_timeout=10)
    cur = conn.cursor()
    cur.execute(f"IF DB_ID(N'{DATABASE}') IS NULL CREATE DATABASE {DATABASE}")
    # The login password is a bound parameter inside dynamic SQL (CREATE LOGIN takes no variables).
    cur.execute(
        "DECLARE @sql nvarchar(max) = CASE WHEN SUSER_ID(%s) IS NULL THEN N'CREATE LOGIN ' ELSE N'ALTER LOGIN ' END "
        "+ QUOTENAME(%s) + N' WITH PASSWORD = ' + QUOTENAME(%s, '''') + N', CHECK_POLICY = OFF, "
        f"DEFAULT_DATABASE = {DATABASE}'; EXEC (@sql)",
        (READER, READER, ro),
    )
    cur.execute(f"USE {DATABASE}")
    for stmt in (
        f"IF USER_ID(N'{READER}') IS NULL CREATE USER {READER} FOR LOGIN {READER}",
        f"ALTER ROLE db_datareader ADD MEMBER {READER}",
        "IF SCHEMA_ID(N'cert') IS NULL EXEC (N'CREATE SCHEMA cert')",
        "DROP TABLE IF EXISTS cert.other",
        "DROP TABLE IF EXISTS cert.items",
        "CREATE TABLE cert.items (id int PRIMARY KEY, name nvarchar(100) NOT NULL, secret nvarchar(100), "
        "amount decimal(10, 2), created_at datetime2)",
        "CREATE TABLE cert.other (id int PRIMARY KEY, item_id int REFERENCES cert.items (id), note nvarchar(200))",
        "INSERT INTO cert.items SELECT TOP (50) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)), "
        "CONCAT(N'item ', ROW_NUMBER() OVER (ORDER BY (SELECT NULL))), N'do-not-read', 1.25, SYSUTCDATETIME() "
        "FROM sys.all_objects",
        "INSERT INTO cert.other SELECT id, id, N'other' FROM cert.items",
    ):
        cur.execute(stmt)
    conn.close()
    print(f"seeded {DATABASE} (cert.items, cert.other) and login {READER} on {host}:{port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
