"""Conversation channels (spec S09-T5, FR-022, FR-054): Microsoft Teams, Slack and email, outbound and inbound.

**Outbound** messages are N-3 deliveries: a `teams` or `slack` destination is an incoming-webhook URL held as
a secret (`url_ref = env:ANALYSTOS_WEBHOOK_<NAME>`, resolved per send) whose host must be one of the platform's
hosts (`channel_teams_hosts`, `channel_slack_hosts`) or an operator-listed private host. The destination is
authorized by a hash-bound approval like every destination, and a rule monitor's alert additionally verifies
the rule's approval (`deliveries._authorize`). The card carries only what an approver saw: severity, title,
the monitor's aggregate message or the report's counts, and a deep link back into the platform; never rows.
Email goes through SMTP, or Microsoft Graph `sendMail` when `delivery_email_transport=graph`.

**Inbound** messages create Request Desk requests (S13) and nothing else: the text is the request, never an
instruction to the platform (CLAUDE.md rule 7). Each binding has its own URL and secret:

* Slack Events API: `X-Slack-Signature: v0=<hex HMAC-SHA256 of "v0:<ts>:<body>">`, `X-Slack-Request-Timestamp`
  within five minutes; `url_verification` is answered with its challenge; bot messages are ignored.
* Teams outgoing webhook: `Authorization: HMAC <base64 HMAC-SHA256 of the body>` keyed by the base64 security
  token; the activity id is the replay key.
* Email (an inbound-parse relay): the platform's own `timestamp.body` HMAC, as for trigger webhooks.

The sender is mapped to a workspace member (email address; a Slack or Teams id in the user's attributes,
`slack_user_id` / `teams_aad_object_id`), else the binding's default requester; otherwise it is refused.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from analystos.core.errors import AnalystOSError, InvalidInput, NotFound, PolicyDenied, Unauthenticated
from analystos.core.ids import canonical_json, new_id
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import User, WorkspaceMember
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import require_role
from analystos.tenancy import system_scope
from analystos.tenancy.background import as_owner_of
from analystos.triggers.models import ChannelBinding, ChannelMessage

log = get_logger(__name__)
INBOUND_KINDS = ("slack", "teams", "email")
CHANNEL_SECRET_REF = re.compile(r"^env:ANALYSTOS_CHANNEL_[A-Z0-9_]+$")
MAX_TEXT = 4000
_IDENTITY_ATTRIBUTE = {"slack": "slack_user_id", "teams": "teams_aad_object_id"}


def _settings():  # noqa: ANN202
    from analystos.core.config import get_settings

    return get_settings()


def _hosts(value: str) -> list[str]:
    return [h.strip().lower() for h in (value or "").split(",") if h.strip()]


# ================================================================================== outbound
def channel_host_allowed(kind: str, host: str) -> bool:
    """A channel URL must point at the platform's own webhook hosts (or one an operator listed)."""
    from analystos.tools import http as outbound

    s = _settings()
    host = (host or "").lower().rstrip(".")
    if host in outbound.split_hosts(s.outbound_private_hosts):
        return True
    for entry in _hosts(s.channel_teams_hosts if kind == "teams" else s.channel_slack_hosts):
        if host == entry.lstrip(".") or (entry.startswith(".") and host.endswith(entry)):
            return True
    return False


def resolve_channel_url(kind: str, url_ref: str) -> str:
    from analystos.connectors.secrets import resolve_secret
    from analystos.services.deliveries import check_webhook_url

    url = check_webhook_url(resolve_secret(url_ref) or "")
    if not channel_host_allowed(kind, urlsplit(url).hostname or ""):
        raise PolicyDenied(f"the {kind} webhook host is not a {kind} host this platform sends to")
    return url


def deep_link(workspace_id: str, subject_type: str, subject_id: str) -> str:
    base = (_settings().web_url or "").rstrip("/")
    return f"{base}/w/{workspace_id}/{'alerts' if subject_type == 'alert' else 'outputs'}/{subject_id}"


