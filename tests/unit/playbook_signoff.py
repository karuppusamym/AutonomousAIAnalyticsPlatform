"""Publishing a workspace playbook the way S06-T6 requires: simulate, request a signature, a second person
approves, publish (which verifies the approval and signs). Tests that need a published playbook use this
instead of calling `definitions.publish` on an unsigned draft."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.autonomy import playbook_signing
from analystos.db.models import Definition, User, Workspace, WorkspaceMember
from analystos.governance import approvals
from analystos.services import definitions as defs
from analystos.tenancy import DEFAULT_TENANT_ID

SIGNER = "usr_pb_signer"


def signer(s: Session, workspace_id: str) -> User:
    """An approver who is not the author (playbook signing always separates duties)."""
    u = s.get(User, SIGNER)
    if u is None:
        u = User(id=SIGNER, email="signer@x", name="Signer", password_hash="x", is_admin=False, active=True, attributes={})
        s.add(u)
        s.flush()
    if s.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace_id,
                                              WorkspaceMember.user_id == SIGNER)) is None:
        s.add(WorkspaceMember(workspace_id=workspace_id, user_id=SIGNER, role="approver"))
        s.flush()
    return u


def sign_off(s: Session, author: User, row: Definition, *, fixtures: list | None = None) -> Definition:
    """Simulate the draft and have its signature approved; the next `definitions.publish` signs it."""
    ws = s.get(Workspace, row.workspace_id)
    if ws.tenant_id is None:
        ws.tenant_id = DEFAULT_TENANT_ID  # signing keys are per tenant
    if row.status != "tested":
        defs.test(s, author, row, row.revision, arguments={"fixtures": fixtures} if fixtures else None)
    approval = playbook_signing.request_signature(s, author, row, row.revision)
    approvals.decide(s, approval.id, signer(s, row.workspace_id), approve=True)
    playbook_signing.apply_decision(s, approval)
    return row


def publish_signed(s: Session, author: User, row: Definition, **kw) -> Definition:
    sign_off(s, author, row, **kw)
    return defs.publish(s, author, row, row.revision)
