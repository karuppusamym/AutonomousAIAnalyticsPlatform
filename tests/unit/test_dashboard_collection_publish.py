"""BI-VIEWS-02: publish one topic collection of an investigation on its own, and group existing Superset dashboards
under the workspace name. Both are side effects bound to an approval of the exact payload, re-verified immediately
before the BI call; a fake BI tool records what would be written (no services)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from tests.unit.step_fixtures import WS, world  # noqa: F401

from analystos.contracts.bi import ChartSpec, DashboardSpec, PublishResult
from analystos.core.config import get_settings
from analystos.core.errors import ApprovalRequired, Conflict, InvalidInput, PolicyDenied, UpstreamUnavailable
from analystos.core.ids import stable_hash
from analystos.db.base import session_scope
from analystos.db.models import AnalysisRun, Approval, Artifact, AuditEvent, Publication
from analystos.governance.approvals import decide

RUN = "run_collections"
DS_SQL = "SELECT id, state, amount, ordered_at, late FROM sales.orders"


class FakeBI:
    """Records publishes and renames; holds existing dashboards the way Superset's REST API returns them."""

    destination = "superset"

    def __init__(self, dashboards: list[dict] | None = None):
        self.published: list = []
        self.renamed: list[tuple[int, str]] = []
        self.fail_publish = False
        self.fail_rename_after: int | None = None
        self.dashboards = {d["id"]: dict(d) for d in dashboards or []}

    def publish(self, bundle, *, idempotency_key, previous=None):  # noqa: ANN001
        if self.fail_publish:
            return PublishResult(destination="superset", status="failed", errors=["superset down"])
        self.published.append((bundle, idempotency_key))
        ids = {"datasets": {d.name: 7 for d in bundle.datasets},
               "charts": {c.key: 100 + i for i, c in enumerate(bundle.charts)},
               "dashboards": {d.key: 200 + i for i, d in enumerate(bundle.dashboards)}}
        return PublishResult(destination="superset", status="succeeded", external_ids=ids,
                             urls={k: f"http://bi.test/superset/dashboard/{v}/" for k, v in ids["dashboards"].items()})

    def list_dashboards(self, workspace_id):  # noqa: ANN001
        return [{"id": d["id"], "title": d["dashboard_title"], "slug": d["slug"], "published": True,
                 "url": f"http://bi.test/superset/dashboard/{d['id']}/"} for d in self.dashboards.values()]

    def object_state(self, resource, oid):  # noqa: ANN001
        d = self.dashboards.get(int(oid))
        return dict(d) if d else None

    def rename_dashboard(self, external_id, title):  # noqa: ANN001
        from analystos.core.errors import UpstreamUnavailable as Down

        if self.fail_rename_after is not None and len(self.renamed) >= self.fail_rename_after:
            raise Down("superset down")
        self.dashboards[int(external_id)]["dashboard_title"] = title
        self.renamed.append((int(external_id), title))


def chart(key: str, title: str, **kw) -> dict:
    return ChartSpec(key=key, title=title, chart_type="bar", intent="comparison", dataset="orders", metric="record_count",
                     dimension="state", preview={"columns": ["x", "y"], "rows": [["Open", 4]]}, **kw).model_dump()


def board(key: str, title: str, charts: list[str], audience: str = "operational") -> dict:
    return DashboardSpec(key=key, title=title, audience=audience, charts=charts,
                         layout=[{"chart": c, "row": i, "col": 0, "width": 12} for i, c in enumerate(charts)]).model_dump()


@pytest.fixture
def setup(world, monkeypatch):  # noqa: F811
    from datetime import datetime

    from analystos.governance import approvals

    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone
    monkeypatch.setattr(get_settings(), "superset_url", "http://bi.test")
    monkeypatch.setattr("analystos.services.platform_settings.get",
                        lambda: SimpleNamespace(features=SimpleNamespace(superset_publishing=True)))
    with session_scope() as s:
        s.add(AnalysisRun(id=RUN, workspace_id=WS, objective="Why are orders late?", status="COMPLETED", plan={},
                          plan_version=1, plan_hash="plan-1", policy_version=1, requested_by="usr_owner"))
        arts = [("dataset", "orders", {"name": "orders", "sql": DS_SQL, "columns": [{"name": c} for c in
                                                                                    ("id", "state", "amount", "ordered_at", "late")],
                                       "source_assets": ["sales.orders"]}),
                ("metric", "record_count", {"name": "record_count", "display_name": "Orders", "definition": "count",
                                            "sql_expression": "COUNT(*)"}),
                ("metric", "late_rate", {"name": "late_rate", "display_name": "Late %", "definition": "late share",
                                         "sql_expression": "AVG(CASE WHEN late THEN 1.0 ELSE 0 END)"}),
                ("chart", "kpi_record_count", chart("kpi_record_count", "Orders")),
                ("chart", "trend_volume", chart("trend_volume", "Volume by month")),
                ("chart", "late_by_state", {**chart("late_by_state", "Late deliveries by state"), "metric": "late_rate"}),
                ("chart", "detail_table", chart("detail_table", "Detail")),
                ("dashboard", "executive", board("executive", "steps / Executive overview",
                                                 ["kpi_record_count", "late_by_state"], "executive")),
                ("dashboard", "topic_activity", board("topic_activity", "steps / Activity and volume",
                                                      ["kpi_record_count", "trend_volume"])),
                ("dashboard", "topic_delivery", board("topic_delivery", "steps / Delivery reliability",
                                                      ["late_by_state", "detail_table"]))]
        for i, (type_, name, content) in enumerate(arts):
            s.add(Artifact(id=f"art_{i:02d}", workspace_id=WS, run_id=RUN, type=type_, name=name, version=1, plan_version=1,
                           status="draft", content=content, content_hash=stable_hash(content)))
    return world