def _facts(message: Any) -> tuple[str, str, list[tuple[str, str]], str]:
    """(title, summary, facts, link) from the delivery body: permitted aggregates only."""
    body = message.body
    subject = body.get("subject") or {}
    link = deep_link(body.get("workspace_id", ""), subject.get("type", ""), subject.get("id", ""))
    if subject.get("type") == "alert":
        a = body.get("alert") or {}
        return (f"[{a.get('severity')}] {a.get('title')}", a.get("message") or "",
                [("Severity", str(a.get("severity"))), ("Status", str(a.get("status")))], link)
    r = body.get("report") or {}
    return (str(r.get("title") or "Report"), f"{r.get('insights', 0)} findings, {r.get('metrics', 0)} metrics, "
            f"{r.get('alerts', 0)} open alerts.", [("Report data hash", str(r.get("report_data_hash")))], link)


def teams_card(message: Any) -> dict[str, Any]:
    title, summary, facts, link = _facts(message)
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [{"type": "TextBlock", "size": "Medium", "weight": "Bolder", "text": title[:250], "wrap": True},
                     {"type": "TextBlock", "text": summary[:2000], "wrap": True},
                     {"type": "FactSet", "facts": [{"title": k, "value": v[:200]} for k, v in facts]}],
            "actions": [{"type": "Action.OpenUrl", "title": "Open in the platform", "url": link}]}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def slack_blocks(message: Any) -> dict[str, Any]:
    title, summary, facts, link = _facts(message)
    return {"text": title[:250],
            "blocks": [{"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
                       {"type": "section", "text": {"type": "mrkdwn", "text": summary[:2900]}},
                       {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{k}:* {v[:200]}"} for k, v in facts]},
                       {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text",
                                                                                    "text": "Open in the platform"},
                                                         "url": link}]}]}


class ChannelTransport:
    """POST a Teams card or Slack blocks to the destination's incoming webhook, through the SSRF guard."""

    def __init__(self, kind: str, *, resolver: Callable | None = None, http_transport: Any | None = None):
        self.kind, self.resolver, self.http_transport = kind, resolver, http_transport

    def send(self, config: dict[str, Any], message: Any) -> dict[str, Any]:
        from analystos.tools import http as outbound

        s = _settings()
        url = resolve_channel_url(self.kind, config["url_ref"])
        payload = teams_card(message) if self.kind == "teams" else slack_blocks(message)
        body = canonical_json(payload).encode()
        result = outbound.call(url, method="POST", allowlist=None, private_hosts=outbound.split_hosts(s.outbound_private_hosts),
                               timeout=float(s.delivery_timeout_seconds), max_bytes=64_000, content=body,
                               headers={"Content-Type": "application/json", "User-Agent": "AnalystOS-Delivery/1",
                                        "Idempotency-Key": message.idempotency_key},
                               resolver=self.resolver, transport=self.http_transport,
                               internal_only=bool(getattr(s, "air_gapped", False)))
        return {"status_code": result["status_code"], "bytes": len(body), "channel": self.kind}


