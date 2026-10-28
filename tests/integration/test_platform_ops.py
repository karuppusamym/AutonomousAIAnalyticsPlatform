"""S12 on Postgres: migration 0072 (upgrade from empty, downgrade one step, re-upgrade), the `cost_ledger` view under
row-level security (a tenant sees only its own lines; the priced service agrees with the view), the restricted
role can read the model_matrix_result matrix, and `analystos ops rls-check` passes on a migrated database."""
from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def _migration():
    spec = importlib.util.spec_from_file_location("m0072", ROOT / "migrations" / "versions" / "0072_platform_ops.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def migrated_db(control_db):
    """A separate database migrated with Alembic from empty (the session database is create_all)."""
    from alembic import command
    from alembic.config import Config


    base_url = control_db.rsplit("/", 1)[0]
    name = control_db.rsplit("/", 1)[1] + "_mig66"
    admin = create_engine(f"{base_url}/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = f"{base_url}/{name}"
    from analystos.db import vectors

    engine = create_engine(url)
    with engine.begin() as c:
        vectors.ensure_extension(c)
    engine.dispose()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    try:
        command.upgrade(cfg, "head")
        yield url, cfg
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_migration_0072_applies_reverts_and_passes_the_rls_check(migrated_db):
    from alembic import command

    from analystos.ops.checks import rls_check

    url, cfg = migrated_db
    engine = create_engine(url)
    try:
        with engine.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0072_platform_ops"
            cols = {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns "
                                                 "WHERE table_name = 'query_execution'"))}
            assert {"scan_bytes", "credits"} <= cols
            assert c.execute(text("SELECT reloptions FROM pg_class WHERE relname = 'cost_ledger'")).scalar() == \
                ["security_invoker=true"]
            assert c.execute(text("SELECT count(*) FROM model_matrix_result")).scalar() == 0
            result = rls_check(c)
        assert result["ok"], result["problems"]
        command.downgrade(cfg, "-1")
        with engine.connect() as c:
            assert c.execute(text("SELECT to_regclass('cost_ledger')")).scalar() is None
            assert c.execute(text("SELECT to_regclass('model_matrix_result')")).scalar() is None
        command.upgrade(cfg, "head")
        with engine.connect() as c:
            assert c.execute(text("SELECT to_regclass('cost_ledger')")).scalar() == "cost_ledger"
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def ledger_rows(control_db):
    """Two tenants' spend in the session database, plus the view (create_all does not run migrations)."""
    from sqlalchemy import select

    from analystos.core.ids import new_id
    from analystos.db.base import get_engine, session_scope
    from analystos.db.models import ModelCall, QueryExecution, Tenant, User, Workspace
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    with get_engine().begin() as c:
        c.execute(text("ALTER TABLE query_execution ADD COLUMN IF NOT EXISTS scan_bytes bigint"))
        c.execute(text("ALTER TABLE query_execution ADD COLUMN IF NOT EXISTS credits double precision"))
        c.execute(text(_migration().COST_LEDGER_VIEW))
        from analystos.core.config import get_settings

        c.execute(text(f'GRANT SELECT ON cost_ledger TO "{get_settings().tenancy_app_role}"'))
    tb = new_id("tnt")
    at = datetime.now(UTC) - timedelta(hours=1)
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tb, slug=f"s12-{tb[-6:]}", name="S12 tenant B"))
    with tenant_scope(tb), session_scope() as s:
        u = User(id=new_id("usr"), email=f"b-{tb}@b.test", name="B", password_hash="-", is_admin=False)
        s.add(u)
        s.flush()
        ws_b = Workspace(id=new_id("ws"), name="S12 B", created_by=u.id)
        s.add(ws_b)
        s.flush()
        s.add(ModelCall(workspace_id=ws_b.id, purpose="sql_generation", profile="coding", provider="openrouter",
                        model="qwen/qwen3-32b", status="ok", input_tokens=10, output_tokens=5, cost_usd=0.7, created_at=at))
        ws_b_id = ws_b.id
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws_a = Workspace(id=new_id("ws"), name="S12 A", created_by=admin.id)
        s.add(ws_a)
        s.flush()
        s.add(ModelCall(workspace_id=ws_a.id, purpose="sql_generation", profile="coding", provider="openrouter",
                        model="qwen/qwen3-32b", status="ok", input_tokens=10, output_tokens=5, cost_usd=0.3, created_at=at))
        s.add(QueryExecution(id=new_id("qry"), workspace_id=ws_a.id, actor=f"user:{admin.id}", sql="select 1",
                             status="ok", scan_bytes=10**12, created_at=at))
        ws_a_id = ws_a.id
    return {"tenant_b": tb, "ws_a": ws_a_id, "ws_b": ws_b_id}


def test_cost_ledger_view_and_spend_are_tenant_bound(ledger_rows):
    from analystos.db.base import session_scope
    from analystos.ops import cost_ledger
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    def view_ws(tenant: str | None) -> set[str]:
        scope = tenant_scope(tenant) if tenant else system_scope()
        with scope, session_scope() as s:
            return {r[0] for r in s.execute(text("SELECT DISTINCT workspace_id FROM cost_ledger WHERE workspace_id IN (:a, :b)"),
                                            {"a": ledger_rows["ws_a"], "b": ledger_rows["ws_b"]})}

    assert view_ws(DEFAULT_TENANT_ID) == {ledger_rows["ws_a"]}
    assert view_ws(ledger_rows["tenant_b"]) == {ledger_rows["ws_b"]}
    assert view_ws(None) == {ledger_rows["ws_a"], ledger_rows["ws_b"]}

    with tenant_scope(ledger_rows["tenant_b"]), session_scope() as s:
        out = cost_ledger.spend(s, group_by="workspace,category", days=2)
        view_model = s.execute(text("SELECT coalesce(sum(cost_usd), 0) FROM cost_ledger WHERE category = 'model'")).scalar()
    assert {g["workspace"] for g in out["groups"]} == {ledger_rows["ws_b"]}
    assert out["totals"]["by_category"]["model"]["cost_usd"] == pytest.approx(float(view_model))
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        a = cost_ledger.spend(s, group_by="category", days=2, workspace_ids=[ledger_rows["ws_a"]])
    by = {g["category"]: g["cost_usd"] for g in a["groups"]}
    assert by["model"] == pytest.approx(0.3) and by["query"] == pytest.approx(cost_ledger.load_rates().scan_usd_per_tb)


def test_the_restricted_role_reads_the_evaluation_matrix(ledger_rows):
    from analystos.db.base import session_scope
    from analystos.ops import model_matrix
    from analystos.tenancy import DEFAULT_TENANT_ID, system_scope, tenant_scope

    with system_scope(), session_scope() as s:
        model_matrix.record(s, [{"profile": "coding", "model": "qwen/qwen3-32b", "provider": "openrouter", "status": "passed",
                                 "metrics": {"execution_correctness": 1.0}}], suite="ask", mode="fake", gates_version=6,
                            gates_sha256=None)
    with tenant_scope(DEFAULT_TENANT_ID), session_scope() as s:
        view = model_matrix.matrix(s, profile="coding", model="qwen/qwen3-32b")
    assert view["evaluations"] and view["evaluations"][0]["eligible"]
