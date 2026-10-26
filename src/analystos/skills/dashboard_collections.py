"""Workspace-labelled topic views over available descriptive charts; never invent measures."""
from __future__ import annotations

from analystos.contracts.bi import ChartSpec, DashboardSpec
from analystos.skills.dashboards import build_layout, choose_native_filters

TOPICS = (
    ("activity", "Activity and volume", ("record_count", "trend_volume", "heat_dow_hour")),
    ("delivery", "Delivery reliability", ("late_delivery", "late delivery")),
    ("discounts", "Discount patterns", ("discount",)),
    ("sla", "SLA performance", ("sla", "breach")),
    ("rework", "Rework and reassignment", ("reopen", "reassignment")),
    ("resolution", "Resolution time", ("resolution", "resolved_at")),
    ("waiting", "Process waiting time", ("wait_hours", "wait time")),
    ("handover", "Process handovers", ("handover",)),
)


def topic_dashboards(workspace_name: str, charts: list[ChartSpec], columns: list[dict], *, run_id: str) -> list[DashboardSpec]:
    available = [c for c in charts if not c.insight_codes and c.value_basis == "observed" and "error" not in c.preview]
    dashboards = []
    for key, title, terms in TOPICS:
        matched = [c for c in available if any(t in f"{c.key} {c.title} {c.metric or ''}".lower() for t in terms)]
        if not matched:
            continue
        if key != "activity":
            matched += [c for c in available if c.key == "detail_table" and c not in matched]
        if len(matched) < 2:
            continue
        layout = [cell for cell in build_layout("operational", matched) if cell.get("kind") == "chart"]
        dashboards.append(DashboardSpec(
            key=f"topic_{key}", title=f"{workspace_name} / {title}", audience="operational",
            description=f"Topic view from investigation {run_id}; uses existing descriptive measures, not new causal findings.",
            charts=[c.key for c in matched], layout=layout,
            native_filters=list(choose_native_filters(matched, columns)),
            summary_markdown=f"### {workspace_name}: {title}\n\nDescriptive reporting from `{run_id}`. "
                             "Review data freshness and metric definitions before use. Publication requires approval."))
    return dashboards
