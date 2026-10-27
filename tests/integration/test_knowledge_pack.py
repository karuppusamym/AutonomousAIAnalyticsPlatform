"""P4-K01/K02/K09/K10 against Postgres: packs and revisions, the platform pack, workspace
isolation, reindex identity with an HNSW index, Atlas import/export round trip, publish policy,
the optional remote (push under an approval), re-embedding, context providers (Atlas over MCP
against a MOCK double serving a recorded result) and migration 0018."""
from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select, text

from analystos.core.errors import ApprovalRequired, Conflict, Forbidden
from analystos.core.ids import new_id
from analystos.db import vectors
from analystos.db.base import session_scope
from analystos.db.models import KnowledgePack, User
from analystos.knowledge import bundle, index, store
from analystos.knowledge.embeddings import HashingProvider
from analystos.knowledge.entries import render_entry

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "okf"
ATLAS_ZIP = FIXTURES / "atlas-sample.zip"


def _vector_index_ok(stats: dict) -> bool:
    """pgvector: the HNSW cosine index exists; the array backend has none and ranks in-process."""
    if vectors.uses_pgvector():
        return "hnsw" in (stats["hnsw_index"] or "") and "vector_cosine_ops" in stats["hnsw_index"]
    return stats["hnsw_index"] is None and stats["vector_backend"] == "array"


def _dims_sql() -> str:
    return "vector_dims(embedding)" if vectors.uses_pgvector() else "array_length(embedding, 1)"


QUERIES = ["SLA breach", "time to restore service", "completed orders", "net revenue after returns",
           "which team resolves incidents", "purchase agreement with a customer", "reassignment"]


def _admin(s):
    from analystos.core.config import get_settings

    return s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))


@pytest.fixture()
def two_workspaces(control_db):
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = _admin(s)
        a = create_workspace(s, admin, name=f"k01 a {new_id('t')}", objective="SLA")
        b = create_workspace(s, admin, name=f"k01 b {new_id('t')}", objective="SLA")
        s.flush()
        return a.id, b.id


def _write_ws_doc(ws: str, name: str, body: str, *, path: str | None = None) -> None:
    with session_scope() as s:
        pack = store.workspace_pack(s, ws)
        store.commit(s, pack, {path or f"glossary/{name.lower().replace(' ', '-')}.md":
                               render_entry(kind="term", name=name, body=body).encode()},
                     author="human:test", reason="test", origin="user", merge=True)


def test_seed_builds_a_read_only_platform_pack_indexed_with_hnsw(control_db):
    with session_scope() as s:
        pack = store.platform_pack(s, create=False)
        assert pack is not None and pack.read_only and pack.workspace_id is None
        assert (pack.okf_spec_revision, pack.okf_version) == ("0b87c52c6ef999286c745e19998fdfcd03d5dbee", "0.2")
        files = store.revision_files(s, pack)
        assert "index.md" in files and any(p.startswith("domain/itsm/") for p in files)
        stats = index.stats(s)
        assert stats["knowledge_document"] > 0 and _vector_index_ok(stats)
        assert s.scalar(text("SELECT count(*) FROM context_entry WHERE workspace_id IS NULL")) == 0
        with pytest.raises(Forbidden):
            store.commit(s, pack, {"x.md": b"---\ntype: T\n---\n"}, author="human:x", reason="x", origin="user")
    from analystos.cli import seed

    before = pack.head_revision
    seed()  # an unchanged sync writes no new revision
    with session_scope() as s:
        assert store.platform_pack(s, create=False).head_revision == before


def test_one_workspace_never_sees_another_workspaces_knowledge(two_workspaces):
    from analystos.context.compiler import load_knowledge
    from analystos.knowledge.entries import visible_entries

    a, b = two_workspaces
    _write_ws_doc(a, "Zebra backlog", "Tickets waiting for the zebra team, a secret of workspace A.")
    with session_scope() as s:
        hits_a = index.retrieve(s, a, "zebra backlog")
        hits_b = index.retrieve(s, b, "zebra backlog")
        assert any(h.title == "Zebra backlog" for h in hits_a)
        assert not any(h.title == "Zebra backlog" for h in hits_b)
        assert all(h.pack_kind == "platform" or h.pack_id in {p.id for p in store.visible_packs(s, b)} for h in hits_b)
        pack_a = store.workspace_pack(s, a, create=False)
        assert index.retrieve(s, b, "zebra backlog", pack_ids=[pack_a.id]) == []  # pack_ids only narrows
        assert "Zebra backlog" not in {e.name for e in visible_entries(s, b)}
        assert "Zebra backlog" not in {i.name for i in load_knowledge(s, b, ["glossary"])}
        assert "Zebra backlog" in {i.name for i in load_knowledge(s, a, ["glossary"])}
        from analystos.core.errors import NotFound

        with pytest.raises(NotFound):
            store.get_pack(s, pack_a.id, workspace_id=b)


