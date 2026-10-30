"""P4-09: named business and technical owners of a pilot workspace and its sources, pilot readiness that
lists what is missing where it is missing (owners, connector certification, connection health, single
sign-on mapping, recovery drill), and the SSO mapping preview. SQLite control plane; no services."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from analystos.contracts.pilot import NamedOwner, OwnersIn
from analystos.core.errors import Forbidden, InvalidInput, NotFound
from analystos.db.models import AuditEvent, PilotReview, RunEvent, SemanticMetric, Source, User, WorkspaceMember
from analystos.security import oidc
from analystos.services import pilot
from analystos.services.workspaces import create_workspace

DANA = {"name": "Dana Ops", "email": "dana@pilot.test"}
OMAR = {"name": "Omar Data", "email": "omar@pilot.test"}


@pytest.fixture
def world(sqlite_db):
    s = sqlite_db()
    PilotReview.__table__.create(s.bind, checkfirst=True)
    owner = User(id="usr_owner", email="owner@x.test", name="owner", password_hash="!", is_admin=False, attributes={})
    analyst = User(id="usr_analyst", email="analyst@x.test", name="analyst", password_hash="!", is_admin=False, attributes={})
    s.add_all([owner, analyst])
    s.flush()
    ws = create_workspace(s, owner, name="Pilot ITSM", objective="pilot")
    s.add(WorkspaceMember(workspace_id=ws.id, user_id=analyst.id, role="analyst"))
    src = Source(id="src_pilot", workspace_id=ws.id, kind="csv", name="tickets export", config={"path": "x"}, status="error",
                 last_error="File source path does not exist: x")
    snow = Source(id="src_snow", workspace_id=ws.id, kind="servicenow", name="ServiceNow", config={}, status="registered")
    s.add_all([src, snow])
    s.commit()
    yield s, ws, owner, analyst
    s.close()


def _evidence(tmp_path, *, drill: str | None = "PASSED", kinds=("csv",)):
    for kind in kinds:
        (tmp_path / f"connector-{kind}-20260927.md").write_text(
            f"---\nkind: {kind}\ndate: 2026-09-27\nengine: test engine\ntest: tests/x.py\nresult: pass\n---\n# evidence\n", encoding="utf-8")
    if drill:
        (tmp_path / "2026-09-27-recovery-drill.md").write_text(f"# Recovery drill\n\n**Result: {drill}** — backup\n", encoding="utf-8")
    return tmp_path


def _by(r, check, subject_prefix="workspace"):
    return [c for c in r.checks if c.check == check and c.subject.startswith(subject_prefix)]


def test_owners_are_named_people_with_an_address():
    with pytest.raises(ValidationError):
        NamedOwner(name="Dana", email="not-an-address")
    with pytest.raises(ValidationError):
        OwnersIn.model_validate({"business": DANA, "sponsor": OMAR})  # only business and technical
    assert OwnersIn(business=NamedOwner(**DANA)).technical is None


def test_only_a_workspace_owner_names_owners_and_every_change_is_audited(world):
    s, ws, owner, analyst = world
    with pytest.raises(Forbidden):
        pilot.set_workspace_owners(s, analyst, ws.id, OwnersIn(business=NamedOwner(**DANA)))
    with pytest.raises(InvalidInput, match="does not exist"):
        pilot.set_workspace_owners(s, owner, ws.id, OwnersIn(business=NamedOwner(**DANA, user_id="usr_ghost")))
    pilot.set_workspace_owners(s, owner, ws.id, OwnersIn(business=NamedOwner(**DANA, user_id=analyst.id),
                                                         technical=NamedOwner(**OMAR)))
    s.flush()
    assert ws.owners == {"business": {**DANA, "user_id": analyst.id}, "technical": OMAR}
    pilot.set_source_owners(s, owner, ws.id, "src_pilot", OwnersIn(technical=NamedOwner(**OMAR)))
    with pytest.raises(NotFound):
        pilot.set_source_owners(s, owner, "ws_other", "src_pilot", OwnersIn())
    s.flush()
    actions = [a.action for a in s.query(AuditEvent).order_by(AuditEvent.id)]
    assert actions.count("workspace.owners_changed") == 1 and actions.count("source.owners_changed") == 1
    events = {e.type for e in s.query(RunEvent)}
    assert {"workspace.owners_changed", "source.owners_changed"} <= events
    pilot.set_workspace_owners(s, owner, ws.id, OwnersIn(business=NamedOwner(**DANA)))  # a full replacement clears technical
    assert ws.owners == {"business": DANA}


def test_readiness_lists_every_gap_where_it_is(world, tmp_path):
    s, ws, owner, analyst = world
    r = pilot.readiness(s, analyst, ws.id, evidence_dir=_evidence(tmp_path, drill=None))  # any member may read it
    assert r.verdict == "not_ready"
    assert [c.status for c in _by(r, "owner.business")] == ["missing"] and [c.status for c in _by(r, "owner.technical")] == ["missing"]
    csv, snow = "source:src_pilot", "source:src_snow"
    assert {c.status for c in _by(r, "owner.business", csv) + _by(r, "owner.technical", snow)} == {"missing"}
    assert _by(r, "connector.certified", csv)[0].status == "pass"  # a live evidence file for csv
    cert = _by(r, "connector.certified", snow)[0]
    assert cert.status == "missing" and "no live certification" in cert.reason and "certify_connectors" in cert.remediation
    health = _by(r, "connector.health", csv)[0]
    assert health.status == "fail" and "File source path does not exist" in health.reason  # the cause, not just "error"
    assert _by(r, "connector.health", snow)[0].status == "missing"  # never discovered
    assert [c.status for c in r.checks if c.check == "identity.sso"] == ["missing"]
    assert [c.status for c in r.checks if c.check == "recovery.drill"] == ["missing"]
    assert r.failing == 1 and r.missing == len([c for c in r.checks if c.status == "missing"])
    staged = s.get(Source, "src_pilot")
    staged.status = "ready"  # staged earlier; the last crawl failed (the crawler keeps a usable source usable)
    again = _by(pilot.readiness(s, analyst, ws.id, evidence_dir=tmp_path), "connector.health", csv)[0]
    assert again.status == "fail" and "stays readable" in again.reason


def _rehearse(s, ws_id, user_id):
    s.add(SemanticMetric(id="met", workspace_id=ws_id, name="breach_rate", version=1, status="approved", definition={},
                         expression="avg(x)", normalized_expression="avg(x)", proposed_by=user_id, proposed_via="user",
                         content_hash="h"))
    for i, kind in enumerate(("investigation", "ask", "dashboard", "report", "digest")):
        s.add(PilotReview(id=f"prv{i}", workspace_id=ws_id, subject_type=kind, subject_id=f"subject{i}", reviewer_id=user_id,
                          evidence_kind="rehearsal", task="card", baseline_minutes=10, assisted_minutes=5, accuracy="correct",
                          usefulness=4, would_use_again=True))
    s.add(PilotReview(id="prv_real", workspace_id=ws_id, subject_type="ask", subject_id="x", reviewer_id=user_id,
                      evidence_kind="real_pilot", task="card", baseline_minutes=10, assisted_minutes=5, accuracy="correct",
                      usefulness=4, would_use_again=True))
    s.flush()


def test_readiness_is_ready_only_when_nothing_is_missing(world, tmp_path, monkeypatch):
    from analystos.core.config import get_settings

    s, ws, owner, _ = world
    both = OwnersIn(business=NamedOwner(**DANA), technical=NamedOwner(**OMAR))
    pilot.set_workspace_owners(s, owner, ws.id, both)
    for sid in ("src_pilot", "src_snow"):
        pilot.set_source_owners(s, owner, ws.id, sid, both)
    s.get(Source, "src_snow").kind = "postgres"
    for src in s.query(Source):
        src.status, src.last_error = "discovered", None
        src.last_discovered_at = __import__("analystos.core.ids", fromlist=["utcnow"]).utcnow()
    mapping = tmp_path / "oidc.yaml"
    mapping.write_text(f"workspace_roles:\n  - {{group: pilot-analysts, workspace: '{ws.name}', role: analyst}}\n", encoding="utf-8")
    for k, v in {"ANALYSTOS_OIDC_ISSUER": "https://idp.test", "ANALYSTOS_OIDC_CLIENT_ID": "aos",
                 "ANALYSTOS_OIDC_MAPPING_FILE": str(mapping)}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        evidence = _evidence(tmp_path, kinds=("csv", "postgres"))
        gaps = {c.check: c for c in pilot.readiness(s, owner, ws.id, evidence_dir=evidence).checks if c.status != "pass"}
        assert set(gaps) == {"conversation.definitions", "pilot.rehearsal"}
        assert "Ask, dashboard by conversation, report by conversation, scheduled digest" in gaps["pilot.rehearsal"].reason
        _rehearse(s, ws.id, owner.id)
        r = pilot.readiness(s, owner, ws.id, evidence_dir=evidence)
        assert r.verdict == "ready", [c for c in r.checks if c.status != "pass"]
        assert next(c for c in r.checks if c.check == "conversation.definitions").reason == "1 approved metric, 0 verified queries"
        assert "pilot-analysts -> analyst" in next(c for c in r.checks if c.check == "identity.sso").reason
        (evidence / "2026-09-28-recovery-drill.md").write_text("# Recovery drill\n\n**Result: FAILED**\n", encoding="utf-8")
        again = pilot.readiness(s, owner, ws.id, evidence_dir=evidence)
        drill = next(c for c in again.checks if c.check == "recovery.drill")
        assert again.verdict == "not_ready" and drill.status == "fail"  # the newest drill decides
        overview = pilot.overview(s, User(id="adm", email="a@x", name="a", password_hash="!", is_admin=True))
        assert [o["workspace_id"] for o in overview] == [ws.id]
    finally:
        get_settings.cache_clear()


def test_sso_preview_and_dead_grants(world, tmp_path):
    s, ws, _, _ = world
    mapping = oidc.Mapping(platform_admin_groups=["aos-admins"], workspace_roles=[
        oidc.RoleGrant(group="pilot-analysts", workspace=ws.name, role="analyst"),
        oidc.RoleGrant(group="pilot-leads", workspace=ws.id, role="editor"),
        oidc.RoleGrant(group="pilot-analysts", workspace="Retired workspace", role="viewer")])
    p = oidc.preview(s, ["pilot-analysts", "pilot-leads"], mapping)
    assert p["signs_in"] and not p["is_admin"]
    # one workspace mapped by id (editor) and by name (analyst): one entry, the highest role
    assert p["roles"] == [{"workspace_id": ws.id, "workspace_name": ws.name, "role": "editor",
                           "mapped_as": sorted([ws.id, ws.name])}]
    assert p["unresolved"] == ["Retired workspace"]
    assert oidc.mapping_problems(s, mapping) == [
        "group pilot-analysts: workspace 'Retired workspace' does not exist; the grant (viewer) has no effect"]
    strict = oidc.Mapping(workspace_roles=mapping.workspace_roles, require_group=True)
    refused = oidc.preview(s, ["contractors"], strict)
    assert refused["signs_in"] is False and "mapped" in refused["reason"]
