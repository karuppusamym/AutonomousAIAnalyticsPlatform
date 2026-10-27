"""S01-T5 on Postgres: audit events are sealed into one hash chain per tenant; verification finds an altered
or removed link; the application role cannot write the chain; exports verify offline; API access is
limited to the tenant's admins and auditors."""
from __future__ import annotations

import gzip
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.integration


def _tenant(slug: str) -> str:
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant
    from analystos.tenancy import system_scope

    tid = new_id("tnt")
    with system_scope(), session_scope() as s:
        s.add(Tenant(id=tid, slug=f"{slug}-{tid[-6:]}", name=slug))
    return tid


def _events(tid: str, n: int, action: str = "test.event") -> None:
    from analystos.governance.audit import audit
    from analystos.tenancy import tenant_scope

    with tenant_scope(tid):
        for i in range(n):
            audit("test", action, target=str(i), decision="allow", details={"i": i, "nested": {"b": 2, "a": [1.5, None]}})


def _maintenance(sql: str, **params) -> None:  # noqa: ANN003
    from analystos.db.base import session_scope
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        s.execute(text("SELECT set_config('app.audit_maintenance', 'on', true)"))
        s.execute(text(sql), params)


def _link(tid: str, seq: int) -> dict:
    from analystos.db.base import session_scope
    from analystos.db.models import AuditChain
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        row = s.scalar(select(AuditChain).where(AuditChain.chain == tid, AuditChain.seq == seq))
        return {"event_id": row.event_id, "hash": row.hash}


def test_each_tenant_has_its_own_verified_chain(control_db):
    from analystos.db.base import session_scope
    from analystos.governance import audit_chain as ac
    from analystos.tenancy import system_scope

    a, b = _tenant("chain-a"), _tenant("chain-b")
    _events(a, 5)
    _events(b, 3)
    assert ac.seal(a)["sealed"] == 5 and ac.seal(b)["sealed"] == 3
    va, vb = ac.verify(a), ac.verify(b)
    assert va["ok"] and va["checked"] == 5 and vb["ok"] and vb["checked"] == 3
    assert va["head"]["hash"] != vb["head"]["hash"]
    with system_scope(), session_scope() as s:  # the chain row's tenant comes from its event (fill trigger)
        tenants = set(s.scalars(text("SELECT DISTINCT tenant_id FROM audit_chain WHERE chain = :c"), {"c": a}))
    assert tenants == {a}
    assert ac.seal(a)["sealed"] == 0  # idempotent


def test_an_altered_event_is_found(control_db):
    from analystos.governance import audit_chain as ac

    t = _tenant("tamper")
    _events(t, 6)
    ac.seal(t)
    victim = _link(t, 4)["event_id"]
    _maintenance("UPDATE audit_event SET decision = 'deny' WHERE id = :i", i=victim)
    result = ac.verify(t)
    assert not result["ok"] and result["first_bad"] == {"seq": 4, "event_id": victim, "reason": "altered"}
    assert result["checked"] == 3


def test_a_removed_link_is_found(control_db):
    from analystos.governance import audit_chain as ac

    t = _tenant("gap")
    _events(t, 5)
    ac.seal(t)
    _maintenance("DELETE FROM audit_chain WHERE chain = :c AND seq = 3", c=t)
    assert ac.verify(t)["first_bad"]["reason"] == "gap"


def test_a_rewritten_link_is_found(control_db):
    from analystos.governance import audit_chain as ac

    t = _tenant("relink")
    _events(t, 4)
    ac.seal(t)
    _maintenance("UPDATE audit_chain SET hash = :h WHERE chain = :c AND seq = 2", h="f" * 64, c=t)
    result = ac.verify(t)
    assert result["first_bad"]["seq"] == 2 and result["first_bad"]["reason"] == "altered"


def test_the_application_role_cannot_write_or_change_the_chain(control_db):
    from sqlalchemy.exc import DBAPIError

    from analystos.db.base import session_scope
    from analystos.governance import audit_chain as ac
    from analystos.tenancy import tenant_scope

    t = _tenant("readonly")
    _events(t, 2)
    ac.seal(t)
    link = _link(t, 1)
    with pytest.raises(DBAPIError, match="permission denied"), tenant_scope(t), session_scope() as s:
        s.execute(text("INSERT INTO audit_chain (event_id, chain, seq, prev_hash, hash) "
                       "SELECT id, :k, 99, :z, :z FROM audit_event WHERE tenant_id = :c LIMIT 1"), {"k": t, "c": t, "z": "0" * 64})
    for stmt in ("UPDATE audit_chain SET hash = 'x' WHERE event_id = :i", "DELETE FROM audit_chain WHERE event_id = :i"):
        with pytest.raises(DBAPIError), tenant_scope(t), session_scope() as s:
            s.execute(text(stmt), {"i": link["event_id"]})
    assert ac.verify(t)["ok"]


