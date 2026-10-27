"""Governance engine (§45-§46). Authorization is decided here, on the server, from persisted state.
Prompts, retrieved context and model output never widen what this returns."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.policy import AttributeRule, DataScope, ExecutionIdentity, PolicyDecision, WorkspacePolicyDoc
from analystos.contracts.registry import ToolSpec
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Forbidden, NotFound
from analystos.db.models import Source, SourceAsset, SourceColumn, User, Workspace, WorkspaceMember, WorkspacePolicy
from analystos.governance.audit import audit
from analystos.security.auth import APPROVER_ROLES, role_at_least

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")


def source_dialect(kind: str, execution_mode: str | None) -> str:
    """Dialect the gateway validates a source's SQL in (config/source_kinds.yaml); staged -> postgres."""
    from analystos.connectors.kinds import dialect_for

    try:
        return dialect_for(kind, execution_mode)
    except AnalystOSError:
        return "postgres"

# Autonomy ceiling in the initial release (§39): publication, scheduling, external notification
# and source mutation always need an approval regardless of workspace autonomy level.
ALWAYS_APPROVAL_ACTIONS = {"publish", "schedule", "notify_external", "export", "build"}
NEVER_ALLOWED_ACTIONS = {"source_mutation"}


def get_workspace(session: Session, workspace_id: str, *, allow_disabled: bool = False,
                  allow_archived: bool = False) -> Workspace:
    ws = session.get(Workspace, workspace_id)
    if ws is None or (ws.deleted_at is not None and not allow_archived) or (ws.status != "active" and not allow_disabled):
        raise NotFound(f"workspace {workspace_id} not found")
    return ws


def member_role(session: Session, user: User, workspace_id: str) -> str | None:
    if user.is_admin:
        return "owner"
    return session.scalar(select(WorkspaceMember.role).where(
        WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id))


def require_role(session: Session, user: User, workspace_id: str, minimum: str, *, allow_disabled: bool = False,
                 allow_archived: bool = False) -> str:
    get_workspace(session, workspace_id, allow_disabled=allow_disabled, allow_archived=allow_archived)
    role = member_role(session, user, workspace_id)
    if role is None:
        # Do not reveal existence of workspaces the caller cannot see.
        raise NotFound(f"workspace {workspace_id} not found")
    if not role_at_least(role, minimum) and not (minimum == "approver" and role in APPROVER_ROLES):
        raise Forbidden(f"role '{role}' cannot perform this action (needs {minimum})")
    return role


SCOPED_LOADERS: set[str] = set()


def scoped_loader(fn: Callable[P, R]) -> Callable[P, R]:
    """Mark a helper that loads a workspace child through `load_in_workspace` (tests/unit/
    test_route_workspace_binding.py accepts a route that calls one; it checks the helper does)."""
    SCOPED_LOADERS.add(f"{fn.__module__}.{fn.__qualname__}")
    return fn


def load_in_workspace(session: Session, model: type[T], obj_id: str | None, workspace_id: str | None = None, *,
                      user: User, minimum: str = "viewer", label: str | None = None,
                      workspace_of: Callable[[Session, Any], str | None] | None = None,
                      shared: bool = False, for_update: bool = False) -> T:
    """The one way an API path reaches a workspace child (run, insight, artifact, approval, thread...).

    The child's own workspace decides, never the URL: with `workspace_id` (the path's) a child of any
    other workspace is a 404, even for a caller who is a member of both; without it the caller's role
    is checked in the child's workspace. Every "absent" case — unknown id, other workspace, not a
    member, deleted workspace — is the same 404 with the same message, so a response never tells a
    caller that something exists elsewhere. Only a member below `minimum` sees 403 (with a path
    workspace, before the child is looked up, as a plain role check would). `shared` admits a
    platform-wide child (workspace NULL, e.g. the platform knowledge pack), checked against the path's
    workspace."""
    what = label or getattr(model, "__tablename__", model.__name__).replace("_", " ")
    missing = NotFound(f"{what} not found")
    if workspace_id is not None:  # the path's workspace first: a member below `minimum` learns nothing about the child
        try:
            require_role(session, user, workspace_id, minimum)
        except NotFound:
            raise missing from None
    obj = session.get(model, obj_id, with_for_update=for_update) if obj_id else None
    if obj is None:
        raise missing
    owner = workspace_of(session, obj) if workspace_of is not None else getattr(obj, "workspace_id", None)
    if owner is None and shared and workspace_id is not None:
        owner = workspace_id
    if owner is None or (workspace_id is not None and owner != workspace_id):
        raise missing
    if workspace_id is None:
        try:
            require_role(session, user, owner, minimum)
        except NotFound:
            raise missing from None
    return obj


