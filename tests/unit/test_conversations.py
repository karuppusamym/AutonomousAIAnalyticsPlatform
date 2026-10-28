"""Spec S04 without services: v2 conversations over Ask threads, versioned state and branches, summaries under the
numbers guard, typed corrections and memory tiers, deletion cascade, legal hold, retention, MCP grants."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from analystos.conversations import corrections, retention, summaries
from analystos.conversations import service as conv
from analystos.conversations import state as st
from analystos.conversations.models import (
    ConversationCorrection,
    ConversationState,
    ConversationSummary,
)
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.db.models import AskThread, AskTurn, AuditEvent, User, Workspace, WorkspaceMember

TABLES = ("tenant", "tenant_member", "app_user", "workspace", "workspace_member", "workspace_policy", "ask_thread",
          "ask_turn", "conversation_state", "conversation_summary", "conversation_correction",
          "context_ledger_entry", "context_ledger_change", "source_column", "knowledge_suggestion", "knowledge_pack", "knowledge_document", "audit_event",
          "run_event", "lineage_edge", "evidence_citation_set", "source", "source_asset", "request", "request_event",
          "context_entry", "verification_record", "query_execution", "model_call", "decision")


@pytest.fixture
def db(monkeypatch):
    from analystos.db import base, models
    from analystos.events import bus

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
                           json_serializer=base.json_dumps)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")

    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in TABLES])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(base, "SessionLocal", lambda: factory())
    monkeypatch.setattr(bus, "_redis", False)
    yield factory
    engine.dispose()


def _user(s, name: str, admin: bool = False) -> User:
    u = User(id=new_id("usr"), email=f"{name}@t.local", name=name, password_hash="!", is_admin=admin, active=True,
             attributes={})
    s.add(u)
    return u


@pytest.fixture
def world(db):
    with db() as s:
        alice, bob, owner = _user(s, "alice"), _user(s, "bob"), _user(s, "owner")
        s.flush()
        ws = Workspace(id=new_id("ws"), name="w", created_by=owner.id, status="active", settings={}, policy_version=1)
        other = Workspace(id=new_id("ws"), name="other", created_by=owner.id, status="active", settings={},
                          policy_version=1)
        s.add_all([ws, other])
        s.flush()
        for u, role in ((alice, "analyst"), (bob, "analyst"), (owner, "owner")):
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=u.id, role=role))
        s.add(WorkspaceMember(workspace_id=other.id, user_id=alice.id, role="analyst"))
        s.commit()
        for u in (alice, bob, owner):
            s.expunge(u)
        return SimpleNamespace(ws=ws.id, other=other.id, alice=alice, bob=bob, owner=owner, db=db)


def _ledger(s, ref_table: str, ref_id: str | None = None, *, open_only: bool = True) -> list:
    """The S03 context ledger's rows projecting a conversation owner table."""
    from analystos.context.models import ContextLedgerEntry as E

    q = select(E).where(E.ref_table == ref_table)
    if ref_id is not None:
        q = q.where(E.ref_id == ref_id)
    if open_only:
        q = q.where(E.valid_to.is_(None))
    return list(s.scalars(q.order_by(E.version)))


def _conversation(w, user, title="Incidents") -> str:
    with w.db() as s:
        c = conv.create(s, s.merge(user), w.ws, title=title, channel="api")
        s.commit()
        return c.id


def _turn(w, conversation_id: str, question: str, *, columns=("n",), rows=((42,),), status="answered",
          answered_by="model") -> str:
    """A finished message as the Ask path persists it, then its state step (as _persist_turn does)."""
    with w.db() as s:
        c = s.get(AskThread, conversation_id)
        seq = st.last_seq(s, c) + 1
        result = {"query_id": new_id("qry"), "columns": list(columns), "rows": [list(r) for r in rows],
                  "row_count": len(rows), "result_hash": f"h{seq}", "referenced_assets": ["stg.incident"]}
        t = AskTurn(id=new_id("askt"), thread_id=c.id, workspace_id=c.workspace_id, user_id=c.user_id, seq=seq,
                    question=question, parameters={}, status=status, route="generate", answered_by=answered_by,
                    result=result if status == "answered" else None, attempts=[], stages=[], decisions=[],
                    provenance={"governance": "ad_hoc"}, promotions=[],
                    refusal=None if status == "answered" else {"kind": "ambiguous"})
        s.add(t)
        s.flush()
        st.on_turn(s, t, f"user:{c.user_id}")
        s.commit()
        return t.id


