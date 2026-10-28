"""Production checks run as CronJobs (S12 §5): `analystos ops rls-check`.

The unit test (tests/unit/test_tenancy_rls_coverage.py) proves the metadata is classified; this proves the
live database matches it: every tenant-owned table has row security on, the `tenant_isolation` policy and the
`tenancy_fill` trigger, and the runtime role cannot bypass RLS. A failure exits non-zero, the Job fails, and
the AnalystOSRlsCoverageFailed alert pages.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

RUNTIME_ROLE = "analystos_app"


def rls_check(conn: Any = None) -> dict[str, Any]:
    from analystos.db import models  # noqa: F401 - registers every table on the metadata
    from analystos.db.base import Base, get_engine
    from analystos.tenancy.ddl import rls_status
    from analystos.tenancy.schema import tenant_sources

    tables = sorted(tenant_sources(Base.metadata))
    owned = conn is None
    conn = conn or get_engine().connect()
    try:
        if conn.dialect.name != "postgresql":
            return {"ok": False, "reason": f"rls-check needs Postgres, not {conn.dialect.name}", "tables": len(tables)}
        status = rls_status(conn, tables)
        bypass = conn.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :r"), {"r": RUNTIME_ROLE}).scalar()
    finally:
        if owned:
            conn.close()
    missing = sorted(set(tables) - set(status))
    broken = {t: [k for k, v in s.items() if not v] for t, s in status.items() if not all(s.values())}
    problems = []
    if missing:
        problems.append(f"{len(missing)} tenant-owned table(s) missing from the database")
    if broken:
        problems.append(f"{len(broken)} table(s) without RLS, policy or fill trigger")
    if bypass is None:
        problems.append(f"runtime role {RUNTIME_ROLE} does not exist")
    elif bypass:
        problems.append(f"runtime role {RUNTIME_ROLE} has BYPASSRLS")
    return {"ok": not problems, "tables": len(tables), "checked": len(status), "missing": missing, "broken": broken,
            "runtime_role": RUNTIME_ROLE, "runtime_role_bypassrls": bypass, "problems": problems}
