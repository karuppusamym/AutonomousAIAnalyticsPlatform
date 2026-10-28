# Supply-chain image gate: all five images pass Grype; Superset 4.1.1 → 6.1.0 (2026-10-28)

Gate: `.github/workflows/supply-chain.yml` job `images`. Each image gets a Syft SPDX SBOM, then
`grype --only-fixed --fail-on high` (anchore/scan-action@v6). The same was reproduced locally on Docker 28.5.2:
`anchore/syft:latest docker:<image> -o spdx-json`, then `anchore/grype:v0.97.1 --only-fixed --fail-on high sbom:...`.

## Before (GitHub run 40704649708, commit 3fa70ee): every leg failed

| Image | Findings | Cause |
|---|---|---|
| app, mock, sandbox | `jaraco-context` 5.3.0, `wheel` 0.45.1 | vendored by python:3.11-slim's setuptools 79 |
| web | 48 Alpine packages (openssl, curl, libxml2, musl, zlib…) | `nginx:1.27-alpine` is no longer rebuilt |
| superset | 146, among them `apache-superset` 4.1.1, Debian openssl (CVE-2025-15467, critical), Python deps | upstream image `apache/superset:4.1.1` |

## Changes

* app, mock, sandbox: `pip install --upgrade pip setuptools wheel` in the final image.
* web: `nginx:1.30-alpine` (stable branch) + `apk upgrade --no-cache`.
* superset: `deploy/superset/Dockerfile` builds Superset **6.1.0** (latest stable) on `python:3.11-slim`
  (Python 3.11.18, Debian 13.7), no longer derived from the upstream image:
  * dependencies = upstream's resolved set for 6.1.0 (`deploy/superset/constraints.txt`, read from the image with
    `uv pip freeze`). Raised within Superset's bounds: brotli, certifi, dnspython, mako, pyasn1, pyjwt, pyopenssl,
    urllib3.
  * `deploy/superset/overrides.txt` forces versions **above Superset's own upper bounds** (cryptography 50.0.2,
    Pillow 13.0.0, pyarrow 23.0.1, msgpack 1.2.4): every release inside those bounds has a fixed high/critical CVE.
    This is a dependency set upstream has not tested. The live tests below are the evidence that it works for
    AnalystOS's use.
  * Why not upstream 6.1.0 with `apt-get upgrade`: 73 high/critical findings remain (the four pinned packages and
    the Python 3.10.21 interpreter). 7.0.0rc2 leaves 55 (Pillow<13, Python 3.11.14).
* The code-scanning SARIF upload is `continue-on-error`: a private repository without GitHub Advanced Security
  answers "Resource not accessible by integration". The SARIF is kept as a workflow artifact either way.

## After (local)

| Image | High/critical fixable findings |
|---|---|
| app (`EXTRAS=standard,otel`) | 0 |
| mock | 0 |
| sandbox | 0 |
| web | 0 |
| superset 6.1.0 | 0 |

Smoke checks: the app imports `analystos.api.app`, sklearn and statsmodels; the sandbox imports numpy, pandas, scipy,
statsmodels, sklearn and polars; the mock imports; nginx 1.30 accepts `web/nginx.conf` and serves `/healthz` and
the SPA.

## Superset 6.1.0: live tests

Setup: the new image on `127.0.0.1:8089` on the compose network, with its own throwaway metadata Postgres, booted
through the unchanged `deploy/superset/bootstrap.sh` (db upgrade, secret re-encryption, admin, init, the guest role
with all 7 permissions found). Tests used suffix `ss61` (own control DB, analytics DB and roles), with
`ANALYSTOS_SUPERSET_URL=http://127.0.0.1:8089`.

**24 passed, 0 failed:**
* `test_superset_publish.py` (1): publish, render, replay without duplicates, rollback
* `test_bi_workspace_isolation.py` (7): per-workspace BI logins, SQL Lab isolation, API hiding, role sync
* `test_superset_embedding_live.py` (1): AT-07/AT-08 guest tokens, RLS, refused payload tampering, WHERE injection,
  unnamed chart, legacy explore_json, frame-ancestors / referrer checks. `aos_security.py` works unchanged on 6.1.0.
* `test_dashboards_publishing.py` (9): approvals, guest-token recheck, cross-tenant denial, reconcile, export
* `test_existing_dashboards_live.py` (1): import re-executes charts through the gateway
* `test_crawler_sources.py` (5), including Superset metadata crawl

(The first pass errored 8 tests only because the suffixed control DB did not exist yet when they ran first; they
passed on rerun.)

**Upgrade of an existing 4.1.1 metadata DB:** a `pg_dump` of the running stack's Superset 4.1.1 metadata DB
(37 dashboards, 136 charts, 10 databases), restored into a scratch Postgres. `superset db upgrade` on the new image
ran 18 migrations (48cbb571fa3a → 4b2a8c9d3e1f) and exited 0; dashboards, charts and viz types were unchanged.
The migration is one-way: keep a dump of the Superset metadata DB before the first boot of 6.1.0.
`superset init` then failed only with "Invalid decryption key", because the check deliberately ran with a
different SECRET_KEY; a real upgrade keeps its key in the `superset-home` volume (`bootstrap.sh`).

Not covered: the `e2e_demo.py` scenario against 6.1.0, and the Superset UI in a browser.
