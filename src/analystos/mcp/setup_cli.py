"""`analystos mcp-client create`: connect any MCP application (GitHub Copilot, Claude, Cursor, an in-house app) in one step.

Creates an MCP client and its workspace grant as an administrator would in the admin screens (`mcp/grants.py`, audited),
prints the bearer token once, and can write a client configuration: VS Code (`--vscode`; the token is asked for once
and kept in VS Code's secret storage, never in the file) or the common `mcpServers` JSON (`--json`, token included).
Any other client needs only the endpoint and the `Authorization: Bearer <token>` header (streamable HTTP).

    analystos mcp-client create --workspace <id or name> [--name copilot] [--tools catalog.tables,queries.run,...]
                                [--role analyst] [--url http://127.0.0.1:8000/mcp] [--vscode .vscode/mcp.json]
                                [--json client.json] [--domain D]... [--asset schema.table]...
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

# Metadata for writing SQL, stored queries to run by id, the validator and runner, the context, and Ask.
DEFAULT_TOOLS = ("catalog.tables", "queries.list", "queries.run", "sql.run", "validate_sql", "context.read", "ask")
SERVER_KEY = "analystos"
TOKEN_INPUT = "analystos-mcp-token"


def vscode_config(existing: dict[str, Any] | None, url: str) -> dict[str, Any]:
    """`.vscode/mcp.json` with the AnalystOS server added (other servers and inputs are kept)."""
    cfg = dict(existing or {})
    inputs = [i for i in cfg.get("inputs", []) if isinstance(i, dict) and i.get("id") != TOKEN_INPUT]
    inputs.append({"type": "promptString", "id": TOKEN_INPUT, "password": True,
                   "description": "AnalystOS MCP token (from `analystos mcp-client create`)"})
    servers = dict(cfg.get("servers") or {})
    servers[SERVER_KEY] = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer ${{input:{TOKEN_INPUT}}}"}}
    cfg["inputs"], cfg["servers"] = inputs, servers
    return cfg


def generic_config(url: str, token: str) -> dict[str, Any]:
    """The `mcpServers` shape most MCP clients read (streamable HTTP with a header)."""
    return {"mcpServers": {SERVER_KEY: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}


def _workspace(session: Any, ref: str) -> Any:
    from sqlalchemy import select

    from analystos.core.errors import InvalidInput
    from analystos.db.models import Workspace

    ws = session.get(Workspace, ref)
    if ws is not None:
        return ws
    named = list(session.scalars(select(Workspace).where(Workspace.name == ref)))
    if len(named) != 1:
        raise InvalidInput(f"no single workspace with id or name {ref!r} ({len(named)} match)")
    return named[0]


def create(argv: list[str]) -> int:
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.mcp import grants
    from analystos.tenancy.context import system_scope, tenant_scope

    parser = argparse.ArgumentParser(prog="analystos mcp-client create", description=__doc__.split("\n\n")[0])
    parser.add_argument("--workspace", required=True, help="workspace id or exact name")
    parser.add_argument("--name", default="mcp-app")
    parser.add_argument("--tools", default=",".join(DEFAULT_TOOLS), help="comma-separated (default: %(default)s)")
    parser.add_argument("--role", default="analyst", choices=grants.GRANT_ROLES)
    parser.add_argument("--domain", action="append", default=[], help="limit the grant to a domain (repeatable)")
    parser.add_argument("--asset", action="append", default=[], help="limit the grant to a table schema.table (repeatable)")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp", help="the MCP endpoint the application connects to")
    parser.add_argument("--vscode", type=Path, help="write or update this VS Code mcp.json (the token is not stored)")
    parser.add_argument("--json", type=Path, help="write the generic mcpServers JSON (token included: keep it private)")
    parser.add_argument("--admin", help="administrator who creates it (default: the bootstrap admin)")
    parser.add_argument("--local", nargs="?", const="", metavar="DATA_DIR",
                        help="use the running `analystos local` (optionally its --data-dir)")
    args = parser.parse_args(argv)
    if args.local is not None:
        from analystos.local import use_local_settings

        use_local_settings(Path(args.local) if args.local else None)

    admin_email = args.admin or get_settings().bootstrap_admin_email
    with system_scope(), session_scope() as s:
        ws = _workspace(s, args.workspace)
        ws_id, ws_name, tenant = ws.id, ws.name, str(ws.tenant_id)
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    if (args.domain or args.asset) and args.tools == ",".join(DEFAULT_TOOLS):  # a slice holds only slice-aware tools
        tools = [t for t in tools if t in (*grants.CONTEXT_TOOLS, grants.CONTEXT_READ, *grants.ASSISTANT_TOOLS)]
    with tenant_scope(tenant), session_scope() as s:
        admin = s.scalar(select(User).where(User.email == admin_email, User.is_admin.is_(True)))
        if admin is None:
            raise SystemExit(f"no administrator {admin_email}")
        client, token = grants.create_client(s, admin, args.name)
        grants.set_grant(s, admin, client.id, ws_id, role=args.role, tools=tools,
                         domains=args.domain or None, assets=args.asset or None)
        client_id = client.id
    print(f"MCP client {client_id} ({args.name}) can use {', '.join(tools)} in workspace {ws_name} ({ws_id}).")
    print(f"Endpoint: {args.url}   Header: Authorization: Bearer <token>   (streamable HTTP)")
    print("\nToken (shown once):\n\n  " + token + "\n")
    if args.vscode:
        path = args.vscode
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(vscode_config(existing, args.url), indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {path}: in VS Code open Copilot Chat (Agent mode), start the '{SERVER_KEY}' server, paste the token.")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.json, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only: it holds the token
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(generic_config(args.url, token), indent=2) + "\n")
        print(f"Wrote {args.json} (it contains the token: keep it out of source control).")
    if not args.vscode and not args.json:
        print("VS Code / Copilot: add --vscode .vscode/mcp.json. Other clients: --json <file>, or the endpoint and header.")
    return 0


def _result(res: Any) -> Any:
    """A tool result's JSON (structured content, else the first text block)."""
    if getattr(res, "structured_content", None):
        return res.structured_content
    for block in getattr(res, "content", None) or []:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                return {"text": block.text}
    return {}


