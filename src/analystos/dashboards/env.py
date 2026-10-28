"""The platform side of the ops engine: what a filter value means and what a metric is, for one user.

* Filter values resolve through trusted glossary entries (a synonym names its canonical term), then a
  small geography table (country and region aliases), then the field's profiled vocabulary and the
  values the dashboard's previews show. A value none of these know is refused: the user names it
  exactly rather than the platform guessing.
* A metric is looked up by name and version in the workspace semantic layer; it is allowed when every
  source column it reads is inside the user's resolved scope.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.dashboard_dsl import DashboardDoc, FieldDef
from analystos.dashboards.ops import MetricInfo, OpEnv, Resolution

# alias -> spellings a dataset may use for the same place (ISO 3166 alpha-2/3, common name).
_GEO: dict[str, tuple[str, ...]] = {}
for names in (
    ("US", "USA", "United States", "United States of America", "America", "U.S."),
    ("GB", "GBR", "UK", "United Kingdom", "Great Britain", "Britain", "England"),
    ("DE", "DEU", "Germany", "Deutschland"), ("FR", "FRA", "France"), ("IN", "IND", "India", "Bharat"),
    ("CN", "CHN", "China"), ("JP", "JPN", "Japan"), ("CA", "CAN", "Canada"), ("AU", "AUS", "Australia"),
    ("BR", "BRA", "Brazil"), ("MX", "MEX", "Mexico"), ("ES", "ESP", "Spain"), ("IT", "ITA", "Italy"),
    ("NL", "NLD", "Netherlands", "Holland"), ("SG", "SGP", "Singapore"), ("AE", "ARE", "UAE", "United Arab Emirates"),
    ("SA", "SAU", "Saudi Arabia"), ("ZA", "ZAF", "South Africa"), ("KR", "KOR", "South Korea", "Korea"),
    ("LK", "LKA", "Sri Lanka"), ("IE", "IRL", "Ireland"), ("SE", "SWE", "Sweden"), ("CH", "CHE", "Switzerland"),
    ("EMEA", "Europe, Middle East and Africa", "Europe Middle East Africa"),
    ("APAC", "APJ", "Asia Pacific", "Asia-Pacific"), ("AMER", "AMERICAS", "Americas"),
    ("NA", "NAM", "North America"), ("LATAM", "Latin America", "South America"),
):
    for alias in names:
        _GEO[re.sub(r"[^a-z0-9]+", " ", alias.lower()).strip()] = names


def _key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def vocabulary(session: Session, workspace_id: str, field_name: str, doc: DashboardDoc | None = None) -> list[str]:
    """Values the field is known to take: its profile in the catalog and the dashboard's previews."""
    from analystos.db.models import SourceAsset, SourceColumn

    values: list[str] = []
    rows = session.execute(select(SourceColumn.profile).join(SourceAsset, SourceAsset.id == SourceColumn.asset_id)
                           .where(SourceAsset.workspace_id == workspace_id, SourceColumn.name == field_name).limit(20))
    for (profile,) in rows:
        p = profile or {}
        if p.get("values_complete") and p.get("values"):
            values.extend(str(v) for v in p["values"])
        values.extend(str(t.get("value")) for t in p.get("top_values") or [] if isinstance(t, dict) and t.get("value") is not None)
    for chart in (doc.charts if doc else []):
        cols = list((chart.preview or {}).get("columns") or [])
        if field_name in cols:
            i = cols.index(field_name)
            for row in (chart.preview or {}).get("rows") or []:
                if isinstance(row, list) and i < len(row) and row[i] is not None:
                    values.append(str(row[i]))
    return list(dict.fromkeys(values))


