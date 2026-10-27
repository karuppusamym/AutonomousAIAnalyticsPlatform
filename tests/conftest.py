"""Shared test setup. Control-plane tests run against a throwaway Postgres database
(`analystos_test`) on the compose Postgres; they skip cleanly when it is unreachable.

The analytics plane is isolated too: integration tests get their own analytics database and their
own loader/reader logins (``aostest<suffix>_loader`` / ``_reader``, the loader with CREATEROLE so it
can create the per-workspace reader roles ``aostest<suffix>_r_<ws>``). Everything is created by the
``analytics_plane`` fixture and dropped afterwards, so parallel sessions (``ANALYSTOS_TEST_DP_DB``
suffix) never share roles or staged schemas, and the shared ``analytics`` database is never touched."""
from __future__ import annotations

import os
from pathlib import Path

# Hermetic: a developer's `.env` (settings, provider keys) never reaches the suite. Tests that need a setting
# set it here or with monkeypatch; no test may make a billable model call.
from analystos.core.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None

TEST_DB = os.environ.get("ANALYSTOS_TEST_DATABASE_URL", "postgresql+psycopg://analystos:analystos@localhost:5432/analystos_test")
os.environ["ANALYSTOS_DATABASE_URL"] = TEST_DB
os.environ.setdefault("ANALYSTOS_ORCHESTRATOR", "local")
os.environ.setdefault("ANALYSTOS_JWT_SECRET", "test-secret-test-secret-test-secret-123")

_DP_DB = os.environ.get("ANALYSTOS_TEST_DP_DB", "analystos_test_dp")
TEST_SUFFIX = _DP_DB.removeprefix("analystos_test_dp").strip("_")
# Every session gets its own role namespace. A bare "aostest_" would make this session's cleanup
# (LIKE 'aostest\_%') drop the roles of every parallel session using a suffix.
ROLE_PREFIX = f"aostest_{TEST_SUFFIX or 'default'}_"
# Budget counters (P4-T07) of test runs never share keys with a dev stack on the same Redis.
os.environ.setdefault("ANALYSTOS_BUDGET_COUNTER_PREFIX", f"aostest:{TEST_SUFFIX or 'default'}:budget:")
# The MCP test doubles listen on 127.0.0.1; outbound calls to non-public addresses need the operator's
# explicit listing (P7-11), which a test session gives for loopback only.
os.environ.setdefault("ANALYSTOS_OUTBOUND_PRIVATE_HOSTS", "127.0.0.1")
# ...and an MCP server's host must be on the platform MCP host allowlist (P4-X05).
os.environ.setdefault("ANALYSTOS_MCP_HOST_ALLOWLIST", "127.0.0.1")
ANALYTICS_DB = f"{_DP_DB}_analytics"
_HOSTPORT = TEST_DB.split("@", 1)[1].rsplit("/", 1)[0]
os.environ["ANALYSTOS_ANALYTICS_LOADER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}loader:loader@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_READER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}reader:reader@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_WORKSPACE_ROLE_PREFIX"] = f"{ROLE_PREFIX}r_"
os.environ["ANALYSTOS_ANALYTICS_BI_ROLE_PREFIX"] = f"{ROLE_PREFIX}bi_"  # per-workspace Superset logins (P4-02)
# The BuildGateway's write login and per-workspace build roles (P4-E06), in the same namespace.
os.environ["ANALYSTOS_ANALYTICS_BUILDER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}builder:builder@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_BUILD_ROLE_PREFIX"] = f"{ROLE_PREFIX}b_"
# The managed output writer's login and per-workspace writer roles (P6-03), in the same namespace.
os.environ["ANALYSTOS_ANALYTICS_WRITER_URL"] = f"postgresql+psycopg://{ROLE_PREFIX}writer:writer@{_HOSTPORT}/{ANALYTICS_DB}"
os.environ["ANALYSTOS_ANALYTICS_WRITER_ROLE_PREFIX"] = f"{ROLE_PREFIX}w_"
os.environ.setdefault("ANALYSTOS_BUILD_DIR", os.path.join(__import__("tempfile").gettempdir(), f"{ROLE_PREFIX}builds"))
# Superset reaches the same cluster from inside the compose network.
os.environ["ANALYSTOS_SUPERSET_ANALYTICS_SQLALCHEMY_URI"] = f"postgresql+psycopg2://{ROLE_PREFIX}reader:reader@postgres:5432/{ANALYTICS_DB}"

import pytest  # noqa: E402

PINNED_MODELS = Path(__file__).parent / "fixtures" / "models" / "pinned-2026-09-25.yaml"


@pytest.fixture
def pinned_models_config(monkeypatch):
    """Router, escalation and cost tests assert behaviour of a known model table (tiers, prices, cache
    flags), not whichever models an operator has configured today; config/models.yaml itself is checked
    by `test_models_yaml.py`."""
    from analystos.core.config import get_settings
    from analystos.llm.config import load_models_config

    monkeypatch.setattr(get_settings(), "models_config", PINNED_MODELS)
    load_models_config.cache_clear()
    yield
    load_models_config.cache_clear()


