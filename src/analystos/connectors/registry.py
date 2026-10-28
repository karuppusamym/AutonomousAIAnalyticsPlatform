"""Connector factory: control-plane Source row -> Connector instance.

Every kind in the source-kind catalog (config/source_kinds.yaml) is buildable: ServiceNow and
files keep their dedicated connectors, PostgreSQL and SQL Server keep theirs (both are
GenericSQLConnector subclasses), and every other SQL kind is a GenericSQLConnector.
"""
from __future__ import annotations

from typing import Any

from analystos.connectors import kinds as catalog
from analystos.connectors.base import Connector
from analystos.core.errors import InvalidInput


def _kinds() -> tuple[str, ...]:
    return tuple(catalog.kind_ids())


KINDS = (*_kinds(), "file")


def build_connector(source: Any, settings: Any | None = None, **overrides: Any) -> Connector:
    """Build the connector for ``source`` (an ``analystos.db.models.Source`` or any object with
    ``kind``, ``config`` and ``secret_ref``; ``execution_mode`` is honoured when present).
    ``overrides`` are passed to the connector constructor (e.g. ``http_client`` for tests,
    ``allowed_dir`` for file sources, ``password`` for tests)."""
    raw_kind = (getattr(source, "kind", None) or "").lower()
    try:
        spec = catalog.get_kind(raw_kind)
    except InvalidInput:
        raise InvalidInput(f"Unknown source kind {raw_kind!r}; expected one of {', '.join(_kinds())}") from None
    kind = spec.kind
    config = dict(getattr(source, "config", None) or {})
    secret_ref = getattr(source, "secret_ref", None)
    mode = getattr(source, "execution_mode", None)
    if kind == "csv":
        from analystos.connectors.csv_file import CSVFileConnector, default_upload_dir

        overrides.setdefault("allowed_dir", default_upload_dir(settings))
        return CSVFileConnector(config, secret_ref, **overrides)
    if kind == "servicenow":
        from analystos.connectors.servicenow import ServiceNowConnector

        if not config.get("instance_url") and settings is not None:
            config["instance_url"] = getattr(settings, "servicenow_mock_url", None)
        return ServiceNowConnector(config, secret_ref, **overrides)
    if kind == "rest":  # S02-T8: a declarative, read-only REST description (always staged)
        from analystos.connectors.rest import RESTConnector

        overrides.setdefault("settings", settings)
        return RESTConnector(config, secret_ref, **overrides)
    if isinstance(mode, str) and mode:
        overrides.setdefault("execution_mode", mode)
    if kind == "postgres":
        from analystos.connectors.postgres import PostgresConnector

        return PostgresConnector(config, secret_ref, **overrides)
    if kind == "sqlserver":
        # The driver check stays lazy here (test() reports it, discover() raises) so a SQL Server
        # source can be registered before pymssql is installed, as before.
        from analystos.connectors.sqlserver import SQLServerConnector

        return SQLServerConnector(config, secret_ref, **overrides)
    catalog.require_driver(kind)
    if "path" in spec.required:
        from analystos.connectors.csv_file import default_upload_dir

        overrides.setdefault("allowed_dir", default_upload_dir(settings))
    from analystos.connectors.generic_sql import GenericSQLConnector

    return GenericSQLConnector(kind, config, secret_ref, **overrides)