def load_policy(session: Session, workspace: Workspace) -> WorkspacePolicyDoc:
    row = session.scalar(select(WorkspacePolicy).where(
        WorkspacePolicy.workspace_id == workspace.id, WorkspacePolicy.version == workspace.policy_version))
    doc = WorkspacePolicyDoc.model_validate(row.document) if row else WorkspacePolicyDoc()
    settings = get_settings()
    # Platform ceilings: a workspace can tighten, never loosen.
    doc.max_rows = min(doc.max_rows, settings.query_max_rows)
    doc.query_timeout_seconds = min(doc.query_timeout_seconds, settings.query_timeout_seconds)
    doc.publish_requires_approval = True
    return doc


def save_policy(session: Session, workspace: Workspace, doc: WorkspacePolicyDoc, user_id: str) -> int:
    version = (session.scalar(select(WorkspacePolicy.version).where(WorkspacePolicy.workspace_id == workspace.id)
                              .order_by(WorkspacePolicy.version.desc()).limit(1)) or 0) + 1
    session.add(WorkspacePolicy(workspace_id=workspace.id, version=version, document=doc.model_dump(), created_by=user_id))
    workspace.policy_version = version
    session.flush()
    from analystos.evidence.verification import dependency_changed

    # P7-01: a verdict whose scope, masking or alpha came from the old policy is void if those changed.
    dependency_changed(session, "policy", workspace.id, f"workspace policy v{version} saved", event="policy.changed")
    return version


def _matches(pattern: str, fq: str) -> bool:
    if pattern.startswith("*."):
        return fq.rsplit(".", 1)[-1] == pattern[2:]
    return pattern == fq


PII_ORDER = {"none": 0, "restricted": 1, "allowed": 2}


def _selected_assets(session: Session, source_ids: list[str]) -> tuple[dict[str, list[tuple[str, str]]],
                                                                       dict[str, list[tuple[str, list[str] | None]]]]:
    """Selected assets per source as (asset id, `schema.name`) in name order and their columns in ordinal order as
    (name, tags): two reads for any number of sources and assets (P4-S02; was one per source and asset)."""
    if not source_ids:
        return {}, {}
    in_scope = (SourceAsset.source_id.in_(source_ids), SourceAsset.selected.is_(True))
    assets: dict[str, list[tuple[str, str]]] = {}
    for asset_id, source_id, schema_name, name in session.execute(
            select(SourceAsset.id, SourceAsset.source_id, SourceAsset.schema_name, SourceAsset.name).where(*in_scope)
            .order_by(SourceAsset.schema_name, SourceAsset.name, SourceAsset.id)):
        assets.setdefault(source_id, []).append((asset_id, f"{schema_name}.{name}"))
    columns: dict[str, list[tuple[str, list[str] | None]]] = {}
    for asset_id, name, tags in session.execute(
            select(SourceColumn.asset_id, SourceColumn.name, SourceColumn.tags)
            .join(SourceAsset, SourceAsset.id == SourceColumn.asset_id).where(*in_scope)
            .order_by(SourceColumn.asset_id, SourceColumn.ordinal, SourceColumn.id)):
        columns.setdefault(asset_id, []).append((name, tags))
    return assets, columns


def has_pii_clearance(attributes: dict | None) -> bool:
    """Strictly the boolean True an administrator set: a string such as "false" (or "true") from any
    other path is not a clearance. Identity-provider claims can never set it (security/oidc.py)."""
    return (attributes or {}).get("pii_clearance") is True


