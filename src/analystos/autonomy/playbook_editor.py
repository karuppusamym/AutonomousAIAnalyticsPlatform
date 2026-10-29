"""Playbook editor (S06-T6, spec §3.7): YAML in, the existing playbook contract and registry checks out.

The editor works on workspace `playbook` definitions (services/definitions.py keeps the versions, the
one-draft rule and the If-Match revisions). What it adds is YAML: parsing with `yaml.safe_load` only (no
tags, no code), a check that reports every problem instead of stopping at the first, the same YAML back
from a stored manifest, and the options an author picks from (agents and their behaviours, investigation
checks, replan reasons, fixture workspaces).
"""
from __future__ import annotations

from typing import Any

import yaml
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput

MAX_YAML_BYTES = 256 * 1024


def parse_yaml(text: str) -> dict[str, Any]:
    """One YAML mapping (the manifest). Safe loader only, bounded size, a readable location on error."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidInput("the playbook YAML is empty")
    if len(text.encode()) > MAX_YAML_BYTES:
        raise InvalidInput(f"the playbook YAML is larger than {MAX_YAML_BYTES // 1024} KiB")
    try:
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
        raise InvalidInput(f"the playbook is not valid YAML{where}: {getattr(exc, 'problem', None) or exc}",
                           details={"problems": [f"yaml{where}: {getattr(exc, 'problem', None) or exc}"]}) from None
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise InvalidInput("the playbook YAML must be exactly one mapping (a kind: Playbook manifest)")
    return docs[0]


def to_yaml(spec: dict[str, Any]) -> str:
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=120)


def check(session: Session, workspace_id: str, text: str) -> dict[str, Any]:
    """Validate without saving. Problems are returned, not raised, so the editor can list them all."""
    from analystos.autonomy.simulator import static_warnings
    from analystos.capabilities.playbook import Playbook
    from analystos.contracts.capability import CapabilityManifest
    from analystos.services import definitions as defs

    try:
        raw = parse_yaml(text)
    except InvalidInput as exc:
        return {"ok": False, "problems": (exc.details or {}).get("problems") or [exc.message]}
    key = str(raw.get("id") or "")
    try:
        spec = defs._validate(session, workspace_id, "playbook", key, raw)
    except AnalystOSError as exc:
        problems = (exc.details or {}).get("problems") or [exc.message]
        return {"ok": False, "key": key or None, "problems": [p.split(": ", 2)[-1] if p.startswith("workspace:") else p
                                                              for p in problems]}
    pb = Playbook(CapabilityManifest.model_validate({**spec, "source": f"workspace:{workspace_id}"}))
    published = defs.latest_published(session, workspace_id, "playbook", key)
    builtin = None
    if published is None:
        from analystos.capabilities import registry

        m = registry.current().manifests.get(key)
        builtin = m.model_dump(mode="json", exclude={"source"}) if m is not None and m.kind == "Playbook" else None
    against = published.spec if published is not None else builtin
    return {"ok": True, "key": key, "problems": [], "warnings": static_warnings(pb),
            "content_hash": defs.content_hash("playbook", spec), "manifest": spec,
            "steps": [{"key": s.key, "use": s.use, "behaviour": s.behaviour, "type": s.type, "after": s.after,
                       "optional": s.optional, "when": s.when} for s in pb.body.steps],
            "approval_gates": pb.approval_gates(),
            "diff": {"against": (f"v{published.version}" if published is not None else
                                 "installed" if builtin is not None else None),
                     "changes": defs.diff(against, spec) if against is not None else []}}


def options(session: Session, workspace_id: str) -> dict[str, Any]:
    """What an author can reference: the agents this workspace may use (with behaviours), check names, replan
    reasons, outputs, limits and fixtures, plus the contract's JSON Schema."""
    from analystos.autonomy.simulator import SimulationFixture, fixtures
    from analystos.capabilities import enablement, registry
    from analystos.capabilities.playbook import PlaybookBody
    from analystos.contracts.goals import REPLAN_REASONS, RunLimits
    from analystos.evidence.investigation import CHECK_NAMES

    snap = registry.current()
    explicit = enablement.overrides(session, workspace_id)
    agents = [{"id": m.id, "version": m.version, "summary": m.summary, "default_entry": m.entry is not None,
               "behaviours": sorted(m.spec.get("behaviours") or {}), "certification": m.certification.status,
               "enabled": enablement.is_enabled(m, snap, explicit)} for m in snap.list("Agent")]
    return {"agents": agents, "checks": list(CHECK_NAMES), "replan_reasons": list(REPLAN_REASONS),
            "fixtures": [{"name": f.name, "description": f.description} for f in fixtures().values()],
            "schema": {"playbook": PlaybookBody.model_json_schema(), "limits": RunLimits.model_json_schema(),
                       "fixture": SimulationFixture.model_json_schema()},
            "template": to_yaml(_template(snap))}


def _template(snap: Any) -> dict[str, Any]:
    """A starting point: a small supervised playbook over certified agents, with a gated publication."""
    return {"apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.my_review", "version": "1.0.0",
            "summary": "Profile the selected tables, test hypotheses and summarise verified findings",
            "determinism": "model", "side_effect": "read_source", "cost_class": "llm_small",
            "certification": {"status": "draft"}, "tags": ["workspace"],
            "spec": {"framing": False, "default_limits": {"max_steps": 20, "max_replans": 2},
                     "steps": [
                         {"key": "plan_approval", "use": "agent.supervisor", "behaviour": "plan_approved",
                          "title": "Wait for plan approval", "type": "approval_gate", "payload": "plan",
                          "when": "run.autonomy_level <= 2", "gates_roots": True},
                         {"key": "metadata", "use": "agent.metadata", "title": "Discover technical metadata"},
                         {"key": "profile", "use": "agent.profiler", "title": "Profile selected tables", "after": ["metadata"]},
                         {"key": "finalize", "use": "agent.supervisor", "title": "Consolidate results", "after": ["profile"]}]}} \
        if "agent.supervisor" in snap.manifests else {"apiVersion": "analystos/v1", "kind": "Playbook", "id": "playbook.my_review",
                                                       "version": "1.0.0", "summary": "A workspace playbook",
                                                       "spec": {"steps": []}}