class GraphMailTransport:
    """Email through Microsoft Graph `POST /users/{sender}/sendMail` (bearer token from `graph_token_ref`)."""

    def __init__(self, *, resolver: Callable | None = None, http_transport: Any | None = None):
        self.resolver, self.http_transport = resolver, http_transport

    def send(self, config: dict[str, Any], message: Any) -> dict[str, Any]:
        from analystos.connectors.secrets import resolve_secret
        from analystos.core.errors import FeatureUnavailable
        from analystos.tools import http as outbound

        s = _settings()
        token = resolve_secret(s.graph_token_ref) if s.graph_token_ref else None
        if not s.graph_sender or not token:
            raise FeatureUnavailable("Graph email delivery is not configured",
                                     details={"remedy": "set ANALYSTOS_GRAPH_SENDER and ANALYSTOS_GRAPH_TOKEN_REF=env:NAME"})
        mail = {"message": {"subject": message.subject, "body": {"contentType": "Text", "content": message.text},
                            "toRecipients": [{"emailAddress": {"address": r}} for r in config["recipients"]],
                            "internetMessageHeaders": [{"name": "X-AnalystOS-Delivery", "value": message.delivery_id},
                                                       {"name": "X-AnalystOS-Idempotency", "value": message.idempotency_key}],
                            "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": name,
                                             "contentType": mime, "contentBytes": base64.b64encode(content).decode()}
                                            for name, content, mime in message.attachments]},
                "saveToSentItems": False}
        url = f"{s.graph_base_url.rstrip('/')}/users/{s.graph_sender}/sendMail"
        result = outbound.call(url, method="POST", allowlist=None, private_hosts=outbound.split_hosts(s.outbound_private_hosts),
                               timeout=float(s.delivery_timeout_seconds), max_bytes=64_000,
                               content=canonical_json(mail).encode(),
                               headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                               resolver=self.resolver, transport=self.http_transport,
                               internal_only=bool(getattr(s, "air_gapped", False)))
        return {"status_code": result["status_code"], "accepted": len(config["recipients"]), "via": "graph"}


# ================================================================================== inbound
def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


def verify_slack(secret: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None,
                 tolerance: int = 300) -> bool:
    try:
        fresh = abs((now if now is not None else time.time()) - int(timestamp)) <= tolerance
    except ValueError:
        return False
    return fresh and hmac.compare_digest(slack_signature(secret, timestamp, body), signature or "")


def teams_signature(security_token_b64: str, body: bytes) -> str:
    key = base64.b64decode(security_token_b64)
    return "HMAC " + base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()


def verify_teams(security_token_b64: str, body: bytes, authorization: str) -> bool:
    try:
        expected = teams_signature(security_token_b64, body)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, authorization or "")


@dataclass
class Inbound:
    external_id: str
    sender: str
    text: str
    external_key: str | None = None


@dataclass
class InboundResult:
    status_code: int
    body: dict[str, Any]


def _strip_mentions(text: str) -> str:
    text = re.sub(r"<at>.*?</at>", " ", text or "")  # Teams @mention of the bot
    text = re.sub(r"<@[A-Z0-9]+>", " ", text)  # Slack @mention
    return " ".join(re.sub(r"<[^>]+>", " ", text).split())[:MAX_TEXT]


def parse_slack(doc: dict[str, Any]) -> Inbound | None:
    ev = doc.get("event") or {}
    if doc.get("type") != "event_callback" or ev.get("type") not in ("message", "app_mention"):
        return None
    if ev.get("bot_id") or ev.get("subtype"):  # our own posts, edits, joins: never a request (no loops)
        return None
    return Inbound(external_id=str(doc.get("event_id") or ev.get("client_msg_id") or ev.get("ts")),
                   sender=str(ev.get("user") or ""), text=_strip_mentions(ev.get("text") or ""),
                   external_key=str(doc.get("team_id") or ""))


def parse_teams(doc: dict[str, Any]) -> Inbound | None:
    if doc.get("type") != "message":
        return None
    sender = doc.get("from") or {}
    tenant = ((doc.get("channelData") or {}).get("tenant") or {}).get("id")
    return Inbound(external_id=str(doc.get("id") or ""), sender=str(sender.get("aadObjectId") or sender.get("id") or ""),
                   text=_strip_mentions(doc.get("text") or ""), external_key=str(tenant or "") or None)


def parse_email(doc: dict[str, Any]) -> Inbound | None:
    sender = str(doc.get("from") or "").strip().lower()
    m = re.search(r"<([^>]+)>", sender)
    sender = m.group(1) if m else sender
    text = " ".join(p for p in (str(doc.get("subject") or "").strip(), str(doc.get("text") or "").strip()) if p)
    if not doc.get("message_id") or not sender:
        return None
    return Inbound(external_id=str(doc["message_id"]), sender=sender, text=_strip_mentions(text),
                   external_key=str(doc.get("to") or "").strip().lower() or None)


