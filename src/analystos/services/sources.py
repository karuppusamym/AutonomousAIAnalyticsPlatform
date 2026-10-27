"""Source registration -> discovery -> asset selection -> sync (staged snapshot or pushdown ready)."""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.config import get_settings
from analystos.core.errors import InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db import column_presence as presence
from analystos.db.base import session_scope
from analystos.db.models import Source, SourceAsset, SourceColumn, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader

_CREDENTIAL_KEY = re.compile(r"pass(word|wd|phrase)?|pwd|secret|token|api_?key|private_?key|credential|auth", re.I)
_URL_USERINFO = re.compile(r"://[^/@\s]+:[^/@\s]+@")


def _credential_in_config(config: dict, depth: int = 0) -> bool:
    """Any key that names a credential (any case/prefix) or any value embedding user:password@ in a URL."""
    for k, v in (config or {}).items():
        if _CREDENTIAL_KEY.search(str(k)):
            return True
        if isinstance(v, str) and _URL_USERINFO.search(v):
            return True
        if isinstance(v, dict) and depth < 3 and _credential_in_config(v, depth + 1):
            return True
    return False


def _credentials_in(kind: str, config: dict) -> bool:
    """A REST description's `auth` block names the scheme, header and user only (connectors/rest.py AuthSpec);
    anything else in it, and every other key, is checked like any source config."""
    if kind != "rest":
        return _credential_in_config(config)
    auth = config.get("auth")
    extra = set(auth) - {"type", "header", "username"} if isinstance(auth, dict) else auth is not None
    return bool(extra) or _credential_in_config({k: v for k, v in config.items() if k != "auth"})


def _checked_secret_ref(ref: str | None) -> str | None:
    """A reference names a process secret the platform will hand to the source host: only what the operator
    allows (connectors/secrets.py `check_secret_ref`), checked when it is set, not first at connect time."""
    from analystos.connectors.secrets import check_secret_ref

    if ref is None or not str(ref).strip():
        return None
    check_secret_ref(ref)
    return str(ref).strip()


def register_source(session: Session, user: User, workspace_id: str, *, kind: str, name: str, config: dict,
                    secret_ref: str | None) -> Source:
    """Any kind in config/source_kinds.yaml that the administrator has enabled. Pushdown only where the
    kind supports a read-only session in a dialect the gateway validates, and the admin allows it."""
    from analystos.connectors import kinds
    from analystos.services.platform_settings import get as platform

    require_role(session, user, workspace_id, "editor")
    spec = kinds.get_kind(kind)
    settings = platform().sources
    if settings.enabled_kinds and spec.kind not in settings.enabled_kinds:
        raise InvalidInput(f"source kind '{spec.kind}' is disabled by the administrator")
    config = dict(config or {})
    if _credentials_in(spec.kind, config):
        raise InvalidInput("put credentials in a secret reference (env:NAME or file:/path), never in source config")
    missing = [f for f in spec.required if config.get(f) in (None, "")]
    if missing:
        raise InvalidInput(f"{spec.label} needs: {', '.join(missing)}")
    if spec.secret and spec.secret.required and not secret_ref:
        raise InvalidInput(f"{spec.label} needs a secret reference (env:NAME or file:/path) for its {spec.secret.field}")
    secret_ref = _checked_secret_ref(secret_ref)
    if spec.kind == "rest":  # S02-T8: the description itself is validated (read-only verbs, relative paths)
        from analystos.connectors.rest import parse_spec

        parse_spec(config)
    mode = kinds.execution_mode_for(spec.kind, config.pop("execution_mode", None) if settings.allow_pushdown else "staged")
    if mode == "staged":  # a snapshot must say which population it is (P4-C12)
        from analystos.connectors.sampling import validate_sampling

        validate_sampling(spec.kind, spec.is_sql, config, spec.sqlglot_dialect)
    src = Source(id=new_id("src"), workspace_id=workspace_id, kind=spec.kind, name=name, config=config, secret_ref=secret_ref,
                 status="registered", execution_mode=mode)
    session.add(src)
    audit(f"user:{user.id}", "source.registered", workspace_id=workspace_id, target=src.id,
          details={"kind": spec.kind, "execution_mode": mode}, session=session)
    return src


