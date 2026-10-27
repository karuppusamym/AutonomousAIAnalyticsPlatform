"""Stream D on the real stack: the workspace context downloads as OKF, JSON and Markdown (whole workspace or one
source), without secrets or value lists the policy keeps inside; the OKF zip re-imports as a knowledge pack; the
"what the agents see" preview is exactly the prompt Ask's SQL generation sends; the context cache is visible to
editors and cleared by owners only."""
from __future__ import annotations

import io
import json
import zipfile
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"
SECRET = "env:CTX_EXPORT_SECRET_SHOULD_NOT_LEAK"


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _login(api, email: str) -> dict:
    r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def env(api):
    """Two staged CSV sources (shop: customers + orders; hr: people), selected and profiled, a PII column, a glossary
    term and rule per source, an episode (never exported) and a secret reference on the shop source."""
    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import ContextEntry, Source, SourceAsset, SourceColumn, User
    from analystos.services.sources import discover_source, register_source

    admin = _login(api, "admin@analystos.local")
    r = api.post("/api/workspaces", headers=admin, json={"name": "Context Export", "objective": "Grow order revenue"})
    assert r.status_code == 200, r.text
    ws = r.json()["id"]
    for email, role in (("analyst@analystos.local", "viewer"), ("approver@analystos.local", "editor")):
        assert api.post(f"/api/workspaces/{ws}/members", headers=admin, json={"email": email, "role": role}).status_code == 200
    root = get_settings().upload_dir / ws
    (root / "shop").mkdir(parents=True, exist_ok=True)
    (root / "hr").mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"customer_id": list(range(1, 11)), "segment": ["Enterprise", "SMB"] * 5,
                  "customer_name": [f"Customer {i}" for i in range(1, 11)]}).to_parquet(root / "shop" / "customers.parquet", index=False)
    pd.DataFrame({"order_id": list(range(1, 41)), "customer_id": [1 + i % 10 for i in range(40)],
                  "amount": [10.0 + i for i in range(40)],
                  "ordered_at": pd.date_range("2026-01-01", periods=40, freq="D")}).to_parquet(root / "shop" / "orders.parquet", index=False)
    pd.DataFrame({"person_id": list(range(1, 6)), "department": ["Ops", "Sales", "Ops", "IT", "Sales"]}).to_parquet(
        root / "hr" / "people.parquet", index=False)
    ids = {}
    with session_scope() as s:
        user = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        for name in ("shop", "hr"):
            ids[name] = register_source(s, user, ws, kind="csv", name=name, config={"path": f"{ws}/{name}"}, secret_ref=None).id
        s.flush()
        s.expunge(user)
    for name, tables in (("shop", ["customers", "orders"]), ("hr", ["people"])):
        discover_source(user, ids[name])
        r = api.put(f"/api/workspaces/{ws}/sources/{ids[name]}/selection", headers=admin, json={"assets": tables})
        assert r.status_code == 200, r.text
    with session_scope() as s:
        s.get(Source, ids["shop"]).secret_ref = SECRET
        assets = {a.name: a for a in s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ws))}
        fq = {n: f"{a.schema_name}.{a.name}" for n, a in assets.items()}
        # tagged by a person after profiling: the stored profile still has the value list; the export must reduce it
        dept = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == assets["people"].id, SourceColumn.name == "department"))
        assert (dept.profile or {}).get("values")
        dept.tags, dept.tags_origin = sorted({*(dept.tags or []), "sensitive"}), "user"
        s.add_all([
            ContextEntry(id=new_id("ctx"), workspace_id=ws, kind="term", name="Revenue", body="The total order amount.",
                         synonyms=["sales"], mapped_columns=[f"{fq['orders']}.amount"], origin="user", trusted=True),
            ContextEntry(id=new_id("ctx"), workspace_id=ws, kind="rule", name="Headcount", body="Count people once per department.",
                         synonyms=[], mapped_columns=[f"{fq['people']}.person_id"], origin="user", trusted=True),
            ContextEntry(id=new_id("ctx"), workspace_id=ws, kind="episode", name="Run 12 recap", body="An earlier run's memory.",
                         synonyms=[], mapped_columns=[], origin="agent", trusted=True)])
    return {"ws": ws, "src": ids, "fq": fq, "admin": admin, "viewer": _login(api, "analyst@analystos.local"),
            "editor": _login(api, "approver@analystos.local")}


