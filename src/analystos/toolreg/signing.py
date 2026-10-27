"""Manifest signing and verification (S10-T5; v1 threat "tool supply-chain").

Every registry manifest is signed on publish with the tenant's ed25519 key over its canonical JSON
(every field but `signature`). The runtime refuses an unsigned manifest, one whose signature does not
verify, one signed by a revoked or unknown key, and one whose stored hash no longer matches its content.

No private key is stored. A tenant key is derived when needed from the platform signing secret
(`tool_signing_secret`, else a domain-separated key from `jwt_secret`), the tenant id and the key id.
`tool_signing_key` keeps the public half for listing, rotation and external verifiers; verification
also re-derives it, so rewriting that row cannot make a forged manifest verify.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.contracts.tool_manifest import ToolManifestV2, ToolSignature
from analystos.core.errors import Forbidden, PolicyDenied
from analystos.core.ids import canonical_json, new_id, utcnow
from analystos.db.models import ToolSigningKey

ALG = "ed25519"
_DOMAIN = b"analystos.tool-signing.v1"
_SEED_DOMAIN = "analystos.tool-signing.seed.v1"


class ManifestRefused(PolicyDenied):
    """The runtime will not run this manifest (unsigned, tampered, unknown or revoked key)."""

    code = "manifest_refused"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _master() -> bytes:
    from analystos.core.config import get_settings

    s = get_settings()
    explicit = getattr(s, "tool_signing_secret", None)
    if explicit:
        return explicit.encode()
    return hmac.new(s.jwt_secret.encode(), _DOMAIN, hashlib.sha256).digest()


def _private(tenant_id: str, key_id: str) -> Any:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    seed = hmac.new(_master(), f"{_SEED_DOMAIN}|{tenant_id}|{key_id}".encode(), hashlib.sha256).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_b64(tenant_id: str, key_id: str) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    return _b64(_private(tenant_id, key_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def tenant_of(session: Session, workspace_id: str | None) -> str:
    """The tenant whose key signs this workspace's manifests: the workspace's own, else the bound tenant."""
    from analystos.db.models import Workspace
    from analystos.tenancy import current_tenant

    ws = session.get(Workspace, workspace_id) if workspace_id else None
    tenant = (getattr(ws, "tenant_id", None) if ws is not None else None) or current_tenant()
    if not tenant:
        raise Forbidden("manifest signing needs a tenant")
    return tenant


@dataclass(frozen=True)
class KeyInfo:
    key_id: str
    public_key: str
    status: str


def active_key(session: Session, tenant: str, *, actor: str = "system") -> KeyInfo:
    """The tenant's active signing key, created on first use (`tenant-signing-1`)."""
    row = session.scalar(select(ToolSigningKey).where(ToolSigningKey.tenant_id == tenant, ToolSigningKey.status == "active")
                         .order_by(ToolSigningKey.created_at.desc()).limit(1))
    if row is None:
        n = 1 + len(list(session.scalars(select(ToolSigningKey.id).where(ToolSigningKey.tenant_id == tenant))))
        key_id = f"tenant-signing-{n}"
        row = ToolSigningKey(id=new_id("tsk"), tenant_id=tenant, key_id=key_id, alg=ALG,
                             public_key=_public_b64(tenant, key_id), status="active", created_by=actor)
        session.add(row)
        session.flush()
    return KeyInfo(row.key_id, row.public_key, row.status)


def rotate(session: Session, tenant: str, *, actor: str, revoke_previous: bool = False) -> KeyInfo:
    """A new active key; the previous one keeps verifying (retired) unless revoked, after which every
    manifest it signed is refused until re-signed (`store.resign_all`)."""
    for row in session.scalars(select(ToolSigningKey).where(ToolSigningKey.tenant_id == tenant,
                                                            ToolSigningKey.status == "active")):
        row.status, row.retired_at = ("revoked" if revoke_previous else "retired"), utcnow()
    session.flush()
    return active_key(session, tenant, actor=actor)


def list_keys(session: Session, tenant: str) -> list[dict[str, Any]]:
    return [{"key_id": k.key_id, "alg": k.alg, "public_key": k.public_key, "status": k.status,
             "created_at": k.created_at.isoformat() if k.created_at else None,
             "retired_at": k.retired_at.isoformat() if k.retired_at else None}
            for k in session.scalars(select(ToolSigningKey).where(ToolSigningKey.tenant_id == tenant)
                                     .order_by(ToolSigningKey.created_at))]


def sign(session: Session, tenant: str, manifest: ToolManifestV2, *, actor: str = "system") -> ToolManifestV2:
    key = active_key(session, tenant, actor=actor)
    payload = canonical_json(manifest.signing_payload()).encode()
    value = _b64(_private(tenant, key.key_id).sign(payload))
    return manifest.model_copy(update={"signature": ToolSignature(alg=ALG, key_id=key.key_id, value=value)})


def verify(session: Session, tenant: str, manifest: dict[str, Any], *, manifest_hash: str | None = None) -> ToolManifestV2:
    """The manifest as the runtime may run it, or `ManifestRefused` with why not."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from pydantic import ValidationError

    try:
        m = ToolManifestV2.model_validate(manifest)
    except ValidationError as exc:
        raise ManifestRefused(f"manifest does not validate: {exc.errors()[0]['msg']}", details={"reason": "invalid"}) from None
    if m.signature is None:
        raise ManifestRefused(f"manifest {m.ref} is unsigned", details={"reason": "unsigned"})
    if manifest_hash is not None and not hmac.compare_digest(m.content_hash(), manifest_hash):
        raise ManifestRefused(f"manifest {m.ref} changed after it was signed", details={"reason": "tampered"})
    key =session.scalar(select(ToolSigningKey).where(ToolSigningKey.tenant_id == tenant,
                                                      ToolSigningKey.key_id == m.signature.key_id))
    if key is None or key.status == "revoked":
        raise ManifestRefused(f"manifest {m.ref} is signed by an unknown or revoked key", details={"reason": "key"})
    derived = _public_b64(tenant, key.key_id)
    if not hmac.compare_digest(derived, key.public_key):
        raise ManifestRefused(f"signing key {key.key_id} does not match this platform's secret", details={"reason": "key"})
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(derived)).verify(_unb64(m.signature.value),
                                                                    canonical_json(m.signing_payload()).encode())
    except (InvalidSignature, ValueError):
        raise ManifestRefused(f"manifest {m.ref} has an invalid signature", details={"reason": "tampered"}) from None
    return m