@scoped_loader
def _source(session: Session, user: User, source_id: str, minimum: str = "editor", workspace_id: str | None = None) -> Source:
    return load_in_workspace(session, Source, source_id, workspace_id, user=user, minimum=minimum, label="source")


_UNSET = object()


@scoped_loader
def update_source(session: Session, user: User, workspace_id: str, source_id: str, *, name: str | None = None,
                  config: dict | None = None, secret_ref: Any = _UNSET, clear_secret_ref: bool = False) -> Source:
    """Rename a source, or correct its connection: host, port, tables and other `config` fields, and the
    secret reference. Only the fields given change. The kind cannot change (a different kind is a different
    connector; add a new source and move table selection over instead). Changing `config` or the secret
    reference invalidates nothing already crawled or staged, but the source goes back to `registered` and
    its last error is cleared, so the catalog shows plainly that it has not been checked against the new
    connection yet: discover it again before relying on it.

    Every input is validated before anything is assigned to the ORM-tracked source, so a rejected edit
    never leaves a partial change for the caller's transaction to undo: raising here must be as good as
    never having been called."""
    from analystos.connectors import kinds
    from analystos.services.platform_settings import get as platform

    src = load_in_workspace(session, Source, source_id, workspace_id, user=user, minimum="editor", label="source")
    spec = kinds.get_kind(src.kind)

    if name is not None:
        name = name.strip()
        if not name:
            raise InvalidInput("name cannot be empty")

    new_config, new_mode = None, None
    if config is not None:
        settings = platform().sources
        new_config = dict(config or {})
        if _credentials_in(spec.kind, new_config):
            raise InvalidInput("put credentials in a secret reference (env:NAME or file:/path), never in source config")
        missing = [f for f in spec.required if new_config.get(f) in (None, "")]
        if missing:
            raise InvalidInput(f"{spec.label} needs: {', '.join(missing)}")
        if spec.kind == "rest":
            from analystos.connectors.rest import parse_spec

            parse_spec(new_config)
        new_mode = kinds.execution_mode_for(spec.kind, new_config.pop("execution_mode", None) if settings.allow_pushdown else "staged")
        if new_mode == "staged":  # a snapshot must say which population it is (P4-C12)
            from analystos.connectors.sampling import validate_sampling

            validate_sampling(spec.kind, spec.is_sql, new_config, spec.sqlglot_dialect)

    effective_secret = None if clear_secret_ref else (src.secret_ref if secret_ref is _UNSET else secret_ref)
    if secret_ref is not _UNSET and not clear_secret_ref:
        effective_secret = _checked_secret_ref(effective_secret)
    if not effective_secret and spec.secret and spec.secret.required:
        raise InvalidInput(f"{spec.label} needs a secret reference (env:NAME or file:/path) for its {spec.secret.field}")

    changed: dict[str, Any] = {}
    if name is not None and name != src.name:
        src.name, changed["name"] = name, True
    reconnect = new_config is not None and (new_config != src.config or new_mode != src.execution_mode)
    if reconnect:
        src.config, src.execution_mode, changed["config"] = new_config, new_mode, True
    if effective_secret != src.secret_ref:
        src.secret_ref, changed["secret_ref"], reconnect = effective_secret, True, True
    if not changed:
        raise InvalidInput("nothing to change")
    if reconnect:
        src.status, src.last_error = "registered", None
    audit(f"user:{user.id}", "source.updated", workspace_id=workspace_id, target=src.id,
          details={"changed": sorted(changed), "reconnect_needed": reconnect}, session=session)
    return src


