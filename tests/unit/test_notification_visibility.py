from analystos.core.ids import utcnow
from analystos.db.models import Notification, User, Workspace, WorkspaceMember
from analystos.services.notifications import list_for, mark_read


def test_archived_notifications_do_not_crowd_out_active_demo_notifications(sqlite_db):
    with sqlite_db() as session:
        admin = User(id="admin", email="a@x", name="Admin", password_hash="x", is_admin=True)
        member = User(id="member", email="m@x", name="Member", password_hash="x", is_admin=False)
        session.add_all([admin, member])
        for key in ("active", "archived", "disabled", "other"):
            session.add(Workspace(id=key, name=key, created_by="admin", status="disabled" if key == "disabled" else "active",
                                  deleted_at=utcnow() if key == "archived" else None))
            if key != "other":
                session.add(WorkspaceMember(workspace_id=key, user_id="member", role="viewer"))
        session.add_all([
            Notification(id=1, workspace_id="active", kind="test", title="Keep", read_by=[]),
            Notification(id=2, workspace_id="archived", kind="test", title="Archived clutter", read_by=[]),
            Notification(id=3, workspace_id="disabled", kind="test", title="Disabled clutter", read_by=[]),
            Notification(id=4, workspace_id="other", kind="test", title="Other workspace", read_by=[]),
            Notification(id=5, workspace_id="active", user_id="admin", kind="test", title="Private", read_by=[]),
        ])
        session.commit()
        assert [n.id for n in list_for(session, member)] == [1]
        assert [n.id for n in list_for(session, admin)] == [5, 4, 1]
        # A newest archived notification must be filtered before the limit is applied.
        assert [n.id for n in list_for(session, member, limit=1)] == [1]
        assert mark_read(session, member, [1, 2, 3, 4, 5]) == 1
        assert session.get(Notification, 2).read_by == []
        assert session.get(Notification, 4).read_by == []
        assert session.get(Notification, 5).read_by == []
        assert list_for(session, member, unread_only=True) == []
