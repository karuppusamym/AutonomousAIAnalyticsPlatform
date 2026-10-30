"""Admin control plane: versioned platform settings applied at runtime (Postgres control plane)."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from analystos.core.errors import Forbidden, InvalidInput
from analystos.core.ids import new_id
from analystos.db.base import session_scope
from analystos.db.models import AuditEvent, PlatformSetting, User
from analystos.security.auth import hash_password
from analystos.services import platform_settings as ps

pytestmark = pytest.mark.integration


@pytest.fixture()
def users(control_db):
    with session_scope() as s:
        s.query(PlatformSetting).delete()
        admin = User(id=new_id("usr"), email=f"adm-{new_id('x')}@t", name="A", password_hash=hash_password("x"), is_admin=True)
        plain = User(id=new_id("usr"), email=f"usr-{new_id('x')}@t", name="U", password_hash=hash_password("x"))
        s.add_all([admin, plain])
        ids = {"admin": admin.id, "plain": plain.id}
    ps.invalidate()
    yield ids
    with session_scope() as s:
        s.query(PlatformSetting).delete()
    ps.invalidate()


def test_only_admins_write_and_every_write_is_versioned_and_audited(users):
    with session_scope() as s, pytest.raises(Forbidden):
        ps.update(s, s.get(User, users["plain"]), {"analysis": {"max_charts": 4}})
    with session_scope() as s:
        r = ps.update(s, s.get(User, users["admin"]), {"analysis": {"max_charts": 4}, "crawl": {"llm_enrichment": True}},
                      note="tighten")
    assert r["version"] == 1 and {c["path"] for c in r["changes"]} == {"analysis.max_charts", "crawl.llm_enrichment"}
    assert ps.get().analysis.max_charts == 4 and ps.get().crawl.llm_enrichment is True
    with session_scope() as s:
        assert ps.update(s, s.get(User, users["admin"]), {"analysis": {"max_charts": 4}})["changes"] == []  # no-op, no version
        assert [h["version"] for h in ps.history(s)] == [1]
        assert s.scalar(select(AuditEvent).where(AuditEvent.action == "platform.settings.updated")) is not None


def test_references_are_validated_against_models_yaml(users):
    for patch in ({"llm": {"routing_overrides": {"planning": "no_such_profile"}}},
                  {"llm": {"profile_models": {"low_cost": ["evil/unlisted-model"]}}},
                  {"llm": {"purpose_modes": {"not_a_purpose": "off"}}},
                  {"llm": {"purpose_modes": {"planning": "sometimes"}}}):
        with session_scope() as s, pytest.raises(InvalidInput):
            ps.update(s, s.get(User, users["admin"]), patch)


def test_preset_changes_router_behaviour_and_rollback_restores_it(users):
    from analystos.llm.config import load_models_config
    from analystos.llm.router import ModelRouter

    router = ModelRouter(config=load_models_config(), settings_provider=ps.get, api_key_lookup=lambda _n: "k")
    # Deterministic first by default (P4-T02): planning answers from rules, the model stays available.
    assert router.mode("planning") == "auto" and router.available("planning")
    with session_scope() as s:
        ps.apply_preset(s, s.get(User, users["admin"]), "offline")
    assert router.mode("planning") == "off" and not router.available("planning")
    with session_scope() as s, pytest.raises(Forbidden):
        ps.rollback(s, s.get(User, users["plain"]), 1)
    with session_scope() as s:
        ps.update(s, s.get(User, users["admin"]), {"features": {"jev_decisions": False}})
        r = ps.rollback(s, s.get(User, users["admin"]), 1)
    assert r == {"version": 3, "rolled_back_to": 1}
    assert router.mode("planning") == "off" and ps.get().features.jev_decisions is True


def test_writer_never_merges_onto_a_stale_cached_copy(users):
    with session_scope() as s:
        ps.update(s, s.get(User, users["admin"]), {"analysis": {"max_charts": 5}})
    stale = ps.get()  # cached in this process
    with session_scope() as s:  # another process writes v2; our cache still says v1
        s.add(PlatformSetting(version=2, document={**stale.model_dump(), "analysis": {**stale.analysis.model_dump(), "sample_rows": 1234}},
                              note="elsewhere", created_by=users["admin"]))
    with session_scope() as s:
        ps.update(s, s.get(User, users["admin"]), {"analysis": {"max_charts": 6}})
    ps.invalidate()
    assert ps.get().analysis.sample_rows == 1234 and ps.get().analysis.max_charts == 6


def test_mcp_host_allowlist_is_admin_controlled_and_read_by_the_mcp_client(users, monkeypatch):
    """P4-X05: the platform MCP host allowlist lives here; malformed entries never get stored."""
    import ipaddress

    from analystos.core.config import get_settings
    from analystos.mcp import client as mc
    from analystos.tools.http import OutboundRefused

    monkeypatch.setattr(get_settings(), "mcp_host_allowlist", "")
    public = lambda host, port: [ipaddress.ip_address("93.184.216.34")]  # noqa: E731
    with pytest.raises(OutboundRefused, match="platform MCP host allowlist"):
        mc._validate_url("https://mcp.example.com/mcp", public)
    with session_scope() as s, pytest.raises(Forbidden):
        ps.update(s, s.get(User, users["plain"]), {"outbound": {"mcp_host_allowlist": ["*"]}})
    for bad in (["*.com"], ["0x7f000001"], ["host:99999"]):
        with session_scope() as s, pytest.raises(InvalidInput):
            ps.update(s, s.get(User, users["admin"]), {"outbound": {"mcp_host_allowlist": bad}})
    with session_scope() as s:
        r = ps.update(s, s.get(User, users["admin"]), {"outbound": {"mcp_host_allowlist": ["MCP.example.com"]}})
    assert [c["path"] for c in r["changes"]] == ["outbound.mcp_host_allowlist"]
    assert mc._platform_allowlist() == ["mcp.example.com"]
    assert mc._validate_url("https://mcp.example.com/mcp", public)
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._validate_url("https://other.example.com/mcp", public)



def test_seed_stores_the_dev_default_once_and_never_overwrites_a_saved_choice(users):
    """ADR-016: `analystos seed` on a fresh dev database stores pilot_low_cost; a later seed keeps the admin's edits."""
    from analystos.contracts.platform import PILOT_LOW_COST_DISABLED

    with session_scope() as s:
        r = ps.seed_dev_default(s, s.get(User, users["admin"]))
    assert r is not None and r["version"] == 1
    effective = ps.get()
    assert effective.llm.disabled_models == PILOT_LOW_COST_DISABLED and effective.llm.daily_spend_cap_usd == 2.0
    assert set(effective.llm.purpose_modes.values()) == {"auto"}
    with session_scope() as s:
        assert "ADR-016" in s.get(PlatformSetting, 1).note
        ps.update(s, s.get(User, users["admin"]), {"llm": {"daily_spend_cap_usd": 5.0}}, note="admin choice")
    with session_scope() as s:
        assert ps.seed_dev_default(s, s.get(User, users["admin"])) is None
    assert ps.get().llm.daily_spend_cap_usd == 5.0
