"""`analystos local`: the whole platform on one machine with nothing else installed (lite profile).

For laptops and offices without Docker, a Postgres server or a vector store. Postgres 16 with pgvector comes from
the `pgserver` wheel (the `local` extra): a private server in a data folder under the user's profile, listening on
127.0.0.1 only, started and stopped with this command. There is no separate vector store: embeddings are a
pgvector column in that same database. SQLite is not used for the control plane on purpose: tenant isolation
(row-level security, ADR-009) and the per-workspace analytics roles of the gateway are Postgres features, and a
local run keeps exactly the governance of a server install.

The steps are runbook 05 §6 automated: roles and databases as `deploy/postgres/01-init.sql` with generated passwords (the cluster stops trusting local connections), `migrate`
(which provisions the analytics roles), `seed` on a fresh database, then the API with the MCP endpoint and,
when `web/dist` exists, the web app on the same port. `--demo` also starts the ServiceNow mock and runs
`demo-seed` once the API answers.

    analystos local [--data-dir DIR] [--host 127.0.0.1] [--port 8000] [--demo] [--no-web]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

APP_DB = "analystos"
APP_ROLE = "analystos"
SUPERUSER = "postgres"
# deploy/postgres/01-init.sql, minus Superset (no BI service in a local run)
ANALYTICS_ROLES = (("analystos_loader", "LOGIN CREATEROLE"), ("analystos_reader", "LOGIN NOINHERIT"),
                   ("analystos_builder", "LOGIN NOINHERIT"), ("analystos_writer", "LOGIN NOINHERIT"))
ROLES = (SUPERUSER, APP_ROLE, *(name for name, _ in ANALYTICS_ROLES))
MOCK_PORT = 8090
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LOCAL_SETTINGS = "local-settings.json"


def use_local_settings(data_dir: Path | None = None) -> Path:
    """Point this process at a running `analystos local` (its database URLs and secret), for commands such as
    `analystos mcp-client create --local` started from another terminal."""
    path = (data_dir or default_data_dir()) / LOCAL_SETTINGS
    if not path.exists():
        raise SystemExit(f"no local run found ({path} is missing): start `analystos local` first")
    os.environ.update(json.loads(path.read_text(encoding="utf-8")))
    from analystos.core.config import get_settings

    get_settings.cache_clear()
    return path


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "analystos" / "local"


def _private_write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def passwords(data_dir: Path) -> dict[str, str]:
    """Generated per install, kept next to the data (owner-only): no shared development password reaches the server.
    pgserver initialises the cluster with `trust`; `_bootstrap` sets these and switches it to scram-sha-256."""
    path = data_dir / "pg_passwords.json"
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if set(ROLES) - set(known):
        known = {**{r: secrets.token_urlsafe(24) for r in ROLES}, **known}
        _private_write(path, json.dumps(known, indent=2))
    return known


def _connect(host: str, port: int, db: str, pw: dict[str, str], *, timeout: int = 10) -> Any:
    """As the superuser: with its password, or by `trust` on a cluster `_bootstrap` has not hardened yet."""
    import psycopg

    try:
        return psycopg.connect(host=host, port=port, user=SUPERUSER, password=pw[SUPERUSER], dbname=db,
                               autocommit=True, connect_timeout=timeout)
    except psycopg.OperationalError as exc:
        if "password authentication failed" in str(exc):
            raise
        return psycopg.connect(host=host, port=port, user=SUPERUSER, dbname=db, autocommit=True, connect_timeout=timeout)


def _start_postgres(data_dir: Path, pw: dict[str, str]) -> tuple[Any, str, int]:
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SystemExit('analystos local needs the bundled Postgres: pip install -e ".[local]"') from exc
    from sqlalchemy.engine import make_url

    # A previous run that was killed (laptop closed, terminal shut) can leave its server shutting down: pgserver then
    # reattaches to it and the first connection is dropped. After an unclean stop Postgres runs crash recovery first,
    # which can outlast pgserver's 10 s `pg_ctl -w`; the server keeps starting in the background, so a timeout here
    # means "not yet", not "failed". Wait for a server that answers.
    deadline = time.monotonic() + 180
    while True:
        try:
            server = pgserver.get_server(data_dir / "pg", cleanup_mode="stop")  # stops with this process
        except subprocess.TimeoutExpired:
            if time.monotonic() > deadline:
                raise
            print("Postgres is recovering after an unclean stop; waiting...", flush=True)
            time.sleep(5)
            continue
        url = make_url(server.get_uri())
        host, port = url.host or "127.0.0.1", int(url.port or 5432)
        if _answers(host, port, pw):
            return server, host, port
        if time.monotonic() > deadline:
            raise SystemExit(f"the bundled Postgres in {data_dir / 'pg'} does not answer on {host}:{port}; "
                             "stop any other `analystos local` using this folder and retry")
        print("waiting for the previous Postgres in this folder to stop...", flush=True)
        time.sleep(3)


def _answers(host: str, port: int, pw: dict[str, str]) -> bool:
    import psycopg

    for _ in range(5):
        try:
            with _connect(host, port, "postgres", pw, timeout=3) as c:
                c.execute("SELECT 1")
            return True
        except psycopg.OperationalError:
            time.sleep(1)
    return False


def hardened_hba(text: str) -> str:
    """pg_hba.conf with every `trust` rule turned into scram-sha-256 (comments and other rules untouched)."""
    lines = []
    for line in text.splitlines():
        parts = line.split()
        if parts and not line.lstrip().startswith("#") and parts[-1] == "trust":
            line = line[: line.rstrip().rfind("trust")] + "scram-sha-256"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _harden_hba(c: Any) -> None:
    """Apply `hardened_hba` to the server's file, then reload (idempotent)."""
    hba = Path(c.execute("SHOW hba_file").fetchone()[0])
    text = hba.read_text(encoding="utf-8")
    hardened = hardened_hba(text)
    if hardened != text:
        hba.write_text(hardened, encoding="utf-8")
        c.execute("SELECT pg_reload_conf()")


