"""ABAC decisions (spec S01-T6): {classification, purpose, region, aggregation_grain, audience} decided on every tool
call (`ToolRuntime.authorize` through `policy.evaluate`), every governed action and every query
(`QueryGateway.execute`, before the result cache), not only at sign-in.

The attributes are derived here by code from server-side state (the resolved DataScope, column tags, the
validated SQL, the tool contract), never from a prompt or model output. Rules only deny:

* platform rules (always on): a caller whose region is known and outside the data residency is refused;
  restricted data never goes to an external audience;
* workspace rules (`WorkspacePolicyDoc.abac_rules`), versioned with the workspace policy.

`ANALYSTOS_POLICY_ENGINE=opa` adds an OPA sidecar after the built-in engine: OPA may deny what the built-in
engine allows, never allow what it denies. OPA unreachable, slow, or answering anything but a boolean `allow`
is a deny (fail closed). A denial is `403 policy_denied` with details `{rule, reason, remedy}`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from analystos.contracts.policy import (
    CLASSIFICATION_ORDER,
    AbacAttributes,
    AbacCondition,
    AbacDecision,
    AbacRule,
    AbacScope,
    Classification,
    DataScope,
    PolicyInput,
)
from analystos.core.errors import PolicyDenied
from analystos.core.logging import get_logger

_log = get_logger(__name__)

EXTERNAL_ACTIONS = frozenset({"publish", "export", "notify_external"})
# Column tags that set a classification; any other tag leaves the column `internal`.
TAG_CLASSES: dict[str, Classification] = {
    "public": "public", "internal": "internal", "confidential": "confidential", "sensitive": "confidential",
    "pii": "confidential", "restricted": "restricted",
}


# ------------------------------------------------------------------------------ attributes
def classify_tags(tags: Iterable[str] | None) -> Classification:
    """Highest class a column's tags give it; `public` only when every class tag says so."""
    classes = [TAG_CLASSES[t] for t in (str(x).lower() for x in (tags or ())) if t in TAG_CLASSES]
    return max(classes, key=CLASSIFICATION_ORDER.__getitem__) if classes else "internal"


def highest(classes: Iterable[str]) -> Classification:
    found = [c for c in classes if c in CLASSIFICATION_ORDER]
    return max(found, key=CLASSIFICATION_ORDER.__getitem__) if found else "internal"  # type: ignore[return-value]


def purpose_class(purpose: str | None) -> str:
    """`monitor.freshness` -> `monitor`, `federation.leg:qry_1` -> `federation`."""
    return re.split(r"[.:]", purpose or "analysis", maxsplit=1)[0] or "analysis"


def normalize_region(value: Any) -> str | None:
    """A user's `region` attribute (administrator or IdP mapped) as a lower-case code; anything else is unknown."""
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def scope_attributes(*, region: Any, residency: str | None, tags_by_column: dict[str, set[str]],
                     readable: Iterable[str], rules: list[AbacRule]) -> AbacScope:
    """The AbacScope resolve_scope attaches: classes only for readable columns that are not `internal`."""
    classes = {}
    for col in readable:
        cls = classify_tags(tags_by_column.get(col))
        if cls != "internal":
            classes[col] = cls
    return AbacScope(region=normalize_region(region), residency=(residency or None), column_classes=classes,
                     rules=list(rules))


def readable_columns(scope: DataScope) -> list[str]:
    denied = set(scope.denied_columns)
    return [f"{fq}.{c}" for fq, cols in scope.columns.items() for c in cols if f"{fq}.{c}" not in denied]


def scope_classification(scope: DataScope | None) -> Classification:
    """Highest class of anything the scope can read (a tool call may touch any of it). A column without an
    entry in `column_classes` is `internal`."""
    if scope is None or scope.abac is None:
        return "internal"
    classes = scope.abac.column_classes
    return highest(classes.get(col, "internal") for col in readable_columns(scope))


def query_classification(scope: DataScope, referenced_assets: list[str], referenced_columns: list[str]) -> Classification:
    """Highest class of the columns a statement reads. An asset referenced without a resolved column (for
    example `COUNT(*)`) counts with its highest readable column, so an unresolved reference never lowers it."""
    if scope.abac is None:
        return "internal"
    classes = {k.lower(): v for k, v in scope.abac.column_classes.items()}
    found = [classes.get(c.lower(), "internal") for c in referenced_columns]
    covered = {c.rsplit(".", 1)[0].lower() for c in referenced_columns}
    readable = [c.lower() for c in readable_columns(scope)]
    for asset in referenced_assets:
        if asset.lower() not in covered:
            prefix = asset.lower() + "."
            found += [classes.get(c, "internal") for c in readable if c.startswith(prefix)] or ["internal"]
    return highest(found)