def _export(api, env, fmt: str, who: str = "viewer", **params):
    r = api.get(f"/api/workspaces/{env['ws']}/context/export", headers=env[who], params={"format": fmt, **params})
    assert r.status_code == 200, r.text
    return r


def test_json_export_carries_the_whole_context_and_nothing_secret(api, env):
    r = _export(api, env, "json")
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-disposition"].startswith('attachment; filename="context-export-context-') and \
        r.headers["content-disposition"].endswith('.json"')
    assert SECRET.encode() not in r.content and b"secret_ref" not in r.content and env["ws"].encode() + b"/shop" not in r.content
    doc = r.json()
    assert doc["format"] == "analystos.context/v1" and len(doc["content_digest"]) == 64
    assert doc["workspace"]["objective"] == "Grow order revenue" and doc["scope"] == {"kind": "workspace"}
    assert {s["name"] for s in doc["sources"]} == {"shop", "hr"}
    assert all(s["last_crawl"]["status"] == "succeeded" for s in doc["sources"])
    assets = {a["name"]: a for a in doc["assets"]}
    assert set(assets) >= {"customers", "orders", "people"} and assets["orders"]["selected"]
    assert assets["orders"]["time_column"] == "ordered_at" and assets["orders"]["row_count"] == 40
    cols = {c["name"]: c for c in assets["customers"]["columns"]}
    assert cols["customer_name"]["sensitive"] and cols["customer_name"]["pii"]["category"] == "person_name"  # the crawler's PII rule
    from analystos.skills.profiling import SENSITIVE_PROFILE_FIELDS

    dept = next(c for c in assets["people"]["columns"] if c["name"] == "department")
    assert dept["sensitive"] and set(dept["profile"]) <= SENSITIVE_PROFILE_FIELDS and dept["profile"]["distinct"] == 3
    assert "values" not in cols["segment"]["profile"] and "top_values" not in cols["segment"]["profile"]  # samples policy off
    assert cols["segment"]["profile"]["distinct"] == 2
    assert b"Customer 1" not in r.content  # a PII column's values appear nowhere
    assert {g["name"] for g in doc["glossary"]} == {"Revenue", "Headcount", "Customer segment"}  # never the episode; the pack term a column links to comes along
    assert doc["suggested_model"]["available"] and {t["name"] for t in doc["suggested_model"]["tables"]} >= {"orders", "people"}
    assert doc["counts"]["assets"] == len(doc["assets"]) and doc["counts"]["glossary"] == 3
    again = _export(api, env, "json").json()
    assert again["content_digest"] == doc["content_digest"]  # unchanged context, same digest

    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent

    with session_scope() as s:
        events = list(s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == env["ws"], AuditEvent.action == "context.exported")))
        assert events and events[-1].details["format"] == "json" and events[-1].details["digest"] == doc["content_digest"]


def test_markdown_export_is_one_readable_file(api, env):
    r = _export(api, env, "markdown")
    assert r.headers["content-type"].startswith("text/markdown") and r.headers["content-disposition"].endswith('.md"')
    text = r.text
    assert text.startswith("# Context Export — data context")
    assert f"### {env['fq']['orders']}" in text and "## Suggested data model" in text and "Revenue" in text
    assert SECRET not in text and "Run 12 recap" not in text