def _bootstrap(host: str, port: int, pw: dict[str, str]) -> None:
    """Roles, databases and grants of 01-init.sql, idempotently, as the bundled server's superuser; then every role
    gets its generated password and the cluster stops trusting local connections."""
    from psycopg import sql

    with _connect(host, port, "postgres", pw) as c:
        roles = {r[0] for r in c.execute("SELECT rolname FROM pg_roles")}
        dbs = {r[0] for r in c.execute("SELECT datname FROM pg_database")}
        if APP_ROLE not in roles:
            c.execute(sql.SQL("CREATE ROLE {} LOGIN SUPERUSER").format(sql.Identifier(APP_ROLE)))
        for name, attrs in ANALYTICS_ROLES:
            if name not in roles:
                c.execute(sql.SQL("CREATE ROLE {} " + attrs).format(sql.Identifier(name)))
        for role in ROLES:
            c.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(pw[role])))
        if APP_DB not in dbs:
            c.execute(f"CREATE DATABASE {APP_DB} OWNER {APP_ROLE}")
        if "analytics" not in dbs:
            c.execute("CREATE DATABASE analytics OWNER analystos_loader")
        c.execute(f"REVOKE CONNECT ON DATABASE {APP_DB} FROM PUBLIC")
        c.execute(f"GRANT CONNECT ON DATABASE {APP_DB} TO {APP_ROLE}")
        c.execute("REVOKE CONNECT ON DATABASE analytics FROM PUBLIC")
        c.execute("GRANT CONNECT ON DATABASE analytics TO analystos_loader, analystos_reader, analystos_builder, analystos_writer")
        c.execute("ALTER ROLE analystos_reader SET default_transaction_read_only = on")
        c.execute("ALTER ROLE analystos_reader SET statement_timeout = '60s'")
        _harden_hba(c)
    with _connect(host, port, APP_DB, pw) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    with _connect(host, port, "analytics", pw) as c:
        c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        c.execute("REVOKE ALL ON SCHEMA public FROM analystos_reader")


