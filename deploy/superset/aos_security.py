"""Guest chart-data requests must be the saved chart, nothing else (S08-T5, AT-07).

Superset 4.1.1 compares a guest's chart-data payload with the saved chart (`query_context_modified`), but the
chart is looked up through the permission-filtered chart DAO. A guest holds no datasource permission, so the
lookup finds nothing, `slice_` is None, the request is treated as a native-filter request and the comparison is
skipped: a guest could ask for raw rows, any column, any SQL metric (seen live: 19,996 raw rows from a KPI chart).

Here, before Superset's own checks, a guest's chart-data request:
* names a chart (`slice_id`) that exists and is on the dashboard it names (`dashboardId`);
* asks only for columns, group-bys and metrics of the saved chart (Superset's rule, see `_modified`);
* keeps the saved chart's custom WHERE / HAVING SQL;
* never asks for more rows than the saved chart.
Native-filter requests (`type: NATIVE_FILTER`) keep Superset's own handling: values of a filter configured on
the dashboard. Row-level security is unchanged and still comes from the guest token.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.security.manager import SupersetSecurityManager

log = logging.getLogger("superset.security.aos")  # under superset.*, so it reaches the configured handlers
_BARE_COLUMN = re.compile(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?$')
_COLUMN_KEYS = ("columns", "groupby", "x_axis", "series_columns")


def _canonical(value: Any) -> str:
    """One spelling per column or metric. The frontend sends an axis as {"expressionType": "SQL",
    "sqlExpression": "state", "columnType": "BASE_AXIS", "timeGrain": ...} where the saved chart says "state":
    an SQL expression that is only a column name is that column. Any other expression is compared as written."""
    if isinstance(value, dict):
        expr = value.get("sqlExpression")
        if value.get("expressionType") == "SQL" and isinstance(expr, str) and _BARE_COLUMN.match(expr.strip()):
            return "col:" + _BARE_COLUMN.match(expr.strip()).group(1).lower()
        keep = {k: v for k, v in value.items() if k not in ("optionName", "label", "hasCustomLabel", "isNew",
                                                           "timeGrain", "columnType")}
        return "expr:" + json.dumps(keep, sort_keys=True, default=str)
    if isinstance(value, str):
        return "col:" + value.strip().strip('"').lower()
    return "other:" + json.dumps(value, sort_keys=True, default=str)


def _values(source: Any, keys: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for key in keys:
        raw = source.get(key) if isinstance(source, dict) else getattr(source, key, None)
        for v in raw if isinstance(raw, list) else ([raw] if raw else []):
            out.add(_canonical(v))
    return out


def _modified(query_context: Any, slc: Any, stored: list[dict[str, Any]]) -> bool:
    """Superset's `query_context_modified` rule (requested columns and metrics a subset of the saved chart's),
    with `_canonical` spellings so the frontend's own requests pass and nothing new does."""
    params = slc.params_dict or {}
    allowed_cols = _values(params, _COLUMN_KEYS) | {c for q in stored for c in _values(q, ("columns", "groupby"))}
    allowed_metrics = (_values(params, ("metrics", "metric", "percent_metrics", "timeseries_limit_metric"))
                       | {m for q in stored for m in _values(q, ("metrics",))})
    form_data = query_context.form_data or {}
    asked = [(_values(form_data, _COLUMN_KEYS), _values(form_data, ("metrics",)))]
    asked += [(_values(q, ("columns",)), _values(q, ("metrics",))) for q in query_context.queries]
    for cols, metrics in asked:
        if not cols <= allowed_cols or not metrics <= allowed_metrics:
            # names only, never data: what an operator needs to tell a tampered request from a chart shape we miss
            log.warning("guest chart %s payload refused: columns %s metrics %s not within %s / %s", slc.id,
                        sorted(cols - allowed_cols), sorted(metrics - allowed_metrics), sorted(allowed_cols),
                        sorted(allowed_metrics))
            return True
    return False


def _refuse(message: str) -> None:
    raise SupersetSecurityException(SupersetError(error_type=SupersetErrorType.DASHBOARD_SECURITY_ACCESS_ERROR,
                                                  message=message, level=ErrorLevel.WARNING))


def _stored_queries(slc: Any) -> list[dict[str, Any]]:
    try:
        return list((json.loads(slc.query_context or "{}") or {}).get("queries") or [])
    except (TypeError, ValueError):
        return []


class AosSecurityManager(SupersetSecurityManager):
    def raise_for_access(self, **kwargs: Any) -> None:  # type: ignore[override]
        query_context = kwargs.get("query_context")
        if query_context is not None and self.is_guest_user():
            self._check_guest_query(query_context)
        return super().raise_for_access(**kwargs)

    def _check_guest_query(self, query_context: Any) -> None:
        from superset import db
        from superset.models.dashboard import Dashboard
        from superset.models.slice import Slice

        form_data = query_context.form_data or {}
        if form_data.get("type") == "NATIVE_FILTER":
            return  # Superset checks the filter is configured on the dashboard for this dataset
        slice_id, dashboard_id = form_data.get("slice_id"), form_data.get("dashboardId")
        if slice_id is None or dashboard_id is None:
            _refuse("Guest chart data must name a saved chart and its dashboard")
        slc = db.session.query(Slice).filter(Slice.id == int(slice_id)).one_or_none()
        dashboard = db.session.query(Dashboard).filter(Dashboard.id == int(dashboard_id)).one_or_none()
        if slc is None or dashboard is None or slc.id not in {s.id for s in dashboard.slices}:
            _refuse("Guest chart data must be a chart on the dashboard")
        if query_context.datasource is None or slc.datasource_id != query_context.datasource.id:
            _refuse("Guest user cannot query another dataset")
        # query_context.slice_ stays as Superset resolved it (None for a guest): with it set, Superset's own literal
        # comparison would refuse the frontend's own axis spelling; `_modified` applies the same rule instead.
        stored = _stored_queries(slc)
        if _modified(query_context, slc, stored):
            _refuse("Guest user cannot modify chart payload")
        params = slc.params_dict or {}
        allowed_sql = {(json.dumps((q.get("extras") or {}).get(k) or "")) for q in stored for k in ("where", "having")}
        allowed_sql.add(json.dumps(""))
        limits = [int(v) for v in [*(q.get("row_limit") for q in stored), params.get("row_limit")] if v]
        cap = max(limits) if limits else None
        for query in query_context.queries:
            extras = getattr(query, "extras", None) or {}
            if any(json.dumps(extras.get(k) or "") not in allowed_sql for k in ("where", "having")):
                _refuse("Guest user cannot add SQL to a chart")
            if cap is not None and (not query.row_limit or query.row_limit > cap):
                query.row_limit = cap