def test_okf_export_is_deterministic_and_reimports_as_a_pack(api, env):
    from analystos.knowledge import okf

    first, second = _export(api, env, "okf"), _export(api, env, "okf")
    assert first.headers["content-type"] == "application/zip" and first.headers["content-disposition"].endswith('.okf.zip"')
    assert first.content == second.content  # byte-identical while nothing changed
    zf = zipfile.ZipFile(io.BytesIO(first.content))
    files = {n: zf.read(n) for n in zf.namelist()}
    assert okf.check_publish_policy(files) == []
    assert {"index.md", "brief.md", f"sources/{env['src']['shop']}.md", "model/suggested.md",
            "glossary/term-revenue.md", "glossary/rule-headcount.md"} <= set(files)
    assert any(p.startswith("tables/") and "orders" in p for p in files)
    assert all(SECRET.encode() not in b for b in files.values())

    r = api.post(f"/api/workspaces/{env['ws']}/knowledge/import", headers=env["editor"], data={"slug": "context-export"},
                 files={"file": ("context.okf.zip", first.content, "application/zip")})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["format"] == "okf" and report["okf_root"] == "" and report["changed"]
    assert report["documents"] == len(files) - 1 and report["dangling_links"] == 0 and report["conformance"] == []
    again = api.post(f"/api/workspaces/{env['ws']}/knowledge/import", headers=env["editor"], data={"slug": "context-export"},
                     files={"file": ("context.okf.zip", first.content, "application/zip")}).json()
    assert not again["changed"]  # the same bytes: no new revision


def test_source_scoped_export_keeps_only_that_integration(api, env):
    doc = _export(api, env, "json", source_id=env["src"]["hr"]).json()
    assert doc["scope"] == {"kind": "source", "source_id": env["src"]["hr"], "source_name": "hr"}
    assert [s["name"] for s in doc["sources"]] == ["hr"] and [a["name"] for a in doc["assets"]] == ["people"]
    assert [g["name"] for g in doc["glossary"]] == ["Headcount"]
    assert [t["name"] for t in doc["suggested_model"]["tables"]] == ["people"]
    r = _export(api, env, "markdown", source_id=env["src"]["hr"])
    assert "-hr-context-" in r.headers["content-disposition"]
    other = api.post("/api/workspaces", headers=env["admin"], json={"name": "elsewhere"}).json()["id"]
    assert api.get(f"/api/workspaces/{other}/context/export", headers=env["admin"],
                   params={"format": "json", "source_id": env["src"]["hr"]}).status_code == 404
    assert api.get(f"/api/workspaces/{env['ws']}/context/export", headers=env["viewer"], params={"format": "pdf"}).status_code == 422
    outsider = api.post("/api/workspaces", headers=env["admin"], json={"name": "private"}).json()["id"]
    assert api.get(f"/api/workspaces/{outsider}/context/export", headers=env["viewer"]).status_code == 404


class CapturingRouter:
    """Stands in for the model router: records the messages `llm_json` would send, answers one SQL statement."""

    sink = None

    def __init__(self):
        self.messages = None

    def mode(self, purpose):
        return "always"

    def available(self, purpose, call):
        return True

    def record_skip(self, *a, **k):
        return None

    def complete(self, purpose, messages, **kw):
        self.messages = messages
        return SimpleNamespace(data={"sql": "SELECT 1"}, model="fake/model", cost_usd=0.0, escalated_from=None)


