"""Tamper-evident audit log (spec S01-T5, ADR-009).

Every ``audit_event`` row is sealed into one SHA-256 hash chain per tenant (``platform`` for rows without a
tenant): ``hash = sha256(prev_hash + "\\n" + canonical_json(event))``, starting from 64 zeros. A changed,
deleted or reordered sealed row breaks the chain, and ``verify`` names the first bad link.

Sealing is asynchronous on purpose: chaining inside every audited transaction would serialize all of a
tenant's writes on one lock. The scheduler seals recent rows every iteration and the nightly job seals
everything, verifies every chain and exports new links as gzipped JSONL files (write-once). Each export
and each nightly verification is itself audited, so the chain anchors its own exports.

Offline check of an export file: for each line, ``sha256(prev_hash + "\\n" + canonical_json(event))``
equals ``hash`` (canonical JSON: sorted keys, ``(",", ":")`` separators), and each ``prev_hash`` equals the
previous line's ``hash``.
"""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import exists, func, select, text
from sqlalchemy.orm import Session

from analystos.core.errors import InvalidInput
from analystos.core.ids import canonical_json, utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import AuditChain, AuditEvent, Tenant
from analystos.governance.audit import audit
from analystos.tenancy import system_scope, tenant_scope
from analystos.tenancy.background import platform_loop

log = get_logger(__name__)
GENESIS = "0" * 64
PLATFORM_CHAIN = "platform"
# Recent rows are sealed every scheduler iteration; a transaction that commits an audit row later than
# this after the chain's head is sealed by the nightly full pass.
SEAL_WINDOW = timedelta(hours=1)
BATCH = 2000  # rows per sealing transaction (ADR-014: bounded memory and lock time)
EXPORT_ROWS_PER_FILE = 100_000
NIGHTLY_EVERY = timedelta(hours=23)
_TENANT = AuditEvent.__table__.c.tenant_id
_SAFE_KEY = re.compile(r"[A-Za-z0-9_-]{1,40}")


def chain_key(tenant_id: str | None) -> str:
    return tenant_id or PLATFORM_CHAIN


def event_body(e: AuditEvent, tenant_id: str | None) -> dict[str, Any]:
    """The sealed content of an event: every column, timestamps in UTC."""
    created = e.created_at.astimezone(UTC) if e.created_at.tzinfo else e.created_at.replace(tzinfo=UTC)
    return {"id": e.id, "tenant_id": tenant_id, "workspace_id": e.workspace_id, "run_id": e.run_id, "actor": e.actor,
            "action": e.action, "target": e.target, "decision": e.decision, "reasons": e.reasons,
            "details": e.details, "created_at": created.isoformat()}


def link_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}\n{canonical_json(body)}".encode()).hexdigest()


@contextlib.contextmanager
def _as_chain(tenant_id: str | None) -> Iterator[None]:
    """Read a tenant's chain as that tenant (least privilege); the platform chain needs system scope."""
    scope = tenant_scope(tenant_id) if tenant_id else system_scope()
    with scope:
        yield


def _in_chain(tenant_id: str | None):  # noqa: ANN202
    return _TENANT.is_(None) if tenant_id is None else _TENANT == tenant_id  # noqa: SIM300


def _locked(s: Session, key: str) -> bool:
    if s.get_bind().dialect.name != "postgresql":
        return True
    return bool(s.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext(:k))"), {"k": f"audit_chain:{key}"}))


# --------------------------------------------------------------------------------------------- seal
def seal_chain(tenant_id: str | None, *, full: bool = False, batch: int = BATCH) -> dict[str, Any]:
    """Append up to ``batch`` unsealed events of one chain, oldest id first (one short transaction).
    Without ``full`` only rows created within ``SEAL_WINDOW`` before the head are considered."""
    key = chain_key(tenant_id)
    with system_scope(), session_scope() as s:  # only the sealer writes the chain (the app role cannot)
        if not _locked(s, key):
            return {"chain": key, "sealed": 0, "busy": True}
        head = s.execute(select(AuditChain.seq, AuditChain.hash, AuditChain.event_id)
                         .where(AuditChain.chain == key).order_by(AuditChain.seq.desc()).limit(1)).first()
        seq, prev = (head.seq, head.hash) if head else (0, GENESIS)
        q = select(AuditEvent, _TENANT).where(_in_chain(tenant_id), ~exists().where(AuditChain.event_id == AuditEvent.id))
        if head is not None and not full:
            since = s.scalar(select(AuditEvent.created_at).where(AuditEvent.id == head.event_id))
            if since is not None:
                q = q.where(AuditEvent.created_at >= since - SEAL_WINDOW)
        rows = s.execute(q.order_by(AuditEvent.id).limit(batch)).all()
        for event, tenant in rows:
            seq += 1
            h = link_hash(prev, event_body(event, tenant))
            s.add(AuditChain(event_id=event.id, chain=key, seq=seq, prev_hash=prev, hash=h))
            prev = h
    return {"chain": key, "sealed": len(rows), "head": {"seq": seq, "hash": prev}}