def test_a_late_row_is_sealed_by_the_full_pass(control_db):
    from analystos.core.ids import utcnow
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent
    from analystos.governance import audit_chain as ac
    from analystos.tenancy import tenant_scope

    t = _tenant("late")
    _events(t, 2)
    ac.seal(t)
    with tenant_scope(t), session_scope() as s:  # committed late: older than the window before the head
        s.add(AuditEvent(actor="test", action="test.late", created_at=utcnow() - ac.SEAL_WINDOW - timedelta(hours=2)))
    assert ac.seal(t)["sealed"] == 0
    assert ac.verify(t)["unsealed_stale"] == 1
    assert ac.seal(t, full=True)["sealed"] == 1
    result = ac.verify(t)
    assert result["ok"] and result["unsealed_stale"] == 0 and result["checked"] == 3


def test_export_files_verify_offline_and_are_incremental(control_db, tmp_path):
    from analystos.core.ids import canonical_json
    from analystos.governance import audit_chain as ac

    t = _tenant("export")
    _events(t, 4)
    first = ac.export(t, tmp_path)
    assert first["exported"] == 4 and first["from_seq"] == 1
    lines = [json.loads(x) for x in gzip.decompress(Path(first["file"]).read_bytes()).decode().splitlines()]
    prev = ac.GENESIS
    for line in lines:  # what an external auditor runs: no AnalystOS code needed
        assert line["prev_hash"] == prev
        assert hashlib.sha256(f"{prev}\n{canonical_json(line['event'])}".encode()).hexdigest() == line["hash"]
        prev = line["hash"]
    assert hashlib.sha256(Path(first["file"]).read_bytes()).hexdigest() == first["sha256"]
    # the export audited itself: that event is the next link, and the next export starts after the last one
    second = ac.export(t, tmp_path)
    assert second["exported"] == 1 and second["from_seq"] == 5
    assert ac.export(t, tmp_path)["exported"] == 1  # the second export's own event
    assert ac.verify(t)["ok"]


def test_nightly_runs_once_a_day(control_db, tmp_path):
    from analystos.governance import audit_chain as ac
    from analystos.tenancy import system_scope

    with system_scope():
        first = ac.maybe_run_nightly(out_dir=tmp_path)
        assert first is not None and set(first["broken"]) <= _broken_by_tests()
        assert ac.maybe_run_nightly(out_dir=tmp_path) is None


def _broken_by_tests() -> set[str]:
    """Chains other tests in this module break on purpose."""
    from analystos.db.base import session_scope
    from analystos.db.models import Tenant
    from analystos.tenancy import system_scope

    with system_scope(), session_scope() as s:
        return set(s.scalars(select(Tenant.id).where(Tenant.name.in_(["tamper", "gap", "relink"]))))


def test_api_verify_and_read_are_for_tenant_admins_and_auditors(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app
    from analystos.tenancy import DEFAULT_TENANT_ID

    with TestClient(app) as api:
        def login(email):
            r = api.post("/api/auth/login", json={"email": email, "password": "ChangeMe123!"})
            assert r.status_code == 200, r.text
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        admin, analyst = login("admin@analystos.local"), login("analyst@analystos.local")
        r = api.post(f"/api/tenants/{DEFAULT_TENANT_ID}/audit/verify", headers=admin)
        assert r.status_code == 200 and r.json()["ok"] and r.json()["chain"] == DEFAULT_TENANT_ID, r.text
        r = api.get(f"/api/tenants/{DEFAULT_TENANT_ID}/audit", params={"limit": 3}, headers=admin)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        lines = [json.loads(x) for x in r.text.splitlines()]
        assert len(lines) == 3 and int(r.headers["X-Next-After-Seq"]) == lines[-1]["seq"]
        assert api.post(f"/api/tenants/{DEFAULT_TENANT_ID}/audit/verify", headers=analyst).status_code == 403
        assert api.get("/api/tenants/tnt_someone_else/audit", headers=admin).status_code == 404