def test_platform_knowledge_reaches_the_compiler_and_the_knowledge_version(two_workspaces):
    from analystos.context.compiler import load_knowledge
    from analystos.context.version import knowledge_version

    a, _ = two_workspaces
    with session_scope() as s:
        items = load_knowledge(s, a, ["glossary", "metrics", "business_rules"])
        assert any(i.source == "pack:itsm" for i in items)
        v1 = knowledge_version(s, a)
    _write_ws_doc(a, "Quiet hours", "Hours with no on-call cover.")
    with session_scope() as s:
        assert knowledge_version(s, a) != v1


def test_deleting_the_index_and_reindexing_gives_identical_results(two_workspaces):
    a, _ = two_workspaces
    _write_ws_doc(a, "MTTR", "Mean time to restore service after an incident.")
    with session_scope() as s:
        before = {q: index.signature(index.retrieve(s, a, q, limit=10)) for q in QUERIES}
        assert any(before.values())
        index.drop_index(s)
    with session_scope() as s:
        assert index.retrieve(s, a, "SLA breach") == []
        report = index.reindex(s)
        assert report["documents"] > 0
    with session_scope() as s:
        after = {q: index.signature(index.retrieve(s, a, q, limit=10)) for q in QUERIES}
        assert after == before
        assert _vector_index_ok(index.stats(s))


def test_atlas_bundle_import_export_round_trip_is_lossless(two_workspaces, tmp_path):
    a, b = two_workspaces
    original = zipfile.ZipFile(ATLAS_ZIP)
    members = {i.filename: original.read(i) for i in original.infolist()}
    with session_scope() as s:
        rep = bundle.import_bundle(s, a, ATLAS_ZIP, slug="atlas-revenue", author="human:test")
        assert (rep.format, rep.okf_root, rep.documents, rep.dangling_links) == ("atlas", "bundle", 7, 0)
        assert rep.conformance == [] and rep.warnings == [] and rep.verified_claims >= 1
        pack = s.get(KnowledgePack, rep.pack_id)
        assert pack.kind == "imported" and pack.read_only
        exported = bundle.export_files(s, pack)
        assert exported == members  # every member, byte for byte, including atlas-manifest.json
        again = bundle.import_bundle(s, a, bundle.zip_bytes(exported), slug="atlas-revenue", author="human:test")
        assert not again.changed and again.revision == rep.revision  # re-import of the same bytes: no new revision
        hits = index.retrieve(s, a, "purchase agreement with a customer")
        assert hits and hits[0].type == "Atlas Business Concept" and hits[0].path.startswith("bundle/concepts/")
        assert not index.retrieve(s, b, "purchase agreement with a customer", types=["Atlas Business Concept"])
        with pytest.raises(Forbidden):
            store.commit(s, pack, {"bundle/x.md": b"---\ntype: T\n---\n"}, author="human:x", reason="edit",
                         origin="user")