def seal(tenant_id: str | None, *, full: bool = False) -> dict[str, Any]:
    """Seal everything pending in one chain, ``BATCH`` rows per transaction."""
    total, out = 0, {}
    while True:
        out = seal_chain(tenant_id, full=full)
        total += out["sealed"]
        if out.get("busy") or out["sealed"] < BATCH:
            return {**out, "sealed": total}


def chains() -> list[str | None]:
    with system_scope(), session_scope() as s:
        return [None, *s.scalars(select(Tenant.id).order_by(Tenant.id))]


@platform_loop
def seal_all(*, full: bool = False) -> dict[str, int]:
    """Every scheduler iteration (recent rows) and nightly (``full``). Never raises."""
    out: dict[str, int] = {}
    for tenant_id in chains():
        try:
            sealed = seal(tenant_id, full=full)["sealed"]
        except Exception:  # noqa: BLE001 - one chain must not stop the others
            log.exception("audit sealing failed for chain %s", chain_key(tenant_id))
            continue
        if sealed:
            out[chain_key(tenant_id)] = sealed
    return out


# ------------------------------------------------------------------------------------------- verify
def verify(tenant_id: str | None) -> dict[str, Any]:
    """Walk one chain in order and recompute every link (streamed; memory stays flat).

    ``first_bad.reason``: ``bad_genesis`` | ``gap`` (a link is missing) | ``broken_link`` (prev_hash differs from
    the previous hash) | ``missing_event`` | ``wrong_chain`` | ``altered`` (the event differs from what was sealed).
    A chain whose oldest links were pruned by retention starts at its first remaining link (``anchored_at``)."""
    key = chain_key(tenant_id)
    checked, prev, expected, first_seq = 0, None, None, None
    bad: dict[str, Any] | None = None
    with _as_chain(tenant_id), session_scope() as s:
        rows = s.execute(select(AuditChain, AuditEvent, _TENANT)
                         .join(AuditEvent, AuditEvent.id == AuditChain.event_id, isouter=True)
                         .where(AuditChain.chain == key).order_by(AuditChain.seq)
                         .execution_options(yield_per=BATCH))
        for link, event, tenant in rows:
            reason = None
            if expected is None:
                first_seq = link.seq
                if link.seq == 1 and link.prev_hash != GENESIS:
                    reason = "bad_genesis"
            elif link.seq != expected:
                reason = "gap"
            elif link.prev_hash != prev:
                reason = "broken_link"
            if reason is None:
                if event is None:
                    reason = "missing_event"
                elif tenant != tenant_id:
                    reason = "wrong_chain"
                elif link_hash(link.prev_hash, event_body(event, tenant)) != link.hash:
                    reason = "altered"
            if reason:
                bad = {"seq": link.seq, "event_id": link.event_id, "reason": reason}
                break
            checked, prev, expected = checked + 1, link.hash, link.seq + 1
        stale = s.scalar(select(func.count()).select_from(AuditEvent).where(
            _in_chain(tenant_id), AuditEvent.created_at < utcnow() - SEAL_WINDOW,
            ~exists().where(AuditChain.event_id == AuditEvent.id)))
    return {"chain": key, "ok": bad is None, "checked": checked,
            "head": {"seq": expected - 1, "hash": prev} if expected else None,
            "anchored_at": first_seq if first_seq not in (None, 1) else None,
            "first_bad": bad, "unsealed_stale": int(stale or 0)}


def verify_and_record(tenant_id: str | None, actor: str) -> dict[str, Any]:
    """Seal what is pending, verify, and audit the result in the chain itself."""
    seal(tenant_id)
    result = verify(tenant_id)
    with _as_chain(tenant_id):
        audit(actor, "audit.verified" if result["ok"] else "audit.chain_broken", target=result["chain"],
              decision="allow" if result["ok"] else "deny", details=result)
    if not result["ok"]:
        log.error("audit chain %s broken: %s", result["chain"], result["first_bad"])
    return result


# ------------------------------------------------------------------------------------------- export
def page(tenant_id: str | None, *, after_seq: int = 0, limit: int = 500, action: str | None = None) -> tuple[list[dict], int]:
    """Sealed links after ``after_seq`` with their events (the API's JSONL page) and the next cursor."""
    key = chain_key(tenant_id)
    limit = max(1, min(limit, 10_000))
    with _as_chain(tenant_id), session_scope() as s:
        q = (select(AuditChain, AuditEvent, _TENANT).join(AuditEvent, AuditEvent.id == AuditChain.event_id)
             .where(AuditChain.chain == key, AuditChain.seq > after_seq))
        if action:
            q = q.where(AuditEvent.action == action)
        rows = s.execute(q.order_by(AuditChain.seq).limit(limit)).all()
        items = [{"seq": c.seq, "prev_hash": c.prev_hash, "hash": c.hash, "event": event_body(e, t)} for c, e, t in rows]
    return items, (items[-1]["seq"] if items else after_seq)