@scoped_loader
def discover_source(user: User, source_id: str, workspace_id: str | None = None) -> dict:
    """Full metadata crawl without profiling (one code path with scheduled crawls).

    The crawler keeps owner tags and reviewed descriptions; the previous implementation replaced
    every column row on re-discovery, which silently dropped "restricted" tags."""
    from analystos.services.crawler import crawl_source

    with session_scope() as s:
        _source(s, user, source_id, workspace_id=workspace_id)
    result = crawl_source(user, source_id, mode="full", profile=False)
    with session_scope() as s:
        row = s.get(Source, source_id)
        assets = []
        for a in s.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.lifecycle == "active")
                           .order_by(SourceAsset.name)):
            cols = s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
            assets.append({"source_name": a.source_name, "name": a.name, "schema_name": a.schema_name, "kind": a.kind,
                           "row_count": a.row_count, "description": a.description, "business_name": a.business_name,
                           "freshness_at": a.freshness_at, "semantics": a.semantics, "snapshot": a.snapshot or None,
                           "columns": [{"name": c.name, "data_type": c.data_type, "nullable": c.nullable, "is_key": c.is_key,
                                        "description": c.description, "business_name": c.business_name, "tags": c.tags,
                                        "references": (c.profile or {}).get("references")} for c in cols]})
        emit(row.workspace_id, "source.connected", {"source_id": source_id, "assets": len(assets),
                                                    "latency_ms": result["stats"].get("latency_ms")},
             actor=f"user:{user.id}", session=s)
    return {"assets": assets, "crawl_id": result["crawl_id"], "stats": result["stats"], "changes": result["changes"],
            "test": {"ok": True, "message": "ok", "latency_ms": result["stats"].get("latency_ms", 0)}}


@scoped_loader
def select_assets(user: User, source_id: str, asset_names: list[str], workspace_id: str | None = None, *,
                  refresh: str = "auto", schedule: Any | None = None) -> dict:
    """Mark the assets the workspace may analyse, then sync them (staged: bounded snapshot load, or a
    watermark window for a table the source config declares `incremental`; `refresh` = auto | full |
    reconcile). With `schedule` (the API's BackgroundTasks.add_task) and `crawl.profile_on_select`, the tables
    that are newly selected or never profiled are then profiled and value-sampled by an incremental crawl in
    the background: the first discovery crawl ran before the source was ready, so it could not."""
    out = _select_assets(user, source_id, asset_names, workspace_id, refresh=refresh)
    out["profile_crawl"] = _profile_selection(user, source_id, out.pop("_unprofiled"), schedule)
    return out


def _profile_selection(user: User, source_id: str, keys: list[str], schedule: Any | None) -> dict:
    from analystos.services import crawler
    from analystos.services.platform_settings import get as platform

    if not keys:
        return {"status": "not_needed", "assets": []}
    if schedule is None or not platform().crawl.profile_on_select:
        return {"status": "not_requested", "assets": keys}
    try:
        with session_scope() as s:  # committed before scheduling: the background task reads the row in its own session
            run = crawler.start_crawl(s, s.get(User, user.id), source_id, mode="incremental", include=keys, profile=True,
                                      trigger="selection", actor=f"user:{user.id}")
            crawl_id = run.id
    except InvalidInput as exc:  # e.g. a crawl already running: it will not profile these; say so
        return {"status": "skipped", "assets": keys, "reason": exc.message}
    how = crawler.schedule_crawl(crawl_id, user.id, schedule)
    return {"status": "scheduled", "crawl_id": crawl_id, "assets": keys, "via": how}