def attributes_satisfy(attributes: dict | None, require: dict[str, list[str]]) -> bool:
    """Every required attribute present with an accepted value (list-valued attributes: any element)."""
    attributes = attributes or {}
    for key, accepted in require.items():
        value = attributes.get(key)
        values = value if isinstance(value, list) else [value]
        if not {str(v) for v in values if v is not None} & {str(a) for a in accepted}:
            return False
    return True


def apply_attribute_rules(scope: DataScope, attributes: dict | None, rules: list[AttributeRule],
                          column_tags: dict[str, set[str]] | None = None) -> list[str]:
    """ABAC (SEC-003) on a resolved scope: every rule the caller's attributes do not satisfy removes
    its assets and denies its columns. Only ever narrows. Returns the ids of the rules applied."""
    applied = []
    column_tags = column_tags or {}
    for rule in rules:
        if attributes_satisfy(attributes, rule.require):
            continue
        applied.append(rule.id or "unnamed")
        dropped = [fq for fq in scope.assets if any(_matches(p, fq) for p in rule.assets)]
        for fq in dropped:
            scope.assets.remove(fq)
            scope.asset_sources.pop(fq, None)
            scope.columns.pop(fq, None)
        for fq, cols in scope.columns.items():
            for name in cols:
                col_fq = f"{fq}.{name}"
                if col_fq in scope.denied_columns:
                    continue
                if any(_matches(p, col_fq) for p in rule.columns) or (column_tags.get(col_fq, set()) & set(rule.column_tags)):
                    scope.denied_columns.append(col_fq)
        scope.denied_columns = [c for c in scope.denied_columns if c.rsplit(".", 1)[0] in scope.columns]
    return applied


def resolve_scope(session: Session, user: User, workspace_id: str, *, source_ids: list[str] | None = None,
                  minimum_role: str = "analyst", pii_access: str | None = None) -> DataScope:
    """Server-side authorized data scope for this caller (§12.2). Selected assets only.
    `pii_access` (an agent manifest's policy) can only tighten the workspace policy."""
    workspace = get_workspace(session, workspace_id)  # held, so require_role's lookup hits the identity map
    role = require_role(session, user, workspace_id, minimum_role)
    policy = load_policy(session, workspace)
    if pii_access is not None and PII_ORDER[pii_access] < PII_ORDER[policy.pii_access]:
        policy.pii_access = pii_access
    sources = list(session.scalars(select(Source).where(Source.workspace_id == workspace_id, Source.status == "ready")
                                   .order_by(Source.created_at, Source.name, Source.id)))
    if source_ids:
        sources = [s for s in sources if s.id in source_ids]
    pii_cleared = policy.pii_access == "allowed" or (
        policy.pii_access == "restricted" and has_pii_clearance(user.attributes))
    scope = DataScope(workspace_id=workspace_id, user_id=user.id, role=role, max_rows=policy.max_rows,
                      timeout_seconds=policy.query_timeout_seconds, policy_version=workspace.policy_version)
    assets_by_source, cols_by_asset = _selected_assets(session, [s.id for s in sources])
    tags_by_column: dict[str, set[str]] = {}
    for source in sources:
        scope.source_ids.append(source.id)
        scope.source_dialects[source.id] = source_dialect(source.kind, source.execution_mode)
        for asset_id, fq in assets_by_source.get(source.id, []):
            scope.assets.append(fq)
            scope.asset_sources[fq] = source.id
            cols = cols_by_asset.get(asset_id, [])
            scope.columns[fq] = [name for name, _ in cols]
            for name, col_tags in cols:
                col_fq = f"{fq}.{name}"
                tags = set(col_tags or [])
                tags_by_column[col_fq] = tags
                restricted = "restricted" in tags or any(_matches(p, col_fq) for p in policy.restricted_columns)
                pii = "pii" in tags or any(_matches(p, col_fq) for p in policy.pii_columns)
                if restricted or (pii and not pii_cleared):
                    scope.denied_columns.append(col_fq)
    if policy.attribute_rules:
        apply_attribute_rules(scope, user.attributes, policy.attribute_rules, tags_by_column)
    if policy.row_filters:
        apply_row_filters(scope, user, policy.row_filters)
    from analystos.governance import abac

    scope.abac = abac.scope_attributes(region=(user.attributes or {}).get("region"),
                                       residency=data_residency(session, workspace, policy),
                                       tags_by_column=tags_by_column, readable=abac.readable_columns(scope),
                                       rules=policy.abac_rules)
    return scope


