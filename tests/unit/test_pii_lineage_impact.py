"""P1-5 (review 2026-11-05) without services: sensitive tags follow column lineage (SQL CTEs, dbt chains, recipe runs,
OpenLineage) into review drafts that only ever ADD a tag, and a crawl's schema change lists what reads the changed
columns. Pausing impacted schedules is a hash-bound approval verified just before the side effect."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from analystos.core.errors import ApprovalRequired, Forbidden, InvalidInput
from analystos.core.ids import new_id
from analystos.db.models import (
    Artifact,
    AuditEvent,
    CrawlRun,
    KnowledgeSuggestion,
    Monitor,
    RecipeRun,
    RunEvent,
    Schedule,
    SemanticMetric,
    Source,
    SourceAsset,
    SourceColumn,
    User,
    VerifiedQuery,
    Workspace,
    WorkspaceMember,
)
from analystos.evidence.lineage import propagation as prop

# ------------------------------------------------------------------------------------ pure propagation
RAW = {("raw.customers", "email"): {"pii"}, ("raw.customers", "id"): set(), ("raw.customers", "ssn"): {"restricted"}}


def test_tags_follow_a_cte_chain_across_two_statements() -> None:
    # unqualified columns resolve through the catalog, as the API passes it
    edges = prop.edges_from_sql(
        "CREATE TABLE stg.customers AS WITH c AS (SELECT id, email AS contact FROM raw.customers) "
        "SELECT id, contact FROM c", catalog={"raw.customers": ["id", "email", "ssn"]})
    edges += prop.edges_from_sql("INSERT INTO mart.contacts SELECT s.id, UPPER(s.contact) AS contact_email "
                                 "FROM stg.customers AS s")
    gaps = {(g.target, tuple(g.missing)): g for g in prop.propagate(edges, RAW)}
    assert set(gaps) == {(("stg.customers", "contact"), ("pii",)), (("mart.contacts", "contact_email"), ("pii",))}
    far = gaps[(("mart.contacts", "contact_email"), ("pii",))]
    assert far.carried_from == ("raw.customers", "email")
    assert [e.target for e in far.path] == [("stg.customers", "contact"), ("mart.contacts", "contact_email")]
    assert far.confidence == 0.8  # the UPPER() hop is a derivation, not a copy


def test_filters_and_already_tagged_columns_raise_nothing() -> None:
    edges = prop.edges_from_sql("CREATE TABLE mart.active AS SELECT id FROM raw.customers WHERE email IS NOT NULL")
    assert prop.propagate(edges, RAW) == []  # a WHERE decides rows, it moves no values
    edges = prop.edges_from_sql("CREATE TABLE mart.c AS SELECT email FROM raw.customers")
    assert prop.propagate(edges, {**RAW, ("mart.c", "email"): {"pii", "owner_tag"}}) == []


def test_every_missing_tag_is_listed_and_cycles_terminate() -> None:
    edges = [prop.ColumnEdge(("raw.customers", "ssn"), ("a.t", "x"), "DIRECT", "o"),
             prop.ColumnEdge(("raw.customers", "email"), ("a.t", "x"), "DIRECT", "o"),
             prop.ColumnEdge(("a.t", "x"), ("b.t", "y"), "AGGREGATED", "o"),
             prop.ColumnEdge(("b.t", "y"), ("a.t", "x"), "DIRECT", "o")]
    gaps = {g.target: g for g in prop.propagate(edges, {**RAW, ("a.t", "x"): {"restricted"}})}
    assert gaps[("a.t", "x")].missing == ["pii"] and gaps[("a.t", "x")].present == ["restricted"]
    assert gaps[("b.t", "y")].missing == ["pii", "restricted"] and gaps[("b.t", "y")].confidence == 0.6


def test_tags_follow_a_dbt_model_chain() -> None:
    manifest = {"nodes": {
        "model.p.stg_customers": {
            "resource_type": "model", "name": "stg_customers", "schema": "staging", "database": "an",
            "depends_on": {"nodes": ["source.p.raw.customers"]}, "columns": {},
            "compiled_code": 'WITH src AS (SELECT c.id, c.email FROM "an"."raw"."customers" AS c) '
                             "SELECT s.id AS customer_id, LOWER(s.email) AS email_address FROM src AS s"},
        "model.p.dim_customer": {
            "resource_type": "model", "name": "dim_customer", "schema": "mart", "database": "an",
            "depends_on": {"nodes": ["model.p.stg_customers"]}, "columns": {},
            "compiled_code": 'SELECT st.customer_id, st.email_address AS contact FROM "an"."staging"."stg_customers" AS st'}},
        "sources": {"source.p.raw.customers": {"resource_type": "source", "schema": "raw", "name": "customers",
                                               "database": "an"}}}
    edges = prop.edges_from_dbt_manifest(manifest)
    gaps = {g.target: g for g in prop.propagate(edges, RAW)}
    assert set(gaps) == {("staging.stg_customers", "email_address"), ("mart.dim_customer", "contact")}
    assert gaps[("mart.dim_customer", "contact")].carried_from == ("raw.customers", "email")
    assert {e.origin for e in gaps[("mart.dim_customer", "contact")].path} == {"dbt:model.p.stg_customers",
                                                                              "dbt:model.p.dim_customer"}


def test_openlineage_and_recipe_lineage_adapters() -> None:
    from analystos.build.lineage import openlineage_events

    manifest = {"nodes": {"model.p.m": {
        "resource_type": "model", "name": "m", "schema": "mart", "depends_on": {"nodes": ["source.p.raw.customers"]},
        "columns": {}, "compiled_code": ('SELECT c.email AS e FROM "an"."raw"."customers" AS c '
                                         "WHERE c.state = 'closed'")}},
        "sources": {"source.p.raw.customers": {"resource_type": "source", "schema": "raw", "name": "customers",
                                               "database": "an"}}}
    now = datetime(2026, 11, 5, tzinfo=UTC)
    events = openlineage_events(job_id="bld_1", workspace_id="ws", manifest=manifest,
                                run_results={"results": [{"unique_id": "model.p.m", "status": "success"}]},
                                namespace="pg://h", database="an", started_at=now, finished_at=now)
    edges = prop.edges_from_openlineage(events, "build_job:bld_1")
    assert [(e.source, e.target) for e in edges] == [(("raw.customers", "email"), ("mart.m", "e"))]
    lineage = [{"output": "o", "columns": {"mail": [{"asset": "raw.customers", "column": "email", "subtype": "IDENTITY"}]},
                "dataset": [{"asset": "raw.customers", "column": "id", "subtypes": ["FILTER"]}]}]
    edges = prop.edges_from_recipe(lineage, {"o": {"table": "recipes.r_o"}}, "recipe_run:r1")
    assert [(e.source, e.target) for e in edges] == [(("raw.customers", "email"), ("recipes.r_o", "mail"))]


# ------------------------------------------------------------------------------------ database fixture
TABLES = ("tenant", "tenant_member", "app_user", "workspace", "workspace_member", "workspace_policy", "source",
          "source_asset", "source_column", "knowledge_suggestion", "knowledge_pack", "knowledge_document",
          "audit_event", "run_event", "lineage_edge", "recipe_run", "build_job", "crawl_run", "semantic_model",
          "semantic_metric", "verified_query", "artifact", "monitor", "schedule", "approval", "context_entry",
          "context_ledger_entry", "context_ledger_change", "conversation_correction", "request", "request_event")


@pytest.fixture
def world(monkeypatch):
    from analystos.db import base, models
    from analystos.events import bus

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
                           json_serializer=base.json_dumps)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")

    models.Base.metadata.create_all(engine, tables=[models.Base.metadata.tables[t] for t in TABLES
                                                    if t in models.Base.metadata.tables])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(base, "SessionLocal", lambda: factory())
    monkeypatch.setattr(bus, "_redis", False)
    with factory() as s:
        users = {}
        for name in ("owner", "editor", "approver"):
            users[name] = User(id=new_id("usr"), email=f"{name}@t.local", name=name, password_hash="!", is_admin=False,
                               active=True, attributes={})
            s.add(users[name])
        s.flush()
        ws = Workspace(id=new_id("ws"), name="w", created_by=users["owner"].id, status="active", settings={},
                       policy_version=1)
        s.add(ws)
        s.flush()
        for name, role in (("owner", "owner"), ("editor", "editor"), ("approver", "approver")):
            s.add(WorkspaceMember(workspace_id=ws.id, user_id=users[name].id, role=role))
        src = Source(id=new_id("src"), workspace_id=ws.id, name="crm", kind="postgres", config={})
        s.add(src)
        assets = {}
        for schema, name, cols in (("raw", "customers", {"id": [], "email": ["pii"], "region": []}),
                                   ("recipes", "r_contacts", {"customer_id": [], "mail": ["owner_tag"]}),
                                   ("recipes", "r_summary", {"region": [], "mail_domain": []})):
            a = SourceAsset(id=new_id("ast"), source_id=src.id, workspace_id=ws.id, schema_name=schema, name=name,
                            source_name=name, lifecycle="active", semantics={"source_key": f"{schema}.{name}"})
            s.add(a)
            s.flush()
            for i, (c, tags) in enumerate(cols.items()):
                s.add(SourceColumn(asset_id=a.id, name=c, ordinal=i, data_type="text", tags=tags, profile={},
                                   semantics={}, tags_origin="crawler"))
            assets[f"{schema}.{name}"] = a.id
        # recipe 1: raw.customers -> recipes.r_contacts; recipe 2: r_contacts.mail -> r_summary.mail_domain
        for rid, out, cols in (("rcp_1", "contacts", {"customer_id": ("raw.customers", "id"),
                                                      "mail": ("raw.customers", "email")}),
                               ("rcp_2", "summary", {"region": ("raw.customers", "region"),
                                                     "mail_domain": ("recipes.r_contacts", "mail")})):
            s.add(RecipeRun(id=new_id("rrun"), workspace_id=ws.id, recipe_id=rid, recipe_name=rid, recipe_version=1,
                            spec_hash="h", mode="materialize", status="succeeded", plan={}, outputs={
                                out: {"table": f"recipes.r_{out}"}},
                            lineage=[{"output": out, "dataset": [], "columns": {
                                c: [{"asset": a, "column": col, "subtype": "TRANSFORMATION"}]
                                for c, (a, col) in cols.items()}}], created_by=users["owner"].id))
        s.commit()
        for u in users.values():
            s.expunge(u)
    return SimpleNamespace(db=factory, ws=ws.id, src=src.id, assets=assets, **users)


def _tags(s, asset_id: str, name: str) -> list[str]:
    return s.scalar(select(SourceColumn.tags).where(SourceColumn.asset_id == asset_id, SourceColumn.name == name))


# ------------------------------------------------------------------------------------ PII scan + review
def test_scan_proposes_add_only_drafts_through_a_recipe_chain(world) -> None:
    from analystos.services.pii_lineage import scan

    with world.db() as s:
        out = scan(s, world.ws, actor="user:x")
        s.commit()
        cols = {(g["asset"], g["column"]) for g in out["gaps"]}
        assert cols == {("recipes.r_contacts", "mail"), ("recipes.r_summary", "mail_domain")}
        far = next(g for g in out["gaps"] if g["column"] == "mail_domain")
        assert far["hops"] == 2 and far["carried_from"] == {"asset": "raw.customers", "column": "email"}
        drafts = s.scalars(select(KnowledgeSuggestion).where(KnowledgeSuggestion.kind == "column_tag")).all()
        assert len(drafts) == 2 and all(d.fields["tags"]["value"] == ["pii"] for d in drafts)
        assert all(d.origin == "pii_lineage" and d.status == "pending" for d in drafts)
        # nothing is applied by the scan
        assert _tags(s, world.assets["recipes.r_contacts"], "mail") == ["owner_tag"]
        assert s.scalar(select(RunEvent).where(RunEvent.type == "lineage.pii_gap_detected")) is not None
        again = scan(s, world.ws, actor="user:x")  # idempotent: the same pending drafts, no new rows
        assert sorted(again["proposed"]) == sorted(out["proposed"])
        assert len(s.scalars(select(KnowledgeSuggestion)).all()) == 2


def test_approval_adds_tags_and_never_removes_one(world, monkeypatch) -> None:
    from analystos.knowledge import store
    from analystos.knowledge.suggestions import review
    from analystos.services.pii_lineage import scan

    committed: list[dict] = []  # a rejection writes negative knowledge into the pack (Postgres-only index there)
    monkeypatch.setattr(store, "workspace_pack", lambda *a, **k: SimpleNamespace(id="kpk_test", head_revision=7))
    monkeypatch.setattr(store, "commit", lambda session, pack, files, **k: committed.append(files))

    with world.db() as s:
        out = scan(s, world.ws, actor="user:x")
        by_col = {g["column"]: g["suggestion_id"] for g in out["gaps"]}
        user = s.merge(world.editor)
        # an edit cannot smuggle in a tag outside the sensitive set, and cannot empty the list
        res = review(s, world.ws, user, [{"id": by_col["mail"], "action": "edit", "fields": {"tags": ["public"]}},
                                         {"id": by_col["mail_domain"], "action": "edit", "fields": {"tags": []}}])
        assert {e["error"] for e in res["errors"]} == {"tag_not_allowed", "no_tags"}
        res = review(s, world.ws, user, [{"id": by_col["mail"], "action": "approve"},
                                         {"id": by_col["mail_domain"], "action": "reject", "reason": "hashed"}])
        s.commit()
        assert res["approved"][0]["catalog"] == "tags added: pii"
        contacts = s.scalar(select(SourceColumn).where(SourceColumn.asset_id == world.assets["recipes.r_contacts"],
                                                       SourceColumn.name == "mail"))
        assert contacts.tags == ["owner_tag", "pii"] and contacts.tags_origin == "user"  # the owner tag stays
        assert _tags(s, world.assets["recipes.r_summary"], "mail_domain") == []  # a rejection changes nothing
        assert [p.split("/")[0] for p in committed[0]] == ["negative"]  # ...but is remembered as negative knowledge
        audit = s.scalar(select(AuditEvent).where(AuditEvent.action == "column.tags_added"))
        assert audit.details["before"] == ["owner_tag"] and audit.details["after"] == ["owner_tag", "pii"]
        # the tagged output is now a seed: the rejected draft is not proposed again, nothing new appears
        after = scan(s, world.ws, actor="user:x")
        assert [g["column"] for g in after["gaps"]] == ["mail_domain"] and after["proposed"] == []
        assert after["gaps"][0]["rejected_before"] and after["gaps"][0]["hops"] == 1  # now carried from mail


def test_uncatalogued_targets_are_reported_not_drafted(world) -> None:
    from analystos.services.pii_lineage import scan

    extra = prop.edges_from_sql("CREATE TABLE scratch.t AS SELECT c.email AS e FROM raw.customers AS c")
    with world.db() as s:
        out = scan(s, world.ws, actor="user:x", extra_edges=extra, propose_drafts=False)
        assert [(g["asset"], g["column"]) for g in out["uncatalogued"]] == [("scratch.t", "e")]
        assert out["proposed"] == [] and s.scalars(select(KnowledgeSuggestion)).all() == []


# ------------------------------------------------------------------------------------ impact report
def _crawl_with_removed_email(s, world) -> CrawlRun:
    crawl = CrawlRun(id=new_id("crawl"), source_id=world.src, workspace_id=world.ws, mode="full", trigger="manual",
                     status="succeeded", stage="done", options={}, stats={}, log=[], started_by="user:x",
                     changes={"new": [], "missing": [], "deprecated": [], "rename_candidates": [], "changed": [
                         {"key": "raw.customers", "previous_fingerprint": "a", "fingerprint": "b", "added": ["phone"],
                          "removed": ["email"], "retyped": [], "attributes_changed": False}]})
    s.add(crawl)
    return crawl


def _dependents(s, world) -> dict[str, str]:
    owner = world.owner.id
    vq = VerifiedQuery(id=new_id("vq"), workspace_id=world.ws, name="emails by region", patterns=[], parameters=[],
                       sql_template="SELECT c.region, c.email FROM raw.customers AS c WHERE c.region = {{region}}",
                       origin={}, created_by=owner)
    vq_safe = VerifiedQuery(id=new_id("vq"), workspace_id=world.ws, name="regions", patterns=[], parameters=[],
                            sql_template="SELECT region, COUNT(*) AS n FROM raw.customers GROUP BY region", origin={},
                            created_by=owner)
    metric = SemanticMetric(id=new_id("met"), workspace_id=world.ws, name="contactable", version=1, status="approved",
                            definition={"name": "contactable", "dataset": "customers", "source_columns": ["email"],
                                        "expressions": [{"dialect": "postgres", "expression": "COUNT(email)"}]},
                            expression="COUNT(email)", normalized_expression="count(email)", proposed_by=owner,
                            proposed_via="user", content_hash="h")
    dash = Artifact(id=new_id("art"), workspace_id=world.ws, type="dashboard", name="Contacts", status="published",
                    content={"charts": [{"sql": "SELECT mail_domain FROM recipes.r_summary"}]}, content_hash="h")
    other = Artifact(id=new_id("art"), workspace_id=world.ws, type="dashboard", name="Regions", status="published",
                     content={"charts": [{"sql": "SELECT region FROM raw.customers"}]}, content_hash="h")
    mon = Monitor(id=new_id("mon"), workspace_id=world.ws, name="contactable drop", kind="metric_threshold",
                  config={"metric": "contactable"}, created_by=owner)
    sch_mon = Schedule(id=new_id("sch"), workspace_id=world.ws, name="evaluate monitors", kind="monitor", cron="0 * * * *",
                       config={"monitor_id": mon.id}, owner_id=owner, enabled=True, revision=1)
    sch_other = Schedule(id=new_id("sch"), workspace_id=world.ws, name="refresh regions", kind="dataset_refresh",
                         cron="0 * * * *", config={"asset": "raw.customers", "columns": ["region"]}, owner_id=owner,
                         enabled=True, revision=1)
    s.add_all([vq, vq_safe, metric, dash, other, mon, sch_mon, sch_other])
    return {"vq": vq.id, "metric": metric.id, "dash": dash.id, "monitor": mon.id, "schedule": sch_mon.id,
            "unrelated_schedule": sch_other.id, "unrelated_dash": other.id, "unrelated_vq": vq_safe.id}


def test_impact_lists_what_reads_a_removed_column(world) -> None:
    from analystos.services.change_impact import changed_columns, impact

    with world.db() as s:
        crawl = _crawl_with_removed_email(s, world)
        ids = _dependents(s, world)
        s.commit()
        rep = impact(s, world.ws, changed_columns(s, crawl))  # unfiltered: what the system sees
    assert rep["changed"] == [{"asset": "raw.customers", "asset_id": world.assets["raw.customers"], "key": "raw.customers",
                               "column": "email", "change": "removed"}]  # an added column breaks nothing
    assert {(d["asset"], d["column"]) for d in rep["downstream"]} == {("recipes.r_contacts", "mail"),
                                                                      ("recipes.r_summary", "mail_domain")}
    assert [q["id"] for q in rep["verified_queries"]] == [ids["vq"]]
    assert [m["id"] for m in rep["metrics"]] == [ids["metric"]]
    assert [d["id"] for d in rep["dashboards"]] == [ids["dash"]]  # reads a downstream column
    assert rep["dashboards"][0]["reads"][0]["change"] == "downstream"
    assert [m["id"] for m in rep["monitors"]] == [ids["monitor"]]
    assert rep["monitors"][0]["via"] == ["metric:contactable"]
    assert [x["id"] for x in rep["schedules"]] == [ids["schedule"]]
    assert rep["schedules"][0]["via"] == [f"monitor:{ids['monitor']}"]


def test_a_deprecated_table_impacts_everything_reading_it(world) -> None:
    from analystos.services.change_impact import changed_columns, impact

    with world.db() as s:
        crawl = _crawl_with_removed_email(s, world)
        crawl.changes = {**crawl.changes, "changed": [], "deprecated": ["raw.customers"]}
        ids = _dependents(s, world)
        s.commit()
        rep = impact(s, world.ws, changed_columns(s, crawl))
    assert {d["id"] for d in rep["dashboards"]} == {ids["dash"], ids["unrelated_dash"]}
    assert {q["id"] for q in rep["verified_queries"]} == {ids["vq"], ids["unrelated_vq"]}
    assert ids["unrelated_schedule"] in {x["id"] for x in rep["schedules"]}


def test_pausing_impacted_schedules_needs_a_bound_approval(world) -> None:
    from analystos.governance.approvals import decide
    from analystos.services.change_impact import execute_pause, request_pause

    with world.db() as s:
        crawl = _crawl_with_removed_email(s, world)
        ids = _dependents(s, world)
        s.commit()
        editor = s.merge(world.editor)
        with pytest.raises(InvalidInput):  # only schedules the change impacts
            request_pause(s, editor, crawl.id, [ids["unrelated_schedule"]])
        req = request_pause(s, editor, crawl.id, [ids["schedule"]])
        s.commit()
        assert s.get(Schedule, ids["schedule"]).enabled  # a request changes nothing
        with pytest.raises(ApprovalRequired):
            execute_pause(s, editor, req["approval_id"])
        with pytest.raises(Forbidden):
            decide(s, req["approval_id"], editor, approve=True)  # an editor cannot approve
        decide(s, req["approval_id"], s.merge(world.approver), approve=True)
        s.commit()
        out = execute_pause(s, editor, req["approval_id"])
        s.commit()
        sch = s.get(Schedule, ids["schedule"])
        assert out["paused"] == [ids["schedule"]] and not sch.enabled and sch.revision == 2
        assert s.get(Schedule, ids["unrelated_schedule"]).enabled
        assert s.scalar(select(RunEvent).where(RunEvent.type == "schedule.paused_for_change")) is not None
        with pytest.raises(ApprovalRequired):  # single use
            execute_pause(s, editor, req["approval_id"])


def test_a_schedule_edited_after_approval_needs_a_new_one(world) -> None:
    from analystos.governance.approvals import decide
    from analystos.services.change_impact import execute_pause, request_pause

    with world.db() as s:
        crawl = _crawl_with_removed_email(s, world)
        ids = _dependents(s, world)
        s.commit()
        editor = s.merge(world.editor)
        req = request_pause(s, editor, crawl.id, [ids["schedule"]])
        decide(s, req["approval_id"], s.merge(world.approver), approve=True)
        s.get(Schedule, ids["schedule"]).revision = 5  # edited in the meantime
        s.commit()
        with pytest.raises(ApprovalRequired):
            execute_pause(s, editor, req["approval_id"])
        assert s.get(Schedule, ids["schedule"]).enabled


def test_impact_report_hides_what_the_caller_cannot_read(world) -> None:
    from analystos.services.change_impact import changed_columns, filter_for_scope, impact

    with world.db() as s:
        crawl = _crawl_with_removed_email(s, world)
        _dependents(s, world)
        s.commit()
        rep = impact(s, world.ws, changed_columns(s, crawl))
        # the removed column is tagged pii and denied; the recipe outputs are not in this caller's scope
        scope = SimpleNamespace(assets=["raw.customers"], denied_columns=["raw.customers.email"])
        seen = filter_for_scope(s, rep, scope)
    assert seen["changed"] == [] and seen["downstream"] == []
    assert all(not seen[k] for k in ("metrics", "verified_queries", "dashboards", "monitors", "schedules"))
    assert seen["hidden"] > 0 and seen["counts"]["schedules"] == 0


def test_pii_scan_returns_only_gaps_the_caller_may_read(world) -> None:
    from analystos.services.pii_lineage import scan

    with world.db() as s:
        # an uncleared caller: the pii source column is denied, so no gap (or its column names) comes back
        blind = SimpleNamespace(assets=["raw.customers", "recipes.r_contacts", "recipes.r_summary"],
                                denied_columns=["raw.customers.email"])
        out = scan(s, world.ws, actor="user:x", scope=blind)
        assert out["gaps"] == [] and out["proposed"] == [] and out["hidden"] == 2
        assert s.scalars(select(KnowledgeSuggestion)).all() == []
        cleared = SimpleNamespace(assets=blind.assets, denied_columns=[])
        out = scan(s, world.ws, actor="user:x", scope=cleared)
        assert len(out["gaps"]) == 2 and out["hidden"] == 0
        ev = s.scalars(select(RunEvent).where(RunEvent.type == "lineage.pii_gap_detected")).all()[-1]
        assert "columns" not in ev.payload and "mail" not in str(ev.payload)  # ids and counts only