@scoped_loader
def _select_assets(user: User, source_id: str, asset_names: list[str], workspace_id: str | None, *, refresh: str) -> dict:
    from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
    from analystos.connectors.registry import build_connector
    from analystos.evidence.manifest import mark_stale
    from analystos.staging.loader import StagingLoader
    from analystos.staging.snapshots import stage_asset

    with session_scope() as s:
        src = _source(s, user, source_id, workspace_id=workspace_id)
        if src.kind == "recipe":
            raise InvalidInput("recipe outputs are written by recipe runs; they cannot be re-staged")
        assets = list(s.scalars(select(SourceAsset).where(SourceAsset.source_id == source_id)))
        wanted = set(asset_names)
        unknown = wanted - {a.name for a in assets} - {a.source_name for a in assets}
        if unknown:
            raise InvalidInput(f"unknown assets: {', '.join(sorted(unknown))}")
        unprofiled = []
        for a in assets:
            was = a.selected
            a.selected = a.name in wanted or a.source_name in wanted
            if a.selected and (not was or not (a.stats or {}).get("profile_meta")):
                unprofiled.append((a.semantics or {}).get("source_key") or a.name)
                if not was:
                    a.stats = {}  # measurements of an earlier selection describe an earlier snapshot
        selected = [(a.id, a.source_name, a.name, a.schema_name,
                     [DiscoveredColumn(name=c.name, data_type=c.data_type, nullable=c.nullable, is_key=c.is_key,
                                       references=(c.profile or {}).get("references"))
                      for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
                      if c.name != "aos_deleted_at"])
                    for a in assets if a.selected]
        s.expunge(src)
    loaded = []
    if src.execution_mode == "staged":
        from analystos.services.platform_settings import get as platform

        platform_max = platform().sources.staged_max_rows
        settings = get_settings()
        connector = build_connector(src, settings)
        loader = StagingLoader(settings)
        from analystos.connectors import sampling
        from analystos.staging.incremental import incremental_for, stage_incremental, supports

        for asset_id, source_name, name, schema, cols in selected:
            d = DiscoveredAsset(source_name=source_name, name=name, columns=cols, kind="api_table")
            inc = incremental_for(src.config, source_name, name) if supports(connector) else None
            if inc is not None:  # P6-02: windows by watermark, merged on the key, deletes reconciled when due
                cap = sampling.row_cap(src.config, platform_max, sampling.sampling_for(src.config, source_name, name))
                info = stage_incremental(loader, connector, source_id, d, inc, workspace_id=src.workspace_id,
                                         cap=cap, mode=refresh)
            else:
                info = stage_asset(loader, connector, source_id, d, config=src.config, platform_max=platform_max,
                                   workspace_id=src.workspace_id)
            loaded.append({"asset": f"{schema}.{name}", **info})
            with session_scope() as s:
                a = s.get(SourceAsset, asset_id)
                a.row_count, a.freshness_at, a.snapshot = info.get("row_count"), utcnow(), info["snapshot"]
                _register_soft_delete_column(s, a, info)
                s.flush()
                # P4-03: findings bound to an older version of this snapshot now need re-verification
                mark_stale(s, a.workspace_id, source_id, f"{schema}.{name}")
    with session_scope() as s:
        row = s.get(Source, source_id)
        row.status = "ready"
        row.last_discovered_at = utcnow()  # bumps the gateway cache's source version after a re-stage
        audit(f"user:{user.id}", "source.assets_selected", workspace_id=row.workspace_id, target=source_id,
              details={"assets": asset_names, "loaded": loaded}, session=s)
        emit(row.workspace_id, "metadata.collected", {"source_id": source_id, "selected": asset_names, "loaded": loaded},
             actor=f"user:{user.id}", session=s)
    return {"selected": asset_names, "loaded": loaded, "_unprofiled": sorted(unprofiled)}


def _register_soft_delete_column(session: Session, asset: SourceAsset, info: dict) -> None:
    """A `deletes: soft` table carries `aos_deleted_at`; the catalog lists it so queries can filter on it."""
    from analystos.contracts.recipe import SOFT_DELETE_COLUMN

    staged = {c["name"]: c["type"] for c in info.get("columns") or []}
    if SOFT_DELETE_COLUMN not in staged:
        return
    known = {c.name: c for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == asset.id)
                                                .execution_options(**presence.INCLUDE_ABSENT))}
    if SOFT_DELETE_COLUMN in known:
        presence.restore(known[SOFT_DELETE_COLUMN])
    else:
        session.add(SourceColumn(asset_id=asset.id, name=SOFT_DELETE_COLUMN, ordinal=len(known),
                                 data_type=staged[SOFT_DELETE_COLUMN], tags=[], profile={}, semantics={},
                                 description="Set when the source no longer holds this key (soft delete)"))


