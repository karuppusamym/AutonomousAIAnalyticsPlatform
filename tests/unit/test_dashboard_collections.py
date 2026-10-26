from analystos.contracts.bi import ChartSpec
from analystos.skills.dashboard_collections import topic_dashboards


def chart(key, title="", **kw):
    return ChartSpec(key=key,title=title or key,chart_type="bar",intent="comparison",dataset="orders",**kw)


def test_workspace_topic_titles_and_only_available_charts():
    charts=[chart("kpi_late_delivery_rate"),chart("trend_late_delivery_rate"),chart("detail_table")]
    boards=topic_dashboards("Retail",charts,[],run_id="run_1")
    assert [b.key for b in boards]==["topic_delivery"]
    board=boards[0]
    assert board.title=="Retail / Delivery reliability"
    assert set(board.charts)=={c.key for c in charts}
    assert {c['chart'] for c in board.layout}==set(board.charts)
    assert "run_1" in board.description


def test_does_not_invent_sla_dashboard_or_reuse_findings_or_failed_previews():
    charts=[chart("kpi_record_count"),chart("sla_finding",insight_codes=["I-1"]),
            chart("sla_broken",preview={"error":"unavailable"})]
    assert topic_dashboards("Retail",charts,[],run_id="r")==[]


def test_workspace_names_differentiate_the_same_topic_without_changing_its_key():
    charts=[chart("kpi_record_count"),chart("trend_volume")]
    a=topic_dashboards("Operations",charts,[],run_id="a")[0]
    b=topic_dashboards("Incidents",charts,[],run_id="b")[0]
    assert a.key==b.key and a.title!=b.title