def sql_grain(sql: str, dialect: str) -> str:
    """`aggregate` when the outermost SELECT groups or only computes aggregates (every UNION branch), else `row`.
    Syntactic by design: an unparseable statement is `row` (the stricter grain)."""
    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 - the validator has parsed it already; anything else is the strict grain
        return "row"

    def select_grain(node: Any) -> str:
        if isinstance(node, exp.Union):
            return "aggregate" if all(select_grain(n) == "aggregate" for n in (node.left, node.right)) else "row"
        if isinstance(node, exp.Subquery):
            return select_grain(node.this)
        if not isinstance(node, exp.Select):
            return "row"
        if node.args.get("group"):
            return "aggregate"
        kinds = [_kind(p) for p in node.expressions]
        if "aggregate" in kinds and set(kinds) <= {"aggregate", "constant"}:
            return "aggregate"
        return "row"

    def _kind(projection: Any) -> str:
        """aggregate | constant | row for one SELECT item (a window function is per row)."""
        inner = projection.this if isinstance(projection, exp.Alias) else projection
        if any(True for _ in inner.find_all(exp.Window, exp.Subquery, exp.Select)):
            return "row"  # a window or a scalar subquery is evaluated per row of the outer FROM
        has_agg = any(True for _ in inner.find_all(exp.AggFunc))
        if any(True for _ in _bare_columns(inner)) or isinstance(inner, exp.Star):
            return "row"
        return "aggregate" if has_agg else "constant"

    def _bare_columns(node: Any) -> Iterable[Any]:
        for col in node.find_all(exp.Column):
            if col.find_ancestor(exp.AggFunc) is None:
                yield col

    return select_grain(tree)


def audience_for(action: str, tool: Any | None) -> str:
    if action in EXTERNAL_ACTIONS or (tool is not None and getattr(tool, "side_effects", None) == "external_write"):
        return "external"
    return "workspace"


# ------------------------------------------------------------------------------ built-in engine
def _region_matches(region: str, value: str) -> bool:
    region, value = region.lower(), value.strip().lower()
    return region == value or region.startswith(value + "-")


def condition_holds(cond: AbacCondition, attrs: AbacAttributes) -> bool:
    value = getattr(attrs, cond.attribute)
    if cond.attribute == "classification":
        rank = CLASSIFICATION_ORDER[value]
        if cond.op == "at_least":
            return rank >= CLASSIFICATION_ORDER[cond.values[0]]
        if cond.op == "at_most":
            return rank <= CLASSIFICATION_ORDER[cond.values[0]]
        hit = value in cond.values
    elif cond.attribute == "region":
        hit = value is not None and any(_region_matches(value, v) for v in cond.values)
    elif cond.attribute == "purpose":
        hit = value in cond.values or purpose_class(value) in cond.values
    else:
        hit = value in cond.values
    return hit if cond.op == "in" else not hit


def _platform_rules(inp: PolicyInput) -> AbacDecision | None:
    a = inp.attributes
    if inp.residency and a.region and not _region_matches(a.region, inp.residency):
        return AbacDecision(allow=False, rule="platform.data_residency",
                            reason=f"caller region {a.region} is outside the data residency {inp.residency}",
                            remedy=f"act from a region inside {inp.residency}, or ask a tenant admin to review the "
                                   "workspace data residency")
    if a.classification == "restricted" and a.audience == "external":
        return AbacDecision(allow=False, rule="platform.restricted_external",
                            reason="restricted data cannot reach an external audience",
                            remedy="remove or aggregate the restricted columns before publishing or exporting")
    return None


def builtin_decide(inp: PolicyInput, rules: list[AbacRule]) -> AbacDecision:
    platform = _platform_rules(inp)
    if platform is not None:
        return platform
    for rule in rules:
        if inp.point in rule.applies_to and all(condition_holds(c, inp.attributes) for c in rule.when):
            return AbacDecision(allow=False, rule=rule.id, reason=rule.reason or f"workspace ABAC rule {rule.id}",
                                remedy=rule.remedy or "ask a workspace owner to review the ABAC rules")
    return AbacDecision(allow=True)


# ------------------------------------------------------------------------------ OPA adapter
Post = Callable[[str, dict[str, Any], float], Any]


