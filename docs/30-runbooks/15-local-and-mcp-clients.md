# Runbook 15 — One machine, nothing installed, and any MCP application on top

For an office laptop without Docker, a database server or a vector store, and for connecting GitHub Copilot, Claude,
Cursor or an in-house application to AnalystOS over MCP. Tested on Windows 11 (2026-11-06): a fresh data folder
reached a running platform in 90 seconds; a restart after a hard kill recovered in about 50 seconds.

## 1. Run it: `analystos local`

```powershell
py -3.12 -m venv .venv                       # 3.11 works too
.venv\Scripts\pip install -e ".[local]"      # core + MCP + the bundled Postgres (pgserver wheel)
.venv\Scripts\analystos local                # add --demo for the ServiceNow demo workspaces
```

It prints the address, the sign-in and the MCP endpoint. Everything is on one port: the web app, the API at `/api`,
and MCP at `/mcp`.

| Concern | What `analystos local` does |
|---|---|
| Database | Postgres 16 bundled in the `pgserver` wheel, in `%LOCALAPPDATA%\analystos\local\pg` (`--data-dir` to move it). It listens on 127.0.0.1 only and stops with the command. No installer, no admin rights, no Docker. Every role, the superuser included, gets a generated password (`pg_passwords.json`, owner-only), and `pg_hba.conf` is switched from `trust` to scram-sha-256, so another local user cannot connect. |
| Vector store | None to run: embeddings are a pgvector column in the same database (pgvector ships in the wheel). |
| Roles and databases | `deploy/postgres/01-init.sql` (minus Superset), created idempotently, then `migrate` (also provisions the per-workspace analytics roles) and, on a fresh database, `seed`. |
| Profile | `lite`: the local orchestrator and scheduler in the API process; no Redis, Temporal or Superset (publications go to the in-platform preview). |
| Web app | Served from `web/dist` by the API. The first run builds it when Node is on the PATH (`npm ci`, `npm run build`). Without Node, it serves the API only (`--no-web`), or copy a `web/dist` built elsewhere. |
| Secrets | A per-install JWT secret in the data folder (owner-only). `local-settings.json` there lets commands run from a second terminal reach the running instance (`--local`). Source credentials are `secret_ref`s, as everywhere (`env:ANALYSTOS_SOURCE_SECRET_…`). |
| Your settings | Exported `ANALYSTOS_*` values win (a model key, a proxy, `ANALYSTOS_VECTOR_BACKEND=array`). The database URLs always point at the bundled server. |

**Why not SQLite.** Tenant isolation is Postgres row-level security (ADR-009, strict mode fails closed). The gateway
reads staged data through per-workspace Postgres roles (`SET LOCAL ROLE`). Run leases, spend caps and full-text search
also use Postgres features. A SQLite control plane would have to switch those off. The bundled server keeps a laptop
run exactly as governed as a server install, at the cost of a 13 MB wheel.

**Your own data.** Add the office database as a source (Data sources → Add), with its credential as a `secret_ref`.
Queries run against it through the gateway, read-only, under each user's scope.

## 2. Connect an application: `analystos mcp-client create`

Any MCP client that speaks streamable HTTP works. It needs the endpoint and a bearer token.

```powershell
.venv\Scripts\analystos mcp-client create --local --workspace "Sales analytics" --name copilot --vscode .vscode\mcp.json
```

`--local` points the command at the running `analystos local` through its data folder (`--local D:\data` for another
`--data-dir`). Against a server install, set `ANALYSTOS_DATABASE_URL` as for any other operator command.

* It creates the client and its grant, as the admin screens would, with an audit record. It prints the token once.
* `--vscode` writes or updates `.vscode/mcp.json`. The token is **not** written to the file: VS Code asks for it
  once and keeps it in its secret storage. Then open Copilot Chat in **Agent** mode and start the `analystos` server.
* `--json client.json` writes the common `mcpServers` JSON (Claude, Cursor and many others), with the token included,
  so keep that file private. Any other client needs only the endpoint `http://127.0.0.1:8000/mcp` and the header
  `Authorization: Bearer <token>`.
* `--domain sales` or `--asset sales.orders` limit the client to part of the workspace.
  * The context tools and the assistant tools enforce the limit.
  * SQL that reads another table gets one generic refusal, before any validation that could describe that table.
  * `ask` and `validate_sql` are left out of a limited grant.
* `--tools` changes the tool set. The default is the set below.

Check a connection exactly as an application would:

```powershell
$env:ANALYSTOS_MCP_TOKEN = "<token>"
.venv\Scripts\analystos mcp-client check --url http://127.0.0.1:8000/mcp
```

It connects, lists the tools, reads the catalog, counts a table through `sql.run`, checks that a `DELETE` is
refused, and runs a stored query with defaults if there is one.

## 3. What an application can do

The application brings its own model: Copilot, Claude, or the application's own LLM writes the SQL. AnalystOS
supplies the metadata and runs what is allowed: it validates, applies the scope, masks restricted columns, caps rows
and time, and audits every call (CLAUDE.md rule 3: models propose, code decides).

| Tool | Use it to | Role |
|---|---|---|
| `catalog.tables` | list the tables and columns the client may query, per source, with the **SQL dialect** for each | viewer |
| `context.lookup` | get one block of context for up to 10 tables: keys, joins, glossary terms, metrics, quality, profile, people's guidelines | viewer |
| `context.search`, `context.get`, `context.compile` | search glossary, metrics, verified queries and findings; build a context bundle within a token budget | viewer |
| `queries.list` | list **stored, verified queries** with typed parameters; with `question`, get the best match and the values found in it. Enum values and defaults are data values, so they are shown only to a grant holding `context.lookup.values`. A query that reads a column the client may not see is not listed | viewer |
| `queries.run` | run a stored query by id with parameter values. Values are bound as typed literals; a missing value comes back as `needs_input`, never guessed | analyst |
| `validate_sql` | check SQL against the gateway validator without running it | analyst |
| `sql.run` | run the application's own read-only SQL (SELECT/WITH) through the gateway: at most 200 rows back, writes refused | analyst |
| `ask` | let AnalystOS answer a question itself (verified queries first, then its own model if one is configured) | analyst |

Suggested order for an application:
1. `queries.list` with the user's question. If a stored query matches, run it with `queries.run`; stored queries are
   the verified answers.
2. If none matches, call `catalog.tables`, then `context.lookup` for the candidate tables (joins, meanings, filters
   people use).
3. Write the SQL in the dialect `catalog.tables` gives, and run it with `sql.run`. A refusal comes back as
   `{executed: false, code, reason}`: fix the query and retry.

Example prompt in Copilot Chat (Agent mode): *"Using the analystos tools, how many priority-1 incidents are open per
state? Use a stored query if there is one."*

## 4. Limits

* The API listens on loopback only. `--host 0.0.0.0` also needs `--allow-remote`, `ANALYSTOS_ENV` other than `dev`
  (so no demo accounts) and a changed `ANALYSTOS_BOOTSTRAP_ADMIN_PASSWORD`.
* One API process drives local runs (runbook 04 §1). For several users on a shared machine, use the server profiles.
* The Python sandbox is unavailable on Windows. Python notebook steps are refused with the reason; SQL, Ask,
  investigations, schedules and monitors are unaffected.
* `sql.run` returns at most 200 rows to the application. Larger results stay in the platform (the result is marked
  `large_result`); use a stored query or a published dataset for exports.
