# Superset embedding and RLS live — AT-07 / AT-08 — 2026-10-28

Windows 11 dev host. Postgres 16 (compose `analystos-postgres-1`), Superset 4.1.1 built from `deploy/superset`
(`analystos-superset:4.1.1`) started from this branch's config as a separate container on :8089 with its own
metadata database `superset_embed`, so the shared :8088 Superset was not restarted. Data: the ServiceNow mock staged
by `tests/dataplane_fixtures.py` (20,000 incidents).

## What changed

* `deploy/superset/superset_config.py`: `EMBEDDED_SUPERSET` on; `GUEST_ROLE_NAME = aos_embed_guest`;
  `GUEST_TOKEN_JWT_SECRET` from `SUPERSET_GUEST_TOKEN_SECRET`; token TTL 300 s; `X-Frame-Options: SAMEORIGIN` replaced
  by `Content-Security-Policy: frame-ancestors 'self' $SUPERSET_EMBED_ORIGINS` (default the two loopback UI origins).
* `deploy/superset/guest_role.py` (run by `bootstrap.sh` after `superset init`): the guest role is reset on every
  boot to 7 read-only permissions. It has no database or dataset access, no write on charts or dashboards (Gamma
  has both), no `can_explore_json`, no `can_dashboard`. A guest reads data only through the dashboard its token
  names.
* `deploy/superset/aos_security.py` (`CUSTOM_SECURITY_MANAGER`): a guest's chart-data request must name a saved
  chart on the dashboard it names, ask only for that chart's columns and metrics, keep its WHERE/HAVING SQL, and
  is capped at its row limit. Superset 4.1.1's own guard does not work for guests (below).
* `publishing/superset_access.py::ensure_embedded` keeps the embed config's allowed domains equal to the platform's
  (`ANALYSTOS_SUPERSET_EMBED_DOMAINS`, else the origin of `ANALYSTOS_WEB_URL`), updated in place (same uuid).
* Web: `components/EmbeddedDashboard.tsx` (`@superset-ui/embedded-sdk` 0.4.0, loaded lazily) on a published
  Superset dashboard in the builder. Every token, including SDK refreshes, comes from
  `POST /api/v2/dashboards/{id}/embed-token`; the first load reuses the token already minted.

## Governance review and what it changed (same day)

The governance-reviewer read the diff. Fixed before commit:

1. **High:** if a published asset left the viewer's scope (deselected, its source no longer ready, a name differing
   in case), `viewer_rls` emitted no clause. A guest holds no workspace role, so the static rules never reach it,
   and it would have seen unfiltered rows. `viewer_rls` now refuses unless every source asset of every published
   dataset is in the viewer's current scope (case-insensitive), and refuses a dataset that names no source asset.
   `test_viewer_rls_refuses_an_asset_that_left_the_viewers_scope` covers it, and so does
   `test_guest_token_rechecks_auth` through the real endpoint: the asset is deselected after publish, 403 "no longer
   in your data scope", then reselected and 200. The test world now has a ready source with the asset.
2. **Medium:** a chart an editor added to the dashboard in Superset read a dataset the publication does not name,
   with no guest clause. `lock_unpublished_datasets` adds `1 = 0` to the token for every such dataset
   (`test_datasets_added_in_superset_are_locked_for_guests`).
3. **Medium:** `can_explore_json` (its legacy endpoint skips the guest payload check) and three unneeded
   permissions were removed from the guest role.
4. **Medium-low:** an empty allowed-domains list means "any site" in Superset. `embed_domains` now refuses an empty,
   wildcard or scheme-only origin, and `ensure_embedded` never writes or overwrites with an empty list. Both
   settings take commas or spaces; Superset's `frame-ancestors` keeps only `http(s)://host` entries. Checked live:
   `http://localhost:5173,http://localhost:5174, *, https:` gave `frame-ancestors 'self' http://localhost:5173
   http://localhost:5174`.

Found while rerunning live: **Superset 4.1.1 skips its guest payload check.** It loads the chart through the
permission-filtered chart DAO. A guest has no datasource permission, so the DAO finds no chart, and Superset treats
the request as a native-filter request. A guest token pulled 19,996 raw rows (priority, state, opened_at) from a
KPI chart and ran an arbitrary SQL metric, still under the token's RLS. `aos_security.py` closes this. It applies
Superset's rule itself and treats an SQL expression that is only a column name as that column, because the frontend
sends a bar chart's axis as `{"expressionType": "SQL", "sqlExpression": "state", "columnType": "BASE_AXIS"}`.

Probe after the fix:
* The saved payload: 200.
* `row_limit: 100000`: capped to the chart's own limit.
* Refused with 403: raw columns, an extra column, an SQL metric, an SQL-wrapped column not on the chart,
  `(SELECT 1)`, an added WHERE, a request that names no chart.

