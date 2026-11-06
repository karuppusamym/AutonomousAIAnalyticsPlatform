"""P2-8: the generated context index (deterministic, curated documents kept, per-caller) and the MCP grant slice
(domains/assets withheld before ranking, never counted)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.context import benchmark, index, retrieval
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.mcp import grants as G

WS = "ws_idx"
ITSM = {"Backlog", "Change window", "Open incidents", "main.incident", "main.change"}  # everything in the itsm domain
LONG = "Open incidents are tickets in a non-terminal state; the backlog is counted at the end of each day. " * 5


@pytest.fixture
def db(sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.db.base import session_scope
    from analystos.db.models import Source, SourceAsset, User, Workspace, WorkspaceMember

    engine = sqlite_db.kw["bind"]
    # the search index needs Postgres (tsvector): the pack store runs here without it
    monkeypatch.setattr("analystos.knowledge.index.index_pack", lambda *a, **k: None)
    names = ["knowledge_pack", "knowledge_object", "knowledge_revision", "knowledge_document", "knowledge_suggestion",
             "mcp_client", "mcp_grant", "mcp_usage"]
    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in (
        "context_entry", "verified_query", "request", "semantic_relationship_candidate", "ask_thread",
        "conversation_summary", "conversation_correction", *names)])
    with session_scope() as s:
        s.add(User(id="u_a", email="a@x", name="a", password_hash="x", is_admin=True, active=True, attributes={}))
        s.add(User(id="u_svc", email="svc@x", name="svc", password_hash="!", is_admin=False, active=True, attributes={}))
        s.flush()
        s.add(Workspace(id=WS, name=WS, description="", objective="", autonomy_level=3, created_by="u_a"))
        s.flush()
        s.add_all([WorkspaceMember(workspace_id=WS, user_id="u_a", role="owner"),
                   WorkspaceMember(workspace_id=WS, user_id="u_svc", role="analyst")])
        s.add(Source(id="src1", workspace_id=WS, kind="postgres", name="main", config={}, status="ready", execution_mode="pushdown"))
        s.flush()
        for name, dom, desc in (("incident", "itsm", "Support tickets"), ("change", "itsm", "Change requests"),
                                ("orders", "retail", "Customer orders")):
            s.add(SourceAsset(id=f"a_{name}", source_id="src1", workspace_id=WS, schema_name="main", name=name, source_name=name,
                              kind="table", selected=True, description=desc, semantics={"domain": dom}))
        s.flush()
    return sqlite_db


def _seed(s, n=1):
    from analystos.context.service import add_entry

    add_entry(s, workspace_id=WS, kind="term", name="Backlog", body=LONG, mapped_columns=["main.incident.state"])
    add_entry(s, workspace_id=WS, kind="term", name="Change window", body=LONG, mapped_columns=["main.change.window"])
    add_entry(s, workspace_id=WS, kind="term", name="Basket size", body=LONG, mapped_columns=["main.orders.qty"])
    add_entry(s, workspace_id=WS, kind="metric", name="Open incidents", body=LONG, mapped_columns=["main.incident.state"])
    for i in range(n):
        add_entry(s, workspace_id=WS, kind="term", name=f"General rule {i}", body=LONG)  # no table reference


def _user(s, uid="u_svc"):
    from analystos.db.models import User

    return s.get(User, uid)


def _principal(**over):
    grant = {"role": "viewer", "tools": ["context.read"], "quotas": {}, "tool_patterns": [], "rate_per_min": None,
             "expires_at": None, "domains": [], "assets": [], **over}
    return G.ClientPrincipal(client_id="c", user_id="u_svc", grants={WS: grant})


def test_index_is_deterministic_and_lists_by_domain(db):
    from analystos.db.base import session_scope

    with session_scope() as s:
        _seed(s)
    with session_scope() as s:
        a, b = index.render(index.build(s, WS)), index.render(index.build(s, WS))
        assert a == b and index.document(index.build(s, WS)) == index.document(index.build(s, WS))
        assert "main.incident" in a and "Backlog" in a and "Open incidents" in a and "main.orders" in a
        assert index.domains(s, WS) == ["itsm", "retail"]
        itsm = index.build(s, WS, domain="itsm")
        titles = {r["title"] for sec in itsm["sections"].values() for r in sec}
        assert {"Backlog", "Change window", "Open incidents", "main.incident", "main.change"} <= titles
        assert "Basket size" not in titles and "main.orders" not in titles and "General rule 0" not in titles


def test_refresh_writes_drafts_keeps_curated_and_is_idempotent(db):
    from analystos.db.base import session_scope
    from analystos.knowledge import store
    from analystos.knowledge.drafts import write_drafts

    curated = "---\ntype: Index\ntitle: Our own map\ngenerated:\n  by: human:alice\n---\n\nHand written map.\n"
    with session_scope() as s:
        _seed(s)
        write_drafts(s, WS, {index.domain_path("retail"): curated}, author="user:alice", reason="curate", origin="user")
    with session_scope() as s:
        first = index.refresh(s, WS)
    assert index.WORKSPACE_PATH in first["written"] and index.domain_path("itsm") in first["written"]
    assert first["kept_curated"] == [index.domain_path("retail")]
    with session_scope() as s:
        second = index.refresh(s, WS)
        assert second["written"] == [] and second["revision"] is None
        files = store.revision_files(s, store.workspace_pack(s, WS))
        assert files[index.domain_path("retail")] == curated.encode()
        assert b"Backlog" in files[index.WORKSPACE_PATH] and b"kind: index" in files[index.WORKSPACE_PATH]


def test_slice_hides_out_of_domain_entries_before_ranking_and_counts(db):
    from analystos.db.base import session_scope

    with session_scope() as s:
        _seed(s)
    with session_scope() as s:
        u = _user(s)
        everything = {h.entry.title for h in retrieval.search(s, u, WS, "backlog basket change window rule")}
        assert {"Basket size", "General rule 0"} <= everything
        sliced = retrieval.search(s, u, WS, "backlog basket change window rule", domains=["itsm"])
        assert {"Backlog", "Change window"} <= {h.entry.title for h in sliced} <= ITSM
        only_asset = retrieval.search(s, u, WS, "basket backlog", assets=["main.orders"])
        assert {h.entry.title for h in only_asset} == {"Basket size"}
        out = retrieval.compile_bundle(s, u, WS, purpose="t", objective="basket size", domains=["itsm"], budget_tokens=2000)
        assert "Basket size" not in str(out["bundle"]) and out["bundle"]["omissions"] == []  # not named, not counted
        entry = next(e for e in retrieval.candidates(s, retrieval.access_for(s, u, WS), kinds=None, trusts=retrieval.MODEL_TRUSTS)
                     if e.title == "Basket size")
        with pytest.raises(NotFound):
            retrieval.get(s, u, entry.id, workspace_id=WS, domains=["itsm"])
        feed = retrieval.changes(s, u, WS, domains=["itsm"])
        feed_titles = {c["title"] for c in feed["changes"]}
        assert {"Backlog", "Change window", "Open incidents"} <= feed_titles <= ITSM
        view = index.view(s, u, WS, domains_=["itsm"])
        assert view["counts"]["tables"] == 2 and "Basket size" not in view["markdown"] and "orders" not in view["markdown"]


def test_index_first_compile_reads_the_index_and_spends_fewer_tokens(db):
    from analystos.db.base import session_scope

    with session_scope() as s:
        _seed(s, n=12)
    with session_scope() as s:
        u = _user(s)
        first = retrieval.compile_bundle(s, u, WS, purpose="t", objective="backlog incidents", index_first=True)
        assert next(iter(first["bundle"]["sections"])) == "index"
        assert first["bundle"]["sections"]["index"]["glossary"]["count"] >= 4
        saved = benchmark.index_first_savings(s, u, WS, "backlog incidents")
        assert saved["index_first"]["items"] == saved["flat"]["items"] > 0
        assert saved["tokens_saved"] > 0 and saved["saved_ratio"] > 0.2


def test_grant_slice_is_context_only_and_enforced_at_the_gate(db):
    from analystos.db.base import session_scope

    with session_scope() as s:
        admin = _user(s, "u_a")
        client, _ = G.create_client(s, admin, "agent")
        with pytest.raises(InvalidInput):
            G.set_grant(s, admin, client.id, WS, role="analyst", tools=["ask"], domains=["itsm"])
        with pytest.raises(InvalidInput):
            G.set_grant(s, admin, client.id, WS, role="viewer", tools=["context.read"], assets=["orders"])
        g = G.set_grant(s, admin, client.id, WS, role="viewer", tools=["context.read"], domains=["itsm", "itsm"], assets=["main.change"])
        assert g.domains == ["itsm"] and g.assets == ["main.change"]
        assert G.list_clients(s)[0]["grants"][0]["domains"] == ["itsm"]
        principal = _principal(domains=["itsm"])
        assert G.check_grant(principal, WS, "context.search")
        principal.grants[WS]["tools"] = ["context.read", "ask"]  # a hand-edited grant still cannot reach the data plane
        with pytest.raises(Forbidden):
            G.check_grant(principal, WS, "ask")


def test_mcp_tools_and_resources_follow_the_slice(db):
    from analystos.db.base import session_scope
    from analystos.mcp import server

    with session_scope() as s:
        _seed(s)
        index.refresh(s, WS)
    sliced, whole = _principal(domains=["itsm"]), _principal()

    def titles(p):
        return {r["title"] for r in server._tool_context_search(p, WS, {"q": "basket backlog change"})["results"]}

    assert titles(whole) >= {"Basket size", "Backlog"} and {"Backlog", "Change window"} <= titles(sliced) <= ITSM
    uris = [r["uri"] for r in server.list_resource_rows(sliced)]
    assert f"analystos://{WS}/index/itsm" in uris and f"analystos://{WS}/index/retail" not in uris
    assert not any("/knowledge/" in u or "/metrics/" in u for u in uris)
    doc = server.read_resource_doc(sliced, f"analystos://{WS}/index/workspace")
    assert "Basket" not in doc["markdown"] and "Backlog" in doc["markdown"]
    with pytest.raises(NotFound):
        server.read_resource_doc(sliced, f"analystos://{WS}/index/retail")
    with pytest.raises(NotFound):
        server.read_resource_doc(sliced, f"analystos://{WS}/knowledge/anything")
    assert "Basket" in server.read_resource_doc(whole, f"analystos://{WS}/index/workspace")["markdown"]


def test_index_documents_are_not_knowledge(db):
    from analystos.context.models import ContextLedgerEntry
    from analystos.db.base import session_scope

    with session_scope() as s:
        _seed(s)
        index.refresh(s, WS)
    with session_scope() as s:
        assert not [e for e in s.scalars(select(ContextLedgerEntry)) if "Context index" in e.title]


def test_pack_copy_omits_tagged_tables_and_unscoped_callers_see_only_their_domains(db):
    from analystos.db.base import session_scope
    from analystos.db.models import SourceColumn

    with session_scope() as s:
        _seed(s)
        s.add(SourceColumn(asset_id="a_orders", name="qty", data_type="int", ordinal=1,
                           tags=["pii"]))
    with session_scope() as s:
        model = index.build(s, WS)
        text = index.render(model)
        assert "main.orders" not in text and "Customer orders" not in text and "Basket size" not in text
        assert "main.incident" in text and "Backlog" in text
        report = index.refresh(s, WS)
        assert index.domain_path("retail") not in report["written"] and index.domain_path("itsm") in report["written"]
        # a caller whose data scope lacks the retail tables is not told the domain exists
        user = _user(s)
        access = retrieval.access_for(s, user, WS)
        access.assets.discard("main.orders")
        assert index.domains(s, WS, access) == ["itsm"]
        assert index.build(s, WS, access=access)["counts"]["tables"] == 2