def test_export_enforces_the_publish_policy(two_workspaces):
    a, _ = two_workspaces
    _write_ws_doc(a, "Kept", "A document that stays.")
    _write_ws_doc(a, "Broken", "See [the missing doc](/glossary/nope.md).", path="glossary/broken.md")
    with session_scope() as s:
        pack = store.workspace_pack(s, a, create=False)
        with pytest.raises(Conflict, match="LINK_DANGLING"):
            bundle.export_zip(s, pack)
        store.commit(s, pack, {}, deletes=("glossary/broken.md",), merge=True, author="human:test", reason="fix",
                     origin="user")
        data = bundle.export_zip(s, pack)
        assert zipfile.ZipFile(__import__("io").BytesIO(data)).namelist()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_push_to_a_remote_needs_a_verified_single_use_approval(two_workspaces, tmp_path):
    from analystos.governance import approvals
    from analystos.knowledge import remote

    a, _ = two_workspaces
    _write_ws_doc(a, "Backlog", "Open work not yet started.")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    with session_scope() as s:
        admin = _admin(s)
        pack = store.workspace_pack(s, a, create=False)
        remote.set_remote(s, pack, str(bare), "knowledge")
        apr = remote.request_push(s, pack, admin)
        with pytest.raises(ApprovalRequired):
            remote.push(s, pack, admin, apr.id)  # still pending
    with session_scope() as s:
        admin = _admin(s)
        approvals.decide(s, apr.id, admin, approve=True)
    _write_ws_doc(a, "Backlog", "Changed after approval.")
    with session_scope() as s:
        admin = _admin(s)
        pack = store.workspace_pack(s, a, create=False)
        with pytest.raises(ApprovalRequired):
            remote.push(s, pack, admin, apr.id)  # the payload (revision, digest) moved: invalidated
    with session_scope() as s:
        admin = _admin(s)
        pack = store.workspace_pack(s, a, create=False)
        apr2 = remote.request_push(s, pack, admin)
    with session_scope() as s:
        approvals.decide(s, apr2.id, _admin(s), approve=True)
    with session_scope() as s:
        admin = _admin(s)
        pack = store.workspace_pack(s, a, create=False)
        out = remote.push(s, pack, admin, apr2.id)
        files = store.revision_files(s, pack)
    with session_scope() as s, pytest.raises(ApprovalRequired):
        remote.push(s, store.workspace_pack(s, a, create=False), _admin(s), apr2.id)  # consumed
    listed = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", "knowledge"],
                            capture_output=True, text=True, check=True).stdout.split()
    assert sorted(listed) == sorted(files)
    head = subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "knowledge"], capture_output=True, text=True,
                          check=True).stdout.strip()
    assert head == out["commit"]


def test_reembed_changes_dimension_and_rebuilds_the_hnsw_index(two_workspaces):
    a, _ = two_workspaces
    from analystos.tenancy import system_scope

    with session_scope() as s:
        before = index.signature(index.retrieve(s, a, "SLA breach"))
    with system_scope(), session_scope() as s:  # a platform operation (`analystos knowledge reembed`)
        rep = index.reembed(s, HashingProvider(dim=128))
        assert rep["to"]["dim"] == 128 and rep["sections"] > 0
    with session_scope() as s:
        assert s.scalar(text(f"SELECT {_dims_sql()} FROM knowledge_section LIMIT 1")) == 128
        assert _vector_index_ok(index.stats(s))
        assert index.retrieve(s, a, "SLA breach")
    _write_ws_doc(a, "Late closure", "Closed after the SLA window.")  # a new revision joins the index's space
    with session_scope() as s:
        assert s.scalar(text(f"SELECT count(DISTINCT {_dims_sql()}) FROM knowledge_section")) == 1
        store.commit(s, store.workspace_pack(s, a, create=False), {}, deletes=("glossary/late-closure.md",), merge=True,
                     author="human:test", reason="undo", origin="user")
    with system_scope(), session_scope() as s:
        index.reembed(s, HashingProvider(dim=256))
    with session_scope() as s:
        assert index.signature(index.retrieve(s, a, "SLA breach")) == before  # same provider, same results


def test_okf_import_provider_and_local_provider(two_workspaces):
    from analystos.knowledge.providers import LocalProvider, OkfImportProvider

    a, _ = two_workspaces
    prov = OkfImportProvider(slug="atlas-p")
    with session_scope() as s:
        assert prov.retrieve(s, a, "orders").status == "UNAVAILABLE"
        prov.sync(s, a, author="human:test", source=ATLAS_ZIP)
        res = prov.retrieve(s, a, "completed orders")
        assert res.status == "MATCHED" and all(i.path.startswith("bundle/") for i in res.items)
        assert res.receipts[0]["sha256"]
        local = LocalProvider().retrieve(s, a, "completed orders")
        assert all(not i.path.startswith("bundle/") for i in local.items)  # local excludes imported packs


def _atlas_double(token: str):
    """MOCK Atlas MCP server: serves the recorded `atlas__get_knowledge_context` result."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    recorded = json.loads((FIXTURES / "atlas-mcp-get-knowledge-context.mock.json").read_text(encoding="utf-8"))
    srv = MCPServer("atlas_mock")
    calls: list[dict] = []

    @srv.tool(name="atlas__get_knowledge_context", structured_output=False)  # Atlas returns text content only
    def get_knowledge_context(product_key: str, version: int, question: str, max_chars: int | None = None) -> str:
        """Select the sections of a context product's approved OKF knowledge bundle that a question needs."""
        calls.append({"product_key": product_key, "version": version, "question": question})
        return "\n".join(c["text"] for c in recorded["content"])

    inner = srv.streamable_http_app(stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))

    async def app(scope, receive, send):
        if scope["type"] == "http" and dict(scope.get("headers") or []).get(b"authorization", b"").decode() != f"Bearer {token}":
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await inner(scope, receive, send)

    return app, calls


