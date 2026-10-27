"""P4-T03/T04/T06 against Postgres: knowledge candidates from the context store and prior runs,
the workspace knowledge version (a knowledge edit misses the L0 cache), and the per-call record
of cached prompt tokens and context receipts on model_call."""
from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fakes import FakeTransport

from analystos.context.compiler import compile_context, load_knowledge
from analystos.contracts.platform import PlatformSettings, PurposeProfile
from analystos.core.ids import new_id
from analystos.db import vectors
from analystos.db.base import session_scope
from analystos.db.models import Hypothesis, Insight, ModelCall, User
from analystos.llm.cache import ResponseCache
from analystos.llm.router import ModelRouter
from analystos.runtime.usage import DbUsageSink

pytestmark = pytest.mark.integration

KEY = {"OPENROUTER_API_KEY": "sk-test-000000000000000000000000"}


@pytest.fixture()
def workspace(control_db):
    from analystos.core.config import get_settings
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        ws = create_workspace(s, admin, name=f"ctx {new_id('t')}", objective="SLA breaches")
        s.flush()
        return ws.id


def test_knowledge_candidates_come_from_the_store_and_other_runs(workspace):
    from analystos.context.service import add_entry

    with session_scope() as s:
        add_entry(s, workspace_id=workspace, kind="term", name="Breach rate", body="Share of incidents that missed SLA.",
                  mapped_columns=["sn.incident.made_sla"])
        add_entry(s, workspace_id=workspace, kind="rule", name="P1 escalation", body="P1 incidents escalate after 30 minutes.")
        add_entry(s, workspace_id=workspace, kind="episode", name="Run x", body="old run")
        for run_id, status in (("run_old", "verified"), ("run_now", "verified")):
            s.add(Insight(id=new_id("ins"), workspace_id=workspace, run_id=run_id, code="I-1", title=f"Finding of {run_id}",
                          finding="Network breaches SLA more often.", status=status))
        s.add(Hypothesis(id=new_id("hyp"), workspace_id=workspace, run_id="run_old", code="H-1",
                         statement="Weekday drives SLA breach", spec={"outcome": "made_sla", "segment": "weekday"}, status="rejected"))
        s.add(Hypothesis(id=new_id("hyp"), workspace_id=workspace, run_id="run_old", code="H-2",
                         statement="Priority drives SLA breach", status="supported"))
    sections = ["glossary", "business_rules", "prior_findings", "negative_knowledge"]
    with session_scope() as s:
        items = load_knowledge(s, workspace, sections, run_id="run_now")
    by_section: dict[str, list] = {}
    for i in items:
        by_section.setdefault(i.section, []).append(i)
    assert "Breach rate" in {i.name for i in by_section["glossary"]}
    assert any(i.source.startswith("pack:") for i in by_section["glossary"])  # installed pack knowledge is global
    assert [i.name for i in by_section["business_rules"] if i.source == "user"] == ["P1 escalation"]
    assert [i.source for i in by_section["prior_findings"]] == ["run:run_old"]  # never the current run's own findings
    assert [i.name for i in by_section["negative_knowledge"]] == ["Weekday drives SLA breach"]
    assert "episodes" not in by_section

    profile = PurposeProfile(sections=["catalog", *sections], max_chars=30_000)
    c = compile_context("hypothesis_generation", profile, objective="What drives SLA breaches?", required={"objective": "x"},
                        catalog=[{"asset": "sn.incident", "columns": [{"name": "made_sla", "semantic_type": "boolean"}]}],
                        knowledge=items)
    ids = {r["id"] for r in c.receipts}
    assert any(i.id in ids for i in by_section["glossary"] if i.name == "Breach rate")
    assert any(r["id"].startswith("hypothesis:") for r in c.receipts)


