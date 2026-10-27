"""P7-10: compare-and-set approval claims on a real Postgres control plane.

Two threads read the same pending (or approved) approval, then both try to claim it. The barrier
makes the interleaving deterministic: both have passed every read-side check before either writes.
Exactly one guarded ``UPDATE ... WHERE status = <expected>`` may change the row; the other must see
rowcount 0 and raise ``Conflict`` -- one decision, one execution, whatever the timing."""
from __future__ import annotations

import threading

import pytest

from analystos.core.errors import Conflict
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import Approval, User
from analystos.governance import approvals
from analystos.security.auth import hash_password
from analystos.services.workspaces import add_member, create_workspace

pytestmark = pytest.mark.integration


def _user(s, email):  # noqa: ANN001, ANN202
    u = User(id=new_id("usr"), email=email, name=email, password_hash=hash_password("x"))
    s.add(u)
    s.flush()
    return u


@pytest.fixture()
def world(control_db):  # noqa: ANN001, ANN201
    with session_scope() as s:
        owner = _user(s, f"owner-{new_id('x')}@t")
        a1, a2 = _user(s, f"appr1-{new_id('x')}@t"), _user(s, f"appr2-{new_id('x')}@t")
        ws = create_workspace(s, owner, name="CAS test", objective="claims", autonomy_level=3)
        s.flush()
        for u in (a1, a2):
            add_member(s, owner, ws.id, u.email, "approver")
        apr = approvals.request_approval(s, workspace_id=ws.id, run_id=None, action="publish_dashboard",
                                         payload={"n": new_id("p")}, plan_hash=None, policy_version=ws.policy_version,
                                         requested_by=owner.id, risk_tier="medium", destination="superset",
                                         affected_assets=[])
        return {"ws": ws.id, "a1": a1.id, "a2": a2.id, "apr": apr.id}


def _race(n: int, action) -> tuple[list[object], list[Exception]]:  # noqa: ANN001
    barrier, results, errors = threading.Barrier(n), [], []

    def run(i: int) -> None:
        try:
            with session_scope() as s:
                results.append(action(s, i, barrier))
        except Exception as exc:  # recorded, asserted by the caller
            errors.append(exc)

    from analystos.tenancy.background import carry_context  # racers act for the test's tenant

    threads = [threading.Thread(target=carry_context(run), args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_two_approvers_deciding_at_once_one_wins(world, monkeypatch) -> None:  # noqa: ANN001
    real = approvals.get_workspace
    local = threading.local()

    def gated(session, workspace_id):  # noqa: ANN001, ANN202
        local.barrier.wait(timeout=10)  # both deciders have read status == pending by now
        return real(session, workspace_id)

    monkeypatch.setattr(approvals, "get_workspace", gated)

    def decide(s, i, barrier):  # noqa: ANN001, ANN202
        local.barrier = barrier
        user = s.get(User, world["a1"] if i == 0 else world["a2"])
        return approvals.decide(s, world["apr"], user, approve=i == 0, reason=f"thread {i}").status

    results, errors = _race(2, decide)
    assert len(results) == 1 and len(errors) == 1, (results, errors)
    assert isinstance(errors[0], Conflict) and "claimed concurrently" in errors[0].message
    with session_scope() as s:
        row = s.get(Approval, world["apr"])
        assert row.status == results[0]
        assert row.decided_by == (world["a1"] if row.status == "approved" else world["a2"])


def test_two_executors_consuming_at_once_one_wins(world) -> None:  # noqa: ANN001
    with session_scope() as s:
        approvals.decide(s, world["apr"], s.get(User, world["a1"]), approve=True)

    def consume(s, i, barrier):  # noqa: ANN001, ANN202
        apr = s.get(Approval, world["apr"])
        assert apr.status == "approved"
        barrier.wait(timeout=10)  # both executors saw an approved, unconsumed approval
        return approvals.consume(s, apr).status

    results, errors = _race(2, consume)
    assert results == ["executed"] and len(errors) == 1 and isinstance(errors[0], Conflict), (results, errors)


def test_a_decided_approval_cannot_be_decided_again(world) -> None:  # noqa: ANN001
    with session_scope() as s:
        approvals.decide(s, world["apr"], s.get(User, world["a1"]), approve=False, reason="no")
    with session_scope() as s, pytest.raises(Conflict):
        approvals.decide(s, world["apr"], s.get(User, world["a2"]), approve=True)
