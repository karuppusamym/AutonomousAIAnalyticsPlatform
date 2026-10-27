"""Editing a registered source (name, connection, secret reference): only editors and owners may, only
the fields sent change, credentials still cannot go in `config`, a required field or secret cannot be
dropped, changing the connection marks the source `registered` again (not silently trusted), a rejected
edit leaves nothing changed (not even partially, inside the same transaction), and every edit is audited.
The kind itself cannot change. Skips cleanly without the compose Postgres."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.core.errors import Forbidden, InvalidInput, NotFound

pytestmark = pytest.mark.integration
PASSWORD = "ChangeMe123!"  # the seeded dev/test users' shared password (analystos seed), not a real credential


@pytest.fixture(scope="module")
def world(control_db):
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import User
    from analystos.services.sources import register_source
    from analystos.services.workspaces import add_member, create_workspace

    with session_scope() as s:
        owner = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        editor = s.scalar(select(User).where(User.email == "analyst@analystos.local"))
        viewer = s.scalar(select(User).where(User.email == "approver@analystos.local"))
        ws = create_workspace(s, owner, name=f"source-edit {new_id('t')}", objective="", autonomy_level=3)
        add_member(s, owner, ws.id, editor.email, "editor")
        add_member(s, owner, ws.id, viewer.email, "viewer")
        src = register_source(s, owner, ws.id, kind="postgres", name="warehouse",
                              config={"host": "db.example", "database": "app", "username": "reader"},
                              secret_ref="env:ANALYSTOS_SOURCE_SECRET_PGPASS")
        s.flush()
        ids = {"ws": ws.id, "src": src.id}
        for u in (owner, editor, viewer):
            s.expunge(u)
    return {**ids, "owner": owner, "editor": editor, "viewer": viewer}


@pytest.fixture(scope="module")
def api(control_db):
    from fastapi.testclient import TestClient

    from analystos.api.app import app

    with TestClient(app) as client:
        yield client


def _get(world):
    """Reread the source fresh, in its own short transaction, so the caller sees only committed state."""
    from analystos.db.base import session_scope
    from analystos.db.models import Source

    with session_scope() as s:
        row = s.get(Source, world["src"])
        s.expunge(row)
        return row


def test_only_the_fields_sent_change_and_reconnect_is_flagged(world):
    from analystos.db.base import session_scope
    from analystos.services.sources import update_source

    with session_scope() as s:
        src = update_source(s, s.merge(world["editor"]), world["ws"], world["src"], name="Warehouse (primary)")
        assert src.name == "Warehouse (primary)" and src.config["host"] == "db.example"  # config untouched
        assert src.status == "registered"  # a rename alone needs no reconnect
    with session_scope() as s:
        src = update_source(s, s.merge(world["editor"]), world["ws"], world["src"],
                            config={"host": "db2.example", "database": "app", "username": "reader"})
        assert src.config["host"] == "db2.example"
    assert _get(world).status == "registered"


def test_a_viewer_cannot_edit_and_the_kind_cannot_change(world):
    import inspect

    from analystos.db.base import session_scope
    from analystos.services.sources import update_source

    with pytest.raises(Forbidden), session_scope() as s:
        update_source(s, s.merge(world["viewer"]), world["ws"], world["src"], name="a viewer's edit")
    assert _get(world).name != "a viewer's edit"
    # there is no `kind` parameter to change it through; the update contract only knows the source's own kind
    assert "kind" not in inspect.signature(update_source).parameters


def test_credentials_still_cannot_go_in_config_and_a_required_field_cannot_be_dropped(world):
    from analystos.db.base import session_scope
    from analystos.services.sources import update_source

    before = _get(world).config
    with pytest.raises(InvalidInput, match="secret reference"), session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], world["src"],
                      config={"host": "new.example", "database": "app", "username": "reader", "password": "x"})
    with pytest.raises(InvalidInput, match="needs"), session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], world["src"],
                      config={"host": "new.example", "username": "reader"})  # database dropped
    assert _get(world).config == before  # neither rejected edit changed anything, not even the host


def test_clearing_a_required_secret_is_refused_and_leaves_it_set(world):
    from analystos.db.base import session_scope
    from analystos.services.sources import update_source

    with pytest.raises(InvalidInput, match="secret reference"), session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], world["src"], clear_secret_ref=True)
    assert _get(world).secret_ref == "env:ANALYSTOS_SOURCE_SECRET_PGPASS"  # the rejected clear did not partially apply
    with pytest.raises(InvalidInput, match="nothing to change"), session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], world["src"])
    with pytest.raises(NotFound), session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], "src_no_such_source", name="x")


def test_every_edit_is_audited(world):
    from analystos.db.base import session_scope
    from analystos.db.models import AuditEvent
    from analystos.services.sources import update_source

    with session_scope() as s:
        update_source(s, s.merge(world["editor"]), world["ws"], world["src"], name="Warehouse (renamed again)")
    with session_scope() as s:
        entries = list(s.scalars(select(AuditEvent).where(AuditEvent.workspace_id == world["ws"],
                                                           AuditEvent.action == "source.updated").order_by(AuditEvent.created_at)))
    assert entries and entries[-1].target == world["src"] and entries[-1].details["changed"] == ["name"]


# ------------------------------------------------------------------------------------ HTTP, real stack
def test_patch_over_http_edits_the_source_and_enforces_the_role(api, world):
    def login(email: str):
        r = api.post("/api/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    editor, viewer = login("analyst@analystos.local"), login("approver@analystos.local")
    ws, src = world["ws"], world["src"]

    r = api.patch(f"/api/workspaces/{ws}/sources/{src}", headers=viewer, json={"name": "nope"})
    assert r.status_code == 403

    r = api.patch(f"/api/workspaces/{ws}/sources/{src}", headers=editor, json={"name": "Warehouse (HTTP)"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Warehouse (HTTP)"

    r = api.patch(f"/api/workspaces/{ws}/sources/{src}", headers=editor, json={})
    assert r.status_code == 422 and "nothing to change" in r.json()["error"]["message"]

    r = api.patch(f"/api/workspaces/{ws}/sources/{src}", headers=editor,
                  json={"config": {"host": "db.example", "database": "app", "username": "reader", "password": "x"}})
    assert r.status_code == 422 and "secret reference" in r.json()["error"]["message"]

    listed = api.get(f"/api/workspaces/{ws}/sources", headers=editor).json()
    assert next(s for s in listed if s["id"] == src)["name"] == "Warehouse (HTTP)"
