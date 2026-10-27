"""Secret-manager schemes (S02-T2): wire shape against a mocked HTTP transport, SigV4 against AWS's published
vector, ≤ 5 min bounded cache, values never in errors. No live Vault/AWS/Azure/GCP account is reachable here."""
from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from analystos.connectors import aws_sigv4, secrets
from analystos.core.errors import Forbidden, UpstreamUnavailable


def _settings(**over):
    base = dict(secret_env_prefixes="ANALYSTOS_SOURCE_SECRET_", secret_env_names="", secret_file_dirs="",
                process_role="all", credential_roles="all,query", secret_cache_seconds=300, secret_timeout_seconds=5.0,
                vault_addr="https://vault.test", vault_token_ref="env:VAULT_BOOT_TOKEN", vault_namespace="ns1",
                vault_prefix="secret/data/analystos/", aws_region="eu-west-1", aws_sm_prefix="analystos/",
                azure_kv_vaults="corp-kv", azure_kv_prefix="analystos-", gcp_sm_projects="corp-proj",
                gcp_sm_prefix="analystos-")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def http():
    calls: list[httpx.Request] = []
    routes: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                return resp
        return httpx.Response(404)

    secrets.set_http_transport(httpx.MockTransport(handler))
    yield SimpleNamespace(calls=calls, routes=routes)
    secrets.set_http_transport(None)