def test_preview_is_the_prompt_ask_sends_and_is_not_counted(api, env):
    from analystos.agents.common import llm_json
    from analystos.agents.sql_agent import ask_dialect, generation_context
    from analystos.context import cache as context_cache
    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall, User
    from analystos.services.ask import adhoc_context

    question = "What is the total order amount by customer segment?"
    before = context_cache.stats()
    url = f"/api/workspaces/{env['ws']}/context/preview"
    first = api.get(url, headers=env["admin"], params={"purpose": "sql_generation", "question": question})
    assert first.status_code == 200, first.text
    p = first.json()
    assert p["purpose"] == "sql_generation" and p["label"] == "Writing SQL for Ask"
    assert env["fq"]["orders"] in p["scope"]["assets"] and "CATALOG" in p["preamble_text"] and env["fq"]["orders"] in p["preamble_text"]
    volatile = json.loads(p["volatile_text"])
    assert volatile["question"] == question and volatile["omitted"] == {"tables_not_sent": [env["fq"]["people"]]}
    assert any(o.get("asset") == env["fq"]["people"] and o["reason"] == "not referenced" for o in p["omitted"])
    assert p["estimated_tokens"]["total"] == p["estimated_tokens"]["stable"] + p["estimated_tokens"]["volatile"] > 0
    assert {s["name"] for s in p["sections"]} >= {"system", "catalog", "question"} and p["knowledge_version"]
    assert p["cache"]["kind"] == "retrieval" and p["cache"]["key"] and p["cache"]["hit"] is False
    # S01-T7: cache entries are keyed by the caller's whole DataScope (role included), so a viewer does not reuse
    # the admin's entry; the viewer's own second preview does. The prompt text is the same either way.
    second = api.get(url, headers=env["viewer"], params={"purpose": "sql_generation", "question": question}).json()
    assert second["cache"]["hit"] is False and second["preamble_text"] == p["preamble_text"]
    third = api.get(url, headers=env["viewer"], params={"purpose": "sql_generation", "question": question}).json()
    assert third["cache"]["hit"] is True and third["preamble_text"] == p["preamble_text"]
    assert context_cache.stats() == before  # previews are not hits or misses

    with session_scope() as s:  # Ask's own context and generation path, through llm_json's real message assembly
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ctx = adhoc_context(s, admin, env["ws"])
        calls_before = s.query(ModelCall).filter(ModelCall.workspace_id == env["ws"]).count()
    router = CapturingRouter()
    ctx.services = SimpleNamespace(router=router)
    compiled, _, _ = generation_context(ctx, question)
    data, model = llm_json(ctx, "sql_generation", "sql_generation.v1", compiled, prompt_vars={"dialect": ask_dialect(ctx)})
    assert data == {"sql": "SELECT 1"} and model == "fake/model"
    system, preamble, volatile = (m["content"] for m in router.messages)
    assert (system, preamble, volatile) == (p["system_text"], p["preamble_text"], p["volatile_text"])
    with session_scope() as s:
        assert s.query(ModelCall).filter(ModelCall.workspace_id == env["ws"]).count() == calls_before  # the preview called nothing

    download = api.get(url, headers=env["viewer"], params={"purpose": "planning", "question": question, "download": 1})
    assert download.status_code == 200 and download.headers["content-type"].startswith("text/plain")
    assert "-context-preview-planning-" in download.headers["content-disposition"]
    assert "===== 2. WORKSPACE PREAMBLE (cached) =====" in download.text and question in download.text
    scoped = api.get(url, headers=env["viewer"], params={"purpose": "hypothesis_generation", "source_id": env["src"]["hr"]}).json()
    assert scoped["scope"]["assets"] == [env["fq"]["people"]] and scoped["question"]
    bad = api.get(url, headers=env["viewer"], params={"purpose": "insight_narrative"})
    assert bad.status_code == 422 and "sql_generation" in bad.text
    purposes = api.get(f"/api/workspaces/{env['ws']}/context/purposes", headers=env["viewer"]).json()
    assert purposes[0] == {"purpose": "sql_generation", "label": "Writing SQL for Ask",
                           "description": "What the model sees when Ask writes new SQL for a question."}


def test_context_cache_is_visible_to_editors_and_cleared_by_owners(api, env):
    url = f"/api/workspaces/{env['ws']}/context/cache"
    api.get(f"/api/workspaces/{env['ws']}/context/preview", headers=env["viewer"],
            params={"purpose": "sql_generation", "question": "orders per customer segment"})
    assert api.get(url, headers=env["viewer"]).status_code == 403
    stats = api.get(url, headers=env["editor"]).json()
    assert stats["workspace_id"] == env["ws"] and stats["entries"].get("retrieval", 0) >= 1 and stats["enabled"]
    assert stats["ttl_seconds"] > 0 and set(stats["totals"]) == {"hits", "misses", "chars_reused"}
    assert api.delete(url, headers=env["editor"]).status_code == 403
    cleared = api.delete(url, headers=env["admin"])
    assert cleared.status_code == 200 and cleared.json()["cleared"] >= 1
    assert api.get(url, headers=env["editor"]).json()["total_entries"] == 0

    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent

    with session_scope() as s:
        assert s.scalar(select(AuditEvent).where(AuditEvent.workspace_id == env["ws"], AuditEvent.action == "context.cache_cleared"))