Not changed (follow-ups):
* The dev placeholder guest-token secret follows the existing `SECRET_KEY` pattern, but anyone who knows it can
  mint tokens with no RLS. Production must set `SUPERSET_GUEST_TOKEN_SECRET`. Refusing the placeholder at startup
  and binding :8088 to loopback are not done here.
* The embed config is still written on the token path rather than inside the approved publish. It can no longer
  be widened.

## AT-08 — static RLS on the workspace roles (live)

`tests/integration/test_superset_embedding_live.py::test_at07_at08_live_guest_token_and_rls`, passed twice (33 s).
A dashboard is published with the real `SupersetPublisher` (connection under the workspace's BI login). Three policy
row filters go through `static_rls_rules` → `sync_rls`:

| Filter | Rule in Superset | A Superset user holding only `ws_…_viewer` sees |
|---|---|---|
| `priority IN (1, 2)` and `priority = 2` on `incident` | two rules, both workspace roles | exactly the `priority = 2` count from the analytics DB: the filters AND, never OR |
| `type = {{user.change_type}}` on `change_request` | `1 = 0` (needs a viewer attribute) | 0 rows: closed for direct Superset use |

## AT-07 — guest tokens and the embedded page (live)

* The token with the viewer's clause `(priority = 3)` reads the embedded chart through `/api/v1/chart/data`, which
  returns exactly the analytics DB's `priority = 3` count. A token without clauses returns the full count, so the
  workspace-role rules do not reach a guest: the token's clauses (`viewer_rls`) are what apply.
* The same token is refused (401/403) for a chart on another dashboard, and for that chart even when the request
  names the token's own dashboard. A forged token is refused. So are a changed payload (raw columns, 100,000 rows),
  an added WHERE, a request that names no chart, and `/superset/explore_json/`. Listing dashboards shows only the
  token's dashboard.
* `/embedded/{uuid}` returns 200 with referrer `http://localhost:5173`, carrying
  `frame-ancestors 'self' http://localhost:5173`. With referrer `http://evil.test` it returns 403.

## Browser check (built-in browser, real component, real Superset)

`EmbeddedDashboard` ran on a temporary Vite harness page. A local stand-in for the embed-token endpoint minted real
guest tokens from the :8089 Superset for a dashboard over a persistent staging schema, with the clause
`(priority = 3)`.

1. Harness on :5174, not an allowed origin: the browser refused the frame (`Framing 'http://localhost:8089/'
   violates … frame-ancestors`) and Superset answered 403 for the referrer.
2. With :5174 allowed (Superset restarted with `SUPERSET_EMBED_ORIGINS=http://localhost:5174`, embed config updated
   by `ensure_embedded`): the dashboard rendered in place. The KPI showed **6.88k**, the analytics DB's 6,880
   `priority = 3` rows out of 20,000, and the bar chart showed incidents by state. The page minted one token in
   total (the first SDK request reused it).
3. Rechecked after the review fixes (7-permission role, `aos_security.py`): both charts render with the same
   numbers, and no guest request was refused. Before the axis-spelling fix the bar chart was refused; that refusal
   is how the frontend's axis spelling came to light.

## Regression

`tests/unit/test_superset_access.py` 10 passed, including the allowed-domains upsert, `embed_domains`, assets out
of scope and locked datasets. `tests/integration/test_dashboards_publishing.py` 9 passed. The live test passed again
after the fixes. Web: `vitest run` 457 passed (1 skipped), including
`src/test/embeddedDashboard.test.tsx` (2). `tsc --noEmit` and `vite build` passed. Targeted ruff passed.

## Limits

* The browser check used a stand-in for the platform endpoint, with a superuser-backed connection on the throwaway
  :8089 Superset. The endpoint's own checks (`dashboards/access.py::guest_token`: membership, audience, current
  publication, viewer scope, audit) are proven by `test_guest_token_rechecks_auth` against the in-memory Superset.
  The live test drives the real Superset through the same publisher calls. The whole chain was not run as one
  journey (platform API → live Superset → browser).
* The shared :8088 Superset still runs the main checkout's config. It picks up embedding when this branch is
  merged and that container is recreated (`docker compose up -d --force-recreate superset`).
* Dev defaults for the guest-token secret are placeholders (see the review section). Production sets
  `SUPERSET_GUEST_TOKEN_SECRET` and `SUPERSET_EMBED_ORIGINS` / `ANALYSTOS_SUPERSET_EMBED_DOMAINS` to the real UI
  origin.
* `aos_security.py` works around how Superset 4.1.1 behaves. Recheck it on a Superset upgrade, and on each new
  chart type the publisher emits (its axis or column spelling may need `_canonical`).
* SDK 0.4.0 was verified against Superset 4.1.1 only.
