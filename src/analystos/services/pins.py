"""Pinned schedules (ADR-0021 §3–4, P7-03).

A schedule stores the frozen set its baseline run used: the definition (a workspace version, or a
built-in playbook's manifest version + digest), every capability manifest the run bound, the method
versions, the semantic model and metric versions, and the baseline's tested `AnalysisSpec`s. A fire
replays exactly that set on new data; it never sends the objective back to a planner.

The comparison with what is current is computed in code (`status`): a newer published/approved
version of anything pinned is *upgrade available* (with a diff) and changes nothing until the owner
accepts it into a new schedule revision (`accept_upgrade`); a deprecated pin keeps running with a
warning; a retired or rejected pin blocks the schedule and notifies its owner.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.definition import BLOCKING_ITEM_STATES, PinItem, PinStatus
from analystos.core.errors import Conflict, Forbidden, InvalidInput, PreconditionFailed
from analystos.core.ids import stable_hash, utcnow
from analystos.db.models import AnalysisRun, Definition, Hypothesis, Schedule, SemanticMetric, SemanticModel, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, member_role
from analystos.services.notifications import notify

PINNED_KINDS = ("reanalysis", "saved_analysis", "step")


# ------------------------------------------------------------------------------------ capture
def frozen_analyses(session: Session, run_id: str) -> list[dict[str, Any]]:
    """The AnalysisSpecs the baseline actually tested (not the opt-in novelty round's): the question set."""
    from analystos.registries.hypotheses import TESTED, spec_hash

    out: dict[str, dict[str, Any]] = {}
    for h in session.scalars(select(Hypothesis).where(Hypothesis.run_id == run_id, Hypothesis.status.in_(TESTED),
                                                      Hypothesis.origin != "novelty").order_by(Hypothesis.created_at, Hypothesis.code)):
        out.setdefault(spec_hash(h.spec), {"spec_hash": spec_hash(h.spec), "spec": dict(h.spec), "statement": h.statement,
                                           "question": h.question})
    return list(out.values())


def capture(session: Session, run: AnalysisRun, *, revision: int) -> dict[str, Any]:
    """Pins from a completed run's binding (a run bound before P7-03 gets the versions current now)."""
    from analystos.capabilities import registry
    from analystos.capabilities.binding import requested_playbook, semantic_versions
    from analystos.services.definitions import builtin_ref

    caps = dict(run.capabilities or {})
    playbook = requested_playbook(run)
    definition = caps.get("definition")
    if not definition:
        manifest = registry.current().manifests.get(playbook)
        definition = builtin_ref(manifest).model_dump() if manifest is not None else {"kind": "playbook", "key": playbook}
    return {"revision": revision, "baseline_run_id": run.id, "captured_at": utcnow().isoformat(), "playbook": playbook,
            "definition": definition, "refs": list(caps.get("refs") or []), "manifests": dict(caps.get("manifests") or {}),
            "semantic": caps.get("semantic") or semantic_versions(session, run.workspace_id),
            "methods": list(caps.get("methods") or sorted(m.ref for m in registry.current().list("Method"))),
            "analyses": frozen_analyses(session, run.id)}


def saved_analysis_pins(definition: dict[str, Any], semantic: dict[str, Any] | None) -> dict[str, Any]:
    """A saved Ask calculation pins its published definition and the semantic versions it compiled from."""
    semantic = semantic or {}
    return {"revision": 1, "captured_at": utcnow().isoformat(), "definition": definition,
            "semantic": {"model": {"id": semantic["model_id"], "content_hash": semantic.get("model_hash")} if semantic.get("model_id") else None,
                         "metrics": {m["name"]: {"id": m["id"], "version": m.get("version"), "content_hash": m.get("hash")}
                                     for m in semantic.get("metrics") or []}}}


def for_run(sch: Schedule) -> dict[str, Any]:
    """What a fire hands the new run: everything pinned, minus bookkeeping."""
    pins = sch.pins or {}
    return {"revision": pins.get("revision"), "schedule_id": sch.id, "baseline_run_id": pins.get("baseline_run_id"),
            "playbook": pins.get("playbook"), "definition": pins.get("definition"),
            "manifests": pins.get("manifests") or {}, "semantic": pins.get("semantic"), "methods": pins.get("methods") or [],
            "analyses": pins.get("analyses") or []}


# ------------------------------------------------------------------------------------ comparison
def _digest(raw: dict[str, Any]) -> str:
    from analystos.services.definitions import manifest_digest

    return manifest_digest(raw)


def _definition_item(session: Session, d: dict[str, Any]) -> PinItem | None:
    from analystos.services.definitions import diff, latest_published

    if d.get("source") != "workspace":
        return None  # a built-in playbook is compared as a pinned capability manifest
    label = f"{d.get('kind')}:{d.get('key')}@{d.get('version')}"
    row = session.get(Definition, d.get("id")) if d.get("id") else None
    if row is None or row.status == "retired":
        return PinItem(type="definition", id=str(d.get("key")), pinned=label, state="retired",
                       reason=(row.reason if row else "the pinned version no longer exists"))
    if row.kind == "playbook":  # S06-T6: a pinned playbook whose signature no longer verifies never fires
        from analystos.autonomy.playbook_signing import verify_definition
        from analystos.toolreg.signing import ManifestRefused

        try:
            verify_definition(session, row)
        except ManifestRefused as exc:
            return PinItem(type="definition", id=row.key, pinned=label, state="rejected", reason=exc.message)
    newer = latest_published(session, row.workspace_id, row.kind, row.key)
    if newer is not None and newer.version > row.version:
        return PinItem(type="definition", id=row.key, pinned=label, current=f"{row.kind}:{row.key}@{newer.version}", state="newer",
                       diff=diff(row.spec, newer.spec))
    if row.status == "deprecated":
        return PinItem(type="definition", id=row.key, pinned=label, current=label, state="deprecated", reason=row.reason)
    return None


def _capability_items(pins: dict[str, Any], snap: Any) -> list[PinItem]:
    from analystos.services.definitions import diff

    items: list[PinItem] = []
    for cap_id, raw in sorted((pins.get("manifests") or {}).items()):
        if str(raw.get("source", "")).startswith("definition:"):
            continue  # a workspace definition: its version row decides (above)
        kind = "method" if raw.get("kind") == "Method" else "capability"
        pinned = f"{cap_id}@{raw.get('version')}"
        cur = snap.manifests.get(cap_id)
        if cur is None:
            items.append(PinItem(type=kind, id=cap_id, pinned=pinned, state="retired", reason="no longer installed"))
            continue
        cur_raw = cur.model_dump(mode="json")
        if _digest(cur_raw) != _digest(raw):
            items.append(PinItem(type=kind, id=cap_id, pinned=pinned, current=cur.ref, state="newer",
                                 diff=diff({k: v for k, v in raw.items() if k != "source"},
                                           {k: v for k, v in cur_raw.items() if k != "source"}, limit=50)))
        elif cur.certification.status == "deprecated":
            items.append(PinItem(type=kind, id=cap_id, pinned=pinned, current=cur.ref, state="deprecated",
                                 reason=f"{cur.ref} is deprecated"))
    bound = set(pins.get("manifests") or {})
    current_methods = {m.id: m for m in snap.list("Method")}
    for ref in pins.get("methods") or []:
        mid, _, version = ref.partition("@")
        if mid in bound:
            continue
        cur = current_methods.get(mid)
        if cur is None:
            items.append(PinItem(type="method", id=mid, pinned=ref, state="retired", reason="no longer installed"))
        elif cur.version != version:
            items.append(PinItem(type="method", id=mid, pinned=ref, current=cur.ref, state="newer",
                                 diff=[{"path": "version", "from": version, "to": cur.version}]))
    return items


def _workspace_agent_items(session: Session, pins: dict[str, Any]) -> list[PinItem]:
    """A pinned workspace agent (P7-19) compares like a pinned definition: a retired version blocks, a
    newer published version is an upgrade, a deprecated one warns."""
    from analystos.services.definitions import diff, latest_published

    items: list[PinItem] = []
    for cap_id, raw in sorted((pins.get("manifests") or {}).items()):
        source = str(raw.get("source", ""))
        if raw.get("kind") != "Agent" or not source.startswith("definition:"):
            continue
        pinned = f"{cap_id}@{raw.get('version')}"
        row = session.get(Definition, source.split(":", 1)[1])
        if row is None or row.status == "retired":
            items.append(PinItem(type="definition", id=cap_id, pinned=pinned, state="retired",
                                 reason=(row.reason if row else None) or "the pinned agent version no longer exists"))
            continue
        newer = latest_published(session, row.workspace_id, row.kind, row.key)
        if newer is not None and newer.version > row.version:
            items.append(PinItem(type="definition", id=cap_id, pinned=pinned, current=f"{cap_id}@{newer.spec.get('version')}",
                                 state="newer", diff=diff(row.spec, newer.spec, limit=50)))
        elif row.status == "deprecated":
            items.append(PinItem(type="definition", id=cap_id, pinned=pinned, state="deprecated", reason=row.reason))
    return items


def _semantic_items(session: Session, workspace_id: str, semantic: dict[str, Any] | None) -> list[PinItem]:
    from analystos.semantic.service import approved_metrics
    from analystos.services.definitions import diff

    items: list[PinItem] = []
    semantic = semantic or {}
    approved = approved_metrics(session, workspace_id)
    for name, ref in sorted((semantic.get("metrics") or {}).items()):
        pinned = f"{name}@v{ref.get('version')}"
        row = session.get(SemanticMetric, ref.get("id"))
        if row is None or row.workspace_id != workspace_id or row.status == "rejected":
            items.append(PinItem(type="metric", id=name, pinned=pinned, state="rejected",
                                 reason=(row.reason if row else None) or "the pinned metric version was rejected or removed"))
            continue
        current = approved.get(name)
        if current is not None and current.id != row.id and current.version > row.version:
            items.append(PinItem(type="metric", id=name, pinned=pinned, current=f"{name}@v{current.version}", state="newer",
                                 diff=diff({"expression": row.expression, "definition": row.definition},
                                           {"expression": current.expression, "definition": current.definition})))
        elif row.status == "deprecated":
            items.append(PinItem(type="metric", id=name, pinned=pinned, state="deprecated",
                                 reason=row.reason or "deprecated with no approved successor"))
    model = semantic.get("model")
    if model and model.get("id"):
        row = session.get(SemanticModel, model["id"])
        # Only an *approved* newer structure is an upgrade; an agent's proposed model version is not.
        current = session.scalar(select(SemanticModel).where(
            SemanticModel.workspace_id == workspace_id, SemanticModel.status == "approved",
            SemanticModel.version > (row.version if row else 0)).order_by(SemanticModel.version.desc()).limit(1))
        pinned = f"semantic_model@v{row.version}" if row else f"semantic_model:{model['id']}"
        if current is not None and current.id != model["id"]:
            items.append(PinItem(type="semantic_model", id="semantic_model", pinned=pinned, current=f"semantic_model@v{current.version}",
                                 state="newer", diff=diff({"datasets": row.datasets, "relationships": row.relationships} if row else {},
                                                          {"datasets": current.datasets, "relationships": current.relationships}, limit=50)))
        elif row is not None and row.status == "deprecated":
            items.append(PinItem(type="semantic_model", id="semantic_model", pinned=pinned, state="deprecated",
                                 reason="the pinned semantic model version is deprecated"))
    return items


def status(session: Session, sch: Schedule) -> PinStatus:
    from analystos.capabilities import registry

    pins = sch.pins or {}
    if not pins:
        return PinStatus(state="unpinned", checked_at=utcnow().isoformat())
    items: list[PinItem] = []
    if (d := _definition_item(session, pins.get("definition") or {})) is not None:
        items.append(d)
    items += _capability_items(pins, registry.current())
    items += _workspace_agent_items(session, pins)
    items += _semantic_items(session, sch.workspace_id, pins.get("semantic"))
    blocking = [f"{i.pinned}: {i.reason or i.state}" for i in items if i.state in BLOCKING_ITEM_STATES]
    newer = [i for i in items if i.state == "newer"]
    warnings = [f"{i.pinned}: {i.reason or 'deprecated'}" for i in items if i.state == "deprecated"]
    state = "blocked" if blocking else "upgrade_available" if newer else "deprecated" if warnings else "current"
    return PinStatus(state=state, revision=int(pins.get("revision") or 0), items=items, warnings=warnings, blocking=blocking,
                     upgrade_hash=stable_hash(sorted((i.type, i.id, i.current) for i in newer)) if newer else None,
                     checked_at=utcnow().isoformat())


def refresh(session: Session, sch: Schedule) -> PinStatus:
    """Recompute and store the pin status; a change of state (or of the offered upgrade) is an event, and
    blocked / upgrade-available also notify the owner."""
    st = status(session, sch)
    prev = sch.pin_status or {}
    sch.pin_status = st.model_dump(mode="json")
    if st.state == prev.get("state") and st.upgrade_hash == prev.get("upgrade_hash"):
        return st
    summary = {"schedule": sch.id, "state": st.state, "revision": st.revision, "upgrade_hash": st.upgrade_hash,
               "items": [{"type": i.type, "id": i.id, "pinned": i.pinned, "current": i.current, "state": i.state}
                         for i in st.items][:50]}
    link = {"type": "schedule", "id": sch.id}
    if st.state == "blocked":
        emit(sch.workspace_id, "schedule.blocked", {**summary, "blocking": st.blocking}, session=session)
        notify(session, sch.workspace_id, kind="schedule", title=f"Schedule blocked: {sch.name}",
               body="A pinned version was retired or rejected: " + "; ".join(st.blocking), link=link, user_id=sch.owner_id)
    elif st.state == "upgrade_available":
        emit(sch.workspace_id, "schedule.upgrade_available", summary, session=session)
        notify(session, sch.workspace_id, kind="schedule", title=f"Upgrade available: {sch.name}",
               body="Newer versions of what this schedule pins: " + ", ".join(
                   f"{i.pinned} -> {i.current}" for i in st.items if i.state == "newer") +
               ". It keeps running the pinned versions until you accept.", link=link, user_id=sch.owner_id)
    elif st.state == "deprecated":
        emit(sch.workspace_id, "schedule.pin_warning", {**summary, "warnings": st.warnings}, session=session)
    return st


def refresh_workspace(session: Session, workspace_id: str | None = None) -> dict[str, str]:
    stmt = select(Schedule).where(Schedule.kind.in_(PINNED_KINDS))
    if workspace_id is not None:
        stmt = stmt.where(Schedule.workspace_id == workspace_id)
    return {sch.id: refresh(session, sch).state for sch in session.scalars(stmt) if sch.pins}


# ------------------------------------------------------------------------------------ upgrade
def _current_pins(session: Session, sch: Schedule, accepted_by: str) -> dict[str, Any]:
    from analystos.capabilities.binding import bind_run
    from analystos.services.definitions import latest_published

    pins = dict(sch.pins or {})
    caps: dict[str, Any] = {"playbook": pins.get("playbook")}
    d = pins.get("definition") or {}
    if d.get("source") == "workspace":
        row = latest_published(session, sch.workspace_id, d.get("kind", "playbook"), d.get("key"))
        if row is None:
            raise Conflict(f"{d.get('kind')} {d.get('key')} has no published version to upgrade to")
        from analystos.services.definitions import ref_of

        caps["definition"] = {**ref_of(row).model_dump(), "manifest": row.spec}
    ws = get_workspace(session, sch.workspace_id)
    probe = SimpleNamespace(id=None, workspace_id=sch.workspace_id, autonomy_level=ws.autonomy_level,
                            origin={"type": "schedule", "schedule_id": sch.id}, capabilities=caps)
    b = bind_run(session, probe)
    return {"revision": int(pins.get("revision") or 0) + 1, "baseline_run_id": None, "captured_at": utcnow().isoformat(),
            "accepted_by": accepted_by, "playbook": b.playbook.manifest.id, "definition": b.definition, "refs": b.refs,
            "manifests": b.manifests, "semantic": b.semantic, "methods": b.methods, "analyses": pins.get("analyses") or [],
            "previous": {"revision": pins.get("revision"), "baseline_run_id": pins.get("baseline_run_id"),
                         "refs": pins.get("refs") or [], "definition": pins.get("definition")}}


def accept_upgrade(session: Session, user: User, sch: Schedule, *, expected_revision: int | None,
                   upgrade_hash: str | None = None) -> dict[str, Any]:
    """The owner moves the schedule to the current versions: a new schedule revision whose first fire is
    the new baseline. Bound to the upgrade the owner reviewed (`upgrade_hash`) and to the revision."""
    if sch.owner_id != user.id and member_role(session, user, sch.workspace_id) != "owner" and not user.is_admin:
        raise Forbidden("only the schedule owner (or a workspace owner) accepts an upgrade")
    if expected_revision is not None and expected_revision != sch.revision:
        raise PreconditionFailed(f"schedule {sch.id} is at revision {sch.revision}, not {expected_revision}",
                                 details={"current_revision": sch.revision})
    if sch.kind != "reanalysis":
        raise InvalidInput("a saved analysis is re-pinned by asking again and scheduling the new answer "
                           "(its SQL was compiled from the pinned metric versions)")
    before = status(session, sch)
    if before.state not in ("upgrade_available", "blocked", "deprecated"):
        raise Conflict(f"no upgrade to accept (pins are {before.state})")
    if upgrade_hash is not None and upgrade_hash != before.upgrade_hash:
        raise Conflict("the available upgrade changed since it was reviewed; review it again",
                       details={"upgrade_hash": before.upgrade_hash})
    new = _current_pins(session, sch, user.id)
    old_refs = set((sch.pins or {}).get("refs") or [])
    sch.pins = new
    sch.revision += 1
    after = refresh(session, sch)
    if after.state == "blocked":
        raise Conflict("the current versions are not runnable either: " + "; ".join(after.blocking))
    details = {"schedule": sch.id, "revision": sch.revision, "pin_revision": new["revision"],
               "added": sorted(set(new["refs"]) - old_refs), "removed": sorted(old_refs - set(new["refs"])),
               "metrics": {k: v.get("version") for k, v in ((new.get("semantic") or {}).get("metrics") or {}).items()}}
    emit(sch.workspace_id, "schedule.upgraded", details, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "schedule.upgraded", workspace_id=sch.workspace_id, target=sch.id, details=details, session=session)
    return {"schedule_id": sch.id, "revision": sch.revision, "pin_revision": new["revision"], "status": after.model_dump(mode="json"),
            **{k: details[k] for k in ("added", "removed")}}


def view(sch: Schedule) -> dict[str, Any]:
    """Pins without the bound manifests (refs are enough for a client)."""
    pins = sch.pins or {}
    return {k: v for k, v in pins.items() if k not in ("manifests", "analyses")} | \
        {"analyses": len(pins.get("analyses") or []), "manifests": len(pins.get("manifests") or {})} if pins else {}
