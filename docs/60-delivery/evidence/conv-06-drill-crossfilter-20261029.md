# CONV-06 live run — drill-down and cross-filtering in the dashboard preview (2026-10-29)

**What ran.** `tests/integration/test_dashboard_interact.py` against the compose Postgres 16 (`analystos-postgres-1`;
its own test databases `analystos_test_conv06` / `analystos_test_dp_conv06`, migrated and seeded by the `control_db`
fixture), strict tenancy, with `ANALYSTOS_EVIDENCE_OUT` set. Windows 11 dev host, branch
`claude/conv-06-drill-crossfilter-dca8b1`. **No model was called** (the provider key was blanked for the module).

**Setup (in the test).** A workspace over one staged table (`orders.parquet`, 72 rows: priority, category, region,
opened_at, resolution_hours, customer_email tagged `pii`) through a `csv` source. Semantic model proposed by the analyst
and approved by the approver; metrics `order_count = COUNT(number)` and `resolution_time = AVG(resolution_hours)` with
dimensions priority, category, region and opened_at. The analyst built dashboard `art_425ad35644df` (v5) in the
builder chat: "add order count", "add order count by priority", "add resolution time by category", "add monthly order count".

## Results

| Step | Request | Outcome |
|---|---|---|
| Explore | `GET /api/v2/dashboards/{id}/explore` | drill kinds {"c_order_count_by_priority": "dimension", "c_resolution_time_by_category": "dimension", "c_monthly_order_count": "time"} |
| Cross-filter (analyst) | `POST …/view` priority = 1 - Critical from the priority chart | KPI order count **24.0** (pandas: rows with that priority), resolution time by category re-queried through the semantic compiler (equals pandas), the priority chart `source`, the monthly trend `not_filtered`: "This chart has no Priority field.". Version unchanged; `base_version` + 5 → 409 |
| Viewer with a row filter | approver as `viewer`, policy row filter `region IN {{user.regions}}`, attribute regions = [EMEA]; same click | KPI **8.0** (EMEA only, equals pandas); category averages equal pandas over EMEA rows |
| Rows behind a member | `POST …/drill {path: [priority = 2 - High], detail: true}` as the viewer | 8 rows, all EMEA / 2 - High; columns number, priority, category, region, opened_at, resolution_hours: `customer_email` (PII) never listed |
| Refusals | value `1' OR '1'='1`, field `customer_email`, field `orders.opened_at` | 422 each, before any statement |
| Time drill | month 2026-07-01 → days | 15 day rows summing to the month's orders (pandas) |
| Dimension drill | priority 1 - Critical → category | [["Network", 24]] (equals pandas) |
| Apply as dashboard filter | the view's `dashboard.add_filter` op to `/ops` | viewer 403; analyst → version + 1; `refresh_saved` re-queries the priority chart to 1 - Critical only |
| Gateway audit | `QueryExecution` rows with purpose `dashboard.*` | 11 statements, all `ok`; purposes dashboard.cross_filter, dashboard.drill, dashboard.detail; actors are the users who clicked (the viewer for every detail query) |

Command (from the worktree, Windows):

```
set PYTHONPATH=<worktree>\src;<worktree>
set ANALYSTOS_TEST_DATABASE_URL=postgresql+psycopg://analystos:analystos@127.0.0.1:5432/analystos_test_conv06
set ANALYSTOS_TEST_DP_DB=analystos_test_dp_conv06
set ANALYSTOS_EVIDENCE_OUT=<file>.json
.venv\Scripts\python -m pytest -q -m integration tests/integration/test_dashboard_interact.py
```

Result: 1 passed.

Raw summary:

```json
{
  "workspace": "ws_dc9c4beb8e70",
  "dashboard": "art_425ad35644df",
  "version": 5,
  "explore": {
    "c_order_count_by_priority": "dimension",
    "c_resolution_time_by_category": "dimension",
    "c_monthly_order_count": "time"
  },
  "analyst_view": {
    "c_order_count_by_priority": {
      "status": "source",
      "value": null,
      "reason": null
    },
    "c_resolution_time_by_category": {
      "status": "filtered",
      "value": null,
      "reason": null
    },
    "c_monthly_order_count": {
      "status": "not_filtered",
      "value": null,
      "reason": "This chart has no Priority field."
    },
    "kpi_order_count": {
      "status": "filtered",
      "value": 24.0,
      "reason": null
    }
  },
  "viewer": {
    "kpi": 8.0,
    "detail_columns": [
      "number",
      "priority",
      "category",
      "region",
      "opened_at",
      "resolution_hours"
    ],
    "detail_rows": 8
  },
  "drill": {
    "month": "2026-07-01T00:00:00",
    "days": 15,
    "by_category": [
      [
        "Network",
        24
      ]
    ]
  },
  "gateway": {
    "dashboard.cross_filter": [
      "user:usr_8c5e510de673",
      "user:usr_d933826c759a"
    ],
    "dashboard.drill": [
      "user:usr_8c5e510de673",
      "user:usr_d933826c759a"
    ],
    "dashboard.detail": [
      "user:usr_d933826c759a"
    ],
    "statements": 11
  }
}
```