def _secret(data_dir: Path) -> str:
    """A per-install JWT secret kept next to the data (never the development default on a machine people use)."""
    path = data_dir / "jwt_secret"
    if not path.exists():
        _private_write(path, secrets.token_urlsafe(48))
    return path.read_text(encoding="utf-8").strip()


def environment(data_dir: Path, host: str, port: int, *, api_host: str, api_port: int, web_dist: Path | None,
                demo: bool) -> dict[str, str]:
    """The settings of a local run. Values the user already exported win (an office proxy, a model key); the
    database URLs always point at the bundled server with its generated passwords."""
    from urllib.parse import quote

    pw = passwords(data_dir)
    pg = f"{host}:{port}"

    def url(role: str, db: str) -> str:
        return f"postgresql+psycopg://{role}:{quote(pw[role], safe='')}@{pg}/{db}"

    env = {
        "ANALYSTOS_PROFILE": "lite",
        "ANALYSTOS_ORCHESTRATOR": "local",
        "ANALYSTOS_REDIS_URL": "",
        "ANALYSTOS_VECTOR_BACKEND": "pgvector",
        "ANALYSTOS_DATABASE_URL": url(APP_ROLE, APP_DB),
        "ANALYSTOS_ANALYTICS_LOADER_URL": url("analystos_loader", "analytics"),
        "ANALYSTOS_ANALYTICS_READER_URL": url("analystos_reader", "analytics"),
        "ANALYSTOS_ANALYTICS_BUILDER_URL": url("analystos_builder", "analytics"),
        "ANALYSTOS_ANALYTICS_WRITER_URL": url("analystos_writer", "analytics"),
        "ANALYSTOS_JWT_SECRET": _secret(data_dir),
        "ANALYSTOS_WEB_URL": f"http://{api_host}:{api_port}",
        "ANALYSTOS_WORKER_ARTIFACT_URL": f"http://{api_host}:{api_port}",
    }
    if web_dist is not None:
        env["ANALYSTOS_WEB_DIST"] = str(web_dist)
    if demo:
        env["ANALYSTOS_SERVICENOW_MOCK_URL"] = f"http://127.0.0.1:{MOCK_PORT}"
        env.setdefault("ANALYSTOS_SOURCE_SECRET_SERVICENOW", "admin")  # the mock's development credential
    forced = {k for k in env if k.endswith("_URL") and ("DATABASE" in k or "ANALYTICS" in k)}  # the bundled server
    return {k: v if k in forced else os.environ.get(k, v) for k, v in env.items()}


def check_bind(host: str, allow_remote: bool, env: str, admin_password: str, default_password: str) -> None:
    """Loopback only by default. Another address needs --allow-remote and no development defaults: the dev seed
    creates accounts with a known password."""
    if host in LOOPBACK:
        return
    if not allow_remote:
        raise SystemExit(f"refusing to listen on {host}: a local run is for this machine (pass --allow-remote to share it)")
    if env == "dev" or admin_password == default_password:
        raise SystemExit("--allow-remote needs ANALYSTOS_ENV other than dev (no demo accounts) and a changed "
                         "ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD")


def _web_dist(root: Path, build: bool) -> Path | None:
    dist = root / "web" / "dist"
    if (dist / "index.html").exists():
        return dist
    npm = shutil.which("npm")
    if not build or npm is None or not (root / "web" / "package.json").exists():
        return None
    print("building the web app once (npm ci, npm run build)...", flush=True)
    web = root / "web"
    install = [npm, "ci", "--no-audit", "--no-fund"] + (["--os=win32"] if sys.platform == "win32" else [])
    if not (web / "node_modules").exists() and subprocess.call(install, cwd=web) != 0:
        return None
    return dist if subprocess.call([npm, "run", "build"], cwd=web) == 0 and (dist / "index.html").exists() else None


def _wait_for(url: str, seconds: float = 120.0) -> bool:
    import httpx

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    return False