def _requester(session: Session, binding: ChannelBinding, inbound: Inbound) -> User | None:
    members = select(User).join(WorkspaceMember, WorkspaceMember.user_id == User.id).where(
        WorkspaceMember.workspace_id == binding.workspace_id, User.active.is_(True))
    if binding.kind == "email":
        user = session.scalar(members.where(User.email == inbound.sender))
    else:
        attr = _IDENTITY_ATTRIBUTE[binding.kind]
        user = next((u for u in session.scalars(members.limit(5000)) if (u.attributes or {}).get(attr) == inbound.sender), None)
    if user is None and binding.default_requester_id:
        user = session.scalar(members.where(User.id == binding.default_requester_id))
    return user


def _verify(binding: ChannelBinding, secret: str, headers: dict[str, str], body: bytes, now: float | None) -> bool:
    if binding.kind == "slack":
        return verify_slack(secret, headers.get("x-slack-request-timestamp", ""), body, headers.get("x-slack-signature", ""),
                            now=now, tolerance=int(_settings().trigger_skew_seconds))
    if binding.kind == "teams":
        return verify_teams(secret, body, headers.get("authorization", ""))
    from analystos.services.deliveries import verify_signature

    return verify_signature(secret, headers.get("x-analystos-timestamp", ""), body, headers.get("x-analystos-signature", ""),
                            tolerance_seconds=int(_settings().trigger_skew_seconds),
                            now=now if now is not None else time.time())


def _refuse(binding_id: str, reason: str) -> Unauthenticated:
    try:
        with system_scope(), session_scope() as s:
            audit("anonymous", "channel.auth_failed", target=binding_id[:40], decision="deny", reasons=[reason], session=s)
    except Exception:
        log.exception("could not audit a refused channel message")
    return Unauthenticated("the message is not signed for this channel")


def accept_inbound(kind: str, binding_id: str, headers: dict[str, str], body: bytes, *,
                   now: float | None = None, submit: Callable[..., dict[str, Any]] | None = None) -> InboundResult:
    """Verify one inbound platform message and turn it into a Desk request (or answer a Slack URL check)."""
    from analystos.connectors.secrets import resolve_secret

    h = {k.lower(): v for k, v in headers.items()}
    if len(body) > int(_settings().trigger_max_body_bytes):
        raise InvalidInput("the message is too large")
    with system_scope(), session_scope() as s:
        binding = s.get(ChannelBinding, binding_id)
        usable = binding is not None and binding.enabled and binding.kind == kind
        secret_ref = binding.secret_ref if usable else None
        if usable:
            s.expunge(binding)
    if not usable:
        raise _refuse(binding_id, "unknown, disabled or other-kind channel binding")
    try:
        secret = resolve_secret(secret_ref)
    except AnalystOSError:
        secret = None
    if not secret or not _verify(binding, secret, h, body, now):
        raise _refuse(binding_id, "bad signature or stale timestamp")
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        raise InvalidInput("the message must be JSON") from None
    if kind == "slack" and doc.get("type") == "url_verification":
        return InboundResult(200, {"challenge": str(doc.get("challenge") or "")})
    inbound = {"slack": parse_slack, "teams": parse_teams, "email": parse_email}[kind](doc if isinstance(doc, dict) else {})
    if inbound is None or not inbound.external_id:
        return InboundResult(200, {"status": "ignored"})
    if binding.external_key and inbound.external_key and inbound.external_key.lower() != binding.external_key.lower():
        raise _refuse(binding_id, f"message from another {kind} workspace or mailbox")
    with as_owner_of("channel_binding", binding_id):
        return _record(binding_id, kind, inbound, submit)