@scoped_loader
def refresh_asset(user: User, source_id: str, asset_name: str, workspace_id: str | None = None, *, mode: str,
                  since: Any = None, until: Any = None) -> dict:
    """Re-stage one selected table of an incremental source (P6-02): `full` (swap), `reconcile` (a window plus
    the full reconcile of deletes), `replay` / `backfill` (merge the watermark range [since, until] without
    moving the watermark). Only an editor of the source's workspace; recipe outputs are not re-staged."""
    from analystos.connectors import sampling
    from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn
    from analystos.connectors.registry import build_connector
    from analystos.evidence.manifest import mark_stale
    from analystos.services.platform_settings import get as platform
    from analystos.staging.incremental import MODES, incremental_for, stage_incremental, supports
    from analystos.staging.loader import StagingLoader

    if mode not in MODES or mode == "auto":
        raise InvalidInput(f"mode must be one of {', '.join(m for m in MODES if m != 'auto')}")
    with session_scope() as s:
        src = _source(s, user, source_id, workspace_id=workspace_id)
        if src.kind == "recipe" or src.execution_mode != "staged":
            raise InvalidInput("only staged sources are refreshed by watermark")
        a = s.scalar(select(SourceAsset).where(SourceAsset.source_id == source_id, SourceAsset.selected.is_(True),
                                               (SourceAsset.name == asset_name) | (SourceAsset.source_name == asset_name)))
        if a is None:
            raise NotFound(f"{asset_name} is not a selected table of this source")
        cols = [DiscoveredColumn(name=c.name, data_type=c.data_type, nullable=c.nullable, is_key=c.is_key)
                for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id).order_by(SourceColumn.ordinal))
                if c.name != "aos_deleted_at"]
        d = DiscoveredAsset(source_name=a.source_name, name=a.name, columns=cols, kind="api_table")
        asset_id, fq = a.id, f"{a.schema_name}.{a.name}"
        s.expunge(src)
    settings = get_settings()
    connector = build_connector(src, settings)
    inc = incremental_for(src.config, d.source_name, d.name)
    if inc is None or not supports(connector):
        raise InvalidInput(f"{asset_name} declares no incremental block (config.incremental) or its source cannot "
                           "read by watermark")
    cap = sampling.row_cap(src.config, platform().sources.staged_max_rows,
                           sampling.sampling_for(src.config, d.source_name, d.name))
    info = stage_incremental(StagingLoader(settings), connector, source_id, d, inc, workspace_id=src.workspace_id, cap=cap,
                             mode=mode, window=(since, until) if mode in ("replay", "backfill") else None)
    with session_scope() as s:
        a = s.get(SourceAsset, asset_id)
        a.row_count, a.freshness_at, a.snapshot = info.get("row_count"), utcnow(), info["snapshot"]
        _register_soft_delete_column(s, a, info)
        mark_stale(s, a.workspace_id, source_id, fq)
        row = s.get(Source, source_id)
        row.last_discovered_at = utcnow()
        audit(f"user:{user.id}", "source.asset_refreshed", workspace_id=row.workspace_id, target=source_id,
              details={"asset": fq, "mode": mode, "incremental": info["incremental"]}, session=s)
        emit(row.workspace_id, "metadata.collected", {"source_id": source_id, "refreshed": fq, "mode": mode,
                                                      "incremental": info["incremental"]}, actor=f"user:{user.id}", session=s)
    return {"asset": fq, "row_count": info.get("row_count"), "content_fingerprint": info.get("content_fingerprint"),
            "incremental": info["incremental"]}


@scoped_loader
def tag_column(session: Session, user: User, asset_id: str, column: str, tags: list[str]) -> SourceColumn:
    asset = load_in_workspace(session, SourceAsset, asset_id, user=user, minimum="owner", label="asset")
    col = session.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset_id, SourceColumn.name == column))
    if col is None:
        raise NotFound("column not found")
    allowed = {"pii", "restricted", "sensitive"}
    if set(tags) - allowed:
        raise InvalidInput(f"tags must be within {sorted(allowed)}")
    col.tags, col.tags_origin = sorted(set(tags)), "user"
    audit(f"user:{user.id}", "column.tagged", workspace_id=asset.workspace_id, target=f"{asset.schema_name}.{asset.name}.{column}",
          details={"tags": col.tags}, session=session)
    return col


@scoped_loader
def curate_column(session: Session, user: User, asset_id: str, column: str, patch: dict[str, str | None]) -> SourceColumn:
    """A person's business name / description for a column: origin `user`, so no crawl or knowledge ingest
    overwrites it. Same role as column tagging; an empty string clears the value (and it stays cleared)."""
    asset = load_in_workspace(session, SourceAsset, asset_id, user=user, minimum="owner", label="asset")
    col = session.scalar(select(SourceColumn).where(SourceColumn.asset_id == asset_id, SourceColumn.name == column))
    if col is None:
        raise NotFound("column not found")
    if not patch:
        raise InvalidInput("nothing to change")
    if "business_name" in patch:
        col.business_name, col.business_name_origin = (patch["business_name"] or "").strip() or None, "user"
    if "description" in patch:
        col.description, col.description_origin = (patch["description"] or "").strip() or None, "user"
    audit(f"user:{user.id}", "column.curated", workspace_id=asset.workspace_id,
          target=f"{asset.schema_name}.{asset.name}.{column}", details=patch, session=session)
    return col