def data_residency(session: Session, workspace: Workspace, policy: WorkspacePolicyDoc) -> str | None:
    """The workspace policy's residency, else its tenant's (S01 `tenant.data_residency`)."""
    if policy.data_residency:
        return policy.data_residency
    tenant_id = getattr(workspace, "tenant_id", None)
    if not tenant_id:
        return None
    from analystos.db.models import Tenant

    tenant = session.get(Tenant, tenant_id)
    return tenant.data_residency if tenant is not None else None


def row_filter_user(user: User, role: str) -> dict:
    """What `{{user.<attr>}}` can name: the caller's attributes plus id, email and workspace role."""
    return {**(user.attributes or {}), "id": user.id, "email": user.email, "role": role}


def apply_row_filters(scope: DataScope, user: User, filters: list) -> None:
    """Render every row filter that applies to an asset in scope for this caller (P7-02). Fail closed: an
    attribute the caller lacks, or a predicate over a column the asset does not have, withholds the
    asset from the scope instead of reading it unfiltered."""
    from analystos.governance import row_filters as rls

    who = row_filter_user(user, scope.role)
    for fq in list(scope.assets):
        dialect = scope.source_dialects.get(scope.asset_sources.get(fq, ""), "postgres")
        rendered, reason = [], None
        for f in filters:
            if not rls.applies(f.assets, fq) or scope.role in f.exempt_roles:
                continue
            try:
                sql, cols = rls.render(f.predicate, who, dialect, filter_id=f.id)
            except (rls.MissingAttribute, ValueError) as exc:
                reason = str(exc)
                break
            known = {c.lower() for c in scope.columns.get(fq, [])}
            unknown = [c for c in cols if c.lower() not in known]
            if unknown:
                reason = f"row filter {f.id} reads {', '.join(unknown)}, which {fq} does not have"
                break
            rendered.append(sql)
        if reason is not None:
            scope.assets.remove(fq)
            scope.asset_sources.pop(fq, None)
            scope.columns.pop(fq, None)
            scope.withheld_assets[fq] = reason
        elif rendered:
            scope.row_filters[fq] = rendered
    scope.denied_columns = [c for c in scope.denied_columns if c.rsplit(".", 1)[0] in scope.columns]


