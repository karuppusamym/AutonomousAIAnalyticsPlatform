from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from analystos.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _json_default(value):
    """numpy scalars/arrays, datetimes and Decimals come out of the statistics stack; store them as plain JSON."""
    import datetime as _dt
    import decimal

    if hasattr(value, "tolist"):  # numpy scalars and arrays (np.bool_, np.float64, ndarray)
        return value.tolist()
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _finite(value):
    """JSONB rejects NaN/Infinity; statistics can produce them (e.g. undefined effect sizes) -> null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def json_dumps(value) -> str:
    return json.dumps(_finite(json.loads(json.dumps(value, default=_json_default))), allow_nan=False)


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    from analystos.db.pools import engine_kwargs

    url = url or get_settings().database_url
    return create_engine(url, json_serializer=json_dumps, **engine_kwargs("control", url))


@lru_cache
def _factory(url: str | None = None) -> sessionmaker[Session]:
    from sqlalchemy import event

    from analystos.tenancy.session import on_after_begin

    factory = sessionmaker(bind=get_engine(url), expire_on_commit=False)
    event.listen(factory, "after_begin", on_after_begin)  # tenant binding per transaction (ADR-009)
    return factory


def SessionLocal() -> Session:  # noqa: N802 - conventional name
    return _factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short transactions only. Never hold one open across a model call or a source query."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