def test_sigv4_matches_the_published_get_object_vector():
    signed = aws_sigv4.signed_headers_for(
        method="GET", host="examplebucket.s3.amazonaws.com", path="/test.txt", headers={"range": "bytes=0-9"},
        payload=b"", moment=datetime(2013, 5, 24, tzinfo=UTC), region="us-east-1", access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert signed["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "wJalrXUtnFEMI" not in signed["Authorization"]


def test_vault_kv2_field_namespace_and_cache(http, monkeypatch):
    monkeypatch.setenv("VAULT_BOOT_TOKEN", "hvs.boot")
    http.routes["https://vault.test/v1/secret/data/analystos/shop/pg"] = httpx.Response(
        200, json={"data": {"data": {"password": "pgpw", "value": "v"}, "metadata": {"version": 3}}})
    s = _settings()
    assert secrets.resolve_secret("vault:secret/data/analystos/shop/pg#password", settings=s) == "pgpw"
    assert secrets.resolve_secret("vault:secret/data/analystos/shop/pg#password", settings=s) == "pgpw"
    assert len(http.calls) == 1  # second read from the bounded in-memory cache
    req = http.calls[0]
    assert req.headers["X-Vault-Token"] == "hvs.boot" and req.headers["X-Vault-Namespace"] == "ns1"


def test_cache_is_capped_at_five_minutes_and_bounded(http, monkeypatch):
    monkeypatch.setenv("VAULT_BOOT_TOKEN", "t")
    http.routes["https://vault.test/v1/"] = httpx.Response(200, json={"data": {"data": {"value": "x"}}})
    clock = [1000.0]
    monkeypatch.setattr(secrets.time, "monotonic", lambda: clock[0])
    s = _settings(secret_cache_seconds=300)
    secrets.resolve_secret("vault:secret/data/analystos/a", settings=s)
    clock[0] += 299
    secrets.resolve_secret("vault:secret/data/analystos/a", settings=s)
    assert len(http.calls) == 1
    clock[0] += 2
    secrets.resolve_secret("vault:secret/data/analystos/a", settings=s)
    assert len(http.calls) == 2
    cache = secrets._SecretCache(max_entries=3)
    for i in range(10):
        cache.put(str(i), "v", 60)
    assert len(cache.data) == 3


def test_aws_secrets_manager_signs_and_picks_json_key(http, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-material")
    http.routes["https://secretsmanager.eu-west-1.amazonaws.com/"] = httpx.Response(
        200, json={"SecretString": json.dumps({"username": "ro", "password": "awspw"})})
    assert secrets.resolve_secret("aws-sm:analystos/shop#password", settings=_settings()) == "awspw"
    req = http.calls[0]
    assert req.method == "POST" and req.headers["x-amz-target"] == "secretsmanager.GetSecretValue"
    assert req.headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/eu-west-1/secretsmanager/aws4_request" in req.headers["Authorization"]
    assert "aws-secret-material" not in str(req.headers)
    assert json.loads(req.content) == {"SecretId": "analystos/shop"}


def test_azure_key_vault_client_credentials(http, monkeypatch):
    for k, v in {"AZURE_TENANT_ID": "tid", "AZURE_CLIENT_ID": "cid", "AZURE_CLIENT_SECRET": "csecret"}.items():
        monkeypatch.setenv(k, v)
    http.routes["https://login.microsoftonline.com/tid/oauth2/v2.0/token"] = httpx.Response(
        200, json={"access_token": "aad-token", "expires_in": 3600})
    http.routes["https://corp-kv.vault.azure.net/secrets/analystos-shop"] = httpx.Response(200, json={"value": "azpw"})
    assert secrets.resolve_secret("azure-kv:corp-kv/analystos-shop", settings=_settings()) == "azpw"
    token_req, secret_req = http.calls
    assert b"scope=https%3A%2F%2Fvault.azure.net%2F.default" in token_req.content
    assert secret_req.headers["Authorization"] == "Bearer aad-token"
    assert secret_req.url.params["api-version"] == "7.4"


def test_gcp_secret_manager_metadata_token_and_version(http, monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    http.routes["http://metadata.google.internal/"] = httpx.Response(200, json={"access_token": "gce-token"})
    http.routes["https://secretmanager.googleapis.com/v1/projects/corp-proj/secrets/analystos-shop/versions/7:access"] = \
        httpx.Response(200, json={"payload": {"data": base64.b64encode(b'{"password": "gcppw"}').decode()}})
    assert secrets.resolve_secret("gcp-sm:corp-proj/analystos-shop@7#password", settings=_settings()) == "gcppw"
    assert http.calls[0].headers["Metadata-Flavor"] == "Google"
    assert http.calls[1].headers["Authorization"] == "Bearer gce-token"


def test_gcp_service_account_jwt(http, monkeypatch, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    sa = tmp_path / "sa.json"
    sa.write_text(json.dumps({"client_email": "svc@corp.iam", "private_key": pem,
                              "token_uri": "https://oauth2.googleapis.com/token"}), encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(sa))
    http.routes["https://oauth2.googleapis.com/token"] = httpx.Response(200, json={"access_token": "sa-token"})
    http.routes["https://secretmanager.googleapis.com/"] = httpx.Response(
        200, json={"payload": {"data": base64.b64encode(b"plainpw").decode()}})
    assert secrets.resolve_secret("gcp-sm:corp-proj/analystos-shop", settings=_settings()) == "plainpw"
    assert b"grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer" in http.calls[0].content


def test_errors_never_carry_values_and_fail_closed(http, monkeypatch):
    monkeypatch.setenv("VAULT_BOOT_TOKEN", "hvs.boot")
    http.routes["https://vault.test/v1/secret/data/analystos/denied"] = httpx.Response(403, json={"errors": ["hvs.boot"]})
    http.routes["https://vault.test/v1/secret/data/analystos/down"] = httpx.Response(503)
    with pytest.raises(Forbidden) as denied:
        secrets.resolve_secret("vault:secret/data/analystos/denied", settings=_settings())
    assert "hvs.boot" not in denied.value.message
    with pytest.raises(UpstreamUnavailable):
        secrets.resolve_secret("vault:secret/data/analystos/down", settings=_settings())


def test_manager_refs_are_policy_checked_before_any_request(http):
    with pytest.raises(Forbidden):
        secrets.resolve_secret("vault:secret/data/platform/jwt", settings=_settings())
    assert http.calls == []