def _correct(w, user, conversation_id, term, meaning, scope="session"):
    with w.db() as s:
        me = s.merge(user)
        out = corrections.correct(s, me, conv.load(s, me, conversation_id), term=term, meaning=meaning, scope=scope)
        s.commit()
        return out


# ------------------------------------------------------------------------------------ T1: conversations
def test_conversation_is_an_ask_thread_with_tenant_and_channel(world):
    cid = _conversation(world, world.alice)
    with world.db() as s:
        view = conv.view(s, conv.load(s, s.merge(world.alice), cid))
    assert view["channel"] == "api" and view["owner_id"] == world.alice.id and view["participants"] == [world.alice.id]
    assert "tenant_id" in view and view["legal_hold"] is False and view["state_version"] == 0
    with world.db() as s, pytest.raises(InvalidInput):
        conv.create(s, s.merge(world.alice), world.ws, channel="fax")


def test_only_the_participant_reads_messages_and_lists_only_their_own(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many incidents?")
    with world.db() as s:
        bob = s.merge(world.bob)
        with pytest.raises(NotFound):
            conv.detail(s, bob, cid)
        with pytest.raises(NotFound):
            conv.get_state(s, bob, cid)
        assert conv.list_mine(s, bob, workspace_id=world.ws) == []
        mine = conv.list_mine(s, s.merge(world.alice))
        assert [c["id"] for c in mine] == [cid] and mine[0]["message_count"] == 1
        # a platform admin gets no v2 bypass to raw messages either
        admin = _user(s, "root", admin=True)
        s.flush()
        with pytest.raises(NotFound):
            conv.detail(s, admin, cid)


def test_a_conversation_in_another_workspace_is_a_404_through_a_workspace_path(world):
    cid = _conversation(world, world.alice)
    with world.db() as s, pytest.raises(NotFound):
        conv.load(s, s.merge(world.alice), cid, world.other)


# ------------------------------------------------------------------------------------ T2: state and branches
def test_state_is_versioned_per_message_and_holds_handles_not_rows(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many incidents?", rows=((42,),))
    _turn(world, cid, "By priority?", columns=("priority", "n"), rows=(("P1", 3), ("P2", 9)))
    with world.db() as s:
        out = conv.get_state(s, s.merge(world.alice), cid)
    assert out["version"] == 2 and out["at_seq"] == 2 and out["cause"] == "message"
    handles = out["state"]["result_handles"]
    assert [h["seq"] for h in handles] == [1, 2] and all("rows" not in h for h in handles)
    assert out["state"]["sources"] == ["stg.incident"]


def test_branch_restores_state(world):
    cid = _conversation(world, world.alice)
    m1 = _turn(world, cid, "How many incidents?")
    _correct(world, world.alice, cid, "active", "logged in within 30 days")
    m2 = _turn(world, cid, "How many active users?", rows=((17,),))
    _correct(world, world.alice, cid, "region", "the customer's billing region")  # after m2, before m3
    with world.db() as s:
        expected = st.at_message(s, cid, 2)
        expected_state, expected_version = dict(expected.state), expected.version
    _turn(world, cid, "And by region?", columns=("region", "n"), rows=(("EU", 5),))
    _correct(world, world.alice, cid, "active", "ordered in the last quarter")  # after m3: not in the branch
    with world.db() as s:
        me = s.merge(world.alice)
        branch = conv.branch(s, me, cid, m2)
        s.commit()
    assert branch["parent_conversation_id"] == cid and branch["branch_from_message_id"] == m2
    assert branch["restored_state"] == {"conversation_id": cid, "version": expected_version}
    with world.db() as s:
        me = s.merge(world.alice)
        restored = conv.get_state(s, me, branch["id"])
        assert restored["state"] == expected_state and restored["cause"] == "branch" and restored["at_seq"] == 2
        terms = {c["term"]: c["meaning"] for c in restored["state"]["clarifications"]}
        assert terms == {"active": "logged in within 30 days", "region": "the customer's billing region"}
        detail = conv.detail(s, me, branch["id"])
        assert [(m["id"], m["inherited"]) for m in detail["messages"]] == [(m1, True), (m2, True)]
        # the next message in the branch continues the numbering and builds on the branch point's answer
        c = s.get(AskThread, branch["id"])
        assert st.last_seq(s, c) == 2 and conv.previous_answer(s, c, 3).id == m2
        # the parent is unchanged
        assert conv.get_state(s, me, cid)["version"] == 6


def test_branch_of_a_branch_inherits_through_both(world):
    cid = _conversation(world, world.alice)
    m1 = _turn(world, cid, "q1")
    m2 = _turn(world, cid, "q2")
    with world.db() as s:
        me = s.merge(world.alice)
        b1 = conv.branch(s, me, cid, m2)
        s.commit()
    b1_m3 = _turn(world, b1["id"], "q3 in branch")
    with world.db() as s:
        me = s.merge(world.alice)
        b2 = conv.branch(s, me, b1["id"], m1)  # an inherited message of b1
        s.commit()
        assert [m["id"] for m in conv.detail(s, me, b2["id"])["messages"]] == [m1]
        assert [m["id"] for m in conv.detail(s, me, b1["id"])["messages"]] == [m1, m2, b1_m3]
        with pytest.raises(NotFound):
            conv.branch(s, me, b2["id"], m2)  # m2 is past b2's branch point


def test_state_patch_checks_scope_and_version(world):
    cid = _conversation(world, world.alice)
    with world.db() as s:
        c = s.get(AskThread, cid)
        row = st.patch(s, c, {"filters": [{"column": "priority", "op": "=", "value": "P1"}], "time_window": "last 30 days"},
                       actor="user:a", allowed_sources={"stg.incident"})
        assert row.version == 1 and row.state["filters"][0]["column"] == "priority"
        with pytest.raises(NotFound):
            st.patch(s, c, {"sources": ["secret.payroll"]}, actor="user:a", allowed_sources={"stg.incident"})
        with pytest.raises(InvalidInput):
            st.patch(s, c, {"clarifications": []}, actor="user:a", allowed_sources=set())
        from analystos.core.errors import PreconditionFailed

        with pytest.raises(PreconditionFailed):
            st.patch(s, c, {"time_window": None}, actor="user:a", allowed_sources=set(), expected_version=0)


# ------------------------------------------------------------------------------------ T3: summaries
def _values_of(turn: AskTurn) -> list[float]:
    return summaries._numbers(turn.result or {})


def test_summary_numbers_cite_turns(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many P1 incidents in the last 30 days?", columns=("n",), rows=((42,),))
    _turn(world, cid, "Average resolution hours?", columns=("avg_hours",), rows=((7.25,),))
    _turn(world, cid, "Which team is worst?", status="clarify")

    def model(payload):
        assert all("rows" not in m for m in payload["messages"])  # only single-row aggregates leave the platform
        return {"answers": [
            {"seq": 1, "claim": "There were 42 P1 incidents in the window."},
            {"seq": 2, "claim": "Resolution took 9.5 hours on average."},  # invented: not in message 2's result
            {"seq": 7, "claim": "Something with 3 numbers."},  # cites no answered message
        ]}, "fake-small"

    out = summaries.summarize(world.alice, cid, "manual", llm=model)
    s_ = out["summary"]
    assert [q["seq"] for q in s_["questions"]] == [1, 2, 3]
    assert [q["seq"] for q in s_["open_questions"]] == [3]
    claims = {a["evidence_ref"]["seq"]: a for a in s_["answers"]}
    assert claims[1]["claim"] == "There were 42 P1 incidents in the window." and claims[1]["by"] == "model"
    assert claims[2]["by"] == "rules" and "7.25" in claims[2]["claim"]  # the invented number never reaches the summary
    dropped = {d["claim"] for d in out["guard"]["dropped"]}
    assert dropped == {"Resolution took 9.5 hours on average.", "Something with 3 numbers."}
    assert out["produced_by"] == "model:fake-small"
    with world.db() as s:  # every published number binds to the computed result of the message it cites
        for a in s_["answers"]:
            turn = s.get(AskTurn, a["evidence_ref"]["message_id"])
            assert summaries.check_claim(a["claim"], turn)["ok"], a
            assert a["evidence_ref"]["query_id"] == turn.result["query_id"]


def test_rules_summary_needs_no_model_and_goes_to_the_ledger_as_personal_by_default(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "Top 5 groups by incidents", columns=("group", "n"), rows=(("Network", 3), ("DB", 2)))
    out = summaries.summarize(world.alice, cid, "close", use_model=False)
    assert out["produced_by"] == "rules" and out["trust"] == "personal"
    assert out["summary"]["answers"][0]["claim"] == "Top 5 groups by incidents: 2 rows"
    with world.db() as s:
        [entry] = _ledger(s, "conversation_summary")
        assert entry.kind == "conversation_summary" and entry.ref_id == out["id"]
        assert entry.trust == "personal" and entry.owner_id == world.alice.id and entry.embedding
        assert entry.refs == ["stg.incident"] and entry.source == "conversation"
        assert "Network" not in entry.summary  # never raw rows


def test_a_newer_summary_retires_the_older_projection(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many?")
    first = summaries.summarize(world.alice, cid, "manual", use_model=False)
    _turn(world, cid, "And now?", rows=((43,),))
    second = summaries.summarize(world.alice, cid, "manual", use_model=False)
    with world.db() as s:
        assert [e.ref_id for e in _ledger(s, "conversation_summary")] == [second["id"]]
        assert [e.valid_to is not None for e in _ledger(s, "conversation_summary", first["id"], open_only=False)] == [True]


def test_ledger_retrieval_shows_a_personal_summary_to_its_owner_only(world):
    from analystos.context import retrieval

    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many incidents?")
    summaries.summarize(world.alice, cid, "manual", use_model=False)

    def access(user, ws):
        return retrieval.Access(workspace_id=ws, user_id=user.id, role="analyst", assets={"stg.incident"}, denied=set(),
                                known_assets={"stg.incident"})

    with world.db() as s:
        def found(user, ws):
            return [e.kind for e in retrieval.candidates(s, access(user, ws), kinds=["conversation_summary"],
                                                         trusts=retrieval.MODEL_TRUSTS)]
        assert found(world.alice, world.ws) == ["conversation_summary"]
        assert found(world.bob, world.ws) == []  # personal: its author only
        assert found(world.alice, world.other) == []  # another workspace
        narrow = retrieval.Access(workspace_id=world.ws, user_id=world.alice.id, role="analyst", assets=set(),
                                  denied=set(), known_assets={"stg.incident"})
        assert retrieval.candidates(s, narrow, kinds=["conversation_summary"], trusts=retrieval.MODEL_TRUSTS) == []


def test_shared_workspace_summaries_are_workspace_trust(world, monkeypatch):
    monkeypatch.setattr(summaries, "_shared", lambda s, ws: True)
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many?")
    assert summaries.summarize(world.alice, cid, "manual", use_model=False)["trust"] == "workspace"


def test_idle_conversations_get_a_summary_once(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many?")
    later = utcnow() + timedelta(minutes=31)
    assert summaries.summarize_idle(later) == [cid]
    assert summaries.summarize_idle(later) == []  # nothing new since
    assert summaries.summarize_idle(utcnow()) == []



def test_idle_sweep_skips_archived_workspaces(world):
    """An archived workspace's older conversations cannot be summarized; they must not fill the batch and starve
    the live ones (seen on a dev database with hundreds of archived demo conversations)."""
    stale = _conversation(world, world.alice)
    _turn(world, stale, "Old question")
    live = _conversation(world, world.alice, "live")
    _turn(world, live, "How many?")
    with world.db() as s:
        ws = s.get(Workspace, world.ws)
        ws.status, ws.deleted_at = "disabled", utcnow()
        for turn in s.scalars(select(AskTurn).where(AskTurn.thread_id == stale)):
            turn.created_at = utcnow() - timedelta(hours=1)  # older, so it would come first in the batch
        s.add(Workspace(id="ws_live", name="live", created_by=world.owner.id, status="active", settings={}, policy_version=1))
        s.flush()
        s.get(AskThread, live).workspace_id = "ws_live"
        s.add(WorkspaceMember(workspace_id="ws_live", user_id=world.alice.id, role="analyst"))
        s.commit()
    later = utcnow() + timedelta(minutes=31)
    assert summaries.summarize_idle(later, limit=1) == [live]

# ------------------------------------------------------------------------------------ T4: corrections
def test_correction_applies_to_the_session_at_once(world):
    cid = _conversation(world, world.alice)
    out = _correct(world, world.alice, cid, "Active", "logged in within 30 days")
    assert out["correction"]["status"] == "active" and out["state_version"] == 1
    with world.db() as s:
        mem = corrections.memory(s, world.alice.id, world.ws, cid)
        assert [(i["term_key"], i["scope"]) for i in mem["items"]] == [("active", "session")]
        other = _conversation(world, world.alice, "second")
        assert corrections.memory(s, world.alice.id, world.ws, other)["items"] == []


def test_personal_memory_isolated(world):
    a1 = _conversation(world, world.alice)
    out = _correct(world, world.alice, a1, "active", "logged in within 30 days", scope="personal")
    assert out["correction"]["expires_at"]
    a2, b1 = _conversation(world, world.alice, "later"), _conversation(world, world.bob, "bob's")
    with world.db() as s:
        assert [i["scope"] for i in corrections.memory(s, world.alice.id, world.ws, a2)["items"]] == ["personal"]
        assert corrections.memory(s, world.bob.id, world.ws, b1)["items"] == []
        assert corrections.memory(s, world.alice.id, world.other, None)["items"] == []  # another workspace
        assert corrections.mine(s, s.merge(world.bob), world.ws) == []
        with pytest.raises(NotFound):
            corrections.load_correction(s, s.merge(world.bob), out["correction"]["id"])
        [entry] = _ledger(s, "conversation_correction", out["correction"]["id"])
        assert entry.kind == "correction" and entry.trust == "personal" and entry.owner_id == world.alice.id


def test_a_newer_personal_meaning_supersedes_the_older(world):
    a1 = _conversation(world, world.alice)
    first = _correct(world, world.alice, a1, "active", "logged in within 30 days", scope="personal")["correction"]
    _correct(world, world.alice, a1, "Active", "ordered within 90 days", scope="personal")
    with world.db() as s:
        assert s.get(ConversationCorrection, first["id"]).status == "superseded"
        mine = corrections.mine(s, s.merge(world.alice), world.ws)
        assert [m["meaning"] for m in mine] == ["ordered within 90 days"]
        assert _ledger(s, "conversation_correction", first["id"], open_only=False) == []  # erased, not just retired


def test_team_correction_applies_to_others_only_after_approval(world):
    """AT-18 (unit half; the Postgres test approves through the knowledge review)."""
    a1 = _conversation(world, world.alice)
    out = _correct(world, world.alice, a1, "active", "logged in within 30 days", scope="workspace_proposed")
    corr = out["correction"]
    assert corr["status"] == "proposed" and corr["suggestion_id"]
    b1, a2 = _conversation(world, world.bob, "bob's"), _conversation(world, world.alice, "alice later")
    with world.db() as s:
        assert [i["scope"] for i in corrections.memory(s, world.alice.id, world.ws, a1)["items"]] == ["session"]
        assert corrections.memory(s, world.alice.id, world.ws, a2)["items"] == []
        assert corrections.memory(s, world.bob.id, world.ws, b1)["items"] == []
        from analystos.db.models import KnowledgeSuggestion

        sug = s.get(KnowledgeSuggestion, corr["suggestion_id"])
        assert sug.origin == corrections.ORIGIN and sug.kind == "definition" and sug.status == "pending"
        corrections.on_suggestions_decided(s, [(sug, "approved")])
        s.commit()
    with world.db() as s:
        mem = corrections.memory(s, world.bob.id, world.ws, b1)
        assert [(i["term"], i["meaning"], i["scope"]) for i in mem["items"]] == [
            ("active", "logged in within 30 days", "workspace")]
        [entry] = _ledger(s, "conversation_correction", corr["id"])
        assert entry.trust == "approved" and entry.kind == "correction"


def test_personal_meaning_never_silently_replaces_the_team_definition(world):
    a1 = _conversation(world, world.alice)
    team = _correct(world, world.alice, a1, "active", "logged in within 30 days", scope="workspace_proposed")
    with world.db() as s:
        from analystos.db.models import KnowledgeSuggestion

        corrections.on_suggestions_decided(s, [(s.get(KnowledgeSuggestion, team["correction"]["suggestion_id"]),
                                                "approved")])
        s.commit()
    b1 = _conversation(world, world.bob)
    _correct(world, world.bob, b1, "active", "paid in the last 90 days", scope="personal")
    b2 = _conversation(world, world.bob, "bob later")
    with world.db() as s:
        mem = corrections.memory(s, world.bob.id, world.ws, b2)
    assert [(i["scope"], i["meaning"]) for i in mem["items"]] == [("personal", "paid in the last 90 days")]
    assert mem["overrides"] == [{"term": "active", "used": "personal", "meaning": "paid in the last 90 days",
                                 "team_meaning": "logged in within 30 days",
                                 "team_correction_id": team["correction"]["id"]}]


def test_promotion_flow_session_to_personal_to_team(world):
    a1 = _conversation(world, world.alice)
    sess = _correct(world, world.alice, a1, "churn", "no order in 180 days")["correction"]
    with world.db() as s:
        me = s.merge(world.alice)
        c = conv.load(s, me, a1)
        personal = corrections.promote(s, me, c, corrections.load_correction(s, me, sess["id"]), "personal")["correction"]
        assert personal["scope"] == "personal" and personal["promoted_from"] == sess["id"]
        team = corrections.promote(s, me, c, corrections.load_correction(s, me, personal["id"]), "workspace_proposed")
        assert team["correction"]["scope"] == "workspace_proposed" and team["correction"]["suggestion_id"]
        with pytest.raises(InvalidInput):
            corrections.promote(s, me, c, corrections.load_correction(s, me, team["correction"]["id"]), "personal")
        with pytest.raises(NotFound):  # someone else's correction
            corrections.load_correction(s, s.merge(world.bob), sess["id"])


def test_correction_text_is_bounded_and_typed(world):
    a1 = _conversation(world, world.alice)
    with world.db() as s:
        me = s.merge(world.alice)
        c = conv.load(s, me, a1)
        for kwargs in ({"term": " ", "meaning": "x", "scope": "session"},
                       {"term": "t", "meaning": "x" * 2001, "scope": "session"},
                       {"term": "t", "meaning": "x", "scope": "global"}):
            with pytest.raises(InvalidInput):
                corrections.correct(s, me, c, **kwargs)
        out = corrections.correct(s, me, c, term="t\x07erm", meaning="  a\nb ", scope="session")
        assert out["correction"]["term"] == "t erm" and out["correction"]["meaning"] == "a b"


def test_memory_reaches_the_generation_prompt_as_data(monkeypatch):
    from analystos.agents import sql_agent

    monkeypatch.setattr(sql_agent, "catalog_for_prompt", lambda ctx, **kw: "catalog")
    monkeypatch.setattr(sql_agent, "compile_for", lambda ctx, purpose, required, **kw: required)
    ctx = SimpleNamespace(conversation_memory={"clarifications": [{"term": "active", "meaning": "30 days", "from": "session"}]})
    _, required, _ = sql_agent.generation_context(ctx, "how many active users")
    assert required["conversation_clarifications"] == [{"term": "active", "meaning": "30 days", "from": "session"}]
    _, required, _ = sql_agent.generation_context(SimpleNamespace(), "how many active users")
    assert "conversation_clarifications" not in required


# ------------------------------------------------------------------------------------ T5: retention, delete, hold
def test_retention_delete_cascades_embeddings(world):
    cid = _conversation(world, world.alice)
    _turn(world, cid, "How many?")
    _correct(world, world.alice, cid, "active", "30 days")
    personal = _correct(world, world.alice, cid, "region", "billing region", scope="personal")["correction"]
    team = _correct(world, world.alice, cid, "churn", "no order in 180 days", scope="workspace_proposed")["correction"]
    summaries.summarize(world.alice, cid, "manual", use_model=False)
    with world.db() as s:
        derived = [*_ledger(s, "conversation_summary"), *_ledger(s, "conversation_correction")]
        assert len(derived) == 2 and all(e.embedding for e in derived)  # summary, personal correction
        assert _ledger(s, "knowledge_suggestion")  # the team proposal is in the ledger as its suggestion
        with pytest.raises(NotFound):
            conv.delete(s, s.merge(world.bob), cid)
        out = conv.delete(s, s.merge(world.alice), cid, reason="user request")
        s.commit()
    assert out["messages"] == 1 and out["summaries"] == 1 and out["memory_entries"] == 2 and out["embeddings"] == 2
    assert out["team_corrections_kept"] == 1
    with world.db() as s:
        for model in (AskTurn, ConversationState, ConversationSummary):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model
        assert s.get(AskThread, cid) is None and s.get(ConversationCorrection, personal["id"]) is None
        from analystos.context.models import ContextLedgerChange

        assert _ledger(s, "conversation_summary", open_only=False) == []
        assert _ledger(s, "conversation_correction", open_only=False) == []
        assert {c.entry_id for c in s.scalars(select(ContextLedgerChange))} & {e.id for e in derived} == set()
        kept = s.get(ConversationCorrection, team["id"])
        assert kept is not None and kept.conversation_id is None
        tomb = s.scalar(select(AuditEvent).where(AuditEvent.action == "conversation.deleted"))
        assert tomb.target == cid and tomb.details["tombstone"] and tomb.details["messages"] == 1
        assert "How many" not in str(tomb.details) and "billing" not in str(tomb.details)


def test_legal_hold_blocks_deletion_and_needs_a_tenant_officer(world):
    cid = _conversation(world, world.alice)
    with world.db() as s:
        with pytest.raises(Forbidden):
            conv.set_legal_hold(s, s.merge(world.alice), cid, True, "litigation")
        admin = _user(s, "root", admin=True)
        s.flush()
        with pytest.raises(InvalidInput):
            conv.set_legal_hold(s, admin, cid, True, "")
        assert conv.set_legal_hold(s, admin, cid, True, "litigation 42")["legal_hold"] is True
        s.commit()
    with world.db() as s:
        with pytest.raises(Conflict):
            conv.delete(s, s.merge(world.alice), cid)
        assert s.scalar(select(AuditEvent.details).where(AuditEvent.action == "conversation.legal_hold_set"))["reason"]


def test_retention_sweep_skips_held_and_expires_payloads_and_personal_memory(world):
    held, free = _conversation(world, world.alice, "held"), _conversation(world, world.alice, "free")
    old_free, old_held = _turn(world, free, "old q"), _turn(world, held, "old q")
    mid = _turn(world, free, "mid q", columns=("n",), rows=((5,),))
    new = _turn(world, free, "new q")
    p = _correct(world, world.alice, free, "active", "30 days", scope="personal")["correction"]
    now = utcnow()
    with world.db() as s:
        s.get(AskThread, held).legal_hold = True
        for tid, days in ((old_free, 400), (old_held, 400), (mid, 40), (new, 1)):
            s.get(AskTurn, tid).created_at = now - timedelta(days=days)
        s.get(ConversationCorrection, p["id"]).expires_at = now - timedelta(days=1)
        s.commit()
    with world.db() as s:
        counts = retention.apply_tenant(s, None, now)
        s.commit()
    assert counts == {"messages": 1, "result_payloads": 1, "personal_expired": 1}
    with world.db() as s:
        assert s.get(AskTurn, old_free) is None and s.get(AskTurn, old_held) is not None
        expired = s.get(AskTurn, mid).result
        assert expired["expired"] and expired["rows"] == [] and expired["query_id"] and expired["result_hash"]
        assert s.get(AskTurn, new).result["rows"] == [[42]]
        assert s.get(ConversationCorrection, p["id"]).status == "expired"


def test_retention_policy_is_bounded_and_defaults():
    assert retention.policy_of(None)["messages_days"] == 365
    tenant = SimpleNamespace(settings={"conversations": {"messages_days": 30, "results_days": 0}})
    pol = retention.policy_of(tenant)
    assert pol["messages_days"] == 30 and pol["results_days"] == 30  # an out-of-range value keeps the default


def test_retention_policy_is_set_by_a_tenant_admin_only(world):
    with pytest.raises(Forbidden):
        retention.set_policy(world.alice, "t1", {"messages_days": 90})
    with pytest.raises(InvalidInput):
        retention.set_policy(world.alice, "t1", {"keep_forever": True})


# ------------------------------------------------------------------------------------ T6: MCP
def test_mcp_conversation_tools_are_granted_by_role():
    from analystos.mcp import grants as G
    from analystos.mcp.server import TOOL_DEFS, TOOL_IMPLS

    for name in ("conversation.start", "conversation.say", "conversation.summary"):
        assert name in G.TOOLS and name in TOOL_DEFS and name in TOOL_IMPLS
    assert G.min_role("conversation.start") == G.min_role("conversation.say") == "analyst"
    assert G.min_role("conversation.summary") == "viewer"
    assert TOOL_DEFS["conversation.summary"]["read_only"] and not TOOL_DEFS["conversation.say"]["read_only"]


# ------------------------------------------------------------------------------------ S11-T2: answered by + cost
def test_each_message_carries_what_answering_it_cost(world):
    from analystos.db.models import ModelCall

    cid = _conversation(world, world.alice)
    m1, m2 = _turn(world, cid, "How many?"), _turn(world, cid, "And by group?", answered_by="registry")
    with world.db() as s:
        base = {"workspace_id": world.ws, "purpose": "sql_generation", "profile": "chat", "provider": "openrouter",
                "attempt": 1, "latency_ms": 5, "input_tokens": 100, "output_tokens": 20}
        s.add_all([ModelCall(task_id=m1, model="small-1", status="ok", cost_usd=0.0012, answered_by="llm_small", **base),
                   ModelCall(task_id=m1, model="large-1", status="ok", cost_usd=0.0040, answered_by="llm_large", **base),
                   ModelCall(task_id=m1, model="-", status="skipped", cost_usd=0.0, tokens_saved=600, answered_by="rules",
                             **base),
                   ModelCall(task_id=m2, model="-", status="cache_hit", cost_usd=0.0, answered_by="cache", **base)])
        s.commit()
    with world.db() as s:
        msgs = {m["id"]: m["answer"]["cost"] for m in conv.detail(s, s.merge(world.alice), cid)["messages"]}
    assert msgs[m1] == {"usd": 0.0052, "model_calls": 2, "cache_hits": 0, "avoided": 1, "tokens_saved": 600,
                        "rungs": ["llm_small", "llm_large"], "models": ["small-1", "large-1"]}
    assert msgs[m2]["usd"] == 0.0 and msgs[m2]["cache_hits"] == 1 and msgs[m2]["model_calls"] == 0
