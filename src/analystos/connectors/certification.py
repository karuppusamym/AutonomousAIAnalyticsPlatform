"""Connector certification derived from live evidence files (spec v1 §62, spec v3 §3.1, ADR-0011).

A source kind is `certified` only when a dated live-run evidence file for it exists:

    docs/60-delivery/evidence/connector-<kind>-YYYYMMDD[-suffix].md

with YAML frontmatter that names the same kind and date, the engine it ran against, the test that
ran, and `result: pass`. There is no hand-kept list: deleting the file un-certifies the kind, and
`scripts/certify_connectors.py` writes the file only after the kind's real-engine tests passed.
A mock or catalog test is not live evidence; without a file a kind is `tested` (catalog and unit
tests, `tests/unit/test_source_kinds.py`).

S02-T6: the certification kit (`analystos certify-connector`, connectors/certify_kit.py) writes
`docs/evidence/connectors/<kind>-YYYY-MM-DD.md` with the same frontmatter. Both layouts count, and only
evidence younger than ``MAX_AGE_DAYS`` (180) certifies: an engine certified once is re-certified, not trusted
forever.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from analystos.core.config import DATA_ROOT

EVIDENCE_DIR = DATA_ROOT / "docs" / "60-delivery" / "evidence"
KIT_EVIDENCE_DIR = DATA_ROOT / "docs" / "evidence" / "connectors"
MAX_AGE_DAYS = 180
UNIT_EVIDENCE = "tests/unit/test_source_kinds.py"
FILE_NAME = re.compile(r"^connector-(?P<kind>[a-z0-9_]+)-(?P<date>\d{8})(?:-[A-Za-z0-9_.-]+)?\.md$")
KIT_FILE_NAME = re.compile(r"^(?P<kind>[a-z0-9_]+)-(?P<date>\d{4}-\d{2}-\d{2})\.md$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
REQUIRED = ("kind", "date", "engine", "test", "result")


class Evidence(BaseModel):
    kind: str
    date: date
    path: str  # repository-relative
    engine: str
    test: str


def parse_evidence(path: Path, kind: str) -> tuple[Evidence | None, str | None]:
    """(evidence, None) when `path` is valid live evidence for `kind`, else (None, why not)."""
    m = FILE_NAME.match(path.name) or KIT_FILE_NAME.match(path.name)
    if not m or m.group("kind") != kind:
        return None, f"{path.name}: not a connector-{kind}-YYYYMMDD*.md or {kind}-YYYY-MM-DD.md file"
    stamp = m.group("date").replace("-", "")
    try:
        day = datetime.strptime(stamp, "%Y%m%d").date()
        fm = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        meta = (yaml.safe_load(fm.group(1)) if fm else None) or {}
    except (ValueError, OSError, yaml.YAMLError) as exc:
        return None, f"{path.name}: {exc}"
    missing = [k for k in REQUIRED if not meta.get(k)]
    if missing:
        return None, f"{path.name}: frontmatter lacks {', '.join(missing)}"
    if str(meta["kind"]) != kind or str(meta["date"]).replace("-", "") != stamp:
        return None, f"{path.name}: frontmatter kind/date do not match the file name"
    if str(meta["result"]).lower() != "pass":
        return None, f"{path.name}: result is {meta['result']!r}, not pass"
    try:
        rel = str(path.resolve().relative_to(DATA_ROOT))
    except ValueError:
        rel = str(path)
    return Evidence(kind=kind, date=day, path=rel, engine=str(meta["engine"]), test=str(meta["test"])), None


def _folders(evidence_dir: Path | None) -> list[Path]:
    return [evidence_dir] if evidence_dir is not None else [EVIDENCE_DIR, KIT_EVIDENCE_DIR]


def evidence_for(kind: str, evidence_dir: Path | None = None, *, today: date | None = None) -> Evidence | None:
    """The newest valid live evidence file for `kind` younger than MAX_AGE_DAYS, if any."""
    oldest = (today or date.today()) - timedelta(days=MAX_AGE_DAYS)
    found = [e for folder in _folders(evidence_dir) if folder.is_dir()
             for p in [*folder.glob(f"connector-{kind}-*.md"), *folder.glob(f"{kind}-*.md")]
             for e, _ in [parse_evidence(p, kind)] if e is not None and e.date >= oldest]
    return max(found, key=lambda e: (e.date, e.path)) if found else None


def certification(kind: str, evidence_dir: Path | None = None, *, today: date | None = None) -> dict[str, Any]:
    ev = evidence_for(kind, evidence_dir, today=today)
    if ev is not None:
        return {"status": "certified", "evidence": ev.path, "evidence_date": ev.date.isoformat(), "engine": ev.engine}
    return {"status": "tested", "evidence": UNIT_EVIDENCE, "evidence_date": None, "engine": None}


@lru_cache(maxsize=1)
def _cached_statuses(folder: str | None, stamp: tuple[tuple[str, float], ...], today: date) -> dict[str, dict[str, Any]]:
    from analystos.connectors import kinds

    return {k: certification(k, Path(folder) if folder else None, today=today) for k in kinds.kind_ids()}


def statuses(evidence_dir: Path | None = None, *, today: date | None = None) -> dict[str, dict[str, Any]]:
    """Certification of every kind; recomputed whenever an evidence file appears, changes or goes, and daily
    (evidence ages out)."""
    stamp = tuple(sorted((str(p), p.stat().st_mtime) for folder in _folders(evidence_dir) if folder.is_dir()
                         for p in folder.glob("*.md")))
    return _cached_statuses(str(evidence_dir) if evidence_dir is not None else None, stamp, today or date.today())


def connector_manifests(evidence_dir: Path | None = None) -> list[dict[str, Any]]:
    """`kind: Connector` manifests generated from the source-kind catalog (one source of truth)."""
    from analystos.connectors import kinds

    certs = statuses(evidence_dir)
    out: list[dict[str, Any]] = []
    for k in kinds.list_kinds():
        cert = certs[k.kind]
        mode = kinds.execution_mode_for(k.kind)
        out.append({
            "kind": "Connector", "id": f"connector.{k.kind}", "version": "1.0.0", "summary": f"{k.label} ({k.category})",
            "entry": f"builtin:connector:{k.kind}", "determinism": "deterministic", "side_effect": "read_source",
            "cost_class": "query", "permissions": ["role:editor"],
            "certification": {"status": cert["status"], "evidence": cert["evidence"]},
            "tags": [k.category, mode],
            "spec": {"source_kind": k.kind, "label": k.label, "category": k.category, "sqlglot_dialect": k.sqlglot_dialect,
                     "default_execution_mode": mode, "pushdown_allowed": k.pushdown_allowed,
                     "readonly_session": bool(k.session_sql.readonly), "required": k.required, "optional": k.optional,
                     "driver": k.driver.model_dump(), "evidence_date": cert["evidence_date"], "engine": cert["engine"]},
        })
    return out