def _httpx_post(url: str, body: dict[str, Any], timeout: float) -> Any:
    import httpx

    response = httpx.post(url, json=body, timeout=timeout)
    response.raise_for_status()
    return response.json()


class OpaEngine:
    """`POST {url}/v1/data/{path}` with `{"input": PolicyInput + workspace_rules}`. The document at `path` must
    be `{"allow": bool, "rule"?, "reason"?, "remedy"?}` (or a bare boolean). Anything else denies."""

    def __init__(self, url: str, path: str, timeout: float, post: Post | None = None) -> None:
        self.endpoint = f"{url.rstrip('/')}/v1/data/{path.strip('/')}"
        self.timeout = timeout
        self._post = post or _httpx_post

    def decide(self, inp: PolicyInput, rules: list[AbacRule]) -> AbacDecision:
        body = {"input": {**inp.model_dump(mode="json"), "workspace_rules": [r.model_dump(mode="json") for r in rules]}}
        try:
            answer = self._post(self.endpoint, body, self.timeout)
        except Exception as exc:  # noqa: BLE001 - any transport or HTTP failure fails closed
            _log.warning("OPA unreachable (%s); denying", exc.__class__.__name__)
            return AbacDecision(allow=False, rule="opa.unavailable", engine="opa",
                                reason=f"the policy engine could not be reached ({exc.__class__.__name__})",
                                remedy="retry later; an operator should check the OPA sidecar "
                                       "(ANALYSTOS_POLICY_OPA_URL)")
        result = answer.get("result") if isinstance(answer, dict) else None
        if isinstance(result, bool):
            result = {"allow": result}
        if not isinstance(result, dict) or not isinstance(result.get("allow"), bool):
            return AbacDecision(allow=False, rule="opa.invalid_decision", engine="opa",
                                reason="the policy engine returned no boolean allow at "
                                       f"{self.endpoint.split('/v1/data/', 1)[1]}",
                                remedy="an operator should check the OPA policy bundle")
        if result["allow"]:
            return AbacDecision(allow=True, engine="opa")
        return AbacDecision(allow=False, engine="opa", rule=str(result.get("rule") or "opa.deny")[:120],
                            reason=str(result.get("reason") or "denied by the OPA policy")[:500],
                            remedy=str(result.get("remedy") or "ask a tenant admin to review the OPA policy")[:500])


_OPA_OVERRIDE: OpaEngine | None = None


def use_opa(engine: OpaEngine | None) -> None:
    """Replace the OPA engine (tests); None = build it from settings."""
    global _OPA_OVERRIDE
    _OPA_OVERRIDE = engine


def _opa(settings: Any) -> OpaEngine:
    if _OPA_OVERRIDE is not None:
        return _OPA_OVERRIDE
    return OpaEngine(settings.policy_opa_url, settings.policy_opa_decision_path, settings.policy_opa_timeout_seconds)


def decide(inp: PolicyInput, rules: list[AbacRule] | None = None) -> AbacDecision:
    """Built-in first; with `policy_engine=opa`, OPA too (it can only add a denial)."""
    from analystos.core.config import get_settings

    rules = list(rules or [])
    builtin = builtin_decide(inp, rules)
    settings = get_settings()
    if not builtin.allow or getattr(settings, "policy_engine", "builtin") != "opa":
        return builtin
    return _opa(settings).decide(inp, rules)


def denial(decision: AbacDecision, *, what: str) -> PolicyDenied:
    """`403 policy_denied {rule, reason, remedy}`."""
    return PolicyDenied(f"{what} denied by ABAC rule {decision.rule}: {decision.reason}",
                        details={"rule": decision.rule, "reason": decision.reason, "remedy": decision.remedy,
                                 "engine": decision.engine})


def decide_query(scope: DataScope, *, tenant_id: str | None, source_id: str | None, sql: str, dialect: str,
                 referenced_assets: list[str], referenced_columns: list[str], purpose: str) -> AbacDecision:
    abac = scope.abac or AbacScope()
    attrs = AbacAttributes(classification=query_classification(scope, referenced_assets, referenced_columns),
                           purpose=purpose or "analysis", region=abac.region, aggregation_grain=sql_grain(sql, dialect),
                           audience="workspace")
    inp = PolicyInput(point="query", action="query", tenant_id=tenant_id, workspace_id=scope.workspace_id,
                      user_id=scope.user_id, role=scope.role, source_id=source_id, assets=list(referenced_assets),
                      residency=abac.residency, policy_version=scope.policy_version, attributes=attrs)
    return decide(inp, abac.rules)