def evaluate(session: Session, user: User, identity: ExecutionIdentity, action: str, *,
             tool: ToolSpec | None = None, destination: str | None = None, autonomy_level: int | None = None,
             record: bool = True, scope: DataScope | None = None) -> PolicyDecision:
    """allow | deny | approval_required with machine-readable reasons. Re-evaluated at execution time.
    ABAC (S01-T6) is decided after the role and autonomy checks, on every call: a denial adds the reason
    `abac:<rule>` and `details.abac = {rule, reason, remedy, engine}` (`abac.denial` builds the 403)."""
    workspace = get_workspace(session, identity.workspace_id)
    policy = load_policy(session, workspace)
    role = member_role(session, user, identity.workspace_id)
    level = workspace.autonomy_level if autonomy_level is None else min(autonomy_level, workspace.autonomy_level)
    reasons: list[str] = []
    decision = "allow"
    risk = "low"

    if role is None or not user.active:
        decision, reasons = "deny", ["not_a_workspace_member"]
    elif action in NEVER_ALLOWED_ACTIONS:
        decision, reasons, risk = "deny", ["source_systems_are_read_only_in_this_release"], "high"
    else:
        minimum = tool.min_role if tool else ("editor" if action in ALWAYS_APPROVAL_ACTIONS else "analyst")
        if not role_at_least(role, minimum):
            decision, reasons = "deny", [f"role_{role}_below_{minimum}"]
        if tool is not None:
            risk = tool.risk
            if tool.tool_id in policy.tool_denylist:
                decision, reasons = "deny", [*reasons, "tool_denied_by_workspace_policy"]
        if action in {"publish", "export"} and destination and destination not in policy.publish_destinations:
            decision, reasons, risk = "deny", [*reasons, f"destination_{destination}_not_allowed"], "high"
        if decision == "allow":
            if action in ALWAYS_APPROVAL_ACTIONS or (tool and tool.approval_policy == "always") or \
                    (tool and tool.approval_policy == "publish_only" and action == "publish"):
                decision, risk = "approval_required", "high" if action == "publish" else "medium"
                reasons.append("initial_release_requires_approval_for_external_side_effects")
            elif level <= 0 and action in {"run_analysis", "tool"}:
                decision, reasons = "deny", ["autonomy_level_0_manual_only"]
            elif level == 1 and action == "run_analysis":
                decision = "approval_required"
                reasons.append("autonomy_level_1_agent_recommends_only")
            elif level == 2 and action == "run_analysis":
                decision = "approval_required"
                reasons.append("autonomy_level_2_executes_after_plan_approval")
            else:
                reasons.append(f"within_policy_autonomy_{level}")
    details: dict[str, Any] = {"role": role, "action": action, "tool": tool.tool_id if tool else None,
                               "destination": destination, "agent": identity.agent_id, "purpose": identity.purpose}
    if decision != "deny":
        verdict = _abac(session, user, identity, action, workspace=workspace, policy=policy, role=role, tool=tool,
                        scope=scope)
        details["abac"] = verdict.model_dump(exclude_none=True)
        if not verdict.allow:
            decision, reasons = "deny", [*reasons, f"abac:{verdict.rule}"]
    result = PolicyDecision(decision=decision, reasons=reasons, risk_tier=risk, policy_version=workspace.policy_version,
                            effective_autonomy=level, details=details)
    if record:
        audit(f"user:{user.id}" if not identity.agent_id else f"agent:{identity.agent_id}", f"policy.{action}",
              workspace_id=identity.workspace_id, run_id=identity.run_id, target=tool.tool_id if tool else destination,
              decision=decision, reasons=reasons, details=result.details, session=session)
    return result


def denied(decision: PolicyDecision, message: str) -> Forbidden:
    """The error for a denied decision: `403 policy_denied {rule, reason, remedy}` when ABAC denied it."""
    from analystos.core.errors import PolicyDenied

    verdict = decision.details.get("abac") or {}
    if verdict.get("allow") is False:
        return PolicyDenied(message, details={k: verdict.get(k) for k in ("rule", "reason", "remedy", "engine")})
    return PolicyDenied(message)


def _abac(session: Session, user: User, identity: ExecutionIdentity, action: str, *, workspace: Workspace,
          policy: WorkspacePolicyDoc, role: str | None, tool: ToolSpec | None, scope: DataScope | None):
    """The ABAC decision for a tool call or action. A scope (a run step, an Ask turn) gives the classification
    of what the call can read and the region and residency it was resolved with; without one the caller's
    region attribute and the workspace residency are used, and the data counts as `internal`."""
    from analystos.contracts.policy import AbacAttributes, PolicyInput
    from analystos.governance import abac

    facts = scope.abac if scope is not None else None
    region = facts.region if facts is not None else abac.normalize_region((user.attributes or {}).get("region"))
    residency = facts.residency if facts is not None else data_residency(session, workspace, policy)
    attrs = AbacAttributes(classification=abac.scope_classification(scope), purpose=identity.purpose or "analysis",
                           region=region, aggregation_grain="none", audience=abac.audience_for(action, tool))
    inp = PolicyInput(point="tool" if tool is not None else "action", action=action,
                      tenant_id=getattr(workspace, "tenant_id", None), workspace_id=identity.workspace_id,
                      user_id=user.id, role=role, agent_id=identity.agent_id, tool_id=tool.tool_id if tool else None,
                      source_id=identity.source_id, assets=list(scope.assets) if scope is not None else [],
                      residency=residency, policy_version=workspace.policy_version, attributes=attrs)
    return abac.decide(inp, policy.abac_rules)