async def _check(url: str, token: str, workspace: str | None) -> list[tuple[bool, str]]:
    import httpx
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    ws = {"workspace_id": workspace} if workspace else {}
    out: list[tuple[bool, str]] = []
    async with (httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=httpx.Timeout(120.0),
                                  follow_redirects=False) as http,
                Client(streamable_http_client(url, http_client=http), read_timeout_seconds=120.0, cache=None) as client):
        tools = sorted(t.name for t in (await client.list_tools()).tools)
        out.append((bool(tools), f"connected; {len(tools)} tools: {', '.join(tools)}"))
        if "catalog.tables" in tools:
            cat = _result(await client.call_tool("catalog.tables", {**ws, "include_columns": False}))
            tables = [t["table"] for src in cat.get("sources", []) for t in src.get("tables", [])]
            out.append((bool(tables), f"catalog.tables: {len(tables)} tables in {len(cat.get('sources', []))} sources"))
            if tables and "sql.run" in tools:
                run = _result(await client.call_tool("sql.run", {**ws, "sql": f"SELECT COUNT(*) AS n FROM {tables[0]}"}))
                out.append((bool(run.get("executed")), f"sql.run COUNT(*) on {tables[0]}: "
                            + (f"{run['rows'][0][0]} rows" if run.get("executed") else str(run.get("reason")))))
                refused = await client.call_tool("sql.run", {**ws, "sql": f"DELETE FROM {tables[0]}"})
                body = _result(refused)
                out.append((bool(getattr(refused, "is_error", False)) or body.get("executed") is False,
                            f"sql.run DELETE refused: {(body.get('error') or {}).get('code') or body.get('code')}"))
        if "queries.list" in tools:
            listed = _result(await client.call_tool("queries.list", ws))
            queries = listed.get("queries", [])
            out.append((True, f"queries.list: {len(queries)} stored queries"))
            ready = next((q for q in queries if all(p.get("default") is not None for p in q.get("parameters", []))), None)
            if ready and "queries.run" in tools:
                ran = _result(await client.call_tool("queries.run", {**ws, "id": ready["id"]}))
                out.append((bool(ran.get("executed")), f"queries.run {ready['name']}: {ran.get('rows_returned', 0)} rows"
                            if ran.get("executed") else f"queries.run {ready['name']}: {ran.get('reason') or ran.get('status')}"))
    return out


def check(argv: list[str]) -> int:
    """Act as any MCP application would: connect, read the metadata, run a count, try a write (refused)."""
    import asyncio
    import os

    parser = argparse.ArgumentParser(prog="analystos mcp-client check")
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--token", default=os.environ.get("ANALYSTOS_MCP_TOKEN"), help="or ANALYSTOS_MCP_TOKEN")
    parser.add_argument("--workspace", help="needed only when the client holds several grants")
    args = parser.parse_args(argv)
    if not args.token:
        raise SystemExit("pass --token or set ANALYSTOS_MCP_TOKEN")
    results = asyncio.run(_check(args.url, args.token, args.workspace))
    for ok, line in results:
        print(f"[{'ok' if ok else 'FAIL'}] {line}")
    return 0 if all(ok for ok, _ in results) else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["create"]:
        return create(argv[1:])
    if argv[:1] == ["check"]:
        return check(argv[1:])
    print("usage: analystos mcp-client create --workspace <id or name> [--vscode .vscode/mcp.json | --json FILE]\n"
          "       analystos mcp-client check --url http://127.0.0.1:8000/mcp --token <token>")
    return 2
