"""AWS Signature Version 4 request signing over the standard library (ported from Atlas `aida.aws_sigv4`).

A signed request is a pure function of the request and the credential, pinned to AWS's published test
vectors (`tests/unit/test_secret_managers.py`). Header-based signing of a single-chunk payload only; the
secret key appears only in `signing_key` and never in a header, log or return value. Used by the
`aws-sm:` secret scheme (S02-T2); boto3 is not a dependency.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Final
from urllib.parse import quote

ALGORITHM: Final = "AWS4-HMAC-SHA256"

#: sha256 of the empty byte string. The payload hash of any bodyless request.
EMPTY_PAYLOAD_SHA256: Final = hashlib.sha256(b"").hexdigest()

#: Characters RFC 3986 calls unreserved. Everything else in a path segment or
#: a query parameter is percent-encoded. S3 encodes a path *once* (unlike the
#: double-encoding every other AWS service applies), which is why the path and
#: the query string are encoded here with different `safe` sets rather than by
#: one shared helper.
_UNRESERVED: Final = "-_.~"


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def canonical_uri(path: str) -> str:
    """Percent-encode a request path, preserving its separators.

    S3 signs the path encoded exactly once. `quote` already leaves the
    unreserved set alone, so the only addition here is keeping `/` literal.
    """
    if not path:
        return "/"
    return quote(path, safe="/" + _UNRESERVED)


def canonical_query_string(query: dict[str, str] | None) -> str:
    """Sort and encode query parameters the way the canonical request wants.

    Sorted by encoded name; a valueless parameter (S3 subresources such as
    `?legal-hold`) still carries its `=`, which is what the specification
    requires and what MinIO and S3 both verify against.
    """
    if not query:
        return ""
    encoded = sorted(
        (quote(name, safe=_UNRESERVED), quote(value, safe=_UNRESERVED))
        for name, value in query.items()
    )
    return "&".join(f"{name}={value}" for name, value in encoded)


def canonical_headers(headers: dict[str, str]) -> tuple[str, str]:
    """Return the canonical header block and the matching signed-header list.

    Header names are lowercased and values stripped of surrounding
    whitespace. Both halves are derived from the same sorted sequence, so
    the block and the list can never disagree about which headers were
    signed -- a mismatch there is the single most common SigV4 defect.
    """
    normalized = sorted((name.lower().strip(), value.strip()) for name, value in headers.items())
    block = "".join(f"{name}:{value}\n" for name, value in normalized)
    signed = ";".join(name for name, _ in normalized)
    return block, signed


def canonical_request(
    *,
    method: str,
    path: str,
    query: dict[str, str] | None,
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """Build the canonical request and the signed-header list it commits to."""
    header_block, signed_headers = canonical_headers(headers)
    request = "\n".join(
        (
            method.upper(),
            canonical_uri(path),
            canonical_query_string(query),
            header_block,
            signed_headers,
            payload_hash,
        )
    )
    return request, signed_headers


def credential_scope(*, moment: datetime, region: str, service: str) -> str:
    return f"{moment.strftime('%Y%m%d')}/{region}/{service}/aws4_request"


def string_to_sign(
    *,
    request: str,
    moment: datetime,
    region: str,
    service: str,
) -> str:
    return "\n".join(
        (
            ALGORITHM,
            moment.strftime("%Y%m%dT%H%M%SZ"),
            credential_scope(moment=moment, region=region, service=service),
            _sha256_hex(request.encode("utf-8")),
        )
    )


def signing_key(*, secret_key: str, moment: datetime, region: str, service: str) -> bytes:
    """Derive the date/region/service-scoped key.

    The secret never leaves this function: every later stage sees only the
    derived key, and the signature it produces is not reversible to it.
    """
    date_key = _hmac(f"AWS4{secret_key}".encode(), moment.strftime("%Y%m%d"))
    region_key = _hmac(date_key, region)
    service_key = _hmac(region_key, service)
    return _hmac(service_key, "aws4_request")


def sign(
    *,
    method: str,
    path: str,
    query: dict[str, str] | None,
    headers: dict[str, str],
    payload_hash: str,
    moment: datetime,
    region: str,
    service: str,
    secret_key: str,
) -> tuple[str, str]:
    """Return the hex signature and the signed-header list for one request."""
    request, signed_headers = canonical_request(
        method=method,
        path=path,
        query=query,
        headers=headers,
        payload_hash=payload_hash,
    )
    to_sign = string_to_sign(request=request, moment=moment, region=region, service=service)
    derived = signing_key(secret_key=secret_key, moment=moment, region=region, service=service)
    return (
        hmac.new(derived, to_sign.encode("utf-8"), hashlib.sha256).hexdigest(),
        signed_headers,
    )


def signed_headers_for(
    *,
    method: str,
    host: str,
    path: str,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    payload: bytes = b"",
    moment: datetime,
    region: str,
    service: str = "s3",
    access_key: str,
    secret_key: str,
    session_token: str = "",
) -> dict[str, str]:
    """Sign a request and return every header it must be sent with.

    The caller supplies content headers (`content-type`, `x-amz-object-lock-*`,
    user metadata); this adds `host`, `x-amz-date`, `x-amz-content-sha256`,
    an optional `x-amz-security-token`, and the `Authorization` header that
    commits to all of them. Everything passed in is signed -- there is no
    header this function accepts and silently leaves out of the signature.
    """
    payload_hash = _sha256_hex(payload)
    to_sign_headers: dict[str, str] = dict(headers or {})
    to_sign_headers["host"] = host
    to_sign_headers["x-amz-date"] = moment.strftime("%Y%m%dT%H%M%SZ")
    to_sign_headers["x-amz-content-sha256"] = payload_hash
    if session_token:
        to_sign_headers["x-amz-security-token"] = session_token

    signature, signed_list = sign(
        method=method,
        path=path,
        query=query,
        headers=to_sign_headers,
        payload_hash=payload_hash,
        moment=moment,
        region=region,
        service=service,
        secret_key=secret_key,
    )
    scope = credential_scope(moment=moment, region=region, service=service)
    to_sign_headers["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_list}, Signature={signature}"
    )
    return to_sign_headers
