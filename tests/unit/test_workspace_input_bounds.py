"""Security self-check 2026-10-30 (ZAP API scan): a workspace name longer than its column was a 500 from the database;
it is now refused at the API boundary with 422."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from analystos.db.base import session_scope
from analystos.db.models import User
from analystos.tenancy import DEFAULT_TENANT_ID


@pytest.fixture
def client(sqlite_db, monkeypatch):
    from analystos.api import deps
    from analystos.api.app import app
    from analystos.db import base, models
    from analystos.tenancy import resolve

    engine = base.SessionLocal().get_bind()
    for table in ("tenant_member", "tenant"):
        models.Base.metadata.tables[table].create(engine, checkfirst=True)
    monkeypatch.setattr(deps, "SessionLocal", base.SessionLocal)
    resolve.invalidate()
    with session_scope() as s:
        s.add(User(id="usr_ws", email="usr_ws@x", name="ws", password_hash="x", is_admin=False, active=True,
                   attributes={}, tenant_id=DEFAULT_TENANT_ID))
    return TestClient(app)


def _hdr() -> dict:
    from analystos.security.auth import issue_token

    return {"Authorization": f"Bearer {issue_token('usr_ws', 'usr_ws@x')}"}


@pytest.mark.parametrize("name", ["x" * 201, ""])
def test_a_workspace_name_outside_its_bounds_is_refused_with_422(client, name):
    r = client.post("/api/workspaces", json={"name": name}, headers=_hdr())
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "invalid_input"