def _record(binding_id: str, kind: str, inbound: Inbound, submit: Callable[..., dict[str, Any]] | None) -> InboundResult:
    from analystos.desk import service as desk

    with session_scope() as s:
        binding = s.get(ChannelBinding, binding_id)
        if s.scalar(select(ChannelMessage.id).where(ChannelMessage.binding_id == binding_id,
                                                    ChannelMessage.external_id == inbound.external_id[:200])):
            return InboundResult(200, {"status": "duplicate"})
        user = _requester(s, binding, inbound)
        msg = ChannelMessage(id=new_id("chm"), workspace_id=binding.workspace_id, binding_id=binding_id,
                             external_id=inbound.external_id[:200], sender=inbound.sender[:320] or "unknown",
                             status="accepted", reason=None)
        if user is None:
            msg.status, msg.reason = "refused", "the sender is not a member of this workspace and the binding has no default requester"
        elif len(inbound.text) < 3:
            msg.status, msg.reason = "refused", "empty message"
        try:
            with s.begin_nested():
                s.add(msg)
        except IntegrityError:
            return InboundResult(200, {"status": "duplicate"})
        audit(f"channel:{binding_id}", "channel.message_received", workspace_id=binding.workspace_id, target=msg.id,
              decision="allow" if msg.status == "accepted" else "deny", reasons=[msg.reason] if msg.reason else [],
              details={"kind": kind, "external_id": msg.external_id}, session=s)
        if msg.status != "accepted":
            return InboundResult(200, _reply(kind, f"Not accepted: {msg.reason}."))
        workspace_id, msg_id = binding.workspace_id, msg.id
        s.expunge(user)
    try:  # triage only: the Desk's own flow processes it; a webhook answers within the platform's timeout
        view = (submit or desk.submit)(user, workspace_id, inbound.text, channel=kind, process=False)
    except AnalystOSError as exc:
        with session_scope() as s:
            m = s.get(ChannelMessage, msg_id)
            m.status, m.reason = "refused", exc.message
        return InboundResult(200, _reply(kind, f"Not accepted: {exc.message}"))
    with session_scope() as s:
        s.get(ChannelMessage, msg_id).request_id = view.get("id")
        emit(workspace_id, "channel.request_created", {"binding_id": binding_id, "request_id": view.get("id"), "kind": kind},
             session=s)
    return InboundResult(202 if kind == "email" else 200,
                         {**_reply(kind, f"Request {view.get('id')} received; you will hear back here."),
                          "request_id": view.get("id")})


def _reply(kind: str, text: str) -> dict[str, Any]:
    return {"type": "message", "text": text} if kind == "teams" else {"text": text}


# ================================================================================== bindings
def create_binding(session: Session, user: User, workspace_id: str, *, kind: str, name: str, secret_ref: str,
                   external_key: str | None = None, default_requester_id: str | None = None) -> ChannelBinding:
    require_role(session, user, workspace_id, "editor")
    if kind not in INBOUND_KINDS:
        raise InvalidInput(f"kind must be one of {list(INBOUND_KINDS)}")
    if not CHANNEL_SECRET_REF.match(secret_ref or ""):
        raise InvalidInput("a channel binding needs secret_ref = env:ANALYSTOS_CHANNEL_<NAME>")
    name = (name or "").strip()
    if not name:
        raise InvalidInput("a channel binding needs a name")
    if default_requester_id is not None and session.scalar(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == default_requester_id)) is None:
        raise NotFound("the default requester is not a member of this workspace")
    b = ChannelBinding(id=new_id("chb"), workspace_id=workspace_id, kind=kind, name=name[:200],
                       external_key=(external_key or None), secret_ref=secret_ref, default_requester_id=default_requester_id,
                       enabled=True, created_by=user.id)
    session.add(b)
    session.flush()
    audit(f"user:{user.id}", "channel.binding_created", workspace_id=workspace_id, target=b.id,
          details={"kind": kind, "default_requester_id": default_requester_id}, session=session)
    return b


def binding_out(b: ChannelBinding) -> dict[str, Any]:
    return {"id": b.id, "workspace_id": b.workspace_id, "kind": b.kind, "name": b.name, "external_key": b.external_key,
            "secret_ref": b.secret_ref, "default_requester_id": b.default_requester_id, "enabled": b.enabled,
            "inbound_url": f"/v2/channels/{b.kind}/{b.id}"}