def _demo(api: str) -> None:
    """The ServiceNow mock in a thread, then `demo-seed` through the API once it answers (idempotent)."""
    import uvicorn

    threading.Thread(target=uvicorn.run, args=("analystos.connectors.servicenow_mock:app",),
                     kwargs={"host": "127.0.0.1", "port": MOCK_PORT, "log_level": "warning"}, daemon=True).start()

    def seed() -> None:
        if not _wait_for(f"{api}/api/health"):
            print("demo-seed skipped: the API did not answer in time", flush=True)
            return
        from analystos.demo.seed import main as demo_seed

        demo_seed(["--api", api, "--servicenow", f"http://127.0.0.1:{MOCK_PORT}", "--web", api])

    threading.Thread(target=seed, daemon=True).start()


def _fresh(database_url: str) -> bool:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url)
    try:
        with engine.connect() as c:
            return not c.execute(text("SELECT to_regclass('public.app_user') IS NOT NULL")).scalar()
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="analystos local", description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=default_data_dir(), help="where the database lives (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1", help="address the API binds (default: loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--allow-remote", action="store_true", help="allow a non-loopback --host (no dev defaults)")
    parser.add_argument("--demo", action="store_true", help="also start the ServiceNow mock and seed the demo workspaces")
    parser.add_argument("--no-web", action="store_true", help="serve the API and MCP only")
    parser.add_argument("--no-build", action="store_true", help="do not build web/dist when it is missing")
    parser.add_argument("--setup-only", action="store_true", help="prepare the database, then exit (prints the settings)")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"starting the bundled Postgres in {data_dir / 'pg'} ...", flush=True)
    pw = passwords(data_dir)
    server, host, port = _start_postgres(data_dir, pw)
    _bootstrap(host, port, pw)
    from analystos.core.config import DATA_ROOT, get_settings

    web_dist = None if args.no_web else _web_dist(DATA_ROOT, build=not args.no_build)
    os.environ.update(environment(data_dir, host, port, api_host=args.host, api_port=args.port, web_dist=web_dist,
                                  demo=args.demo))
    get_settings.cache_clear()
    st = get_settings()
    check_bind(args.host, args.allow_remote, st.env, st.bootstrap_admin_password,
               type(st).model_fields["bootstrap_admin_password"].default)
    from analystos import cli
    from analystos.tenancy import system_scope

    # for `analystos mcp-client --local` and other commands from a second terminal (owner-only: it holds passwords)
    _private_write(data_dir / LOCAL_SETTINGS, json.dumps({k: v for k, v in os.environ.items()
                                                         if k in ("ANALYSTOS_DATABASE_URL", "ANALYSTOS_JWT_SECRET",
                                                                  "ANALYSTOS_PROFILE", "ANALYSTOS_VECTOR_BACKEND")
                                                         or k.startswith("ANALYSTOS_ANALYTICS_")}, indent=2))
    fresh = _fresh(os.environ["ANALYSTOS_DATABASE_URL"])
    with system_scope():
        cli.migrate()
        if fresh:
            cli.seed()
    api = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    if args.setup_only:
        print(f"ready. Postgres on {host}:{port}; set ANALYSTOS_DATABASE_URL={os.environ['ANALYSTOS_DATABASE_URL']}")
        server.cleanup()
        return 0
    settings = get_settings()
    print("\n".join([
        "",
        f"  AnalystOS (local) on {api}" + ("" if web_dist else "  (API only: build the web app with `npm run build` in web/)"),
        f"  sign in as {settings.bootstrap_admin_email}" + (" / analyst@analystos.local, ChangeMe123!" if settings.env == "dev" else ""),
        f"  MCP endpoint  {api}/mcp   (connect any MCP app: analystos mcp-client create --workspace <id or name>)",
        f"  data          {data_dir}   (Postgres {host}:{port}, pgvector; no other service)",
        "",
    ]), flush=True)
    if args.demo:
        _demo(api)
    import uvicorn

    try:
        uvicorn.run("analystos.api.app:app", host=args.host, port=args.port)
    finally:
        server.cleanup()
    return 0