def _svc():
    from analystos.dashboards import collections

    return collections


def _request(user, keys, **kw):
    with session_scope() as s:
        a = _svc().request_collection(s, s.merge(user), RUN, keys, **kw)
        return a.id, a.payload_hash, dict(a.payload)


def _approve(users, approval_id):
    with session_scope() as s:
        decide(s, approval_id, s.merge(users["approver"]), approve=True)


def test_overview_lists_the_runs_dashboards_topic_views_last(setup):
    with session_scope() as s:
        view = _svc().overview(s, s.merge(setup["viewer"]), RUN)
    assert [d["key"] for d in view["dashboards"]] == ["executive", "topic_activity", "topic_delivery"]
    assert [d["kind"] for d in view["dashboards"]] == ["executive", "topic", "topic"]
    assert view["destination"] == "superset" and view["requests"] == []


def test_one_collection_bundle_holds_only_its_dashboards_charts_and_kpis(setup):
    _, _, payload = _request(setup["owner"], ["topic_delivery"])
    bundle = payload["bundle"]
    assert payload["collection"] == ["topic_delivery"] and payload["run_id"] == RUN and payload["plan_hash"] == "plan-1"
    assert [d["key"] for d in bundle["dashboards"]] == ["topic_delivery"]
    assert [c["key"] for c in bundle["charts"]] == ["late_by_state", "detail_table"]
    assert {m["name"] for m in bundle["metrics"]} == {"late_rate", "record_count"}
    assert all(c["preview"] == {} for c in bundle["charts"])  # previews are not published


def test_a_different_selection_is_a_different_approval_hash(setup):
    first_id, first_hash, _ = _request(setup["owner"], ["topic_delivery"])
    again_id, again_hash, _ = _request(setup["owner"], ["topic_delivery"])
    other_id, other_hash, _ = _request(setup["owner"], ["topic_delivery", "topic_activity"])
    assert (again_id, again_hash) == (first_id, first_hash)  # same selection: the open request is reused
    assert other_hash != first_hash and other_id != first_id


def test_unknown_dashboard_and_role_checks_refuse(setup):
    with pytest.raises(InvalidInput):
        _request(setup["owner"], ["topic_sla"])
    from analystos.core.errors import AnalystOSError

    with pytest.raises(AnalystOSError):  # a viewer cannot ask to publish
        _request(setup["viewer"], ["topic_delivery"])


def test_nothing_is_published_without_an_approval(setup):
    approval_id, _, _ = _request(setup["owner"], ["topic_delivery"])
    bi = FakeBI()
    with pytest.raises(ApprovalRequired), session_scope() as s:
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)
    assert bi.published == []


def test_approved_collection_publishes_only_that_selection(setup):
    approval_id, payload_hash, _ = _request(setup["owner"], ["topic_delivery"])
    _approve(setup, approval_id)
    bi = FakeBI()
    with session_scope() as s:
        out = _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)
    assert out["status"] == "published" and out["collection"] == ["topic_delivery"]
    (bundle, key), = bi.published
    assert [d.key for d in bundle.dashboards] == ["topic_delivery"]
    assert key.startswith(f"collection:{RUN}:") and key.endswith(payload_hash[:32])
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "executed"
        pub = s.scalar(select(Publication).where(Publication.approval_id == approval_id))
        assert pub.status == "succeeded" and pub.run_id == RUN
        arts = {a.name: a for a in s.scalars(select(Artifact).where(Artifact.run_id == RUN))}
        assert arts["topic_delivery"].status == "published" and arts["topic_delivery"].external_url
        assert arts["topic_activity"].status == "draft" and arts["executive"].status == "draft"
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "publication.executed", AuditEvent.target == pub.id))
        view = _svc().overview(s, s.merge(setup["viewer"]), RUN)
    assert view["requests"][0]["publication"]["status"] == "succeeded"
    with pytest.raises(ApprovalRequired), session_scope() as s:  # single use
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)


