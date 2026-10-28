"""Create or refresh the role every embedded guest holds (GUEST_ROLE_NAME), idempotently, at container start.

Read-only on purpose: it has no database or dataset access, so a guest reads data only through the dashboard
its token names, and it has no write permission on charts or dashboards (unlike Gamma). Permissions are
reset to exactly this list on each boot, so a hand-added permission does not survive a restart.
"""
from superset.app import create_app

# Deliberately absent: can_explore_json (the legacy endpoint skips Superset's check that a guest's query matches
# the saved chart, so a guest could pull arbitrary columns), can_dashboard (the full dashboard page), the legacy
# can_query / can_query_form_data, every datasource/database permission, SQL Lab, CSV and any write.
PERMISSIONS = [
    ("can_read", "Dashboard"),
    ("can_read", "Chart"),  # includes /api/v1/chart/data, which checks the payload against the saved chart
    ("can_read", "EmbeddedDashboard"),
    ("can_log", "Superset"),
    ("can_read", "DashboardFilterStateRestApi"),
    ("can_write", "DashboardFilterStateRestApi"),  # native filter state in the viewer's own session
    ("can_time_range", "Api"),
]

app = create_app()
with app.app_context():
    from superset import db
    from superset import security_manager as sm

    name = app.config["GUEST_ROLE_NAME"]
    role = sm.find_role(name) or sm.add_role(name)
    wanted, missing = [], []
    for permission, view in PERMISSIONS:
        pvm = sm.find_permission_view_menu(permission, view)
        (wanted if pvm else missing).append(pvm or f"{permission} on {view}")
    role.permissions = wanted
    db.session.commit()
    print(f"guest role {name}: {len(wanted)} permissions" + (f"; not in this Superset: {missing}" if missing else ""))
