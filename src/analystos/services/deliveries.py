"""Approved external delivery: email and webhook destinations for reports and alerts (N-3, SCH-003, §39).

A destination (email recipients, or a webhook URL with an HMAC secret reference) is authorized by an
approval whose payload is the destination itself, so `destination_hash` is what an approver signed. Any
change to the target, its content kinds or its secret reference gives a new hash: the old approval is
invalidated and a new one requested. The authorization is standing (a schedule sends without a human),
lasts `delivery_authorization_days`, and is re-verified with `verify_for_execution` immediately before
every send — requester and approver still hold their roles, the policy version is unchanged, the payload
still hashes to what was approved.

A send is a `delivery` row keyed by an idempotency key (destination, its hash, subject, content hash): the
same report to the same destination is queued once, and the key travels with the send (`Idempotency-Key`,
`Message-ID`) so a receiver drops a retried duplicate. Retries are bounded exponential backoff; a
retryable failure after `max_attempts` is `dead_letter`, a permanent one (4xx, SMTP rejection) goes there
at once, and a governance refusal (not authorized, approval invalid, SSRF guard) is `refused`. Both are
terminal until an editor redrives them.

Webhook URLs go through `tools/http.py`'s outbound guard (public addresses only unless the operator lists a
host in `outbound_private_hosts`, pinned address, no redirects), the same guard MCP and HTTP tools use.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import smtplib
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analystos.core.errors import (
    AnalystOSError,
    ApprovalRequired,
    Conflict,
    FeatureUnavailable,
    InvalidInput,
    NotFound,
    PolicyDenied,
    PreconditionFailed,
    UpstreamUnavailable,
)
from analystos.core.ids import canonical_json, new_id, stable_hash, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import Alert, Approval, Artifact, Delivery, DeliveryDestination, User
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import get_workspace, require_role
from analystos.tenancy.background import owned_by, platform_loop

log = get_logger(__name__)
APPROVAL_ACTION = "delivery_destination.authorize"
KINDS = ("email", "webhook", "teams", "slack")  # teams / slack: incoming webhooks (S09-T5, triggers/channels.py)
CONTENT_KINDS = ("report", "alert")
TERMINAL = ("delivered", "dead_letter", "refused")
LEASE = timedelta(minutes=5)
MAX_RECIPIENTS = 50
SIGNATURE_HEADER = "X-AnalystOS-Signature"
TIMESTAMP_HEADER = "X-AnalystOS-Timestamp"
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
# Webhook signing secrets live in their own namespace, so an editor cannot point a destination at another
# secret (a source password) and learn about it from the signatures or the error text.
_SECRET_REF = re.compile(r"^env:ANALYSTOS_WEBHOOK_[A-Z0-9_]+$")
WEBHOOK_ENV_PREFIXES = ("ANALYSTOS_WEBHOOK_",)  # re-checked when the secret resolves
MIN_SECRET_LENGTH = 16
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_RETRYABLE_HTTP = {408, 425, 429}


def _settings():  # noqa: ANN202
    from analystos.core.config import get_settings

    return get_settings()


def _split(value: str) -> list[str]:
    return [v.strip().lower() for v in (value or "").split(",") if v.strip()]


# ------------------------------------------------------------------------------ destination validation
def check_webhook_url(url: str) -> str:
    """Static SSRF checks at registration (the DNS check runs again, pinned, at every send): https only
    (http only for a host the operator listed), no credentials, no local names or non-public IP literals
    unless the operator listed them in `outbound_private_hosts`."""
    from analystos.tools import http as outbound

    url = (url or "").strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    private_hosts = outbound.split_hosts(_settings().outbound_private_hosts)
    listed = host in private_hosts
    if not host or parts.scheme not in ("https", "http"):
        raise InvalidInput("a webhook URL must be an https URL with a host")
    if parts.scheme == "http" and not listed:
        raise InvalidInput("a webhook URL must use https (http only for a host an operator listed in outbound_private_hosts)")
    if parts.username or parts.password:
        raise InvalidInput("a webhook URL must not embed credentials; the signature secret is a secret_ref")
    if parts.fragment:
        raise InvalidInput("a webhook URL must not carry a fragment")
    try:
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise InvalidInput("the webhook URL has an invalid port") from None
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:
        if not outbound.address_is_public(literal) and not outbound.private_allowed(host, literal, private_hosts):
            raise PolicyDenied(f"webhook host {host} is not a public address; an operator must list it in outbound_private_hosts")
    elif (host == "localhost" or host.endswith(_LOCAL_SUFFIXES) or "." not in host) and not listed:
        raise PolicyDenied(f"webhook host {host} is a local name; an operator must list it in outbound_private_hosts")
    return url


def normalize_config(kind: str, config: dict[str, Any] | None) -> dict[str, Any]:
    """The canonical target the approval binds to; unknown keys are refused so nothing unapproved rides along."""
    config = dict(config or {})
    if kind == "email":
        extra = set(config) - {"recipients", "attach_formats"}
        if extra:
            raise InvalidInput(f"unknown email destination fields: {sorted(extra)}")
        raw = config.get("recipients")
        if isinstance(raw, str):
            raw = [r for r in re.split(r"[,;\s]+", raw) if r]
        if not isinstance(raw, list) or not raw:
            raise InvalidInput("an email destination needs config.recipients (one or more addresses)")
        recipients = sorted({str(r).strip().lower() for r in raw if str(r).strip()})
        if len(recipients) > MAX_RECIPIENTS:
            raise InvalidInput(f"at most {MAX_RECIPIENTS} recipients per destination")
        bad = [r for r in recipients if not _EMAIL.match(r)]
        if bad:
            raise InvalidInput(f"not an email address: {', '.join(bad[:5])}")
        allowed = _split(_settings().delivery_email_allowed_domains)
        outside = [r for r in recipients if allowed and r.rsplit("@", 1)[1] not in allowed]
        if outside:
            raise PolicyDenied(f"recipients outside the allowed email domains ({', '.join(allowed)}): {', '.join(outside[:5])}")
        formats = config.get("attach_formats", ["pdf"])
        from analystos.services.reports import FORMATS

        if not isinstance(formats, list) or any(f not in FORMATS for f in formats):
            raise InvalidInput(f"attach_formats must be within {FORMATS}")
        return {"recipients": recipients, "attach_formats": sorted(set(formats))}
    if kind == "webhook":
        extra = set(config) - {"url", "secret_ref"}
        if extra:
            raise InvalidInput(f"unknown webhook destination fields: {sorted(extra)}")
        secret_ref = str(config.get("secret_ref") or "").strip()
        if not _SECRET_REF.match(secret_ref):
            raise InvalidInput("a webhook destination needs config.secret_ref = env:ANALYSTOS_WEBHOOK_<NAME>, the "
                               "environment variable holding its HMAC signing secret")
        return {"url": check_webhook_url(str(config.get("url") or "")), "secret_ref": secret_ref}
    if kind in ("teams", "slack"):
        extra = set(config) - {"url_ref"}
        if extra:
            raise InvalidInput(f"unknown {kind} destination fields: {sorted(extra)}")
        url_ref = str(config.get("url_ref") or "").strip()
        if not _SECRET_REF.match(url_ref):  # the incoming-webhook URL carries its own token: it is a secret
            raise InvalidInput(f"a {kind} destination needs config.url_ref = env:ANALYSTOS_WEBHOOK_<NAME>, the "
                               "environment variable holding its incoming-webhook URL")
        return {"url_ref": url_ref}
    raise InvalidInput(f"kind must be one of {list(KINDS)}")


def _content_kinds(value: list[str] | None) -> list[str]:
    kinds = sorted(set(value or CONTENT_KINDS))
    if not kinds or any(k not in CONTENT_KINDS for k in kinds):
        raise InvalidInput(f"content_kinds must be within {list(CONTENT_KINDS)}")
    return kinds


def destination_hash(workspace_id: str, kind: str, config: dict[str, Any], content_kinds: list[str]) -> str:
    return stable_hash({"workspace_id": workspace_id, "kind": kind, "config": config, "content_kinds": content_kinds})


def authorization_payload(dest: DeliveryDestination) -> dict[str, Any]:
    """What an approver authorizes, rebuilt from the row's *current* values at every send: an edit that
    bypassed the API changes the payload hash and `verify_for_execution` invalidates the approval."""
    return {"destination_id": dest.id, "workspace_id": dest.workspace_id, "kind": dest.kind, "config": dest.config,
            "content_kinds": list(dest.content_kinds or []),
            "destination_hash": destination_hash(dest.workspace_id, dest.kind, dest.config, list(dest.content_kinds or []))}


def describe(dest: DeliveryDestination) -> str:
    if dest.kind == "email":
        return f"email to {', '.join(dest.config.get('recipients', []))}"
    if dest.kind in ("teams", "slack"):
        return f"{dest.kind} channel (incoming webhook in {dest.config.get('url_ref')})"
    return f"webhook POST to {dest.config.get('url')}"


# ------------------------------------------------------------------------------ destination lifecycle
def _invalidate(session: Session, approval_id: str | None, reason: str) -> None:
    if approval_id:
        session.execute(update(Approval).where(Approval.id == approval_id, Approval.status.in_(["pending", "approved"]))
                        .values(status="invalidated", reason=reason))


def _request_authorization(session: Session, user: User, dest: DeliveryDestination) -> Approval:
    from analystos.governance.approvals import request_approval

    ws = get_workspace(session, dest.workspace_id)
    _invalidate(session, dest.approval_id, "destination changed; a new authorization was requested")
    approval = request_approval(session, workspace_id=dest.workspace_id, run_id=None, action=APPROVAL_ACTION,
                                payload=authorization_payload(dest), plan_hash=None, policy_version=ws.policy_version,
                                requested_by=user.id, risk_tier="high", destination=f"{dest.kind}:{dest.name}"[:120],
                                affected_assets=[], evidence={"summary": describe(dest),
                                                              "content_kinds": list(dest.content_kinds or [])})
    dest.approval_id, dest.status, dest.authorized_hash, dest.authorized_until = approval.id, "pending", None, None
    emit(dest.workspace_id, "delivery.destination_requested", {"destination_id": dest.id, "approval_id": approval.id,
                                                               "kind": dest.kind, "destination_hash": dest.destination_hash},
         actor=f"user:{user.id}", session=session)
    return approval


def create_destination(session: Session, user: User, workspace_id: str, *, name: str, kind: str, config: dict[str, Any],
                       content_kinds: list[str] | None = None) -> DeliveryDestination:
    require_role(session, user, workspace_id, "editor")
    name = (name or "").strip()
    if not name:
        raise InvalidInput("a destination needs a name")
    if session.scalar(select(DeliveryDestination.id).where(DeliveryDestination.workspace_id == workspace_id,
                                                           DeliveryDestination.name == name)):
        raise Conflict(f"a destination named {name!r} already exists in this workspace")
    cfg, kinds = normalize_config(kind, config), _content_kinds(content_kinds)
    dest = DeliveryDestination(id=new_id("dst"), workspace_id=workspace_id, name=name[:200], kind=kind, config=cfg,
                               content_kinds=kinds, destination_hash=destination_hash(workspace_id, kind, cfg, kinds),
                               status="pending", created_by=user.id, revision=1)
    session.add(dest)
    session.flush()
    _request_authorization(session, user, dest)
    audit(f"user:{user.id}", "delivery.destination_created", workspace_id=workspace_id, target=dest.id,
          decision="approval_required", details={"kind": kind, "destination_hash": dest.destination_hash}, session=session)
    return dest


def update_destination(session: Session, user: User, destination_id: str, *, name: str | None = None,
                       config: dict[str, Any] | None = None, content_kinds: list[str] | None = None,
                       expected_revision: int | None = None) -> DeliveryDestination:
    """A renamed destination keeps its authorization; a changed target needs a new one."""
    dest = _get(session, destination_id)
    require_role(session, user, dest.workspace_id, "editor")
    if expected_revision is not None and expected_revision != dest.revision:
        raise PreconditionFailed(f"destination {dest.id} is at revision {dest.revision}, not {expected_revision}",
                                 details={"current_revision": dest.revision})
    if dest.status == "revoked":
        raise Conflict("a revoked destination cannot be edited; create a new one")
    if name is not None and name.strip():
        dest.name = name.strip()[:200]
    cfg = normalize_config(dest.kind, config) if config is not None else dest.config
    kinds = _content_kinds(content_kinds) if content_kinds is not None else list(dest.content_kinds or [])
    new_hash = destination_hash(dest.workspace_id, dest.kind, cfg, kinds)
    changed = new_hash != dest.destination_hash
    if changed:
        dest.config, dest.content_kinds, dest.destination_hash = cfg, kinds, new_hash
        session.flush()
        _request_authorization(session, user, dest)
        _refuse_queued(session, dest, "the destination changed after this delivery was queued")
    dest.revision = (dest.revision or 1) + 1
    audit(f"user:{user.id}", "delivery.destination_updated", workspace_id=dest.workspace_id, target=dest.id,
          decision="approval_required" if changed else None,
          details={"destination_hash": dest.destination_hash, "reauthorization": changed}, session=session)
    return dest


def revoke_destination(session: Session, user: User, destination_id: str, reason: str | None = None) -> DeliveryDestination:
    dest = _get(session, destination_id)
    require_role(session, user, dest.workspace_id, "editor")
    _invalidate(session, dest.approval_id, "destination revoked")
    dest.status, dest.authorized_hash, dest.authorized_until = "revoked", None, None
    dest.revision = (dest.revision or 1) + 1
    refused = _refuse_queued(session, dest, "the destination was revoked")
    emit(dest.workspace_id, "delivery.destination_revoked", {"destination_id": dest.id, "refused_deliveries": refused},
         actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "delivery.destination_revoked", workspace_id=dest.workspace_id, target=dest.id,
          reasons=[reason or ""], details={"refused_deliveries": refused}, session=session)
    return dest


def reauthorize_destination(session: Session, user: User, destination_id: str) -> DeliveryDestination:
    """Ask again for the same target, after an authorization lapsed (expiry, a policy change) or was rejected."""
    dest = _get(session, destination_id)
    require_role(session, user, dest.workspace_id, "editor")
    if dest.status == "revoked":
        raise Conflict("a revoked destination cannot be re-authorized; create a new one")
    if _authorized(dest):
        raise Conflict("the destination is already authorized")
    _request_authorization(session, user, dest)
    dest.revision = (dest.revision or 1) + 1
    audit(f"user:{user.id}", "delivery.destination_reauthorization", workspace_id=dest.workspace_id, target=dest.id,
          decision="approval_required", details={"destination_hash": dest.destination_hash}, session=session)
    return dest


def apply_decision(session: Session, approval: Approval) -> DeliveryDestination | None:
    """Called after an approver decides an authorization (api/routers/artifacts._decide)."""
    dest = session.scalar(select(DeliveryDestination).where(DeliveryDestination.approval_id == approval.id))
    if dest is None or approval.action != APPROVAL_ACTION:
        return None
    if approval.status == "approved":
        if stable_hash(authorization_payload(dest)) != approval.payload_hash:
            _invalidate(session, approval.id, "destination changed while the authorization was pending")
            raise Conflict("the destination changed while its authorization was pending; a new approval is required")
        until = utcnow() + timedelta(days=int(_settings().delivery_authorization_days))
        dest.status, dest.authorized_hash, dest.authorized_until = "authorized", dest.destination_hash, until
        session.execute(update(Approval).where(Approval.id == approval.id).values(expires_at=until))
        session.expire(approval)
        emit(dest.workspace_id, "delivery.destination_authorized", {"destination_id": dest.id, "approval_id": approval.id,
                                                                    "destination_hash": dest.destination_hash},
             actor=f"user:{approval.decided_by}", session=session)
    elif approval.status == "rejected":
        dest.status = "rejected"
        emit(dest.workspace_id, "delivery.destination_rejected", {"destination_id": dest.id, "approval_id": approval.id},
             actor=f"user:{approval.decided_by}", session=session)
    return dest


def _get(session: Session, destination_id: str) -> DeliveryDestination:
    dest = session.get(DeliveryDestination, destination_id)
    if dest is None:
        raise NotFound("delivery destination not found")
    return dest


def check_targets(session: Session, workspace_id: str, ids: Any, content_kind: str) -> list[str]:
    """`config.deliver_to` on a schedule or monitor: destinations of this workspace that accept this content.
    They may still await authorization; an unauthorized one refuses the send, visibly."""
    if ids is None:
        return []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise InvalidInput("config.deliver_to must be a list of delivery destination ids")
    for dest_id in ids:
        dest = session.get(DeliveryDestination, dest_id)
        if dest is None or dest.workspace_id != workspace_id:
            raise InvalidInput(f"delivery destination {dest_id} not found in this workspace")
        if dest.status == "revoked":
            raise InvalidInput(f"delivery destination {dest.name} is revoked")
        if content_kind not in (dest.content_kinds or []):
            raise InvalidInput(f"delivery destination {dest.name} does not accept {content_kind}s")
    return ids


def _refuse_queued(session: Session, dest: DeliveryDestination, reason: str) -> int:
    result = session.execute(update(Delivery).where(Delivery.destination_id == dest.id,
                                                    Delivery.status.in_(["queued", "retrying"]))
                             .values(status="refused", last_error=reason, next_attempt_at=None))
    return result.rowcount or 0


# ------------------------------------------------------------------------------ subjects
@dataclass
class OutboundMessage:
    subject: str
    text: str
    body: dict[str, Any]
    idempotency_key: str
    delivery_id: str
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)


def _subject(session: Session, workspace_id: str, subject_type: str, subject_id: str) -> tuple[Any, str]:
    """The verified thing being sent and its content hash; only a final report or a raised alert."""
    if subject_type == "report":
        art = session.get(Artifact, subject_id)
        if art is None or art.workspace_id != workspace_id or art.type != "report":
            raise NotFound("report not found in this workspace")
        if art.status != "final":
            raise InvalidInput("only a final report snapshot can be delivered")
        return art, art.content_hash
    if subject_type == "alert":
        alert = session.get(Alert, subject_id)
        if alert is None or alert.workspace_id != workspace_id:
            raise NotFound("alert not found in this workspace")
        return alert, stable_hash({"id": alert.id, "severity": alert.severity, "title": alert.title,
                                   "message": alert.message, "monitor_id": alert.monitor_id})
    raise InvalidInput(f"subject_type must be one of {list(CONTENT_KINDS)}")


def build_message(session: Session, delivery: Delivery, dest: DeliveryDestination) -> OutboundMessage:
    subject, content_hash = _subject(session, delivery.workspace_id, delivery.subject_type, delivery.subject_id)
    if content_hash != delivery.content_hash:
        raise PolicyDenied("the subject changed after this delivery was queued; queue it again")
    body: dict[str, Any] = {"type": "analystos.delivery", "version": 1, "delivery_id": delivery.id,
                            "idempotency_key": delivery.idempotency_key, "workspace_id": delivery.workspace_id,
                            "subject": {"type": delivery.subject_type, "id": delivery.subject_id},
                            "content_hash": content_hash}
    attachments: list[tuple[str, bytes, str]] = []
    if delivery.subject_type == "report":
        c = subject.content or {}
        files = {fmt: {k: info.get(k) for k in ("sha256", "bytes", "mime")} for fmt, info in (c.get("files") or {}).items()}
        body["report"] = {"kind": c.get("kind"), "title": c.get("title"), "run_id": subject.run_id,
                          "report_data_hash": c.get("report_data_hash"), "files": files,
                          "insights": c.get("insights"), "metrics": c.get("metrics"), "alerts": c.get("alerts")}
        title = f"Report: {c.get('title') or subject.name}"
        text = (f"{c.get('title') or subject.name}\n\n{c.get('insights', 0)} findings, {c.get('metrics', 0)} metrics, "
                f"{c.get('alerts', 0)} open alerts.\nReport data hash: {c.get('report_data_hash')}\n")
        if dest.kind == "email":
            from analystos.services.reports import report_file

            cap, used, skipped = int(_settings().delivery_attachment_max_bytes), 0, []
            for fmt in dest.config.get("attach_formats", []):
                if fmt not in files:
                    continue
                content, mime, ext = report_file(subject, fmt)  # re-checks the stored file's sha256
                if used + len(content) > cap:
                    skipped.append(fmt)
                    continue
                used += len(content)
                attachments.append((f"{c.get('kind') or 'report'}-{subject.id}.{ext}", content, mime))
            if skipped:
                text += f"\nNot attached (over the size limit): {', '.join(skipped)}. Download them in AnalystOS.\n"
    else:
        body["alert"] = {"id": subject.id, "severity": subject.severity, "title": subject.title, "message": subject.message,
                         "monitor_id": subject.monitor_id, "status": subject.status,
                         "created_at": subject.created_at.isoformat() if subject.created_at else None}
        title = f"[{subject.severity}] {subject.title}"
        text = f"{subject.title}\n\n{subject.message}\n"
    return OutboundMessage(subject=title[:250], text=text, body=body, idempotency_key=delivery.idempotency_key,
                           delivery_id=delivery.id, attachments=attachments)


# ------------------------------------------------------------------------------ transports
class Transport(Protocol):
    def send(self, config: dict[str, Any], message: OutboundMessage) -> dict[str, Any]: ...


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over `<timestamp>.<body>`: the receiver recomputes it and rejects stale timestamps."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str, body: bytes, signature: str, *, tolerance_seconds: int = 300,
                     now: float | None = None) -> bool:
    try:
        fresh = abs((now if now is not None else time.time()) - int(timestamp)) <= tolerance_seconds
    except ValueError:
        return False
    return fresh and hmac.compare_digest(sign(secret, timestamp, body), signature)


class WebhookTransport:
    """POST the canonical JSON body, signed, through the SSRF-safe outbound guard (pinned, no redirects)."""

    def __init__(self, *, resolver: Callable | None = None, http_transport: Any | None = None,
                 clock: Callable[[], float] = time.time):
        self.resolver, self.http_transport, self.clock = resolver, http_transport, clock

    def send(self, config: dict[str, Any], message: OutboundMessage) -> dict[str, Any]:
        from analystos.connectors.secrets import resolve_secret
        from analystos.tools import http as outbound

        secret = resolve_secret(config.get("secret_ref"), purpose="webhook", env_prefixes_override=WEBHOOK_ENV_PREFIXES)
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            raise InvalidInput(f"the webhook signing secret must be at least {MIN_SECRET_LENGTH} characters")
        settings = _settings()
        body = canonical_json(message.body).encode()
        timestamp = str(int(self.clock()))
        headers = {"Content-Type": "application/json", "User-Agent": "AnalystOS-Delivery/1",
                   "Idempotency-Key": message.idempotency_key, "X-AnalystOS-Delivery": message.delivery_id,
                   TIMESTAMP_HEADER: timestamp, SIGNATURE_HEADER: sign(secret, timestamp, body)}
        result = outbound.call(config["url"], method="POST", allowlist=None,
                               private_hosts=outbound.split_hosts(settings.outbound_private_hosts),
                               timeout=float(settings.delivery_timeout_seconds), max_bytes=64_000, headers=headers,
                               resolver=self.resolver, transport=self.http_transport, content=body,
                               internal_only=bool(getattr(settings, "air_gapped", False)))
        return {"status_code": result["status_code"], "bytes": len(body)}


class SmtpTransport:
    """SMTP from the environment (`ANALYSTOS_SMTP_*`); the password is `smtp_password_ref`, resolved per send."""

    def __init__(self, factory: Callable[..., Any] | None = None):
        self.factory = factory or smtplib.SMTP

    def send(self, config: dict[str, Any], message: OutboundMessage) -> dict[str, Any]:
        from analystos.connectors.secrets import resolve_platform_secret

        s = _settings()
        if not s.smtp_host or not s.smtp_from:
            raise FeatureUnavailable("email delivery is not configured on this installation",
                                     details={"remedy": "set ANALYSTOS_SMTP_HOST and ANALYSTOS_SMTP_FROM "
                                                        "(and ANALYSTOS_SMTP_PASSWORD_REF=env:NAME when the relay needs a login)"})
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = s.smtp_from, ", ".join(config["recipients"]), message.subject
        msg["Message-ID"] = f"<{message.idempotency_key}@analystos>"
        msg["X-AnalystOS-Delivery"] = message.delivery_id
        msg.set_content(message.text)
        for filename, content, mime in message.attachments:
            maintype, _, subtype = mime.partition("/")
            msg.add_attachment(content, maintype=maintype or "application", subtype=subtype or "octet-stream",
                               filename=filename)
        password = resolve_platform_secret(s.smtp_password_ref) if s.smtp_password_ref else None
        if s.smtp_username and not s.smtp_starttls:
            raise PolicyDenied("refusing to send SMTP credentials without TLS: set ANALYSTOS_SMTP_STARTTLS=true")
        try:
            with self.factory(s.smtp_host, int(s.smtp_port), timeout=float(s.smtp_timeout_seconds)) as smtp:
                if s.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if s.smtp_username:
                    smtp.login(s.smtp_username, password or "")
                refused = smtp.send_message(msg) or {}
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError) as exc:
            raise UpstreamUnavailable(f"the mail relay rejected the message: {type(exc).__name__}",
                                      details={"permanent": True}) from None
        except (smtplib.SMTPException, OSError) as exc:
            raise UpstreamUnavailable(f"the mail relay is unavailable: {type(exc).__name__}") from None
        return {"accepted": len(config["recipients"]) - len(refused), "refused": sorted(refused)}


def default_transports() -> dict[str, Transport]:
    from analystos.triggers.channels import ChannelTransport, GraphMailTransport

    email: Transport = GraphMailTransport() if _settings().delivery_email_transport == "graph" else SmtpTransport()
    return {"email": email, "webhook": WebhookTransport(), "teams": ChannelTransport("teams"),
            "slack": ChannelTransport("slack")}


# ------------------------------------------------------------------------------ queueing
def idempotency_key(dest: DeliveryDestination, subject_type: str, subject_id: str, content_hash: str) -> str:
    return stable_hash({"destination_id": dest.id, "destination_hash": dest.destination_hash,
                        "subject": [subject_type, subject_id], "content_hash": content_hash})


def enqueue(session: Session, workspace_id: str, *, destination_ids: list[str], subject_type: str, subject_id: str,
            origin: dict[str, Any] | None = None) -> list[Delivery]:
    """Queue one delivery per destination. The same subject to the same destination (at the same hash) is
    queued once; a destination that is not authorized or does not take this content is recorded refused."""
    _, content_hash = _subject(session, workspace_id, subject_type, subject_id)
    out: list[Delivery] = []
    settings = _settings()
    for dest_id in destination_ids or []:
        dest = session.get(DeliveryDestination, dest_id)
        if dest is None or dest.workspace_id != workspace_id:
            audit("system", "delivery.refused", workspace_id=workspace_id, target=dest_id, decision="deny",
                  reasons=["unknown destination"], details={"subject": [subject_type, subject_id]}, session=session)
            continue
        key = idempotency_key(dest, subject_type, subject_id, content_hash)
        existing = session.scalar(select(Delivery).where(Delivery.idempotency_key == key))
        if existing is not None:
            out.append(existing)
            continue
        why = None
        if subject_type not in (dest.content_kinds or []):
            why = f"destination {dest.name} does not accept {subject_type}s"
        elif not _authorized(dest):
            why = f"destination {dest.name} is not authorized ({dest.status})"
        d = Delivery(id=new_id("dlv"), workspace_id=workspace_id, destination_id=dest.id,
                     destination_hash=dest.destination_hash, approval_id=dest.approval_id, subject_type=subject_type,
                     subject_id=subject_id, content_hash=content_hash, idempotency_key=key,
                     status="refused" if why else "queued", attempts=0, max_attempts=int(settings.delivery_max_attempts),
                     next_attempt_at=None if why else utcnow(), last_error=why, response={}, origin=origin or {})
        try:
            with session.begin_nested():
                session.add(d)
        except IntegrityError:  # a concurrent enqueue of the same key won
            out.append(session.scalar(select(Delivery).where(Delivery.idempotency_key == key)))
            continue
        event = "delivery.refused" if why else "delivery.queued"
        emit(workspace_id, event, {"delivery_id": d.id, "destination_id": dest.id, "subject_type": subject_type,
                                   "subject_id": subject_id, **({"reason": why} if why else {})}, session=session)
        audit(_actor(origin), event, workspace_id=workspace_id, target=d.id, decision="deny" if why else "allow",
              reasons=[why] if why else [], details={"destination_id": dest.id, "idempotency_key": key,
                                                     "destination_hash": dest.destination_hash}, session=session)
        out.append(d)
    return out


def _actor(origin: dict[str, Any] | None) -> str:
    origin = origin or {}
    if origin.get("user"):
        return f"user:{origin['user']}"
    if origin.get("schedule_id"):
        return f"schedule:{origin['schedule_id']}"
    if origin.get("monitor_id"):
        return f"monitor:{origin['monitor_id']}"
    return "system"


def _authorized(dest: DeliveryDestination, now: datetime | None = None) -> bool:
    until = dest.authorized_until
    if until is not None and until.tzinfo is None:  # SQLite drops the zone
        from datetime import UTC

        until = until.replace(tzinfo=UTC)
    return (dest.status == "authorized" and dest.authorized_hash == dest.destination_hash
            and until is not None and until > (now or utcnow()))


def redrive(session: Session, user: User, delivery_id: str) -> Delivery:
    """An editor re-queues a dead-lettered or refused delivery; every governance check runs again at send."""
    d = session.get(Delivery, delivery_id)
    if d is None:
        raise NotFound("delivery not found")
    require_role(session, user, d.workspace_id, "editor")
    if d.status not in ("dead_letter", "refused"):
        raise Conflict(f"only a dead-lettered or refused delivery can be redriven (this one is {d.status})")
    d.status, d.attempts, d.next_attempt_at, d.locked_until = "queued", 0, utcnow(), None
    d.response = {**(d.response or {}), "redriven_by": user.id, "previous_error": d.last_error}
    d.last_error = None
    emit(d.workspace_id, "delivery.queued", {"delivery_id": d.id, "redrive": True}, actor=f"user:{user.id}", session=session)
    audit(f"user:{user.id}", "delivery.redriven", workspace_id=d.workspace_id, target=d.id, session=session)
    return d


# ------------------------------------------------------------------------------ sending
def backoff(attempts: int) -> timedelta:
    s = _settings()
    return timedelta(seconds=min(float(s.delivery_backoff_max_seconds),
                                 float(s.delivery_backoff_seconds) * 2 ** max(0, attempts - 1)))


def _claim(delivery_id: str, now: datetime) -> bool:
    """Compare-and-set to `sending` with a lease; a crashed sender's lease lapses and the row is retried
    (at-least-once: the idempotency key lets the receiver drop the duplicate)."""
    with session_scope() as s:
        result = s.execute(update(Delivery).where(
            Delivery.id == delivery_id,
            or_(and_(Delivery.status.in_(["queued", "retrying"]), Delivery.next_attempt_at <= now),
                and_(Delivery.status == "sending", Delivery.locked_until < now)))
            .values(status="sending", locked_until=now + LEASE, attempts=Delivery.attempts + 1)
            .execution_options(synchronize_session=False))
        return result.rowcount == 1


def _authorize(session: Session, d: Delivery, dest: DeliveryDestination | None) -> Approval:
    from analystos.governance.approvals import verify_for_execution

    if dest is None or dest.status == "revoked":
        raise PolicyDenied("the destination was removed or revoked")
    if dest.destination_hash != d.destination_hash:
        raise ApprovalRequired("the destination changed after this delivery was queued; queue it again")
    if not _authorized(dest):
        raise ApprovalRequired(f"the destination is not authorized ({dest.status}); an approver must authorize it")
    if d.subject_type not in (dest.content_kinds or []):
        raise PolicyDenied(f"the destination does not accept {d.subject_type}s")
    try:  # the operator's current egress policy (email domains, private hosts, https) applies at send, not only at registration
        normalize_config(dest.kind, dest.config)
    except InvalidInput as exc:
        raise PolicyDenied(f"the destination no longer satisfies the egress policy: {exc.message}") from None
    _authorize_rule(session, d)
    # Immediately before the side effect (CLAUDE.md rule 5): the approval still binds exactly this destination.
    try:
        return verify_for_execution(session, dest.approval_id, payload=authorization_payload(dest), plan_hash=None)
    except ApprovalRequired:
        dest.status, dest.authorized_hash, dest.authorized_until = "lapsed", None, None  # shown as needing re-approval
        raise


def _authorize_rule(session: Session, d: Delivery) -> None:
    """S09: an alert of a rule monitor (one with `notify` / `on_trigger`) is sent only while the rule's own
    approval verifies — same scope, same recipients — and only to a recipient that approval names."""
    from analystos.db.models import Monitor
    from analystos.triggers import rules

    monitor_id = (d.origin or {}).get("monitor_id")
    monitor = session.get(Monitor, monitor_id) if monitor_id else None
    if monitor is None or not rules.requires_approval(monitor.config):
        return
    rules.verify_rule(session, monitor)
    if d.destination_id not in rules.recipients(session, monitor):
        raise PolicyDenied("the destination is not a recipient of the monitor's approved rule")


def _permanent(exc: AnalystOSError) -> bool:
    if not exc.retryable:
        return True
    status = (exc.details or {}).get("status_code")
    return bool((exc.details or {}).get("permanent")) or (isinstance(status, int) and 400 <= status < 500
                                                           and status not in _RETRYABLE_HTTP)


@owned_by("delivery", "delivery_id")
def attempt(delivery_id: str, *, transports: dict[str, Transport] | None = None, now: datetime | None = None) -> str | None:
    """One send attempt. Returns the new status, or None when another sender holds the row. Never raises."""
    now = now or utcnow()
    if not _claim(delivery_id, now):
        return None
    transports = transports or default_transports()
    with session_scope() as s:
        d = s.get(Delivery, delivery_id)
        dest = s.get(DeliveryDestination, d.destination_id)
        try:
            approval = _authorize(s, d, dest)
            message = build_message(s, d, dest)
            response = transports[dest.kind].send(dest.config, message)
        except (ApprovalRequired, PolicyDenied, NotFound) as exc:
            _close(s, d, "refused", exc.message)
            return d.status
        except AnalystOSError as exc:
            if _permanent(exc) or d.attempts >= d.max_attempts:
                _close(s, d, "dead_letter", exc.message)
            else:
                d.status, d.last_error, d.locked_until = "retrying", exc.message, None
                d.next_attempt_at = now + backoff(d.attempts)
                emit(d.workspace_id, "delivery.retrying", {"delivery_id": d.id, "attempt": d.attempts,
                                                           "next_attempt_at": d.next_attempt_at.isoformat(),
                                                           "error": exc.message}, session=s)
                audit("system", "delivery.retrying", workspace_id=d.workspace_id, target=d.id,
                      details={"attempt": d.attempts, "error": exc.message}, session=s)
            return d.status
        except Exception as exc:  # an unexpected transport bug is retried like a transient failure, bounded
            log.exception("delivery %s failed unexpectedly", delivery_id)
            if d.attempts >= d.max_attempts:
                _close(s, d, "dead_letter", f"unexpected error: {type(exc).__name__}")
            else:
                d.status, d.last_error, d.locked_until = "retrying", f"unexpected error: {type(exc).__name__}", None
                d.next_attempt_at = now + backoff(d.attempts)
            return d.status
        d.status, d.delivered_at, d.locked_until, d.next_attempt_at = "delivered", utcnow(), None, None
        d.approval_id, d.last_error, d.response = approval.id, None, {**(d.response or {}), **response}
        from analystos.artifacts.registry import link

        link(s, d.workspace_id, (d.subject_type, d.subject_id), "delivered_via", ("delivery", d.id))
        link(s, d.workspace_id, ("delivery", d.id), "sent_to", ("delivery_destination", d.destination_id))
        emit(d.workspace_id, "delivery.sent", {"delivery_id": d.id, "destination_id": d.destination_id,
                                               "subject_type": d.subject_type, "subject_id": d.subject_id,
                                               "attempt": d.attempts}, session=s)
        audit("system", "delivery.sent", workspace_id=d.workspace_id, target=d.id, decision="allow",
              details={"destination_id": d.destination_id, "destination_hash": d.destination_hash,
                       "approval_id": approval.id, "idempotency_key": d.idempotency_key, "content_hash": d.content_hash,
                       "attempt": d.attempts, "response": response}, session=s)
        return d.status


def _close(session: Session, d: Delivery, status: str, error: str) -> None:
    from analystos.services.notifications import notify

    d.status, d.last_error, d.locked_until, d.next_attempt_at = status, error, None, None
    event = "delivery.dead_lettered" if status == "dead_letter" else "delivery.refused"
    emit(d.workspace_id, event, {"delivery_id": d.id, "destination_id": d.destination_id, "attempts": d.attempts,
                                 "error": error}, session=session)
    audit("system", event, workspace_id=d.workspace_id, target=d.id, decision="deny" if status == "refused" else None,
          reasons=[error], details={"attempts": d.attempts, "destination_id": d.destination_id}, session=session)
    notify(session, d.workspace_id, kind="delivery",
           title=f"Delivery {'refused' if status == 'refused' else 'failed'}: {d.subject_type} {d.subject_id}",
           body=error, link={"type": "delivery", "id": d.id})


@platform_loop
def process_due(*, limit: int = 20, transports: dict[str, Transport] | None = None,
                now: datetime | None = None) -> dict[str, int]:
    """Every scheduler iteration: attempt what is due (and what a crashed sender left leased)."""
    now = now or utcnow()
    with session_scope() as s:
        ids = list(s.scalars(select(Delivery.id).where(or_(
            and_(Delivery.status.in_(["queued", "retrying"]), Delivery.next_attempt_at <= now),
            and_(Delivery.status == "sending", Delivery.locked_until < now))).order_by(Delivery.next_attempt_at).limit(limit)))
    counts: dict[str, int] = {}
    for delivery_id in ids:
        status = attempt(delivery_id, transports=transports, now=now)
        if status:
            counts[status] = counts.get(status, 0) + 1
    return counts
