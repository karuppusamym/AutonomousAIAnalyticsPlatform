"""Secret references a workspace user registers may name only what the operator allows (S02 security fix).

Before: `env:<ANY_VAR>` resolved any process variable (OPENROUTER_API_KEY, ANALYSTOS_JWT_SECRET) and handed it
to a source host the editor controls as the connection password.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analystos.connectors import secrets
from analystos.connectors.secrets import check_secret_ref, parse_ref, resolve_platform_secret, resolve_secret
from analystos.core.errors import Forbidden, InvalidInput


def _settings(**over):
    base = dict(secret_env_prefixes="ANALYSTOS_SOURCE_SECRET_", secret_env_names="", secret_file_dirs="",
                process_role="all", credential_roles="all,query,crawl,elt", secret_cache_seconds=300,
                secret_timeout_seconds=5.0, vault_addr="", vault_token_ref="", vault_namespace="",
                vault_prefix="secret/data/analystos/", aws_region="", aws_sm_prefix="analystos/",
                azure_kv_vaults="", azure_kv_prefix="analystos-", gcp_sm_projects="", gcp_sm_prefix="analystos-")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("name", ["OPENROUTER_API_KEY", "ANALYSTOS_JWT_SECRET", "ANALYSTOS_DATABASE_URL", "PATH",
                                  "AWS_SECRET_ACCESS_KEY", "ANALYSTOS_SOURCE_SECRET_"])
def test_env_ref_outside_the_allowed_prefix_is_refused(monkeypatch, name):
    monkeypatch.setenv(name, "platform-secret-value")
    with pytest.raises(Forbidden) as exc:
        resolve_secret(f"env:{name}", settings=_settings())
    assert "platform-secret-value" not in str(exc.value.message)
    with pytest.raises(Forbidden):
        check_secret_ref(f"env:{name}", settings=_settings())


def test_env_ref_with_the_allowed_prefix_resolves(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_SHOP_PG", "s3cret")
    assert resolve_secret("env:ANALYSTOS_SOURCE_SECRET_SHOP_PG", settings=_settings()) == "s3cret"


def test_admin_registered_names_are_allowed_but_never_platform_names(monkeypatch):
    monkeypatch.setenv("LEGACY_PG_PASSWORD", "legacy")
    monkeypatch.setenv("ANALYSTOS_JWT_SECRET", "jwt")
    s = _settings(secret_env_names="LEGACY_PG_PASSWORD, ANALYSTOS_JWT_SECRET")
    assert resolve_secret("env:LEGACY_PG_PASSWORD", settings=s) == "legacy"
    with pytest.raises(Forbidden):  # an ANALYSTOS_* name is platform config even when listed
        resolve_secret("env:ANALYSTOS_JWT_SECRET", settings=s)


@pytest.mark.parametrize("prefix", ["ANALYSTOS_", "", "A_", "ANALYSTOS", "ANALY"])
def test_unsafe_prefix_configuration_is_ignored(monkeypatch, prefix):
    monkeypatch.setenv("ANALYSTOS_JWT_SECRET", "jwt")
    with pytest.raises(Forbidden):
        resolve_secret("env:ANALYSTOS_JWT_SECRET", settings=_settings(secret_env_prefixes=prefix))


def test_file_refs_only_below_configured_directories(tmp_path):
    allowed = tmp_path / "secrets"
    allowed.mkdir()
    (allowed / "pg").write_text("filepw\n", encoding="utf-8")
    outside = tmp_path / "jwt"
    outside.write_text("jwt", encoding="utf-8")
    s = _settings(secret_file_dirs=str(allowed))
    assert resolve_secret(f"file:{allowed / 'pg'}", settings=s) == "filepw"
    for ref in (f"file:{outside}", f"file:{allowed / '..' / 'jwt'}", f"file:{allowed}"):
        with pytest.raises(Forbidden):
            resolve_secret(ref, settings=s)
    with pytest.raises(Forbidden):  # no directory configured = no file refs
        resolve_secret(f"file:{allowed / 'pg'}", settings=_settings())


def test_platform_refs_skip_the_workspace_policy(monkeypatch):
    monkeypatch.setenv("SMTP_RELAY_PASSWORD", "relay")
    assert resolve_platform_secret("env:SMTP_RELAY_PASSWORD", settings=_settings()) == "relay"


def test_source_credentials_resolve_only_in_credential_roles(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_X", "x")
    api = _settings(process_role="api")
    with pytest.raises(Forbidden):
        resolve_secret("env:ANALYSTOS_SOURCE_SECRET_X", settings=api)
    assert resolve_secret("env:ANALYSTOS_SOURCE_SECRET_X", purpose="mcp", settings=api) == "x"
    assert resolve_secret("env:ANALYSTOS_SOURCE_SECRET_X", settings=_settings(process_role="query")) == "x"


def test_webhook_namespace_override(monkeypatch):
    monkeypatch.setenv("ANALYSTOS_WEBHOOK_OPS", "w" * 20)
    monkeypatch.setenv("ANALYSTOS_SOURCE_SECRET_X", "x")
    prefixes = ("ANALYSTOS_WEBHOOK_",)
    assert resolve_secret("env:ANALYSTOS_WEBHOOK_OPS", purpose="webhook", settings=_settings(),
                          env_prefixes_override=prefixes) == "w" * 20
    with pytest.raises(Forbidden):
        resolve_secret("env:ANALYSTOS_SOURCE_SECRET_X", purpose="webhook", settings=_settings(),
                       env_prefixes_override=prefixes)


@pytest.mark.parametrize("ref", ["plain-password", "smb:x", "env:", "env:1BAD", "file:relative/path", "vault:../etc",
                                 "vault:", "azure-kv:onlyvault", "gcp-sm:proj", "aws-sm:a#b c"])
def test_malformed_refs_are_invalid_input(ref):
    with pytest.raises(InvalidInput):
        check_secret_ref(ref, settings=_settings())


def test_manager_refs_must_sit_under_the_configured_prefix():
    s = _settings(azure_kv_vaults="corp-kv", gcp_sm_projects="corp-proj")
    for ok in ("vault:secret/data/analystos/shop/pg#password", "aws-sm:analystos/shop-pg#password",
               "azure-kv:corp-kv/analystos-shop", "gcp-sm:corp-proj/analystos-shop@3#password"):
        assert check_secret_ref(ok, settings=s) == parse_ref(ok)
    for bad in ("vault:secret/data/platform/jwt", "vault:secret/data/analystos", "aws-sm:prod/jwt",
                "azure-kv:other-kv/analystos-shop", "azure-kv:corp-kv/jwt", "gcp-sm:other/analystos-x",
                "gcp-sm:corp-proj/jwt"):
        with pytest.raises(Forbidden):
            check_secret_ref(bad, settings=s)


def test_tenant_placeholder_binds_refs_to_the_callers_tenant():
    s = _settings(vault_prefix="secret/data/analystos/{tenant}/")
    assert check_secret_ref("vault:secret/data/analystos/t_a/pg", settings=s, tenant="t_a")
    with pytest.raises(Forbidden):
        check_secret_ref("vault:secret/data/analystos/t_b/pg", settings=s, tenant="t_a")
    with pytest.raises(Forbidden):  # no tenant bound: fail closed
        check_secret_ref("vault:secret/data/analystos/t_a/pg", settings=s, tenant="")


def test_source_registration_checks_the_ref_when_it_is_set():
    """register_source / update_source run this before storing (the API test is in
    tests/integration/test_gateway_s02.py::test_editor_cannot_register_a_platform_env_ref)."""
    from analystos.services.sources import _checked_secret_ref

    with pytest.raises(Forbidden):
        _checked_secret_ref("env:OPENROUTER_API_KEY")
    assert _checked_secret_ref(" env:ANALYSTOS_SOURCE_SECRET_SHOP ") == "env:ANALYSTOS_SOURCE_SECRET_SHOP"
    assert _checked_secret_ref("") is None


def test_mcp_registration_refuses_a_platform_env_ref():
    from analystos.mcp import client as mc

    with pytest.raises(Forbidden):
        mc._validate_secret_ref("env:ANALYSTOS_JWT_SECRET")
    assert mc._validate_secret_ref("env:ANALYSTOS_SOURCE_SECRET_MCP") == "env:ANALYSTOS_SOURCE_SECRET_MCP"


def test_connector_password_never_comes_from_a_platform_variable(monkeypatch):
    """The original finding end to end: a pushdown source pointed at env:OPENROUTER_API_KEY never builds a URL."""
    from analystos.connectors.postgres import PostgresConnector

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-very-secret")
    con = PostgresConnector({"host": "attacker.example", "database": "d", "username": "u"}, "env:OPENROUTER_API_KEY")
    with pytest.raises(Forbidden):
        con.sqlalchemy_url()


def teardown_module():
    secrets.clear_cache()
