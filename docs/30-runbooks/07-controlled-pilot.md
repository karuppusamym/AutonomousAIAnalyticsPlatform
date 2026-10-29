# Runbook — preparing a controlled pilot (P4-09)

What a controlled pilot needs besides the product itself, how to see what is still missing, and how to rehearse
single sign-on without the company identity provider. Status and evidence live in the tracker and the register.

## See what is missing

`GET /api/workspaces/{id}/pilot-readiness` (Settings → Members & policy → **Pilot readiness** in the UI; any member
may read it) lists, check by check and where the gap is:

| Check | Passes when | Otherwise |
|---|---|---|
| `owner.business`, `owner.technical` (workspace) | a named person with an email address is recorded | `missing` |
| `owner.business`, `owner.technical` (each source) | the same, per source | `missing` |
| `connector.certified` (each source) | `docs/60-delivery/evidence/connector-<kind>-<date>.md` exists (written only by `scripts/certify_connectors.py` after the kind's real-engine tests pass) | `missing`: catalog and unit tests only |
| `connector.health` (each source) | the last discovery or crawl succeeded | `fail` with the recorded cause (`source.last_error`), or `missing` if never discovered |
| `identity.sso` | OIDC is configured and a group maps to a role in this workspace | `missing` |
| `recovery.drill` | the newest `docs/60-delivery/evidence/<date>-recovery-drill.md` passed | `missing` / `fail` |

The verdict is `ready` only when nothing is `missing` or `fail`; checks are never averaged. Administrators get every
active workspace's verdict and gaps from `GET /api/admin/pilot-readiness`.

## Name the owners

A workspace owner records the business owner (accepts the decisions the outputs support) and the technical owner
(keeps the source and its access working) — named people, who need not have an AnalystOS account:

```bash
curl -X PUT $API/api/workspaces/$WS/owners -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"business": {"name": "Priya Service Owner", "email": "priya@corp.example"},
       "technical": {"name": "Tom Platform", "email": "tom@corp.example"}}'
curl -X PUT $API/api/workspaces/$WS/sources/$SRC/owners ...   # same body, per source
```

A PUT replaces both roles (an omitted role is cleared). Every change is audited (`workspace.owners_changed`,
`source.owners_changed`) and emitted as an event.

## Rehearse single sign-on locally

`scripts/fake_idp.py` is a real OpenID Connect provider on loopback (discovery, JWKS, authorization with PKCE,
token endpoint, RS256 ID tokens) that signs in whichever configured user you pick. For rehearsals and tests only:
it authenticates nobody.

```bash
.venv/bin/python scripts/fake_idp.py --port 8099 --users users.yaml   # users.yaml: login -> claims incl. groups
ANALYSTOS_OIDC_ISSUER=http://127.0.0.1:8099 ANALYSTOS_OIDC_CLIENT_ID=analystos-web \
ANALYSTOS_OIDC_REDIRECT_URI=http://localhost:8000/api/auth/oidc/callback \
ANALYSTOS_OIDC_MAPPING_FILE=config/oidc.yaml .venv/bin/uvicorn analystos.api.app:app
```

Map groups to workspace roles in `config/oidc.yaml` (by workspace id or name; the highest role any group grants
wins; a group removed at the IdP revokes, at the next sign-in, exactly the membership it granted). Before the pilot:

* `GET /api/admin/sso` shows the mapping as the platform applies it and lists grants that name a workspace that does
  not exist (they grant nothing);
* `POST /api/admin/sso/preview {"groups": [...]}` shows what a sign-in with those groups would grant, without
  signing anyone in.

`tests/integration/test_oidc_local_idp.py` runs this flow end to end over real HTTP.

## Certify the pilot's connectors

```bash
.venv/bin/python scripts/certify_connectors.py --kinds postgres sqlite duckdb csv
```

Only kinds with real-engine tests in this repository can be certified here (PostgreSQL, SQLite, DuckDB, file
sources; MySQL with Docker). ServiceNow has only the Table-API mock and is never certified by this script;
Snowflake, Databricks and the other warehouses need a live instance and a live test before a pilot relies on them.
SQL Server is certified with the S02-T6 kit against the compose `mssql` profile (`scripts/mssql_cert_seed.py`, then
`analystos certify-connector --kind sqlserver --dsn-ref env:ANALYSTOS_CERT_MSSQL_DSN --schemas cert`); re-run it
against the pilot's own server and login before relying on it.

## Recovery

`docs/30-runbooks/06-backup-and-recovery.md`: back up, restore, and the drill (`scripts/recovery_drill.py`).

## Out of scope here

Sign-on against the company identity provider, live warehouse certification and the pilot owner's nomination of
the named people are pilot steps: this repository can only show where they are missing.