def test_a_chart_changed_after_approval_needs_a_new_approval(setup):
    approval_id, _, _ = _request(setup["owner"], ["topic_delivery"])
    _approve(setup, approval_id)
    with session_scope() as s:
        art = s.scalar(select(Artifact).where(Artifact.run_id == RUN, Artifact.name == "detail_table"))
        art.content = {**art.content, "title": "Detail (edited)"}
    bi = FakeBI()
    with pytest.raises(ApprovalRequired), session_scope() as s:
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)
    assert bi.published == []


def test_a_replan_needs_a_new_approval(setup):
    approval_id, _, _ = _request(setup["owner"], ["topic_delivery"])
    _approve(setup, approval_id)
    with session_scope() as s:
        s.get(AnalysisRun, RUN).plan_hash = "plan-2"
    with pytest.raises(ApprovalRequired), session_scope() as s:
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=FakeBI())


def test_failed_bi_call_keeps_the_approval_for_a_retry(setup):
    approval_id, _, _ = _request(setup["owner"], ["topic_activity"])
    _approve(setup, approval_id)
    bi = FakeBI()
    bi.fail_publish = True
    with pytest.raises(UpstreamUnavailable), session_scope() as s:
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)
    # the route commits the failure; here the scope rolled back, so the approval is still approved either way
    bi.fail_publish = False
    with session_scope() as s:
        assert s.get(Approval, approval_id).status == "approved"
        out = _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=bi)
    assert out["status"] == "published"


def test_superset_switched_off_after_approval_refuses(setup, monkeypatch):
    approval_id, _, _ = _request(setup["owner"], ["topic_delivery"])
    _approve(setup, approval_id)
    monkeypatch.setattr("analystos.services.platform_settings.get",
                        lambda: SimpleNamespace(features=SimpleNamespace(superset_publishing=False)))
    with pytest.raises(PolicyDenied), session_scope() as s:
        _svc().execute_collection(s, s.merge(setup["owner"]), approval_id, publisher=FakeBI())


def test_rolling_a_collection_back_keeps_charts_another_publication_shows(setup):
    with session_scope() as s:
        s.add(Publication(id="pub_run", workspace_id=WS, run_id=RUN, approval_id=None, destination="superset",
                          idempotency_key="run-whole", status="succeeded",
                          external_ids={"charts": {"late_by_state": 100}, "datasets": {"orders": 7}, "dashboards": {"executive": 1}}))
        pub = Publication(id="pub_col", workspace_id=WS, run_id=RUN, approval_id=None, destination="superset",
                          idempotency_key=f"collection:{RUN}:abc:def", status="succeeded",
                          external_ids={"charts": {"late_by_state": 100, "detail_table": 101}, "datasets": {"orders": 7},
                                        "dashboards": {"topic_delivery": 200}})
        s.add(pub)
        s.flush()
        ids = _svc().rollback_ids(s, pub)
        assert _svc().selection_of(pub) == f"collection:{RUN}:abc"
    assert ids == {"dashboards": {"topic_delivery": 200}, "charts": {"detail_table": 101}, "datasets": {}}


def test_reconcile_keeps_the_run_and_each_collection_apart(setup):
    from analystos.dashboards.reconcile import latest_publications

    with session_scope() as s:
        for pid, key in (("pub_a", "run-whole"), ("pub_b", f"collection:{RUN}:sel1:h1"), ("pub_c", f"collection:{RUN}:sel1:h2"),
                         ("pub_d", f"collection:{RUN}:sel2:h1")):
            s.add(Publication(id=pid, workspace_id=WS, run_id=RUN, destination="superset", idempotency_key=key,
                              status="succeeded", external_ids={}))
        s.flush()
        assert len(latest_publications(s, WS)) == 3


# ------------------------------------------------------------------------------------------------- grouping
def existing() -> list[dict]:
    css = "/* aos workspace=ws_steps key={k} audience=executive bundle=abc */"
    return [{"id": 16, "slug": "aos-ws-steps-executive", "dashboard_title": "Executive overview: Why are orders late?",
             "css": css.format(k="executive"), "published": True},
            {"id": 17, "slug": "aos-ws-steps-operational", "dashboard_title": "steps / Operations deep-dive",
             "css": css.format(k="operational"), "published": True},
            {"id": 30, "slug": "aos-other-executive", "dashboard_title": "Someone else's", "css": "", "published": True},
            {"id": 31, "slug": "aos-ws-steps-x", "dashboard_title": "Stamped for another workspace",
             "css": "/* aos workspace=ws_other key=x */", "published": True},
            {"id": 40, "slug": "hand-made", "dashboard_title": "Hand made", "css": "", "published": True}]