def test_mcp_provider_calls_atlas_through_the_governed_mcp_client(two_workspaces, monkeypatch):
    pytest.importorskip("mcp", reason="needs the optional `mcp` extra")
    from tests.mcp_double import serve

    from analystos.context.service import build_context_package
    from analystos.mcp import client as mc
    from analystos.services.workspaces import set_policy

    a, _ = two_workspaces
    token = "atlas-mock-" + "y" * 16
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_ATLAS_MOCK_TOKEN", token)
    app, calls = _atlas_double(token)
    with serve(app) as base:
        with session_scope() as s:
            admin = _admin(s)
            srv = mc.register_server(s, admin, a, name="atlas", url=f"{base}/mcp", secret_ref="env:ANALYSTOS_SOURCE_SECRET_ATLAS_MOCK_TOKEN",
                                     config={"result_max_chars": 48_000, "lift_json_blocks": True})
            sid = srv.id
        with session_scope() as s:
            mc.set_allowed(s, _admin(s), a, sid, True)
        with session_scope() as s:
            mc.refresh_tools(s, _admin(s), a, sid)
        with session_scope() as s:
            mc.classify_tool(s, _admin(s), a, sid, "atlas__get_knowledge_context", "read_source")
            set_policy(s, _admin(s), a, {"context_providers": [
                {"kind": "local"}, {"kind": "mcp", "server": "atlas", "product_key": "revenue_context", "version": 2}]})
        with session_scope() as s:
            admin = _admin(s)
            s.expunge(admin)
        with session_scope() as s:
            package = build_context_package(s, a, "What does an order mean?", [], user=admin)
    assert calls == [{"product_key": "revenue_context", "version": 2, "question": "What does an order mean?"}]
    ext = package["external"]
    assert [e["provider"] for e in ext] == ["mcp"] and ext[0]["status"] == "MATCHED", ext
    assert any("purchase agreement" in i["text"] for i in ext[0]["items"])
    assert all(r["sha256"] and r["path"] for r in ext[0]["receipts"])
    with session_scope() as s:
        package = build_context_package(s, a, "What does an order mean?", [])  # no identity: skipped, not failed
    assert package["external"][0]["status"] == "UNAVAILABLE"


def test_migration_0018_moves_global_entries_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig18"
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        c.execute(text(f"CREATE DATABASE {mig_db}"))
    url = f"{base}/{mig_db}"
    engine = create_engine(url)
    try:
        with engine.begin() as c:
            vectors.ensure_extension(c)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "0023")  # the revision before 0018 in the chain
        with engine.begin() as c:
            c.execute(text("INSERT INTO context_entry (id, workspace_id, kind, name, body, synonyms, mapped_columns, origin, "
                           "trusted) VALUES ('ctx_g1', NULL, 'term', 'SLA breach', 'Missed SLA. Longer.', '[\"breach\"]', "
                           "'[\"incident.made_sla\"]', 'pack:itsm', true)"))
        command.upgrade(cfg, "0018")
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM context_entry")).scalar() == 0
            pack = c.execute(text("SELECT id, kind, read_only, head_revision FROM knowledge_pack")).one()
            assert (pack.kind, pack.read_only, pack.head_revision) == ("platform", True, 1)
            files = c.execute(text("SELECT files FROM knowledge_revision WHERE pack_id = :p"), {"p": pack.id}).scalar()
            assert "domain/itsm/sla-breach.md" in files
            hnsw = c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_knowledge_section_embedding_hnsw'")).scalar()
            assert bool(hnsw) == vectors.uses_pgvector()
        command.downgrade(cfg, "0023")
        with engine.begin() as c:
            row = c.execute(text("SELECT workspace_id, kind, name, synonyms, origin FROM context_entry")).one()
            assert (row.workspace_id, row.kind, row.name, row.synonyms, row.origin) == (None, "term", "SLA breach", ["breach"], "pack:itsm")
        command.upgrade(cfg, "0018")
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM knowledge_pack")).scalar() == 1
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