def _admin_url() -> str:
    return TEST_DB.rsplit("/", 1)[0] + "/postgres"


def _drop_analytics_plane(conn) -> None:  # noqa: ANN001
    from sqlalchemy import text

    conn.execute(text(f'DROP DATABASE IF EXISTS "{ANALYTICS_DB}" WITH (FORCE)'))
    like = ROLE_PREFIX.replace("_", "\\_") + "%"
    roles = [r[0] for r in conn.execute(text("SELECT rolname FROM pg_roles WHERE rolname LIKE :p"), {"p": like})]
    # Workspace roles first (their memberships were granted by the loader), then the logins.
    logins = (f"{ROLE_PREFIX}loader", f"{ROLE_PREFIX}reader", f"{ROLE_PREFIX}builder", f"{ROLE_PREFIX}writer")
    for role in sorted(roles, key=lambda r: (r in logins, r)):
        conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))


@pytest.fixture(scope="session")
def analytics_plane():
    """Throwaway analytics DB + test-prefixed loader/reader logins, mirroring deploy/postgres/01-init.sql."""
    from sqlalchemy import create_engine, text

    try:
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
        with admin.connect() as c:
            _drop_analytics_plane(c)
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}loader LOGIN CREATEROLE PASSWORD 'loader'"))
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}reader LOGIN NOINHERIT PASSWORD 'reader'"))
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}builder LOGIN NOINHERIT PASSWORD 'builder'"))
            c.execute(text(f"CREATE ROLE {ROLE_PREFIX}writer LOGIN NOINHERIT PASSWORD 'writer'"))
            c.execute(text(f"ALTER ROLE {ROLE_PREFIX}reader SET default_transaction_read_only = on"))
            c.execute(text(f"ALTER ROLE {ROLE_PREFIX}reader SET statement_timeout = '60s'"))
            c.execute(text(f'CREATE DATABASE "{ANALYTICS_DB}" OWNER {ROLE_PREFIX}loader'))
            c.execute(text(f'REVOKE CONNECT ON DATABASE "{ANALYTICS_DB}" FROM PUBLIC'))
            c.execute(text(f'GRANT CONNECT ON DATABASE "{ANALYTICS_DB}" TO {ROLE_PREFIX}loader, {ROLE_PREFIX}reader, '
                           f'{ROLE_PREFIX}builder, {ROLE_PREFIX}writer'))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres unavailable: {exc}")
    yield {"db": ANALYTICS_DB, "prefix": ROLE_PREFIX, "loader": f"{ROLE_PREFIX}loader", "reader": f"{ROLE_PREFIX}reader",
           "builder": f"{ROLE_PREFIX}builder", "writer": f"{ROLE_PREFIX}writer", "admin_url": _admin_url()}
    from analystos.gateway.engines import dispose_all

    dispose_all()
    with admin.connect() as c:
        _drop_analytics_plane(c)
    admin.dispose()


def _reset_platform_spend_counters() -> None:
    """The platform daily spend counter is seeded from model_call rows; a fresh control database must
    not inherit the counter (and its hard cap) a previous session of this suffix left in Redis."""
    try:
        import redis

        client = redis.Redis.from_url("redis://localhost:6379/0", socket_timeout=0.5, socket_connect_timeout=0.5)
        for key in client.scan_iter(os.environ["ANALYSTOS_BUDGET_COUNTER_PREFIX"] + "platform:*"):
            client.delete(key)
    except Exception:  # no Redis: nothing to reset
        pass


@pytest.fixture(scope="session")
def control_db(analytics_plane):
    """Fresh control-plane schema for the test session."""
    from sqlalchemy import create_engine, text

    try:
        admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            name = TEST_DB.rsplit("/", 1)[1]
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{name}' AND pid <> pg_backend_pid()"))
            c.execute(text(f"DROP DATABASE IF EXISTS {name}"))
            c.execute(text(f"CREATE DATABASE {name}"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Postgres unavailable: {exc}")
    engine = create_engine(TEST_DB)
    from analystos.db import vectors

    with engine.begin() as c:  # ANALYSTOS_VECTOR_BACKEND=array runs the suite on core Postgres
        vectors.ensure_extension(c)
    from analystos.db import models  # noqa: F401
    from analystos.db.base import Base

    Base.metadata.create_all(engine)
    from analystos.core.config import get_settings
    from analystos.tenancy.ddl import install
    from analystos.tenancy.schema import tenant_sources

    with engine.begin() as c:  # tenant isolation as migration 0057 installs it (ADR-009)
        install(c, tenant_sources(Base.metadata), role=get_settings().tenancy_app_role)
    from analystos.cli import seed

    seed()
    _reset_platform_spend_counters()
    yield TEST_DB
    engine.dispose()