def test_grouping_plan_lists_only_this_workspaces_published_dashboards(setup):
    with session_scope() as s:
        plan = _svc().grouping_plan(s, s.merge(setup["viewer"]), WS, publisher=FakeBI(existing()))
    assert plan["available"] and plan["group"] == "steps" and plan["to_change"] == 1
    assert [(d["id"], d["to"], d["grouped"]) for d in plan["dashboards"]] == [
        (16, "steps / Executive overview: Why are orders late?", False), (17, "steps / Operations deep-dive", True)]


def test_grouping_plan_without_a_bi_tool_says_so(setup, monkeypatch):
    monkeypatch.setattr(get_settings(), "superset_url", "")
    with session_scope() as s:
        plan = _svc().grouping_plan(s, s.merge(setup["viewer"]), WS)
    assert plan["available"] is False and plan["dashboards"] == [] and "Superset" in plan["reason"]


def test_grouping_renames_only_after_approval(setup):
    bi = FakeBI(existing())
    with session_scope() as s:
        approval = _svc().request_grouping(s, s.merge(setup["owner"]), WS, [16, 17], publisher=bi)
        approval_id = approval.id
        assert approval.payload["changes"] == [{"id": 16, "slug": "aos-ws-steps-executive",
                                                "from": "Executive overview: Why are orders late?",
                                                "to": "steps / Executive overview: Why are orders late?"}]
    assert bi.renamed == []
    with pytest.raises(ApprovalRequired), session_scope() as s:
        _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)
    assert bi.renamed == []
    _approve(setup, approval_id)
    with session_scope() as s:
        out = _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)
        assert s.get(Approval, approval_id).status == "executed"
    assert out["status"] == "applied" and bi.renamed == [(16, "steps / Executive overview: Why are orders late?")]
    with pytest.raises(Conflict), session_scope() as s:
        _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)


def test_a_title_changed_in_superset_after_approval_is_not_overwritten(setup):
    bi = FakeBI(existing())
    with session_scope() as s:
        approval_id = _svc().request_grouping(s, s.merge(setup["owner"]), WS, [16], publisher=bi).id
    _approve(setup, approval_id)
    bi.dashboards[16]["dashboard_title"] = "Renamed by a person in Superset"
    with pytest.raises(ApprovalRequired), session_scope() as s:
        _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)
    assert bi.renamed == []


def test_grouping_refuses_foreign_and_already_grouped_dashboards(setup):
    bi = FakeBI(existing())
    for ids in ([30], [31], [40], [17]):
        with pytest.raises(InvalidInput), session_scope() as s:
            _svc().request_grouping(s, s.merge(setup["owner"]), WS, ids, publisher=bi)
    from analystos.core.errors import AnalystOSError

    with pytest.raises(AnalystOSError), session_scope() as s:  # an approver cannot ask for the change
        _svc().request_grouping(s, s.merge(setup["approver"]), WS, [16], publisher=bi)


def test_grouping_failure_before_any_rename_keeps_the_approval(setup):
    bi = FakeBI(existing())
    with session_scope() as s:
        approval_id = _svc().request_grouping(s, s.merge(setup["owner"]), WS, [16], publisher=bi).id
    _approve(setup, approval_id)
    bi.fail_rename_after = 0
    with pytest.raises(UpstreamUnavailable), session_scope() as s:
        _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)
    bi.fail_rename_after = None
    with session_scope() as s:
        assert _svc().apply_grouping(s, s.merge(setup["owner"]), WS, approval_id, publisher=bi)["status"] == "applied"


def test_grouped_title_is_idempotent_and_bounded():
    from analystos.dashboards.collections import grouped_title

    assert grouped_title("Retail", "Ops") == "Retail / Ops"
    assert grouped_title("Retail", "Retail / Ops") == "Retail / Ops"
    assert len(grouped_title("Retail", "x" * 600)) == 500


def test_superset_rename_puts_the_title_only():
    import respx
    from tests.unit.test_publishing_superset import BASE, FakeSuperset

    from analystos.publishing.superset import SupersetPublisher

    fake = FakeSuperset()
    fake.dashboards[5] = {"id": 5, "slug": "aos-ws-a-executive", "dashboard_title": "Old", "css": "/* keep */",
                          "position_json": "{}", "published": True}
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.route().mock(side_effect=fake.handle)
        SupersetPublisher(base_url=BASE, username="admin", password="admin").rename_dashboard(5, "A / Old")
    assert fake.dashboards[5]["dashboard_title"] == "A / Old" and fake.dashboards[5]["css"] == "/* keep */"
    assert ("PUT", "/api/v1/dashboard/5") in fake.calls
