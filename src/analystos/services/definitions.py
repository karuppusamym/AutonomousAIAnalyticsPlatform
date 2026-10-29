"""Definition lifecycle (ADR-0021, P7-03): draft → (tested) → published → (deprecated) → retired.

Generic over `kind`: a kind registers a validator that normalizes its spec, so recipes, ML specs and
query tools reuse the same versions, hashes, status checks and events. A published version is
immutable; editing makes a new draft (version = next number). Triggers (API, schedules, monitors,
MCP) resolve what they run through `resolve` + `require_runnable`: drafts only in a workspace whose
settings say `environment: dev`, retired never. Built-in and pack YAML playbooks resolve as
published, identified by manifest version plus content digest (`builtin_ref`).

A kind may register a *tester* (P7-11 query tools): `test` runs the draft once and, when it passes, marks it
`tested` with the evidence bound to its content hash; for such kinds publish requires a tested draft whose
content has not changed since. An edit of a tested draft makes it a draft again. A tested draft is still a
draft for triggers (it runs only in a dev workspace).

Playbooks (S06-T6) are tested by the simulator (`autonomy/simulator.py`: a dry run against fixture workspaces,
no SQL or side effects) and published only signed: an approved, hash-bound `playbook.sign` approval, verified
immediately before the tenant key signs the version (`autonomy/playbook_signing.py`). Triggers refuse a
published playbook whose signature does not verify.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analystos.contracts.capability import CapabilityManifest
from analystos.contracts.definition import RUNNABLE_STATUSES, DefinitionDraftIn, DefinitionPatch, DefinitionRef
from analystos.core.errors import Conflict, InvalidInput, NotFound, PolicyDenied, PreconditionFailed
from analystos.core.ids import new_id, stable_hash, utcnow
from analystos.db.models import Definition, User, Workspace
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role

Validator = Callable[[Session, str, str, dict[str, Any]], dict[str, Any]]
Tester = Callable[[Session, User, Definition, dict[str, Any] | None], dict[str, Any]]
_KINDS: dict[str, Validator] = {}
_TESTERS: dict[str, Tester] = {}
EDITABLE = ("draft", "tested")
# Kinds whose authoring needs more than an editor: an agent definition composes workspace grants (P7-19).
_AUTHOR_ROLE: dict[str, str] = {"agent": "owner"}


def author_role(kind: str) -> str:
    return _AUTHOR_ROLE.get(kind, "editor")


def register_kind(kind: str, validator: Validator | None = None) -> None:
    """A new definition kind (recipe, ML spec, query tool...). The validator returns the normalized
    spec or raises InvalidInput; without one any JSON object is accepted as-is."""
    _KINDS[kind] = validator or (lambda _s, _w, _k, spec: dict(spec))


def register_tester(kind: str, tester: Tester) -> None:
    """A kind whose drafts must pass `test` (returning evidence, or raising) before they can be published."""
    _TESTERS[kind] = tester


def kinds() -> list[str]:
    return sorted(_KINDS)


# ------------------------------------------------------------------------------------ hashing / refs
def manifest_digest(manifest: CapabilityManifest | dict[str, Any]) -> str:
    """Content digest of a manifest. `source` (where the file was found) is not content."""
    raw = manifest.model_dump(mode="json") if isinstance(manifest, CapabilityManifest) else dict(manifest)
    return stable_hash({k: v for k, v in raw.items() if k != "source"})


def content_hash(kind: str, spec: dict[str, Any]) -> str:
    return stable_hash({"kind": kind, "spec": spec})


def builtin_ref(manifest: CapabilityManifest) -> DefinitionRef:
    """Built-in and pack YAML is published by definition: manifest version plus content digest."""
    return DefinitionRef(kind=manifest.kind.lower(), key=manifest.id, version=manifest.version,
                         content_hash=manifest_digest(manifest), source="builtin",
                         status="deprecated" if manifest.certification.status == "deprecated" else "published")


def ref_of(row: Definition) -> DefinitionRef:
    return DefinitionRef(kind=row.kind, key=row.key, version=row.version, content_hash=row.content_hash, source="workspace",
                         status=row.status, id=row.id)


def is_dev(session: Session, workspace_id: str) -> bool:
    ws = session.get(Workspace, workspace_id)
    return ws is not None and str((ws.settings or {}).get("environment", "")).lower() == "dev"


# ------------------------------------------------------------------------------------ kinds
def _playbook(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """A workspace playbook is a `kind: Playbook` manifest composing registered agents; its references
    are checked against the current registry exactly as a pack's would be at load."""
    from pydantic import ValidationError

    from analystos.capabilities import registry
    from analystos.capabilities.validation import validate_kinds

    source = f"workspace:{workspace_id}"
    try:
        m = CapabilityManifest.model_validate({**spec, "source": source})
    except ValidationError as exc:
        raise InvalidInput("spec is not a capability manifest: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    if m.kind != "Playbook":
        raise InvalidInput("a playbook definition's spec must be a kind: Playbook manifest")
    if m.id != key:
        raise InvalidInput(f"the manifest id ({m.id}) must equal the definition key ({key})")
    from analystos.capabilities.agent_forms import grant_problems, playbook_uses, workspace_agents

    snap = registry.current()
    where = f"{source}: {m.id}"
    agents = workspace_agents(session, workspace_id, playbook_uses(m))  # the workspace's own agents (P7-19)
    problems = [p for p in validate_kinds({**snap.manifests, **agents, m.id: m}) if p.startswith(where)]
    problems += [f"{where}: agent {a.id}: {p}" for a in agents.values() for p in grant_problems(session, workspace_id, a)]
    if problems:
        raise InvalidInput("playbook does not validate: " + "; ".join(problems[:5]), details={"problems": problems})
    return m.model_dump(mode="json", exclude={"source"})


def _saved_analysis(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    missing = {"turn_id", "sql", "fingerprint"} - set(spec)
    if missing:
        raise InvalidInput(f"a saved analysis needs {sorted(missing)}")
    return dict(spec)


def _typed(model: Any) -> Validator:
    def check(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
        from pydantic import ValidationError

        try:
            return model.model_validate(spec).model_dump(mode="json")
        except ValidationError as exc:
            raise InvalidInput("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    return check


def _recipe(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    from analystos.contracts.recipe import validate_recipe

    return validate_recipe(spec).stamped()


def _analysis_context(session: Session, workspace_id: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError
    from sqlalchemy import select

    from analystos.contracts.analysis_context import AnalysisContextSpec
    from analystos.db.models import SemanticMetric, Source
    from analystos.security.injection import is_injection

    try:
        data = AnalysisContextSpec.model_validate(spec)
    except ValidationError as exc:
        raise InvalidInput("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from None
    for field in ("purpose", "business_description", "question_template"):
        if is_injection(getattr(data, field)):
            raise InvalidInput(f"analysis context {field} contains instructions unrelated to the business context")
    if len(set(data.source_ids)) != len(data.source_ids):
        raise InvalidInput("an analysis context cannot list a source more than once")
    found = set(session.scalars(select(Source.id).where(Source.workspace_id == workspace_id,
                                                        Source.id.in_(data.source_ids))))
    if found != set(data.source_ids):
        raise InvalidInput("analysis context sources must exist in this workspace")
    if len(set(data.metric_names)) != len(data.metric_names):
        raise InvalidInput("an analysis context cannot list a metric more than once")
    if data.metric_names:
        approved = set(session.scalars(select(SemanticMetric.name).where(
            SemanticMetric.workspace_id == workspace_id, SemanticMetric.status == "approved",
            SemanticMetric.name.in_(data.metric_names))))
        if approved != set(data.metric_names):
            raise InvalidInput("analysis context metrics must be approved in this workspace")
    return data.model_dump(mode="json")


def _simulate_playbook(session: Session, user: User, row: Definition, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """The playbook tester: simulate the draft against fixture workspaces (default: `supervised`). It passes when
    no fixture is refused or fails; the summaries (digests) become the evidence the signature approval binds."""
    from analystos.autonomy import simulator

    names = (arguments or {}).get("fixtures") or ["supervised"]
    if not isinstance(names, list) or len(names) > 10:
        raise InvalidInput("fixtures is a list of up to 10 fixture names or objects")
    reports = [simulator.simulate(dict(row.spec), n, session=session, workspace_id=row.workspace_id, workspace_source=True)
               for n in names]
    failing = [r for r in reports if r["outcome"] not in simulator.PASSING]
    if failing:
        raise InvalidInput("simulation did not pass: " + "; ".join(f"{r['fixture']['name']}: {r['outcome']} ({r.get('reason')})"
                                                               for r in failing),
                           details={"reports": [simulator.summary(r) for r in reports]})
    return {"kind": "simulation", "simulations": [simulator.summary(r) for r in reports]}


def _register_builtin_kinds() -> None:
    from analystos.contracts.work import MLScoringSpec, MLSpec, PipelineSpec

    register_kind("playbook", _playbook)
    register_kind("saved_analysis", _saved_analysis)
    register_kind("analysis_context", _analysis_context)
    register_kind("recipe", _recipe)  # the recipe IR (P6-04)
    register_kind("pipeline", _typed(PipelineSpec))  # P6-01
    register_kind("ml_spec", _typed(MLSpec))  # P5-01 replaces the placeholder contract
    register_kind("ml_scoring", _typed(MLScoringSpec))  # P5-03: approved batch scoring pinned to a model version
    from analystos.tools import query_tools

    register_kind("query_tool", query_tools.validate_spec)  # P7-11: draft -> tested -> published -> retired
    register_tester("query_tool", query_tools.test)
    from analystos.capabilities import agent_forms

    register_kind("agent", agent_forms.validate_definition)  # P7-19: a declarative agent authored in the workspace
    register_tester("playbook", _simulate_playbook)  # S06-T6: a playbook publishes only after its simulation passes


_register_builtin_kinds()


def _validate(session: Session, workspace_id: str, kind: str, key: str, spec: dict[str, Any]) -> dict[str, Any]:
    if kind not in _KINDS:
        raise InvalidInput(f"unknown definition kind {kind}; one of {kinds()}")
    if not isinstance(spec, dict):
        raise InvalidInput("spec must be an object")
    return _KINDS[kind](session, workspace_id, key, spec)


# ------------------------------------------------------------------------------------ lifecycle
def _latest_version(session: Session, workspace_id: str, kind: str, key: str) -> int:
    return session.scalar(select(func.max(Definition.version)).where(
        Definition.workspace_id == workspace_id, Definition.kind == kind, Definition.key == key)) or 0


def next_version(session: Session, workspace_id: str, kind: str, key: str) -> int:
    """The version number a new draft of `key` gets."""
    return _latest_version(session, workspace_id, kind, key) + 1


def latest_published(session: Session, workspace_id: str, kind: str, key: str) -> Definition | None:
    return session.scalar(select(Definition).where(
        Definition.workspace_id == workspace_id, Definition.kind == kind, Definition.key == key,
        Definition.status.in_(RUNNABLE_STATUSES)).order_by(Definition.version.desc()).limit(1))


def _check_revision(row: Definition, expected: int | None) -> None:
    if expected is not None and expected != row.revision:
        raise PreconditionFailed(f"definition {row.id} is at revision {row.revision}, not {expected}",
                                 details={"current_revision": row.revision})


def _event(session: Session, row: Definition, type_: str, actor: str, **extra: Any) -> None:
    payload = {"definition_id": row.id, "kind": row.kind, "key": row.key, "version": row.version,
               "content_hash": row.content_hash, "status": row.status, **extra}
    emit(row.workspace_id, type_, payload, actor=actor, session=session)
    audit(actor, type_, workspace_id=row.workspace_id, target=row.id, details=payload, session=session)


def create_draft(session: Session, user: User, workspace_id: str, body: DefinitionDraftIn) -> Definition:
    require_role(session, user, workspace_id, author_role(body.kind))
    spec = _validate(session, workspace_id, body.kind, body.key, body.spec)
    existing = session.scalar(select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == body.kind,
                                                       Definition.key == body.key, Definition.status.in_(EDITABLE)))
    if existing is not None:
        raise Conflict(f"{body.kind} {body.key} already has a draft ({existing.id}); edit it instead",
                       details={"draft_id": existing.id, "revision": existing.revision})
    row = Definition(id=new_id("defn"), workspace_id=workspace_id, kind=body.kind, key=body.key,
                     version=_latest_version(session, workspace_id, body.kind, body.key) + 1, status="draft",
                     title=body.title, spec=spec, content_hash=content_hash(body.kind, spec), revision=1,
                     created_by=user.id)
    session.add(row)
    session.flush()
    _event(session, row, "definition.draft_saved", f"user:{user.id}")
    return row


def update_draft(session: Session, user: User, row: Definition, patch: DefinitionPatch, expected_revision: int | None) -> Definition:
    require_role(session, user, row.workspace_id, author_role(row.kind))
    if row.status not in EDITABLE:
        raise Conflict(f"version {row.version} is {row.status} and immutable; create a new draft to change it")
    _check_revision(row, expected_revision)
    if patch.spec is not None:
        row.spec = _validate(session, row.workspace_id, row.kind, row.key, patch.spec)
        row.content_hash = content_hash(row.kind, row.spec)
        if row.status == "tested" and (row.test_evidence or {}).get("content_hash") != row.content_hash:
            row.status = "draft"  # the test covered other content
        if row.kind == "playbook" and (row.signature or {}).get("content_hash") not in (None, row.content_hash):
            from analystos.autonomy.playbook_signing import invalidate

            invalidate(session, row, "the playbook changed after its signature was requested")
    if patch.title is not None:
        row.title = patch.title
    row.revision += 1
    _event(session, row, "definition.draft_saved", f"user:{user.id}")
    return row


def publish(session: Session, user: User, row: Definition, expected_revision: int | None, *,
            actor: str | None = None) -> Definition:
    """Freeze the draft. Re-validated against the registry as it is now, so a draft that went stale
    (an agent it uses was removed) cannot be published."""
    require_role(session, user, row.workspace_id, author_role(row.kind))
    if row.status not in EDITABLE:
        raise Conflict(f"version {row.version} is already {row.status}")
    _check_revision(row, expected_revision)
    row.spec = _validate(session, row.workspace_id, row.kind, row.key, row.spec)
    row.content_hash = content_hash(row.kind, row.spec)
    if row.kind in _TESTERS and (row.status != "tested" or (row.test_evidence or {}).get("content_hash") != row.content_hash):
        raise Conflict(f"{row.kind} {row.key} v{row.version} must pass its test before it is published "
                       "(POST .../definitions/{id}/test)", details={"status": row.status})
    if row.kind == "playbook":  # S06-T6: verify the hash-bound approval immediately before signing, then sign
        from analystos.autonomy.playbook_signing import sign_for_publish

        sign_for_publish(session, user, row)
    row.status, row.published_by, row.published_at = "published", actor or user.id, utcnow()
    row.revision += 1
    session.flush()
    _event(session, row, "definition.published", f"user:{user.id}")
    _refresh_pins(session, row.workspace_id)
    return row


def test(session: Session, user: User, row: Definition, expected_revision: int | None, *,
         arguments: dict[str, Any] | None = None) -> Definition:
    """Run a draft of a tested kind once; on success it becomes `tested` with the evidence bound to its content
    hash. A failing test raises the tester's error and leaves the draft as it was."""
    require_role(session, user, row.workspace_id, author_role(row.kind))
    if row.kind not in _TESTERS:
        raise InvalidInput(f"{row.kind} definitions have no test step")
    if row.status not in EDITABLE:
        raise Conflict(f"version {row.version} is {row.status}; only a draft is tested")
    _check_revision(row, expected_revision)
    row.spec = _validate(session, row.workspace_id, row.kind, row.key, row.spec)
    row.content_hash = content_hash(row.kind, row.spec)
    evidence = _TESTERS[row.kind](session, user, row, arguments)
    row.test_evidence = {**evidence, "content_hash": row.content_hash, "tested_by": user.id, "tested_at": utcnow().isoformat()}
    row.status = "tested"
    row.revision += 1
    session.flush()
    _event(session, row, "definition.tested", f"user:{user.id}", evidence={k: v for k, v in evidence.items() if k != "columns"})
    return row


def publish_frozen(session: Session, workspace_id: str, kind: str, key: str, spec: dict[str, Any], *, created_by: str,
                   published_by: str, title: str | None = None) -> Definition:
    """Publish in one step what an approval already froze (e.g. a saved analysis). Same content as the
    newest published version returns that version instead of a duplicate."""
    if kind == "playbook":
        raise InvalidInput("a playbook is published through its simulation and signature")
    spec = _validate(session, workspace_id, kind, key, spec)
    digest = content_hash(kind, spec)
    current = latest_published(session, workspace_id, kind, key)
    if current is not None and current.content_hash == digest:
        return current
    row = Definition(id=new_id("defn"), workspace_id=workspace_id, kind=kind, key=key,
                     version=_latest_version(session, workspace_id, kind, key) + 1, status="published", title=title,
                     spec=spec, content_hash=digest, revision=1, created_by=created_by, published_by=published_by,
                     published_at=utcnow())
    session.add(row)
    session.flush()
    _event(session, row, "definition.published", published_by if ":" in published_by else f"user:{published_by}")
    return row


def deprecate(session: Session, user: User, row: Definition, *, reason: str | None = None) -> Definition:
    require_role(session, user, row.workspace_id, author_role(row.kind))
    if row.status != "published":
        raise Conflict(f"only a published version can be deprecated (this one is {row.status})")
    row.status, row.reason = "deprecated", reason or "deprecated"
    row.revision += 1
    _event(session, row, "definition.deprecated", f"user:{user.id}", reason=row.reason)
    _refresh_pins(session, row.workspace_id)
    return row


def retire(session: Session, user: User, row: Definition, *, reason: str | None = None) -> Definition:
    """Retired versions never run again; schedules pinned to one are blocked and their owners notified."""
    require_role(session, user, row.workspace_id, author_role(row.kind))
    if row.status not in RUNNABLE_STATUSES:
        raise Conflict(f"only a published or deprecated version can be retired (this one is {row.status})")
    row.status, row.reason, row.retired_by, row.retired_at = "retired", reason or "retired", user.id, utcnow()
    row.revision += 1
    _event(session, row, "definition.retired", f"user:{user.id}", reason=row.reason)
    _refresh_pins(session, row.workspace_id)
    return row


# ------------------------------------------------------------------------------------ promotion (ADR-0021 §5)
ENVIRONMENTS = ("dev", "test", "prod")


def referenced_sources(spec: Any) -> list[str]:
    """Every connection a spec names: the values of `source_id` keys and `source_ids` lists, at any depth."""
    found: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "source_id" and isinstance(v, str):
                    found.add(v)
                elif k == "source_ids" and isinstance(v, list):
                    found.update(i for i in v if isinstance(i, str))
                else:
                    walk(v)
        elif isinstance(x, list):
            for i in x:
                walk(i)
    walk(spec)
    return sorted(found)


def apply_bindings(spec: Any, sources: dict[str, str]) -> Any:
    """The spec as it runs in this environment: each bound connection replaced (the stored spec never changes)."""
    if not sources:
        return spec
    if isinstance(spec, dict):
        out = {}
        for k, v in spec.items():
            if k == "source_id" and isinstance(v, str):
                out[k] = sources.get(v, v)
            elif k == "source_ids" and isinstance(v, list):
                out[k] = [sources.get(i, i) if isinstance(i, str) else i for i in v]
            else:
                out[k] = apply_bindings(v, sources)
        return out
    if isinstance(spec, list):
        return [apply_bindings(i, sources) for i in spec]
    return spec


def _bind_sources(session: Session, source_ws: str, target_ws: str, names: list[str],
                  explicit: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    """Each connection of the spec -> the target environment's connection: the one given (keyed by the spec's id or
    by the connection it is bound to in the source environment), else the connection of the same name there. It
    must exist in the target workspace and be of the same kind (the SQL dialect must hold)."""
    from analystos.db.models import Source

    out, problems, used = {}, [], set()
    for sid in names:
        here = current.get(sid, sid)
        origin = session.get(Source, here)
        if origin is None or origin.workspace_id != source_ws:
            problems.append(f"{sid}: not a connection of the source workspace")
            continue
        target = None
        given = sid if sid in explicit else here if here in explicit else None
        if given is not None:
            used.add(given)
            target = session.get(Source, explicit[given])
            if target is None or target.workspace_id != target_ws:
                problems.append(f"{sid}: binding {explicit[given]} is not a connection of the target workspace")
                continue
        else:
            target = session.scalar(select(Source).where(Source.workspace_id == target_ws, Source.name == origin.name)
                                    .order_by(Source.created_at).limit(1))
            if target is None:
                problems.append(f"{sid} ({origin.name}): no connection of that name in the target; bind it explicitly")
                continue
        if target.kind != origin.kind:
            problems.append(f"{sid}: bound to a {target.kind} connection, but the definition was published against {origin.kind}")
            continue
        out[sid] = target.id
    unknown = sorted(set(explicit) - used)
    if unknown:
        problems.append(f"bindings name connections the definition does not use: {', '.join(unknown)}")
    if problems:
        raise InvalidInput("the definition's connections cannot be bound in the target environment: " + "; ".join(problems),
                           details={"problems": problems})
    return out


def promote(session: Session, user: User, row: Definition, target_workspace_id: str, *,
            bindings: dict[str, str] | None = None) -> Definition:
    """Copy a published version to the next environment (dev -> test -> prod) by content hash: the spec and its
    hash are identical; only the connection bindings differ, stored beside it. Promoting the same content again
    returns the version already there."""
    require_role(session, user, row.workspace_id, author_role(row.kind))
    require_role(session, user, target_workspace_id, author_role(row.kind))
    if row.status != "published":
        raise Conflict(f"only a published version is promoted (this one is {row.status})")
    source_env, target_env = workspace_environment(session, row.workspace_id), workspace_environment(session, target_workspace_id)
    if source_env not in ENVIRONMENTS or target_env not in ENVIRONMENTS or \
            ENVIRONMENTS.index(target_env) != ENVIRONMENTS.index(source_env) + 1:
        raise Conflict(f"promotion goes one step dev -> test -> prod; this is {source_env} -> {target_env}",
                       details={"source_environment": source_env, "target_environment": target_env})
    if row.kind == "playbook":
        from analystos.autonomy.playbook_signing import verify_definition

        verify_definition(session, row)  # only a signed version travels, with its signature
    spec = _validate(session, target_workspace_id, row.kind, row.key, dict(row.spec))
    if content_hash(row.kind, spec) != row.content_hash:
        raise Conflict(f"{row.kind} {row.key} does not validate to the same content in the target workspace "
                       "(a reference it uses differs there); promote its dependencies first")
    sources = _bind_sources(session, row.workspace_id, target_workspace_id, referenced_sources(spec), dict(bindings or {}),
                            (row.bindings or {}).get("sources") or {})
    existing = session.scalar(select(Definition).where(Definition.workspace_id == target_workspace_id,
                                                       Definition.kind == row.kind, Definition.key == row.key,
                                                       Definition.content_hash == row.content_hash,
                                                       Definition.status.in_(RUNNABLE_STATUSES)).limit(1))
    if existing is not None:
        if ((existing.bindings or {}).get("sources") or {}) != sources:
            raise Conflict(f"{row.kind} {row.key} with this content is already in {target_env} as v{existing.version} "
                           "with other connection bindings", details={"definition_id": existing.id})
        return existing
    new = Definition(id=new_id("defn"), workspace_id=target_workspace_id, kind=row.kind, key=row.key,
                     version=_latest_version(session, target_workspace_id, row.kind, row.key) + 1, status="published",
                     title=row.title, spec=spec, content_hash=row.content_hash, revision=1, created_by=user.id,
                     published_by=user.id, published_at=utcnow(), test_evidence=row.test_evidence,
                     bindings={"environment": target_env, "sources": sources}, signature=row.signature,
                     promoted_from={"workspace_id": row.workspace_id, "definition_id": row.id, "version": row.version,
                                    "content_hash": row.content_hash, "environment": source_env})
    session.add(new)
    session.flush()
    if new.kind == "playbook":
        from analystos.autonomy.playbook_signing import verify_definition

        verify_definition(session, new)  # the target's tenant key must verify it (no cross-tenant promotion)
    _event(session, new, "definition.promoted", f"user:{user.id}", from_workspace=row.workspace_id,
           from_definition=row.id, from_environment=source_env, environment=target_env, sources=sources)
    _event(session, row, "definition.promoted", f"user:{user.id}", to_workspace=target_workspace_id, to_definition=new.id,
           environment=target_env)
    _refresh_pins(session, target_workspace_id)
    return new


def _refresh_pins(session: Session, workspace_id: str) -> None:
    from analystos.services.pins import refresh_workspace

    session.flush()
    refresh_workspace(session, workspace_id)


# ------------------------------------------------------------------------------------ resolution
def resolve(session: Session, workspace_id: str, ref: DefinitionRef | dict[str, Any] | str) -> tuple[DefinitionRef, dict[str, Any]]:
    """What a trigger names -> (exact ref, spec). A string is a definition row id or a key; a key with no
    version means the newest published version. A playbook key the workspace never defined resolves to
    the built-in/pack manifest of that id."""
    if isinstance(ref, str):
        ref = {"id": ref} if ref.startswith("defn_") else {"key": ref}
    if isinstance(ref, DefinitionRef):
        want = ref
    else:
        from pydantic import ValidationError

        try:
            want = DefinitionRef.model_validate({"kind": "playbook", **ref})
        except (ValidationError, TypeError):
            raise InvalidInput("a definition is named by {key, version} (kind defaults to playbook) or by its id") from None
    if not want.id and not want.key:
        raise InvalidInput("a definition is named by {key, version} or by its id")
    row: Definition | None = None
    if want.id:
        row = session.get(Definition, want.id)
        if row is None or row.workspace_id != workspace_id:
            raise NotFound("definition not found")
    elif want.source == "workspace":
        stmt = select(Definition).where(Definition.workspace_id == workspace_id, Definition.kind == want.kind,
                                        Definition.key == want.key)
        if want.version is not None:
            row = session.scalar(stmt.where(Definition.version == int(want.version)))
        else:
            row = latest_published(session, workspace_id, want.kind, want.key)
    if row is not None:
        if want.content_hash and want.content_hash != row.content_hash:
            raise Conflict(f"{row.kind} {row.key} v{row.version} has content {row.content_hash[:12]}, not {want.content_hash[:12]}")
        return ref_of(row), apply_bindings(dict(row.spec), (row.bindings or {}).get("sources") or {})
    if want.kind == "playbook":
        from analystos.capabilities import registry

        manifest = registry.current().manifests.get(want.key)
        if manifest is not None and manifest.kind == "Playbook":
            got = builtin_ref(manifest)
            if want.version is not None and str(want.version) != got.version:
                raise NotFound(f"{want.key}@{want.version} is not installed (installed: {got.version})")
            return got, manifest.model_dump(mode="json", exclude={"source"})
    raise NotFound(f"{want.kind} {want.key} has no {'version ' + str(want.version) if want.version else 'published version'}")


def require_runnable(session: Session, workspace_id: str, ref: DefinitionRef, *, trigger: str) -> None:
    """Triggers run published versions only; a draft runs only in a workspace marked `environment: dev`."""
    if ref.status == "retired":
        raise PolicyDenied(f"{ref.label} is retired and cannot run", details={"definition": ref.model_dump()})
    if ref.status in EDITABLE and not is_dev(session, workspace_id):
        raise PolicyDenied(f"{ref.label} is a draft: {trigger} runs published versions only (publish it, or mark the "
                           "workspace environment: dev to run drafts)", details={"definition": ref.model_dump()})
    if ref.kind == "playbook" and ref.source == "workspace" and ref.status in RUNNABLE_STATUSES:
        from analystos.autonomy.playbook_signing import verify_definition

        row = session.get(Definition, ref.id) if ref.id else None
        if row is None or row.workspace_id != workspace_id:
            raise NotFound(f"{ref.label} not found")
        verify_definition(session, row)  # S06-T6: an unsigned or changed playbook never runs


def resolve_runnable(session: Session, workspace_id: str, ref: DefinitionRef | dict[str, Any] | str, *,
                     trigger: str) -> tuple[DefinitionRef, dict[str, Any]]:
    got, spec = resolve(session, workspace_id, ref)
    require_runnable(session, workspace_id, got, trigger=trigger)
    return got, spec


# ------------------------------------------------------------------------------------ views
def diff(a: Any, b: Any, path: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Field-level diff of two specs (what an upgrade would change)."""
    out: list[dict[str, Any]] = []

    def walk(x: Any, y: Any, p: str) -> None:
        if len(out) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{p}.{k}" if p else str(k))
        elif x != y:
            out.append({"path": p, "from": x, "to": y})
    walk(a, b, path)
    return out


def out(row: Definition, *, spec: bool = True) -> dict[str, Any]:
    d = {c: getattr(row, c) for c in ("id", "workspace_id", "kind", "key", "version", "status", "title", "content_hash",
                                       "revision", "created_by", "published_by", "retired_by", "reason")}
    for c in ("published_at", "retired_at", "created_at", "updated_at"):
        v = getattr(row, c)
        d[c] = v.isoformat() if v else None
    for c in ("test_evidence", "bindings", "promoted_from"):
        d[c] = getattr(row, c, None)
    if row.kind == "playbook":
        from analystos.autonomy.playbook_signing import state

        d["signature"] = state(row)
    if spec:
        d["spec"] = row.spec
        if row.kind == "agent":
            from analystos.capabilities.agent_forms import form_of

            d["form"] = form_of(row.spec)  # None: the manifest uses fields the form cannot show
    return d


def workspace_environment(session: Session, workspace_id: str) -> str:
    ws = get_workspace(session, workspace_id)
    return str((ws.settings or {}).get("environment") or "prod")
