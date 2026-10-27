"""Branching Data Thread (spec v4 §7, P7-05): fork from any step, compare two branches, merge into a report.

* **Fork.** A branch forked from step S (at its version v) inherits the steps before S at the versions
  they had then (`base`), and gets its own copy S' (`forked_from = {S, v}`: same spec, result and
  receipts, a verdict of its own). `include_downstream` also copies the steps after S so an edit of S'
  replays them; `spec` edits S' at once. The parent pointer and the fork edges are lineage.
* **Compare.** Steps are paired by their lineage root (a fork copy pairs with what it was copied from):
  `shared`, `diverged` (spec, numbers or verdict differ), `only_a`, `only_b`, each with the spec diff,
  the numeric deltas of their results and both verdicts. Read-only.
* **Merge.** A branch's steps become sections of a report artifact (a new one, or appended to one an
  earlier merge made, e.g. the other branch). A VOID verdict refuses the merge with its cause (reports
  refuse VOID, ADR-0020); flagged or failed steps are left out and listed. Lineage: every included step
  `included_in` the report, the branch `merged_into` it, and the fork edges already link the branches.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.step import Branch, ForkIn, MergeIn, StepEdit
from analystos.core.errors import InvalidInput, NotFound, PolicyDenied
from analystos.core.ids import new_id, ordered_utcnow, utcnow
from analystos.db.base import session_scope
from analystos.db.models import AnalysisStep, AnalysisStepVersion, Artifact, StepBranch, User
from analystos.events.bus import emit
from analystos.services import steps as steps_svc
from analystos.skills import selfcheck

MERGE_KINDS = ("query", "method", "chart", "claim")


def branch_view(b: StepBranch) -> dict[str, Any]:
    return Branch(id=b.id, name=b.name, container={"type": b.container_type, "id": b.container_id},
                  parent_branch_id=b.parent_branch_id,
                  forked_from={"step_id": b.forked_from_step_id, "version": b.forked_from_version} if b.forked_from_step_id else None,
                  base=list(b.base or []), status=b.status, merged_into=list(b.merged_into or []), created_by=b.created_by,  # type: ignore[arg-type]
                  created_at=b.created_at.isoformat() if b.created_at else None).model_dump(mode="json")


def branches(session: Session, container_type: str, container_id: str) -> list[dict[str, Any]]:
    return [branch_view(b) for b in session.scalars(select(StepBranch).where(StepBranch.container_type == container_type,
                                                                            StepBranch.container_id == container_id)
                                                    .order_by(StepBranch.created_at, StepBranch.id))]


# ------------------------------------------------------------------------------------ fork
def fork(user: User, workspace_id: str, branch_id: str, body: ForkIn, *, runtime: Any = None) -> dict[str, Any]:
    from analystos.evidence.verification import Dependency

    actor = f"user:{user.id}"
    with session_scope() as s:
        parent = s.get(StepBranch, branch_id)
        if parent is None or parent.workspace_id != workspace_id:
            raise NotFound("branch not found")
        eff = steps_svc.effective_steps(s, parent)
        pos = next((i for i, (st, _, _) in enumerate(eff) if st.id == body.from_step_id), None)
        if pos is None:
            raise InvalidInput(f"step {body.from_step_id} is not in branch {parent.name}")
        src, src_ver, _ = eff[pos]
        child = StepBranch(id=new_id("brn"), workspace_id=workspace_id, container_type=parent.container_type,
                           container_id=parent.container_id, name=(body.name or f"fork of {src.title}")[:200],
                           parent_branch_id=parent.id, forked_from_step_id=src.id, forked_from_version=src_ver.version,
                           base=[{"step_id": st.id, "version": v.version} for st, v, _ in eff[:pos]], status="open",
                           merged_into=[], created_by=actor, created_at=ordered_utcnow())
        s.add(child)
        s.flush()
        to_copy = [(src, src_ver)]
        if body.include_downstream:
            copied_ids = {src.id}
            for st, v, _ in eff[pos + 1:]:
                if set(st.depends_on or []) & copied_ids:
                    to_copy.append((st, v))
                    copied_ids.add(st.id)
        mapping: dict[str, str] = {}
        first_copy = None
        for st, v in to_copy:
            deps = [mapping.get(d, d) for d in st.depends_on or []]
            copy, cv = steps_svc.add_step(s, workspace_id=workspace_id, branch=child, kind=st.kind, title=st.title,
                                          spec=dict(v.spec), depends_on=deps, origin={"type": "fork", "id": st.id},
                                          actor=actor, chart_spec=v.chart_spec,
                                          forked_from={"step_id": st.id, "version": v.version}, reason="forked")
            mapping[st.id] = copy.id
            first_copy = first_copy or copy.id
            cv.receipts, cv.result_snapshot, cv.checks, cv.corrections = list(v.receipts or []), v.result_snapshot, \
                list(v.checks or []), list(v.corrections or [])
            cv.inputs = {mapping.get(u, u): (steps_svc.version_row(s, s.get(AnalysisStep, mapping[u])).id if u in mapping else vid)
                         for u, vid in (v.inputs or {}).items()}
            cv.status, copy.status, cv.finished_at = v.status, v.status, utcnow()
            if v.verification_record_id and v.status in ("ok", "flagged"):
                from analystos.db.models import VerificationRecord

                rec = s.get(VerificationRecord, v.verification_record_id)
                carried = [Dependency(d["kind"], d["ref"], d["version_hash"]) for d in (rec.dependencies if rec else [])
                           if not (d["kind"] == "query" and d["ref"].startswith("step:"))]
                steps_svc._record_copy(s, copy, cv, verdict=rec.verdict if rec else "failed_verification",
                                       checks=list(v.checks or []), extra=carried)
                if rec is not None and rec.state == "VOID":
                    from analystos.evidence.verification import void_dependents

                    void_dependents(s, "query", f"step:{copy.id}", "__carried_void__",
                                    f"forked from a void verdict: {rec.void_reason}", event="step.edited")
            steps_svc._link(s, workspace_id, ("step", st.id), "forked_into", ("step", copy.id))
        steps_svc._link(s, workspace_id, ("step_branch", parent.id), "forked_into", ("step_branch", child.id))
        emit(workspace_id, "branch.forked", {"branch_id": child.id, "parent_branch_id": parent.id, "from_step_id": src.id,
                                             "version": src_ver.version, "copied": len(to_copy)},
             run_id=parent.container_id if parent.container_type == "run" else None, actor=actor, session=s)
        child_id = child.id
    edited = None
    if body.spec is not None:
        edited = steps_svc.edit(user, first_copy, StepEdit(spec=body.spec), expected_version=None, runtime=runtime)
    with session_scope() as s:
        out = steps_svc.thread(s, s.get(StepBranch, child_id))
    return {**out, "edited": edited}


# ------------------------------------------------------------------------------------ compare
def _root(session: Session, step: AnalysisStep) -> str:
    seen, cur = set(), step
    while cur.forked_from and cur.id not in seen:
        seen.add(cur.id)
        nxt = session.get(AnalysisStep, cur.forked_from.get("step_id"))
        if nxt is None:
            return str(cur.forked_from.get("step_id"))
        cur = nxt
    return cur.id


def _numbers(a: dict[str, Any], b: dict[str, Any], limit: int = 50) -> dict[str, Any]:
    ha = selfcheck.headline(a.get("columns") or [], a.get("rows") or []) if a.get("rows") else None
    hb = selfcheck.headline(b.get("columns") or [], b.get("rows") or []) if b.get("rows") else None
    out: dict[str, Any] = {"same_result": bool(a.get("result_hash")) and a.get("result_hash") == b.get("result_hash"),
                           "headline": {"a": ha, "b": hb, "delta": (hb - ha) if ha is not None and hb is not None else None},
                           "row_count": {"a": a.get("row_count"), "b": b.get("row_count")}, "cells": []}
    if a.get("columns") and a.get("columns") == b.get("columns"):
        cols = a["columns"]
        nums = set(selfcheck.numeric_columns(cols, (a.get("rows") or []) + (b.get("rows") or [])))
        keys = [i for i in range(len(cols)) if i not in nums]
        index_b = {tuple(r[i] for i in keys): r for r in b.get("rows") or []}
        for ra in a.get("rows") or []:
            k = tuple(ra[i] for i in keys)
            rb = index_b.get(k)
            if rb is None:
                continue
            for i in sorted(nums):
                if ra[i] != rb[i]:
                    out["cells"].append({"key": list(k), "column": cols[i], "a": ra[i], "b": rb[i],
                                         "delta": (rb[i] - ra[i]) if isinstance(ra[i], int | float) and isinstance(rb[i], int | float) else None})
                    if len(out["cells"]) >= limit:
                        return out
    stat_a, stat_b = a.get("stat") or {}, b.get("stat") or {}
    if stat_a or stat_b:
        out["stat"] = {k: {"a": stat_a.get(k), "b": stat_b.get(k)} for k in ("statistic", "p_value", "effect_size", "n", "supported")
                       if stat_a.get(k) != stat_b.get(k)}
    return out


def _side(session: Session, st: AnalysisStep, v: AnalysisStepVersion, inherited: bool) -> dict[str, Any]:
    return steps_svc.view(session, st, v, inherited=inherited)


def compare(session: Session, a: StepBranch, b: StepBranch) -> dict[str, Any]:
    from analystos.services.definitions import diff

    if (a.container_type, a.container_id) != (b.container_type, b.container_id):
        raise InvalidInput("compare branches of the same thread")
    ea, eb = steps_svc.effective_steps(session, a), steps_svc.effective_steps(session, b)
    by_root_b: dict[str, tuple[AnalysisStep, AnalysisStepVersion, bool]] = {}
    for st, v, inh in eb:
        by_root_b.setdefault(_root(session, st), (st, v, inh))
    pairs, used = [], set()
    for st, v, inh in ea:
        root = _root(session, st)
        other = by_root_b.get(root)
        if other is None:
            pairs.append({"match": "only_a", "root": root, "a": _side(session, st, v, inh), "b": None})
            continue
        used.add(root)
        ost, ov, oinh = other
        ca, cb = steps_svc.snapshot_content(session, v.result_snapshot), steps_svc.snapshot_content(session, ov.result_snapshot)
        va, vb = _side(session, st, v, inh), _side(session, ost, ov, oinh)
        spec_diff = diff(v.spec, ov.spec)
        numbers = _numbers(ca, cb)
        verdicts = {"a": (va["verification_record"] or {}).get("badge", "unverified"),
                    "b": (vb["verification_record"] or {}).get("badge", "unverified")}
        same = st.id == ost.id and v.version == ov.version
        changed = bool(spec_diff) or not numbers["same_result"] and (numbers["cells"] or numbers["headline"]["delta"] or
                                                                     numbers.get("stat")) or verdicts["a"] != verdicts["b"]
        pairs.append({"match": "shared" if same else ("diverged" if changed else "equivalent"), "root": root, "a": va, "b": vb,
                      "spec_diff": spec_diff, "numbers": numbers, "verdicts": verdicts})
    for st, v, inh in eb:
        root = _root(session, st)
        if root not in used and not any(p["root"] == root for p in pairs):
            pairs.append({"match": "only_b", "root": root, "a": None, "b": _side(session, st, v, inh)})
    counts: dict[str, int] = {}
    for p in pairs:
        counts[p["match"]] = counts.get(p["match"], 0) + 1
    return {"a": branch_view(a), "b": branch_view(b), "steps": pairs, "summary": counts}


# ------------------------------------------------------------------------------------ merge
def merge(user: User, workspace_id: str, branch_id: str, body: MergeIn) -> dict[str, Any]:
    from analystos.artifacts.registry import save_artifact

    actor = f"user:{user.id}"
    with session_scope() as s:
        branch = s.get(StepBranch, branch_id, with_for_update=True)
        if branch is None or branch.workspace_id != workspace_id:
            raise NotFound("branch not found")
        eff = steps_svc.effective_steps(s, branch)
        views = [(st, v, steps_svc.view(s, st, v, inherited=inh)) for st, v, inh in eff]
        void = [(st, vw) for st, _, vw in views if (vw["verification_record"] or {}).get("state") == "VOID"]
        if void:
            raise PolicyDenied("merge refused: " + "; ".join(
                f"step '{st.title}' is void ({(vw['verification_record']['void'] or {}).get('kind')}: "
                f"{(vw['verification_record']['void'] or {}).get('reason')})" for st, vw in void[:5])
                + ". Re-run those steps so they are verified against current dependencies.",
                details={"void_steps": [st.id for st, _ in void]})
        sections, excluded = [], []
        for st, v, vw in views:
            if st.kind not in MERGE_KINDS:
                continue
            if v.status not in ("ok", "recorded"):
                excluded.append({"step_id": st.id, "title": st.title, "status": v.status,
                                 "reason": v.error or "; ".join(c["detail"] for c in v.checks or [] if not c.get("passed"))})
                continue
            content = steps_svc.snapshot_content(s, v.result_snapshot)
            sections.append({"step_id": st.id, "version": v.version, "kind": st.kind, "title": st.title, "branch_id": branch.id,
                             "inherited": vw["inherited"], "text": content.get("text"), "chart": v.chart_spec,
                             "result": {k: content.get(k) for k in ("columns", "rows", "row_count", "result_hash", "stat")
                                        if content.get(k) is not None},
                             "snapshot": v.result_snapshot, "receipts": v.receipts,
                             "verification": vw["verification_record"]})
        lineage = {"branch_id": branch.id, "name": branch.name, "parent_branch_id": branch.parent_branch_id,
                   "forked_from": {"step_id": branch.forked_from_step_id, "version": branch.forked_from_version}
                   if branch.forked_from_step_id else None, "merged_at": utcnow().isoformat(), "merged_by": actor}
        if body.report_id:
            art = s.get(Artifact, body.report_id)
            if art is None or art.workspace_id != workspace_id or art.type != "report" or \
                    (art.content or {}).get("kind") != "data_thread":
                raise NotFound("report not found")
            content = dict(art.content)
            content["sections"] = [x for x in content.get("sections") or [] if x.get("branch_id") != branch.id] + sections
            content["branches"] = [x for x in content.get("branches") or [] if x.get("branch_id") != branch.id] + [lineage]
            if body.title:
                content["title"] = body.title
            name = art.name
        else:
            content = {"kind": "data_thread", "title": body.title or f"Report from {branch.name}",
                       "container": {"type": branch.container_type, "id": branch.container_id},
                       "sections": sections, "branches": [lineage]}
            name = f"thread-report-{new_id('r')}"
        art = save_artifact(s, workspace_id=workspace_id, type_="report", name=name, content=content,
                            run_id=branch.container_id if branch.container_type == "run" and not body.report_id else
                            (s.get(Artifact, body.report_id).run_id if body.report_id else None),
                            creator_user=user.id, status="draft")
        for sec in sections:
            steps_svc._link(s, workspace_id, ("step", sec["step_id"]), "included_in", (art.type, art.id))
        steps_svc._link(s, workspace_id, ("step_branch", branch.id), "merged_into", (art.type, art.id))
        branch.status = "merged"
        branch.merged_into = list(dict.fromkeys([*(branch.merged_into or []), art.id]))
        emit(workspace_id, "branch.merged", {"branch_id": branch.id, "report_id": art.id, "sections": len(sections),
                                             "excluded": len(excluded)},
             run_id=branch.container_id if branch.container_type == "run" else None, actor=actor, session=s)
        return {"report": {"id": art.id, "version": art.version, "name": art.name, "content": art.content},
                "branch": branch_view(branch), "included": len(sections), "excluded": excluded}
