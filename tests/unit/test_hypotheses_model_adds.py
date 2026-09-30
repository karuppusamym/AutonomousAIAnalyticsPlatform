"""A model adds hypotheses to the profile playbook, it never replaces it (no services). The live P4-V01
run on 2026-09-28 (max_quality preset, evidence/2026-09-28-analytical-benchmark-live.md) found the
model-first path dropping the playbook whenever the model returned 4+ usable proposals: sales recall
fell to 0/3 and overall to 6/10. The playbook's proposals now always stay candidates, and come first."""
from __future__ import annotations

import contextlib
from types import SimpleNamespace

from analystos.agents import investigator as inv


def _proposal(outcome: str, segment: str, method: str = "numeric_by_segment") -> dict:
    return {"statement": f"{outcome} by {segment}", "priority": "high",
            "spec": {"method": method, "outcome": {"column": outcome}, "segment": {"column": segment}}}


PLAYBOOK = [_proposal("returned", "channel", "rate_by_segment"), _proposal("net_amount", "customer_segment"),
            _proposal("shipping_days", "sales_region")]
MODEL = [_proposal("net_amount", "channel"), _proposal("returned", "device_type", "rate_by_segment"),
         _proposal("net_amount", "sales_region"), _proposal("returned", "customer_segment", "rate_by_segment"),
         _proposal("net_amount", "customer_segment")]  # the last one duplicates the playbook


def _run(monkeypatch, model_answer):
    persisted: list[dict] = []

    def accept(ctx, proposals, types, *, origin, seen):
        out = []
        for p in proposals:
            key = p["statement"]
            if key not in seen:
                seen.add(key)
                out.append({**p, "origin": origin, "priority_score": 1.0})
        return out, []

    monkeypatch.setattr("analystos.registries.replay.replay_settings", lambda run: None)
    monkeypatch.setattr(inv, "semantic_types", lambda ctx: {})
    monkeypatch.setattr(inv, "related_tables", lambda ctx, types: {})  # P8-16 joins: none in this scope
    monkeypatch.setattr(inv, "task_output", lambda run_id, key: {})
    monkeypatch.setattr(inv, "carried_forward", lambda ctx: [])
    monkeypatch.setattr(inv, "defer_compile", lambda ctx, purpose, required: required)
    monkeypatch.setattr(inv, "heuristic_proposals", lambda ctx, types, packs=None: [dict(p) for p in PLAYBOOK])
    monkeypatch.setattr(inv.pack_registry, "for_scope", lambda scope, policy: [])
    monkeypatch.setattr(inv, "model_gate", lambda ctx, purpose, payload, deterministic_ok: True)  # model first
    monkeypatch.setattr(inv, "llm_json", lambda *a, **k: (model_answer, "google/gemini-3.5-flash"))
    monkeypatch.setattr(inv, "_accept", accept)
    monkeypatch.setattr(inv, "_prioritise", lambda ctx, accepted: "rules")
    monkeypatch.setattr(inv, "_persist", lambda ctx, accepted, **kw: persisted.extend(accepted) or [])
    monkeypatch.setattr(inv, "session_scope", lambda: contextlib.nullcontext(SimpleNamespace(get=lambda *a, **k: None)))
    monkeypatch.setattr(inv, "platform", lambda: SimpleNamespace(analysis=SimpleNamespace(heuristic_hypotheses_sufficient=6,
                                                                                           max_round1_hypotheses=8)))
    run = SimpleNamespace(id="run_1", objective="why", plan={}, instructions=[], constraints={}, origin={})
    ctx = SimpleNamespace(run=run, scope=None, policy=SimpleNamespace(max_iterations=1), say=lambda *a, **k: None)
    return inv.generate_hypotheses(ctx), persisted


def test_model_proposals_join_the_playbook_instead_of_replacing_it(monkeypatch):
    out, persisted = _run(monkeypatch, {"hypotheses": [dict(p) for p in MODEL]})
    statements = [p["statement"] for p in persisted]
    for p in PLAYBOOK:  # every planted-effect proposal of the playbook is still tested
        assert p["statement"] in statements
    assert "net_amount by channel" in statements and statements.count("net_amount by customer_segment") == 1
    assert [p["origin"] for p in persisted if p["statement"] == "net_amount by customer_segment"] == ["heuristic"]
    assert out["source"].startswith("llm:google/gemini-3.5-flash")


def test_without_a_model_answer_the_playbook_alone_is_used(monkeypatch):
    out, persisted = _run(monkeypatch, None)
    assert [p["statement"] for p in persisted] == [p["statement"] for p in PLAYBOOK]
    assert out["source"].startswith("rules")


def _p(origin, i, method="numeric_by_segment", score=None):
    return {"origin": origin, "statement": f"{origin}{i}", "priority_score": score if score is not None else 1.0,
            "spec": {"method": method, "outcome": {"column": f"o{i}"}}}


def test_a_model_cannot_fill_the_round_with_its_own_proposals():
    """Live held-out run 2026-09-29: five model proposals ranked `high` filled the cap of 8 and the playbook's
    proposals for the planted effects were never tested (3 tasks fell from accepted to missed)."""
    playbook = [_p("heuristic", i, score=0.5) for i in range(10)]
    model = [_p("agent", i, method=m, score=0.9) for i, m in enumerate(
        ["trend", "correlation", "rate_by_segment", "numeric_by_segment", "pareto"])]
    picked = inv._round1_selection(model + playbook, 8)
    assert len(picked) == 8
    assert sum(a["origin"] == "heuristic" for a in picked) == 6 and sum(a["origin"] == "agent" for a in picked) == 2
    assert [a["origin"] for a in picked[:2]] == ["agent", "agent"]  # still ranked by score inside the round


def test_a_short_playbook_leaves_its_unused_slots_to_the_model():
    playbook = [_p("heuristic", i) for i in range(3)]
    model = [_p("agent", i, method="trend") for i in range(6)]
    picked = inv._round1_selection(playbook + model, 8)
    assert sum(a["origin"] == "heuristic" for a in picked) == 3 and sum(a["origin"] == "agent" for a in picked) == 5
    assert [a["statement"] for a in inv._round1_selection(model, 4)] == [a["statement"] for a in inv.diverse_top(model, 4)]
