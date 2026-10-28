"""Spec S13 (Request Desk) without services: the lifecycle table, triage rules and the triage ladder, the policy
floors a model cannot lower, dedupe, the value metrics against fixture baselines, and the service flows
(auto-with-review needs an analyst, SLA breach alert, answer/accept/reopen) on the SQLite control plane."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.desk import states, triage, value
from analystos.desk.states import STATES, TRANSITIONS, can_transition, check_transition, resolution_for, sla_due

# ------------------------------------------------------------------------------------------- state machine
LIFECYCLE = [("new", "triaged"), ("triaged", "auto_answering"), ("auto_answering", "auto_answered"),
             ("auto_answering", "auto_with_review"), ("auto_answering", "escalated"), ("triaged", "in_investigation"),
             ("triaged", "escalated"), ("in_investigation", "auto_with_review"), ("in_investigation", "escalated"),
             ("auto_with_review", "answered"), ("escalated", "answered"), ("answered", "accepted"),
             ("answered", "reopened"), ("auto_answered", "accepted"), ("auto_answered", "reopened"),
             ("reopened", "escalated"), ("escalated", "declined"), ("new", "declined")]


@pytest.mark.parametrize(("src", "dst"), LIFECYCLE)
def test_lifecycle_transitions_are_allowed(src, dst):
    assert can_transition(src, dst)


@pytest.mark.parametrize(("src", "dst"), [("new", "auto_answered"), ("triaged", "answered"), ("escalated", "auto_answered"),
                                          ("accepted", "reopened"), ("declined", "triaged"), ("auto_answering", "accepted"),
                                          ("auto_with_review", "accepted"), ("in_investigation", "answered")])
def test_shortcuts_and_reopening_closed_requests_are_refused(src, dst):
    assert not can_transition(src, dst)
    with pytest.raises(Conflict):
        check_transition(src, dst)


def test_state_table_is_closed_and_terminal_states_are_accepted_and_declined():
    assert set(TRANSITIONS) == set(STATES)
    assert all(dst in STATES for nxt in TRANSITIONS.values() for dst in nxt)
    assert {"accepted", "declined"} == states.TERMINAL
    assert states.QUEUE_STATES <= states.OPEN
    # every open state can still reach a terminal state
    for start in states.OPEN:
        seen, todo = set(), [start]
        while todo:
            s = todo.pop()
            if s not in seen:
                seen.add(s)
                todo += list(TRANSITIONS[s])
        assert seen & states.TERMINAL, start


def test_resolution_counts_reviewed_auto_answers_as_auto():
    assert resolution_for("accepted", "auto") == "auto"
    assert resolution_for("accepted", "auto_with_review") == "auto"
    assert resolution_for("accepted", "human") == "human"
    assert resolution_for("declined", None) == "declined"
    assert resolution_for("answered", "auto") is None


def test_sla_by_priority_with_workspace_override():
    t0 = datetime(2026, 10, 27, 9, tzinfo=UTC)
    assert sla_due(t0, "urgent") == t0 + timedelta(hours=2)
    assert sla_due(t0, "low") == t0 + timedelta(hours=72)
    assert sla_due(t0, "normal", {"normal": 4}) == t0 + timedelta(hours=4)


# ------------------------------------------------------------------------------------------- triage rules
CATALOG = {"metrics": ["payment_failure_rate", "Revenue"], "verified": ["incidents opened per priority"]}


@pytest.mark.parametrize(("text", "cls"), [
    ("Revenue by region last quarter", "R1"),
    ("What is the payment failure rate this month?", "R1"),
    ("How many incidents opened per priority?", "R1"),
    ("Top 10 merchants by refund count in March", "R2"),
    ("How many incidents were opened last week?", "R2"),
    ("Why did failures rise 18% this week?", "R3"),
    ("Send me the weekly ops pack every Monday", "R4"),
    ("Monthly variance report for finance", "R4"),
    ("Tell me if failure rate goes above baseline plus 2pp", "R5"),
    ("Build me a churn dashboard", "R6"),
    ("What counts as an active customer?", "R7"),
    ("How is churn defined?", "R7"),
    ("Send me the raw rows for all refunds", "R8"),
    ("Export the incident table to CSV", "R8"),
    ("Should we enter the Brazilian market next year?", "R9"),
])
def test_rules_classify_the_product_spec_examples(text, cls):
    assert triage.classify_rules(text, CATALOG).request_class == cls


def test_average_daily_value_is_a_slice_not_a_recurring_report():
    assert triage.classify_rules("Average daily order value last month").request_class == "R2"


def test_sensitive_and_export_flags():
    r = triage.classify_rules("Count customers by email address")
    assert r.request_class == "R2" and "sensitive" in r.flags
    e = triage.classify_rules("Download all customer phone numbers")
    assert e.request_class == "R8" and {"export", "sensitive"} <= set(e.flags)


def test_unmatched_text_has_no_rule_class():
    r = triage.classify_rules("hello there team")
    assert r.request_class is None and r.confidence == 0.0


def test_normalized_intent_is_stable_across_case_and_punctuation():
    a = triage.normalize_intent("How many incidents by priority last month?", "R2")
    b = triage.normalize_intent("how many incidents by priority, last month", "R2")
    c = triage.normalize_intent("How many incidents by state last month?", "R2")
    assert a["key"] == b["key"] != c["key"]
    assert a["dims"] == ["priority"] and a["period"] == "last month" and a["outcome"] == "number"


# ------------------------------------------------------------------------------------------- triage ladder
class Resp:
    def __init__(self, data=None, answers=None, model="m"):
        self.data, self.answers, self.model = data, answers, model


class FakeRouter:
    def __init__(self, mode="auto", decision=None, chat=None, ladder=("cache", "rules", "decision", "llm_small")):
        self._mode, self._ladder = mode, list(ladder)
        self.decision, self.chat = decision, chat
        self.calls: list[str] = []
        self.skips: list[str] = []

    def mode(self, purpose):
        return self._mode

    def ladder(self, purpose):
        return self._ladder

    def available(self, purpose, ctx=None, profile=None):
        return (self.decision if profile == "decision" else self.chat) is not None

    def decide(self, purpose, state, questions, ctx=None, profile=None):
        assert profile == "decision" and set(questions["pick"]["criteria"]) == set(states.CLASSES)
        self.calls.append("decision")
        return Resp(answers={"pick": self.decision})

    def complete_json(self, purpose, system, user, ctx=None, max_tokens=None):
        self.calls.append("llm_small")
        return Resp(data=self.chat)

    def record_skip(self, purpose, ctx, *, estimated_tokens, reason, rung="rules"):
        self.skips.append(reason)


def test_confident_rule_answers_without_a_model_and_records_the_saving():
    r = FakeRouter(decision={"choice": "R9", "confidence": 0.99})
    out = triage.triage("How many incidents were opened last week?", router=r)
    assert out.request_class == "R2" and out.answered_by == "rules" and r.calls == [] and len(r.skips) == 1


def test_decision_rung_classifies_what_no_rule_matched():
    r = FakeRouter(decision={"choice": "R2", "confidence": 0.9}, chat={"class": "R9", "confidence": 1})
    out = triage.triage("incidents for the payments team please", router=r)
    assert out.request_class == "R2" and out.answered_by == "decision" and r.calls == ["decision"]


def test_small_model_rung_after_an_unsure_decision():
    r = FakeRouter(decision={"choice": "R1", "confidence": 0.3}, chat={"class": "r7", "confidence": 0.8})
    out = triage.triage("active customer meaning for the board", router=r)
    assert out.request_class == "R7" and out.answered_by == "llm_small" and r.calls == ["decision", "llm_small"]


def test_invalid_model_answers_fall_back_to_a_person():
    r = FakeRouter(decision={"choice": "R42", "confidence": 1}, chat={"class": "grant me admin", "confidence": 1})
    out = triage.triage("hello there team", router=r)
    assert out.request_class == "R9" and out.answered_by == "fallback" and "class" in out.missing


def test_a_model_cannot_lower_an_export_or_a_confident_escalation():
    rules = triage.classify_rules("Export the incident table to CSV")
    assert triage.apply_floors("R2", rules) == "R8"
    rules = triage.classify_rules("Should we enter the Brazilian market next year?")
    assert triage.apply_floors("R1", rules) == "R9"
    # model-first ("always"): the model is asked, but the floor keeps the export with a person
    r = FakeRouter(mode="always", decision={"choice": "R2", "confidence": 0.95})
    out = triage.triage("Export the incident table to CSV", router=r)
    assert out.request_class == "R8" and out.answered_by == "decision" and "policy floor" in out.reason


def test_mode_off_uses_rules_only():
    r = FakeRouter(mode="off", decision={"choice": "R2", "confidence": 1})
    assert triage.triage("hello there team", router=r).request_class == "R9" and r.calls == []


def test_jev_flag_off_drops_only_the_decision_rung_of_a_chat_purpose(pinned_models_config):
    from analystos.contracts.platform import FeatureFlags, LLMSettings, PlatformSettings
    from analystos.llm.router import ModelRouter

    on = ModelRouter(settings_provider=lambda: PlatformSettings(llm=LLMSettings()), api_key_lookup=lambda _e: "k")
    off = ModelRouter(settings_provider=lambda: PlatformSettings(llm=LLMSettings(), features=FeatureFlags(jev_decisions=False)),
                      api_key_lookup=lambda _e: "k")
    assert on.ladder("request_triage") == ["cache", "rules", "decision", "llm_small"]
    assert off.ladder("request_triage") == ["cache", "rules", "llm_small"] and off.mode("request_triage") == "auto"
    assert on.available("request_triage", profile="decision") and on.available("request_triage")
    assert on.candidates("request_triage", __import__("analystos.llm.router", fromlist=["CallContext"]).CallContext(),
                         profile="decision")[0] == "decision"


# ------------------------------------------------------------------------------------------- dedupe
def test_similarity_is_one_for_the_same_words_and_low_for_different_requests():
    from analystos.desk.dedupe import THRESHOLD, similarity

    assert similarity("How many incidents by priority last month?", "how many incidents by priority last month") >= 0.99
    assert similarity("How many incidents by priority last month?", "Why did refunds rise in Brazil?") < THRESHOLD


# ------------------------------------------------------------------------------------------- value math
T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def _r(cls, state, resolution, tta_s=None, minutes=0, cost=0.0):
    return {"class": cls, "state": state, "resolution": resolution, "created_at": T0,
            "first_answer_at": T0 + timedelta(seconds=tta_s) if tta_s is not None else None,
            "human_minutes": minutes, "cost_usd": cost}


def test_value_metrics_math():
    rows = [
        _r("R1", "accepted", "auto", 10, 0, 0.01),
        _r("R1", "accepted", "auto", 20, 0, 0.01),
        _r("R2", "accepted", "auto", 30, 2, 0.02),       # auto_with_review: the analyst spent 2 minutes
        _r("R2", "accepted", "human", 3600, 25, 0.0),
        _r("R3", "declined", "declined", None, 5),
        _r("R9", "escalated", None, None, 0),            # open: not closed, no answer yet
        _r("R2", "declined", "duplicate", None, 0),      # linked duplicate: not work
        _r("R4", "accepted", "human", 7200, 40),         # no baseline for R4
    ]
    out = value.compute(rows, {"R1": 15.0, "R2": 30.0, "R3": 120.0})
    assert out["n"] == 7 and out["duplicates"] == 1
    assert out["closed"] == 6 and out["accepted"] == 5 and out["auto_resolved"] == 3
    assert out["auto_resolution_rate"] == 0.5 and out["accepted_rate"] == round(5 / 6, 4)
    # time to first answer over answered work: [10, 20, 30, 3600, 7200]
    assert out["tta_p50_seconds"] == 30.0 and out["tta_p95_seconds"] == round(3600 + 0.8 * 3600, 1)
    assert out["human_minutes"] == 72
    # saved = (15-0) + (15-0) + (30-2) + (30-25); R4 has no baseline, declined R3 saves nothing
    assert out["est_minutes_saved"] == 63.0 and out["unbaselined"] == 1
    assert out["cost_usd"] == 0.04
    assert out["by_class"]["R1"]["auto_resolution_rate"] == 1.0 and out["by_class"]["R1"]["baseline_minutes"] == 15.0
    assert out["by_class"]["R2"]["est_minutes_saved"] == 33.0


def test_value_metrics_of_nothing_are_empty_not_zero():
    out = value.compute([], {})
    assert out["auto_resolution_rate"] is None and out["tta_p50_seconds"] is None and out["n"] == 0


# ------------------------------------------------------------------------------------------- service (SQLite)
WS = "ws_desk"


@pytest.fixture
def desk_db(sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.db.base import session_scope
    from analystos.db.models import User, Workspace, WorkspaceMember
    from analystos.desk import service

    engine = sqlite_db.kw["bind"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in (
        "request", "request_event", "class_baseline", "context_entry", "semantic_metric", "verified_query", "ask_turn",
        "desk_daily")])
    with session_scope() as s:
        for uid in ("usr_req", "usr_ana", "usr_ana2", "usr_out"):
            s.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
        s.add(Workspace(id=WS, name="desk", created_by="usr_ana", settings={"desk": {"rota": ["usr_ana2", "usr_ana"]}},
                        policy_version=1))
        s.add_all([WorkspaceMember(workspace_id=WS, user_id="usr_req", role="viewer"),
                   WorkspaceMember(workspace_id=WS, user_id="usr_ana", role="analyst"),
                   WorkspaceMember(workspace_id=WS, user_id="usr_ana2", role="analyst")])
    monkeypatch.setattr(service, "_router", lambda: FakeRouter(mode="off"))
    monkeypatch.setattr(service, "_call_ctx", lambda *a, **k: None)
    with session_scope() as s:
        users = {u: s.get(User, u) for u in ("usr_req", "usr_ana", "usr_ana2", "usr_out")}
        for u in users.values():
            s.expunge(u)
    return users


def _session():
    from analystos.db.base import session_scope

    return session_scope()


def test_intake_triages_escalates_with_sla_assignment_and_data_pack(desk_db):
    from analystos.desk import service

    out = service.submit(desk_db["usr_req"], WS, "Should we enter the Brazilian market next year?", priority="high")
    assert out["state"] == "escalated" and out["class"] == "R9"
    assert out["assignee_id"] == "usr_ana2"  # first on the rota, least loaded
    assert "data_pack" not in out and out["answer"] is None  # a requester never sees the analyst's pack
    with _session() as s:
        full = service.get_request(s, desk_db["usr_ana"], out["id"])
    created = datetime.fromisoformat(full["created_at"])
    assert datetime.fromisoformat(full["sla_due_at"]) - created <= timedelta(hours=8, seconds=5)
    assert full["data_pack"]["reason"].startswith("Novel or strategic") and full["data_pack"]["triage"]["class"] == "R9"
    assert [e["type"] for e in full["timeline"]] == ["created", "triaged", "escalated"]
    # the next escalation goes to the other analyst (rota, least loaded)
    nxt = service.submit(desk_db["usr_req"], WS, "Send me the raw rows for all refunds")
    assert nxt["class"] == "R8" and nxt["assignee_id"] == "usr_ana"


def test_dedupe_links(desk_db):
    from analystos.desk import service

    first = service.submit(desk_db["usr_req"], WS, "Should we enter the Brazilian market next year?")
    dup = service.submit(desk_db["usr_ana"], WS, "should we enter the brazilian market next year")
    assert dup["state"] == "declined" and dup["resolution"] == "duplicate" and dup["duplicate_of"] == first["id"]
    assert dup["original"]["id"] == first["id"]
    with _session() as s:
        orig = service.get_request(s, desk_db["usr_ana"], first["id"])
    assert orig["followers"] == ["usr_ana"] and orig["timeline"][-1]["type"] == "follower_added"
    other = service.submit(desk_db["usr_req"], WS, "Should we invest in the Brazilian market next year?")
    assert other["duplicate_of"] is None  # different text: not a duplicate


def test_auto_with_review_requires_analyst(desk_db):
    from analystos.desk import service
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, "How many incidents were opened last week?", process=False)
    with _session() as s:  # the Ask path's outcome for a sensitive or review_ad_hoc answer
        req = s.get(Request, out["id"])
        service.move(s, req, "auto_answering", "desk")
        req.answer_ref = {"via": "auto_with_review", "kind": "ask_turn", "turn_id": "askt_x", "explanation": "42"}
        service.move(s, req, "auto_with_review", "desk")
    with _session() as s:
        seen = service.get_request(s, desk_db["usr_req"], out["id"])
        assert seen["answer"] is None  # prepared, but not shown before an analyst approves it
        with pytest.raises(Forbidden):
            service.act(s, desk_db["usr_req"], out["id"], "answer", {"human_minutes": 0})
    with _session() as s, pytest.raises(Conflict):
        service.act(s, desk_db["usr_req"], out["id"], "accept")  # nothing to accept before the review
    with _session() as s, pytest.raises(InvalidInput):
        service.act(s, desk_db["usr_ana"], out["id"], "answer", {})  # minutes are required
    with _session() as s:
        done = service.act(s, desk_db["usr_ana"], out["id"], "answer", {"human_minutes": 1})
    assert done["state"] == "answered" and done["answer"]["via"] == "auto_with_review" and done["human_minutes"] == 1
    with _session() as s:
        closed = service.act(s, desk_db["usr_req"], out["id"], "accept")
    assert closed["state"] == "accepted" and closed["resolution"] == "auto" and closed["closed_at"]


def test_reopen_escalates_and_human_answer_counts_as_human(desk_db):
    from analystos.desk import service
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, "How many incidents were opened last week?", process=False)
    with _session() as s:
        req = s.get(Request, out["id"])
        service.move(s, req, "auto_answering", "desk")
        req.answer_ref = {"via": "auto", "kind": "ask_turn", "turn_id": "askt_y"}
        service.move(s, req, "auto_answered", "desk")
    with _session() as s:
        assert service.get_request(s, desk_db["usr_req"], out["id"])["answer"]["via"] == "auto"
        with pytest.raises(Forbidden):
            service.act(s, desk_db["usr_ana"], out["id"], "accept")  # only the requester accepts
    with _session() as s:
        re = service.act(s, desk_db["usr_req"], out["id"], "reopen", {"reason": "the number looks too low"})
    assert re["state"] == "escalated" and [e["type"] for e in re["timeline"]][-2:] == ["reopened", "escalated"]
    with _session() as s:
        ans = service.act(s, desk_db["usr_ana"], out["id"], "answer", {"text": "It is 37.", "human_minutes": 12})
        assert ans["answer"]["via"] == "human"
        acc = service.act(s, desk_db["usr_req"], out["id"], "accept")
    assert acc["resolution"] == "human" and acc["human_minutes"] == 12


def test_sla_breach_alert(desk_db):
    from analystos.db.models import Notification
    from analystos.desk import service
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, "Should we enter the Brazilian market next year?", priority="urgent")
    with _session() as s:
        req = s.get(Request, out["id"])
        later = req.sla_due_at.replace(tzinfo=None) + timedelta(minutes=1) if req.sla_due_at.tzinfo is None \
            else req.sla_due_at + timedelta(minutes=1)
        assert service.sweep_sla(s, workspace_id=WS, now=later) == [out["id"]]
        assert service.sweep_sla(s, workspace_id=WS, now=later) == []  # flagged once
    with _session() as s:
        n = s.query(Notification).filter(Notification.workspace_id == WS, Notification.kind == "request_sla").one()
        assert n.user_id == out["assignee_id"] and n.link == {"type": "request", "id": out["id"]}
        detail = service.get_request(s, desk_db["usr_ana"], out["id"])
    assert detail["sla_breached"] and detail["timeline"][-1]["type"] == "sla_breached"


def test_visibility_views_and_actions_are_role_bound(desk_db):
    from analystos.core.errors import NotFound
    from analystos.desk import service

    out = service.submit(desk_db["usr_req"], WS, "Should we enter the Brazilian market next year?")
    with _session() as s:
        with pytest.raises(NotFound):
            service.get_request(s, desk_db["usr_out"], out["id"])  # not a member: same 404 as absent
        assert [r["id"] for r in service.list_requests(s, desk_db["usr_req"], view_name="mine")] == [out["id"]]
        with pytest.raises(NotFound):  # the queue is for analysts; a viewer of the workspace is told nothing more
            service.list_requests(s, desk_db["usr_out"], view_name="queue", workspace_id=WS)
        with pytest.raises(Forbidden):
            service.list_requests(s, desk_db["usr_req"], view_name="queue", workspace_id=WS)
        queue = service.list_requests(s, desk_db["usr_ana2"], view_name="queue", workspace_id=WS)
        assert [r["id"] for r in queue] == [out["id"]] and queue[0]["data_pack"] is not None
        with pytest.raises(Forbidden):
            service.act(s, desk_db["usr_req"], out["id"], "decline", {"reason": "no"})
        with pytest.raises(InvalidInput):
            service.act(s, desk_db["usr_ana"], out["id"], "assign", {"assignee_id": "usr_req"})
        moved = service.act(s, desk_db["usr_ana"], out["id"], "assign", {})
        assert moved["assignee_id"] == "usr_ana"
        declined = service.act(s, desk_db["usr_ana"], out["id"], "decline", {"reason": "out of scope"})
    assert declined["state"] == "declined" and declined["resolution"] == "declined"


def test_value_report_from_requests_and_baselines(desk_db):
    from analystos.desk import service
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, "How many incidents were opened last week?", process=False)
    with _session() as s:
        req = s.get(Request, out["id"])
        service.move(s, req, "auto_answering", "desk")
        req.answer_ref, req.human_minutes = {"via": "auto"}, 0
        service.move(s, req, "auto_answered", "desk")
        service.move(s, req, "accepted", "user:usr_req")
        with pytest.raises(Forbidden):
            service.set_baseline(s, desk_db["usr_ana"], WS, "R2", 30)  # owners set baselines
    with _session() as s:
        from analystos.desk.models import ClassBaseline

        s.add(ClassBaseline(id="cb_1", workspace_id=WS, request_class="R2", baseline_minutes=30, source="practitioner_protocol"))
    with _session() as s:
        rep = service.value_report(s, desk_db["usr_req"], WS)
    assert rep["auto_resolution_rate"] == 1.0 and rep["est_minutes_saved"] == 30.0 and rep["baselines"] == {"R2": 30.0}


# ------------------------------------------------------------------------------------------- MCP
def test_mcp_request_tools_are_granted_and_create_on_the_mcp_channel(monkeypatch):
    from analystos.desk import service
    from analystos.mcp import grants as G
    from analystos.mcp import server

    assert {"request.create", "request.status"} <= set(G.TOOLS) and G.min_role("request.create") == "analyst" == G.min_role("ask")
    assert G.min_role("request.status") == "viewer"
    assert server.TOOL_DEFS["request.create"]["read_only"] is False and server.TOOL_DEFS["request.status"]["read_only"]
    seen = {}

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def expunge(self, _):
            return None

    monkeypatch.setattr(server, "session_scope", lambda: _S())
    monkeypatch.setattr(server, "_service_user", lambda s, p: "svc-user")

    def submit(user, ws, text, *, channel, priority):
        seen.update(user=user, ws=ws, text=text, channel=channel, priority=priority)
        return {"id": "rq_1", "workspace_id": ws, "state": "auto_answered", "class": "R2", "data_pack": {"secret": 1}}

    monkeypatch.setattr(service, "submit", submit)
    out = server._tool_request_create(None, "ws_1", {"text": "How many incidents last week?"})
    assert seen == {"user": "svc-user", "ws": "ws_1", "text": "How many incidents last week?", "channel": "mcp",
                    "priority": "normal"}
    assert out["state"] == "auto_answered" and "data_pack" not in out  # only the requester's fields go out


def test_r3_starts_an_investigation_and_its_completion_goes_to_review(desk_db, monkeypatch):
    from types import SimpleNamespace

    from analystos.db.models import AnalysisRun
    from analystos.desk import service
    from analystos.services import runs

    started = {}

    def create_run(user, ws, *, objective, origin):
        started.update(user=user.id, ws=ws, objective=objective, origin=origin)
        return SimpleNamespace(id="run_desk")

    monkeypatch.setattr(runs, "create_run", create_run)
    out = service.submit(desk_db["usr_req"], WS, "Why did failures rise 18% this week?")
    assert out["class"] == "R3" and out["state"] == "in_investigation" and out["run_ids"] == ["run_desk"]
    assert started == {"user": "usr_req", "ws": WS, "objective": "Why did failures rise 18% this week?",
                       "origin": {"type": "request", "request_id": out["id"]}}
    with _session() as s:
        s.add(AnalysisRun(id="run_desk", workspace_id=WS, objective="x", requested_by="usr_req", status="COMPLETED"))
    with _session() as s:
        seen = service.get_request(s, desk_db["usr_req"], out["id"])
    assert seen["state"] == "auto_with_review" and seen["answer"] is None and seen["assignee_id"] == "usr_ana2"


def test_r7_answers_from_a_reviewed_definition_or_escalates(desk_db):
    from analystos.db.models import ContextEntry
    from analystos.desk import service

    with _session() as s:
        s.add(ContextEntry(id="ce_1", workspace_id=WS, kind="term", name="Active customer", synonyms=["active user"],
                           body="A customer with an order in the last 90 days.", trusted=True))
    out = service.submit(desk_db["usr_req"], WS, "What counts as an active customer?")
    assert out["class"] == "R7" and out["state"] == "auto_answered"
    assert out["answer"]["entry_id"] == "ce_1" and out["answer"]["definition"].startswith("A customer")
    miss = service.submit(desk_db["usr_req"], WS, "What counts as a dormant merchant?")
    assert miss["state"] == "escalated" and miss["assignee_id"]


# ------------------------------------------------------------------------------------------- S13 remainder
def test_daily_rows_split_by_day_created_and_class_with_an_all_row():
    from analystos.desk.rollup import daily_rows

    d1, d2 = T0, T0 + timedelta(days=1)
    rows = [{**_r("R1", "accepted", "auto", 10), "created_at": d1}, {**_r("R2", "accepted", "human", 60, 20), "created_at": d1},
            {**_r("R2", "escalated", None), "created_at": d2}]
    out = daily_rows(rows, {"R1": 15.0, "R2": 30.0})
    keys = [(r["day"].isoformat(), r["request_class"]) for r in out]
    assert keys == [("2026-10-01", "all"), ("2026-10-01", "R1"), ("2026-10-01", "R2"), ("2026-10-02", "all"),
                    ("2026-10-02", "R2")]
    day1 = out[0]
    assert day1["n"] == 2 and day1["auto_resolution_rate"] == 0.5 and day1["est_minutes_saved"] == 25.0
    assert out[3]["closed"] == 0 and out[3]["auto_resolution_rate"] is None


def _closed_auto(service, desk_db, text):
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, text, process=False)
    with _session() as s:
        req = s.get(Request, out["id"])
        service.move(s, req, "auto_answering", "desk")
        req.answer_ref, req.human_minutes = {"via": "auto"}, 0
        service.move(s, req, "auto_answered", "desk")
        service.move(s, req, "accepted", "user:usr_req")
    return out


def test_nightly_rollup_runs_once_per_night_and_feeds_value_and_export(desk_db):
    from analystos.desk import rollup, service
    from analystos.desk.models import ClassBaseline, DeskDaily

    _closed_auto(service, desk_db, "How many incidents were opened last week?")
    with _session() as s:
        s.add(ClassBaseline(id="cb_r2", workspace_id=WS, request_class="R2", baseline_minutes=20, source="assumed"))
    first = rollup.maybe_rollup_nightly()
    assert first == {"workspaces": 1, "rows": 2, "failed": []}  # today: the `all` row and the R2 row
    assert rollup.maybe_rollup_nightly() is None  # claimed already tonight
    assert rollup.maybe_rollup_nightly(force=True)["rows"] == 2  # re-running replaces, never duplicates
    with _session() as s:
        assert s.query(DeskDaily).count() == 2
        rep = service.value_report(s, desk_db["usr_req"], WS)
        csv_text = service.value_export(s, desk_db["usr_req"], WS)
    assert [d["class"] for d in rep["daily"]] == ["all"] and rep["daily"][0]["est_minutes_saved"] == 20.0
    assert rep["rollup_computed_at"]
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("day,class,n,") and len(lines) == 3 and "How many" not in csv_text  # aggregates only


def test_refresh_is_for_analysts_and_ranges_are_bounded(desk_db):
    from analystos.desk import service

    _closed_auto(service, desk_db, "How many incidents were opened last week?")
    with _session() as s:
        with pytest.raises(Forbidden):
            service.refresh_value(s, desk_db["usr_req"], WS)
        assert service.refresh_value(s, desk_db["usr_ana"], WS)["rows"] == 2
        with pytest.raises(InvalidInput):
            service.value_export(s, desk_db["usr_ana"], WS, start=T0 - timedelta(days=400), end=T0)


def test_sla_sweep_loop_flags_each_workspace_as_its_owner(desk_db):
    from analystos.desk import rollup, service
    from analystos.desk.models import Request

    out = service.submit(desk_db["usr_req"], WS, "Should we enter the Brazilian market next year?", priority="urgent")
    with _session() as s:
        due = s.get(Request, out["id"]).sla_due_at
    later = (due if due.tzinfo else due.replace(tzinfo=UTC)) + timedelta(minutes=5)
    assert rollup.sweep_sla_all(now=later) == 1
    assert rollup.sweep_sla_all(now=later) == 0


@pytest.mark.parametrize(("text", "cron"), [
    ("Send me the weekly ops pack every Monday", "0 9 * * 1"),
    ("Monthly variance report for finance", "0 9 1 * *"),
    ("A quarterly summary of incidents", "0 9 1 1,4,7,10 *"),
    ("Email me a daily report of new incidents", "0 9 * * *"),
    ("Each Friday send the incident summary", "0 9 * * 5"),
])
def test_r4_becomes_a_draft_schedule(text, cron):
    from analystos.desk.proposals import propose

    p = propose("R4", text)
    assert p["kind"] == "schedule" and p["body"]["cron"] == cron and p["body"]["kind"] == "report"
    assert "approval" in p["approval"]


def test_r5_becomes_a_draft_monitor_with_the_stated_threshold():
    from analystos.desk.proposals import propose

    p = propose("R5", "Tell me if failure rate goes above baseline + 2pp")
    assert p["body"]["kind"] == "metric_drift" and p["body"]["config"] | {"objective": None} ==         {"op": ">", "value": 2.0, "unit": "pp", "objective": None}
    q = propose("R5", "Alert me when backlog drops below 40")
    assert q["body"]["kind"] == "metric_threshold" and q["body"]["config"]["op"] == "<" and q["body"]["config"]["value"] == 40
    r = propose("R5", "Let me know when something odd happens")
    assert "op" not in r["body"]["config"] and "the threshold" in r["needs"]
    assert propose("R2", "How many incidents?") is None


def test_r4_request_escalates_with_its_draft_and_creates_nothing(desk_db):
    from analystos.desk import service

    out = service.submit(desk_db["usr_req"], WS, "Send me the weekly ops pack every Monday")
    assert out["class"] == "R4" and out["state"] == "escalated" and "data_pack" not in out
    with _session() as s:
        pack = service.get_request(s, desk_db["usr_ana"], out["id"])["data_pack"]
    assert pack["proposal"]["kind"] == "schedule" and "draft is prepared" in pack["reason"]
