"""A workspace's glossary vocabulary comes from the domain packs that fit its catalog. Found reviewing a downloaded
ticketing workspace: the sales pack's "Sales channel" and "Customer segment" were linked to a ticket's contact
channel and a process case's segment because every pack's terms were visible to every workspace.

* a workspace whose selected tables match one pack sees that pack's terms and none of another pack's;
* one whose tables match no pack keeps seeing every pack's vocabulary (its domain is unknown);
* the policy's `domain_packs`, when set, decides exactly (even empty);
* the crawler's glossary linking and the compiler's knowledge hits use the same filter;
* non-pack entries (a workspace's own terms) are never filtered."""
from __future__ import annotations

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import register_source
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        out = {}
        for key, table in (("itsm", "incident"), ("sales", "orders"), ("none", "widgets"), ("policy", "incident")):
            ws = create_workspace(s, owner, name=f"packs {key} {new_id('t')}", objective="", autonomy_level=3)
            src = register_source(s, owner, ws.id, kind="postgres", name="app",
                                  config={"host": "db.example", "database": "app", "username": "reader"}, secret_ref="env:ANALYSTOS_SOURCE_SECRET_PGPASS")
            s.flush()
            _asset(s, ws.id, src.id, table)
            out[key] = ws.id
        s.expunge(owner)
    out["owner"] = owner
    return out


def _asset(s, workspace_id: str, source_id: str, name: str) -> None:
    from analystos.core.ids import new_id
    from analystos.db.models import SourceAsset, SourceColumn

    a = SourceAsset(id=new_id("ast"), source_id=source_id, workspace_id=workspace_id, schema_name="src", name=name, source_name=name,
                    selected=True, semantics={}, stats={}, snapshot={})
    s.add(a)
    s.flush()
    s.add(SourceColumn(asset_id=a.id, name="id", ordinal=0, data_type="text", tags=[], profile={}, semantics={}))


def _term_origins(workspace_id: str) -> set[str]:
    from analystos.db.base import session_scope
    from analystos.knowledge.entries import visible_entries

    with session_scope() as s:
        return {e.origin for e in visible_entries(s, workspace_id, kinds=["term"])}


def test_a_ticketing_catalog_sees_ticketing_terms_and_no_sales_terms(world):
    origins = _term_origins(world["itsm"])
    assert "pack:itsm" in origins and "pack:sales" not in origins


def test_a_sales_catalog_sees_sales_terms_and_no_ticketing_terms(world):
    origins = _term_origins(world["sales"])
    assert "pack:sales" in origins and "pack:itsm" not in origins


def test_a_catalog_no_pack_matches_keeps_every_packs_vocabulary(world):
    from analystos.db.base import session_scope
    from analystos.knowledge.entries import applicable_domain_packs

    with session_scope() as s:
        assert applicable_domain_packs(s, world["none"]) is None
    assert {"pack:itsm", "pack:sales"} <= _term_origins(world["none"])


def test_the_policy_domain_packs_decide_exactly(world):
    from analystos.db.base import session_scope
    from analystos.governance.policy import get_workspace, load_policy, save_policy
    from analystos.knowledge.entries import applicable_domain_packs

    with session_scope() as s:
        ws = get_workspace(s, world["policy"])
        doc = load_policy(s, ws)
        doc.domain_packs = ["sales"]
        save_policy(s, ws, doc, world["owner"].id)
    with session_scope() as s:
        assert applicable_domain_packs(s, world["policy"]) == {"sales"}
    origins = _term_origins(world["policy"])
    assert "pack:sales" in origins and "pack:itsm" not in origins
    with session_scope() as s:
        ws = get_workspace(s, world["policy"])
        doc = load_policy(s, ws)
        doc.domain_packs = []
        save_policy(s, ws, doc, world["owner"].id)
    assert not {o for o in _term_origins(world["policy"]) if o.startswith("pack:")}


def test_glossary_linking_only_offers_the_workspaces_own_packs_terms(world):
    from analystos.db.base import session_scope
    from analystos.knowledge.entries import visible_entries

    with session_scope() as s:
        names = {k: {t.name for t in visible_entries(s, world[k], kinds=["term"])} for k in ("itsm", "sales")}
    assert "Sales channel" in names["sales"] and "Sales channel" not in names["itsm"]
    assert "Assignment group" in names["itsm"] and "Assignment group" not in names["sales"]


def test_pure_filter_keeps_non_pack_entries_and_everything_when_unknown():
    from types import SimpleNamespace as NS

    from analystos.knowledge.entries import domain_pack_of, in_applicable_packs

    items = [NS(origin="user"), NS(origin="pack:itsm"), NS(origin="pack:sales"), NS(origin="okf:workspace")]
    assert domain_pack_of("pack:itsm") == "itsm" and domain_pack_of("user") is None and domain_pack_of(None) is None
    assert [i.origin for i in in_applicable_packs(items, {"itsm"})] == ["user", "pack:itsm", "okf:workspace"]
    assert [i.origin for i in in_applicable_packs(items, set())] == ["user", "okf:workspace"]
    assert in_applicable_packs(items, None) == items