def resolver(session: Session, workspace_id: str, doc: DashboardDoc):  # noqa: ANN201 - Callable
    def field_type(field_name: str) -> str | None:
        for chart in doc.charts:
            f = chart.field(field_name)
            if f is not None:
                return f.type
        return None

    def resolve(field_name: str, value: Any) -> Resolution | None:
        ftype = field_type(field_name)
        items = value if isinstance(value, list) else [value]
        if ftype in ("temporal", "quantitative"):
            return Resolution(value=value, via="literal")
        known = vocabulary(session, workspace_id, field_name, doc)
        by_key = {_key(v): v for v in known}
        canon: dict[str, str] = {}
        try:
            from analystos.knowledge.entries import visible_entries

            for e in visible_entries(session, workspace_id, kinds=("term", "definition"), trusted_only=True):
                for phrase in (e.name, *(e.synonyms or ())):
                    canon.setdefault(_key(phrase), e.name)
        except Exception:  # noqa: BLE001 - no glossary is not an error: the other resolvers still apply
            canon = {}
        out, vias = [], []
        for item in items:
            k = _key(item)
            if k in by_key:
                out.append(by_key[k])
                vias.append("vocabulary")
                continue
            term = canon.get(k)
            if term is not None and (not known or _key(term) in by_key):
                out.append(by_key.get(_key(term), term))
                vias.append("glossary")
                continue
            spellings = _GEO.get(k)
            hit = next((by_key[_key(s)] for s in spellings or () if _key(s) in by_key), None)
            if hit is not None:
                out.append(hit)
                vias.append("geography")
                continue
            if not known:  # nothing profiled to check against: kept as typed, and labelled so
                out.append(item)
                vias.append("unverified")
                continue
            return None
        via = "vocabulary" if set(vias) == {"vocabulary"} else next(v for v in vias if v != "vocabulary")
        return Resolution(value=out if isinstance(value, list) else out[0], via=via,
                          note="" if out == items else f"{items if isinstance(value, list) else items[0]!r} means {out!r}")

    return resolve


def metric_lookup(session: Session, user: Any, workspace_id: str):  # noqa: ANN201 - Callable
    from analystos.db.models import SemanticMetric
    from analystos.governance.policy import resolve_scope

    scope = resolve_scope(session, user, workspace_id)
    readable = {a.lower() for a in scope.assets}
    denied = {c.lower() for c in scope.denied_columns}

    def lookup(name: str, version: int) -> MetricInfo | None:
        row = session.scalar(select(SemanticMetric).where(SemanticMetric.workspace_id == workspace_id,
                                                          SemanticMetric.name == name, SemanticMetric.version == version))
        if row is None:
            return None
        definition = row.definition or {}
        columns = [str(c) for c in definition.get("source_columns") or []]
        tables = {".".join(c.split(".")[:-1]).lower() for c in columns if c.count(".") >= 2}
        outside = sorted(t for t in tables if t not in readable)
        hidden = sorted(c for c in columns if c.lower() in denied)
        reason = ("outside your scope: " + ", ".join(outside)) if outside else (
            ("restricted columns: " + ", ".join(hidden)) if hidden else "")
        dims = [FieldDef(name=d.split(".")[-1], type="temporal" if _looks_temporal(d) else "nominal")
                for d in definition.get("dimensions") or []]
        sensitivity = "confidential" if hidden or any(c.lower() in denied for c in columns) else "internal"
        return MetricInfo(name=name, version=version, status=row.status, allowed=not outside and not hidden,
                          fields=[FieldDef(name=name, type="quantitative", label=row.display_name or name), *dims],
                          sources=sorted(tables) or [f"metric:{name}@{version}"], sensitivity=sensitivity,
                          display_name=row.display_name or "", reason=reason)

    return lookup


def _looks_temporal(name: str) -> bool:
    return bool(re.search(r"(date|_at|_on|time|day|week|month|quarter|year|period)$", name.lower()))


def latest_metrics(session: Session, workspace_id: str) -> dict[str, int]:
    """name -> newest approved (else newest) version, for the rules rung's "add the metric X"."""
    from analystos.db.models import SemanticMetric

    out: dict[str, tuple[int, int]] = {}
    for name, version, status in session.execute(select(SemanticMetric.name, SemanticMetric.version, SemanticMetric.status)
                                                 .where(SemanticMetric.workspace_id == workspace_id)):
        rank = (1 if status == "approved" else 0, version)
        if name not in out or rank > out[name]:
            out[name] = rank
    return {n: v for n, (_, v) in out.items()}


def op_env(session: Session, user: Any, workspace_id: str, doc: DashboardDoc) -> OpEnv:
    return OpEnv(actor=f"user:{user.id}", resolve_value=resolver(session, workspace_id, doc),
                 metric_lookup=metric_lookup(session, user, workspace_id))
