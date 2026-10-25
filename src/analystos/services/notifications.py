"""In-app notifications, and the hand-off to external delivery. Email and webhooks are side effects
outside the platform: `deliver` only queues them for destinations an approver authorized (§39, N-3);
`services/deliveries.py` re-verifies that approval immediately before each send."""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from analystos.db.models import Notification, User, Workspace, WorkspaceMember
from analystos.events.bus import emit


def notify(session: Session, workspace_id: str, *, kind: str, title: str, body: str = "", link: dict | None = None,
           user_id: str | None = None) -> Notification:
    n = Notification(workspace_id=workspace_id, user_id=user_id, kind=kind, title=title[:300], body=body[:4000],
                     link=link or {}, read_by=[])
    session.add(n)
    emit(workspace_id, "notification.created", {"kind": kind, "title": title[:200]}, session=session)
    return n


def deliver(session: Session, workspace_id: str, destination_ids: list[str] | None, *, subject_type: str,
            subject_id: str, origin: dict | None = None) -> list[str]:
    """Queue a verified report snapshot or an alert for approved external destinations; never raises, so a
    delivery problem cannot fail the schedule or monitor that produced the content (it shows as refused)."""
    if not destination_ids:
        return []
    from analystos.core.errors import AnalystOSError
    from analystos.services import deliveries

    try:
        return [d.id for d in deliveries.enqueue(session, workspace_id, destination_ids=destination_ids,
                                                 subject_type=subject_type, subject_id=subject_id, origin=origin)]
    except AnalystOSError as exc:
        notify(session, workspace_id, kind="delivery", title=f"Delivery not queued: {subject_type} {subject_id}",
               body=exc.message, link={"type": subject_type, "id": subject_id})
        return []


def _visible_to(user: User):
    workspaces = select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    stmt = (select(Notification).join(Workspace, Workspace.id == Notification.workspace_id)
            .where(Workspace.deleted_at.is_(None), Workspace.status == "active",
                   or_(Notification.user_id == user.id, Notification.user_id.is_(None))))
    if not user.is_admin:
        stmt = stmt.where(Notification.workspace_id.in_(workspaces))
    return stmt


def list_for(session: Session, user: User, *, unread_only: bool = False, limit: int = 100) -> list[Notification]:
    stmt = _visible_to(user)
    rows = list(session.scalars(stmt.order_by(Notification.id.desc()).limit(limit)))
    return [n for n in rows if not (unread_only and user.id in (n.read_by or []))]


def mark_read(session: Session, user: User, ids: list[int]) -> int:
    count = 0
    for n in session.scalars(_visible_to(user).where(Notification.id.in_(ids))):
        if user.id not in (n.read_by or []):
            n.read_by = [*(n.read_by or []), user.id]
            count += 1
    return count