def test_knowledge_edit_changes_the_version_and_misses_the_cache(workspace):
    from analystos.context.service import add_entry
    from analystos.runtime.context import workspace_call_ctx

    platform = PlatformSettings()
    transport = FakeTransport(chat=lambda p: {"model": p["model"], "choices": [{"message": {"content": '{"ok":1}'}}],
                                              "usage": {"prompt_tokens": 100, "completion_tokens": 5}})
    router = ModelRouter(transport=transport, sink=DbUsageSink(), api_key_lookup=KEY.get, max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "same request"}]

    before = workspace_call_ctx(workspace)
    router.complete("planning", messages, ctx=before, json_output=True)
    router.complete("planning", messages, ctx=workspace_call_ctx(workspace), json_output=True)
    assert len(transport.chat_calls) == 1  # unchanged knowledge: L0 hit

    with session_scope() as s:
        add_entry(s, workspace_id=workspace, kind="episode", name="Run y", body="episodes do not change the version")
    assert workspace_call_ctx(workspace).knowledge_version == before.knowledge_version

    with session_scope() as s:
        add_entry(s, workspace_id=workspace, kind="term", name="Reopen", body="An incident reopened after resolution.")
    after = workspace_call_ctx(workspace)
    assert after.knowledge_version and after.knowledge_version != before.knowledge_version
    router.complete("planning", messages, ctx=after, json_output=True)
    assert len(transport.chat_calls) == 2  # the knowledge edit missed the cache
    with session_scope() as s:
        statuses = list(s.scalars(select(ModelCall.status).where(ModelCall.workspace_id == workspace).order_by(ModelCall.id)))
    assert statuses == ["ok", "cache_hit", "ok"]


def test_model_call_records_cached_tokens_and_receipts(workspace, pinned_models_config):
    # The pinned table still lists a prompt-cache model (Claude); the shipped models.yaml blocks that family.
    from analystos.runtime.context import workspace_call_ctx

    # A prompt-cache model on purpose: defaults are cheap-first now, and those models take plain strings.
    base = PlatformSettings()
    platform = base.model_copy(update={"llm": base.llm.model_copy(update={
        "profile_models": {**base.llm.profile_models, "analytical_reasoning": ["anthropic/claude-sonnet-5"]}})})
    usage = {"prompt_tokens": 1200, "completion_tokens": 10, "cost": 0.001, "prompt_tokens_details": {"cached_tokens": 900}}
    transport = FakeTransport(chat=lambda p: {"model": p["model"], "choices": [{"message": {"content": '{"ok":1}'}}],
                                              "usage": usage})
    router = ModelRouter(transport=transport, sink=DbUsageSink(), api_key_lookup=KEY.get, max_retries=0,
                         settings_provider=lambda: platform, cache=ResponseCache(None))
    ctx = workspace_call_ctx(workspace, run_id=new_id("run"))
    ctx.context_receipts = [{"id": "ctx_1", "section": "glossary", "source": "user", "sha256": "abc"}]
    router.complete("hypothesis_generation", [{"role": "system", "content": "static", "cache": True},
                                              {"role": "user", "content": "variable"}], ctx=ctx, json_output=True)
    assert transport.chat_calls[0]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    with session_scope() as s:
        row = s.scalar(select(ModelCall).where(ModelCall.run_id == ctx.run_id))
        assert row.cached_input_tokens == 900 and row.input_tokens == 1200
        assert row.context_receipts == ctx.context_receipts


def test_migration_0014_applies_and_reverts(control_db):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from analystos.core.config import REPO_ROOT

    base, name = control_db.rsplit("/", 1)
    mig_db = f"{name}_mig14"
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
        command.upgrade(cfg, "0014")
        assert {"cached_input_tokens", "context_receipts"} <= {c["name"] for c in inspect(engine).get_columns("model_call")}
        command.downgrade(cfg, "0011")
        assert not {"cached_input_tokens", "context_receipts"} & {c["name"] for c in inspect(engine).get_columns("model_call")}
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{mig_db}'"))
            c.execute(text(f"DROP DATABASE IF EXISTS {mig_db}"))
        admin.dispose()