def _last_exported(tenant_id: str | None) -> int:
    with _as_chain(tenant_id), session_scope() as s:
        details = s.scalar(select(AuditEvent.details).where(AuditEvent.action == "audit.exported", _in_chain(tenant_id),
                                                            AuditEvent.target == chain_key(tenant_id))
                           .order_by(AuditEvent.id.desc()).limit(1))
    return int((details or {}).get("to_seq") or 0)


def export(tenant_id: str | None, out_dir: Path, *, actor: str = "system:audit",
           max_rows: int = EXPORT_ROWS_PER_FILE) -> dict[str, Any]:
    """Write the links sealed since the last export to ``<out_dir>/<chain>/<chain>-<from>-<to>.jsonl.gz``
    (never overwritten) and audit the export with the file's SHA-256 and the head it covers."""
    key = chain_key(tenant_id)
    if not _SAFE_KEY.fullmatch(key):  # the key names a directory and a file
        raise InvalidInput(f"not a chain id: {key!r}")
    seal(tenant_id)
    after = _last_exported(tenant_id)
    folder = Path(out_dir) / key
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{key}-{after:012d}.{os.getpid()}.tmp"
    digest, first, last, head, n = hashlib.sha256(), None, None, None, 0
    with _as_chain(tenant_id), session_scope() as s, open(tmp, "wb") as raw:
        rows = s.execute(select(AuditChain, AuditEvent, _TENANT).join(AuditEvent, AuditEvent.id == AuditChain.event_id)
                         .where(AuditChain.chain == key, AuditChain.seq > after).order_by(AuditChain.seq)
                         .limit(max_rows).execution_options(yield_per=BATCH))
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            for c, e, t in rows:
                line = (canonical_json({"seq": c.seq, "prev_hash": c.prev_hash, "hash": c.hash,
                                        "event": event_body(e, t)}) + "\n").encode()
                gz.write(line)
                first = c.seq if first is None else first
                last, head, n = c.seq, c.hash, n + 1
    if not n:
        tmp.unlink(missing_ok=True)
        return {"chain": key, "exported": 0, "after_seq": after}
    final = folder / f"{key}-{first:012d}-{last:012d}.jsonl.gz"
    with open(tmp, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    try:
        os.link(tmp, final)  # fails if the file exists: an export is never overwritten
    finally:
        tmp.unlink(missing_ok=True)
    result = {"chain": key, "exported": n, "from_seq": first, "to_seq": last, "head_hash": head,
              "file": str(final), "sha256": digest.hexdigest()}
    with _as_chain(tenant_id):
        audit(actor, "audit.exported", target=key, decision="allow", details=result)
    return result


# ------------------------------------------------------------------------------------------ nightly
@platform_loop
def maybe_run_nightly(*, now: datetime | None = None, out_dir: Path | None = None) -> dict[str, Any] | None:
    """Once a day across scheduler processes: seal everything, verify every chain, export new links.
    The claim is one short transaction (an ``audit.nightly`` marker under an advisory lock); the work runs
    after it, so no transaction stays open for the whole pass."""
    from analystos.core.config import get_settings

    now = now or utcnow()
    with session_scope() as s:
        if not _locked(s, "nightly"):
            return None
        last = s.scalar(select(func.max(AuditEvent.created_at)).where(AuditEvent.action == "audit.nightly"))
        if last is not None and now - last < NIGHTLY_EVERY:
            return None
        audit("scheduler", "audit.nightly", decision="allow", details={"started_at": now.isoformat()}, session=s)
    out_dir = out_dir or get_settings().audit_export_dir
    summary: dict[str, Any] = {"sealed": seal_all(full=True), "broken": [], "exported": {}}
    for tenant_id in chains():
        try:
            result = verify_and_record(tenant_id, "scheduler")
            if not result["ok"]:
                summary["broken"].append(result["chain"])
            if out_dir:
                exported = export(tenant_id, Path(out_dir), actor="scheduler")
                if exported["exported"]:
                    summary["exported"][exported["chain"]] = exported["exported"]
        except Exception:  # noqa: BLE001 - one chain must not stop the others
            log.exception("audit nightly failed for chain %s", chain_key(tenant_id))
            summary["broken"].append(chain_key(tenant_id))
    audit("scheduler", "audit.nightly_done", decision="deny" if summary["broken"] else "allow", details=summary)
    log.info("audit nightly: %s", summary)
    return summary
