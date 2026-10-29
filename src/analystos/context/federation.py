"""Federation subscriptions (spec S03 §6, FR-057, task S03-T7): one workspace follows another workspace's Context
Ledger through its change feed (`context_ledger_change`), and an external subscriber can follow the same feed over
REST (`GET /api/v2/context/feed`).

**The feed.** Ordered by the source's `seq`. Each item is an `upsert` (the full entry, keyed by the source's
`ref_table:ref_id`, with its `version`) or a `remove` (key + version). Only shareable entries travel: trust
certified/approved/workspace (never `personal` or `suggested`), classification `internal` (an entry the source tags
confidential or restricted stays home, because the subscriber's readers are not governed by the source's column
policy), and allowed by the subject's access in the source workspace (`retrieval.Access`: scope, denied columns).
A version that stops being shareable is sent as a `remove` when its previous version was shared, so a subscriber never
keeps something it may no longer see. Federated copies are never re-shared: federation is one hop, so two workspaces
that follow each other do not bounce copies back and forth.

**Delivery and apply.** A sync reads a batch from the source and applies it in a separate transaction; the cursor
advances in the apply transaction. A crash in between re-delivers the batch (at least once), and the apply is
idempotent: an upsert lands only when its version is newer than the stored copy, a remove only when its version is
not older, and removed copies stay as tombstones so a replay cannot resurrect them. After the feed, the sync
re-checks every live copy against the source's current state, which catches access the subject lost without any
ledger change and entries erased by `ledger.forget`.

**Tenancy (ADR-009).** The subscription and its copies belong to the subscriber workspace (RLS through
`workspace_id`). The source may be in another tenant: a sync leaves the subscriber's tenant through `system_scope()`
only to re-bind to the source workspace's tenant (`as_owner_of`), so every read of the source runs under that tenant's
row-level security, and the subject must be visible there (home tenant or a tenant membership) and still be an owner
of the source. Losing that suspends the subscription and retires every copy. `sync_subscription` is tenant work
(`@owned_by`); `sync_due` is the scheduler's platform loop.

**Context is data.** Copies arrive in the subscriber's ledger as `approved`, read-only entries (projected by
`ledger._federated`); their text is validated for shape and length, never interpreted, and is screened like any other
entry when a compile puts it in front of a model. Nothing in a copy changes tools, scope or policy.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from analystos.context.models import ContextFederatedEntry, ContextLedgerChange, ContextLedgerEntry, ContextSubscription
from analystos.core.errors import Conflict, Forbidden, InvalidInput, NotFound
from analystos.core.ids import new_id, utcnow
from analystos.core.logging import get_logger
from analystos.governance.policy import load_in_workspace, scoped_loader
from analystos.tenancy.background import as_owner_of, owned_by, platform_loop

log = get_logger(__name__)

SHARED_TRUSTS = ("certified", "approved", "workspace")
SHARED_CLASSIFICATIONS = ("internal",)
FEDERATED_TABLE = "context_federated_entry"
SUBSCRIBER_ROLE = "owner"  # manages subscriptions in the subscriber workspace
SOURCE_ROLE = "owner"  # sharing a workspace's context outward is its owner's decision
BATCH = 500
MAX_BATCHES = 20  # per sync; the next sync continues from the cursor
MAX_SUBSCRIPTIONS = 20  # per subscriber workspace
MAX_COPIES = 5000  # live copies per subscriber workspace (= retrieval.MAX_CANDIDATES; ADR-014)
SYNC_INTERVAL = timedelta(minutes=10)
DUE_LIMIT = 50  # subscriptions per housekeeping pass
_KEY = re.compile(r"^[a-z_]{1,60}:[^\s]{1,200}$")
_CHUNK = 500


# ------------------------------------------------------------------------------------------- the feed (source side)
def key_of(entry: Any) -> str:
    return f"{entry.ref_table}:{entry.ref_id}"


def shareable(entry: Any, access: Any) -> bool:
    return (entry.trust in SHARED_TRUSTS and entry.classification in SHARED_CLASSIFICATIONS
            and entry.ref_table != FEDERATED_TABLE and access.allows(entry))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def payload(entry: Any) -> dict[str, Any]:
    return {"key": key_of(entry), "entry_id": entry.id, "version": entry.version, "kind": entry.kind,
            "trust": entry.trust, "title": entry.title, "summary": entry.summary, "classification": entry.classification,
            "fingerprint": entry.fingerprint, "aliases": list(entry.aliases or []), "refs": list(entry.refs or []),
            "valid_from": _iso(entry.valid_from), "content_hash": entry.content_hash}


def _was_shared(session: Session, entry: Any, access: Any) -> bool:
    if entry.version <= 1:
        return False
    prev = session.scalar(select(ContextLedgerEntry).where(
        ContextLedgerEntry.workspace_id == entry.workspace_id, ContextLedgerEntry.ref_table == entry.ref_table,
        ContextLedgerEntry.ref_id == entry.ref_id, ContextLedgerEntry.version == entry.version - 1))
    return prev is not None and shareable(prev, access)


def feed(session: Session, access: Any, *, since: int = 0, limit: int = BATCH,
         kinds: list[str] | None = None) -> dict[str, Any]:
    """Federation items after cursor `since`, oldest first, for what `access` may share. `cursor` advances past
    withheld changes too, so a follower never stalls; pass it back as `since`."""
    limit = max(1, min(int(limit), 1000))
    rows = session.execute(select(ContextLedgerChange, ContextLedgerEntry)
                           .join(ContextLedgerEntry, ContextLedgerEntry.id == ContextLedgerChange.entry_id)
                           .where(ContextLedgerChange.workspace_id == access.workspace_id,
                                  ContextLedgerChange.seq > int(since))
                           .order_by(ContextLedgerChange.seq).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items: list[dict[str, Any]] = []
    for change, entry in rows:
        if kinds and entry.kind not in kinds:
            continue
        if change.op != "retired" and shareable(entry, access):
            items.append({"seq": change.seq, "op": "upsert", **payload(entry)})
        elif (change.op == "retired" and shareable(entry, access)) or (
                change.op != "retired" and _was_shared(session, entry, access)):
            items.append({"seq": change.seq, "op": "remove", "key": key_of(entry), "version": change.version})
    return {"items": items, "cursor": rows[-1][0].seq if rows else int(since), "more": more}


def current(session: Session, access: Any, copies: dict[str, int], kinds: list[str] | None = None) -> list[dict[str, Any]]:
    """The source's state for keys a subscriber holds (`{key: version}`): an upsert of the open, shareable version,
    else a remove at the held version (gone, withheld, or no longer shareable)."""
    by_table: dict[str, list[str]] = {}
    for key in copies:
        table, _, ref = key.partition(":")
        by_table.setdefault(table, []).append(ref)
    found: dict[str, Any] = {}
    for table, refs in sorted(by_table.items()):
        for i in range(0, len(refs), _CHUNK):
            for e in session.scalars(select(ContextLedgerEntry).where(
                    ContextLedgerEntry.workspace_id == access.workspace_id, ContextLedgerEntry.ref_table == table,
                    ContextLedgerEntry.ref_id.in_(refs[i:i + _CHUNK]), ContextLedgerEntry.valid_to.is_(None))):
                found[key_of(e)] = e
    out = []
    for key in sorted(copies):
        e = found.get(key)
        if e is not None and shareable(e, access) and (not kinds or e.kind in kinds):
            out.append({"op": "upsert", **payload(e)})
        else:
            out.append({"op": "remove", "key": key, "version": int(copies[key])})
    return out


# ------------------------------------------------------------------------------------------- apply (subscriber side)
def _str_list(value: Any, n: int) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return [" ".join(v.split())[:200] for v in value[:n] if v.strip()]


def clean(item: Any) -> dict[str, Any] | None:
    """A feed item checked for shape and bounds, or None. The text is data: kept, never interpreted."""
    from analystos.context.ledger import CLASSIFICATIONS, KINDS, SUMMARY_CHARS

    if not isinstance(item, dict) or item.get("op") not in ("upsert", "remove"):
        return None
    key, version = item.get("key"), item.get("version")
    if not isinstance(key, str) or not _KEY.match(key) or not isinstance(version, int) or version < 1:
        return None
    if item["op"] == "remove":
        return {"op": "remove", "key": key, "version": version}
    title, summary = item.get("title"), item.get("summary")
    aliases, refs = _str_list(item.get("aliases") or [], 20), _str_list(item.get("refs") or [], 50)
    fingerprint, entry_id = item.get("fingerprint"), item.get("entry_id")
    if (item.get("kind") not in KINDS or item.get("trust") not in SHARED_TRUSTS
            or item.get("classification") not in CLASSIFICATIONS or item.get("classification") not in SHARED_CLASSIFICATIONS
            or not isinstance(title, str) or not title.strip() or not isinstance(summary, str)
            or aliases is None or refs is None or not isinstance(entry_id, str) or not entry_id
            or not (fingerprint is None or isinstance(fingerprint, str))):
        return None
    valid_from = None
    if isinstance(item.get("valid_from"), str):
        try:
            valid_from = datetime.fromisoformat(item["valid_from"].replace("Z", "+00:00"))
        except ValueError:
            return None
        valid_from = valid_from if valid_from.tzinfo else valid_from.replace(tzinfo=UTC)
    return {"op": "upsert", "key": key, "version": version, "entry_id": entry_id[:40], "kind": item["kind"],
            "trust": item["trust"], "title": " ".join(title.split())[:300], "summary": summary[:SUMMARY_CHARS * 4],
            "classification": item["classification"], "fingerprint": fingerprint[:64] if fingerprint else None,
            "aliases": aliases, "refs": refs, "valid_from": valid_from}


def apply(session: Session, sub: ContextSubscription, items: Iterable[dict[str, Any]],
          now: datetime | None = None) -> dict[str, int]:
    """Apply feed items to the subscription's copies. Idempotent: applying the same items again changes nothing."""
    now = now or utcnow()
    items = list(items)
    counts = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
    keys = sorted({i["key"] for i in items if isinstance(i, dict) and isinstance(i.get("key"), str)})
    rows: dict[str, ContextFederatedEntry] = {}
    for i in range(0, len(keys), _CHUNK):
        rows.update({r.source_key: r for r in session.scalars(select(ContextFederatedEntry).where(
            ContextFederatedEntry.subscription_id == sub.id, ContextFederatedEntry.source_key.in_(keys[i:i + _CHUNK])))})
    live = session.scalar(select(func.count()).select_from(ContextFederatedEntry).where(
        ContextFederatedEntry.workspace_id == sub.workspace_id, ContextFederatedEntry.retired.is_(False))) or 0
    for raw in items:
        item = clean(raw)
        if item is None:
            counts["skipped"] += 1
            continue
        row = rows.get(item["key"])
        if item["op"] == "remove":
            if row is None or row.source_version > item["version"] or row.retired:
                if row is not None and row.retired and row.source_version < item["version"]:
                    row.source_version = item["version"]  # the tombstone keeps the newest version it has seen
                counts["skipped"] += 1
                continue
            live -= 1
            row.retired, row.source_version, row.synced_at = True, item["version"], now
            counts["removed"] += 1
            continue
        if row is not None and row.source_version >= item["version"]:
            counts["skipped"] += 1
            continue
        if row is None or row.retired:
            if live >= MAX_COPIES:
                log.warning("context federation: %s holds %d copies; %s not added", sub.workspace_id, live, item["key"])
                counts["skipped"] += 1
                continue
            live += 1
        if row is None:
            row = ContextFederatedEntry(id=new_id("cfe"), workspace_id=sub.workspace_id, subscription_id=sub.id,
                                        source_key=item["key"])
            session.add(row)
            rows[item["key"]] = row
            counts["added"] += 1
        else:
            counts["updated"] += 1
        row.source_entry_id, row.source_version, row.source_trust = item["entry_id"], item["version"], item["trust"]
        row.kind, row.title, row.summary = item["kind"], item["title"], item["summary"]
        row.classification, row.fingerprint = item["classification"], item["fingerprint"]
        row.aliases, row.refs, row.valid_from = item["aliases"], item["refs"], item["valid_from"]
        row.retired, row.synced_at = False, now
    session.flush()
    return counts


# ------------------------------------------------------------------------------------------- reading the source
@contextmanager
def as_source(source_workspace_id: str) -> Iterator[None]:
    """Act as the source workspace's tenant. System scope only to re-bind (ADR-009: cross-tenant work starts in
    `system_scope`); sessions opened inside run under the source tenant's row-level security."""
    from analystos.tenancy import system_scope

    with system_scope(), as_owner_of("workspace", source_workspace_id):
        yield


def source_access(session: Session, subject_id: str, source_workspace_id: str) -> Any:
    """The subject's access to the source (NotFound when they are not visible in its tenant or not a member,
    Forbidden below the source role). Call inside `as_source`."""
    from analystos.context.retrieval import access_for
    from analystos.db.models import User
    from analystos.governance.policy import require_role

    user = session.get(User, subject_id)
    if user is None or not user.active:
        raise NotFound(f"workspace {source_workspace_id} not found")
    require_role(session, user, source_workspace_id, SOURCE_ROLE)
    return access_for(session, user, source_workspace_id)


# ------------------------------------------------------------------------------------------- subscriptions
def as_dict(sub: ContextSubscription, copies: int | None = None) -> dict[str, Any]:
    out = {"id": sub.id, "workspace_id": sub.workspace_id, "source_workspace_id": sub.source_workspace_id,
           "source_tenant_id": sub.source_tenant_id, "subject_id": sub.subject_id, "kinds": list(sub.kinds or []),
           "status": sub.status, "cursor": int(sub.cursor or 0), "last_synced_at": _iso(sub.last_synced_at),
           "last_error": sub.last_error, "created_by": sub.created_by, "created_at": _iso(sub.created_at)}
    if copies is not None:
        out["copies"] = copies
    return out


def _kinds(kinds: list[str] | None) -> list[str]:
    from analystos.context.ledger import KINDS

    out = sorted({k for k in kinds or [] if k})
    unknown = [k for k in out if k not in KINDS]
    if unknown:
        raise InvalidInput(f"unknown context kinds: {', '.join(unknown)} (known: {', '.join(KINDS)})")
    return out


def subscribe(session: Session, user: Any, workspace_id: str, source_workspace_id: str, *,
              kinds: list[str] | None = None) -> ContextSubscription:
    """Subscribe `workspace_id` to `source_workspace_id`'s ledger. The caller owns both workspaces and becomes the
    subject whose access to the source decides what is shared. A revoked subscription is reactivated from cursor 0."""
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role
    from analystos.tenancy.background import workspace_tenant

    kinds = _kinds(kinds)
    if not source_workspace_id or source_workspace_id == workspace_id:
        raise InvalidInput("a workspace subscribes to another workspace")
    require_role(session, user, workspace_id, SUBSCRIBER_ROLE)
    with as_source(source_workspace_id):
        from analystos.db.base import session_scope

        with session_scope() as src:
            source_access(src, user.id, source_workspace_id)
    sub = session.scalar(select(ContextSubscription).where(ContextSubscription.workspace_id == workspace_id,
                                                           ContextSubscription.source_workspace_id == source_workspace_id))
    if sub is not None and sub.status != "revoked":
        raise Conflict("this workspace already follows that workspace", details={"subscription_id": sub.id})
    active = session.scalar(select(func.count()).select_from(ContextSubscription).where(
        ContextSubscription.workspace_id == workspace_id, ContextSubscription.status != "revoked")) or 0
    if active >= MAX_SUBSCRIPTIONS:
        raise InvalidInput(f"a workspace follows at most {MAX_SUBSCRIPTIONS} workspaces")
    if sub is None:
        sub = ContextSubscription(id=new_id("csub"), workspace_id=workspace_id, source_workspace_id=source_workspace_id,
                                  created_by=user.id)
        session.add(sub)
    sub.source_tenant_id = workspace_tenant(source_workspace_id)
    sub.subject_id, sub.kinds, sub.status, sub.cursor, sub.last_error = user.id, kinds, "active", 0, None
    session.flush()
    audit(f"user:{user.id}", "context.subscription.created", workspace_id=workspace_id, target=sub.id, decision="allow",
          details={"source_workspace_id": source_workspace_id, "source_tenant_id": sub.source_tenant_id, "kinds": kinds},
          session=session)
    return sub


@scoped_loader
def load(session: Session, user: Any, subscription_id: str, role: str = SUBSCRIBER_ROLE) -> ContextSubscription:
    """A subscription through its own (subscriber) workspace: 404 unless the caller is a member, 403 below `role`."""
    return load_in_workspace(session, ContextSubscription, subscription_id, user=user, minimum=role, label="subscription")


def list_subscriptions(session: Session, user: Any, workspace_id: str) -> list[dict[str, Any]]:
    from analystos.governance.policy import require_role

    require_role(session, user, workspace_id, "viewer")
    subs = list(session.scalars(select(ContextSubscription).where(ContextSubscription.workspace_id == workspace_id)
                                .order_by(ContextSubscription.created_at, ContextSubscription.id)))
    counts = dict(session.execute(select(ContextFederatedEntry.subscription_id, func.count())
                                  .where(ContextFederatedEntry.workspace_id == workspace_id,
                                         ContextFederatedEntry.retired.is_(False))
                                  .group_by(ContextFederatedEntry.subscription_id)).all())
    return [as_dict(s, int(counts.get(s.id, 0))) for s in subs]


def _retire_copies(session: Session, sub: ContextSubscription, *, delete: bool) -> int:
    """Remove the copies from the subscriber's ledger (ORM writes, so the ledger hook retires their entries)."""
    from analystos.context import ledger

    rows = list(session.scalars(select(ContextFederatedEntry).where(ContextFederatedEntry.subscription_id == sub.id)))
    n = sum(1 for r in rows if not r.retired)
    for r in rows:
        if delete:
            session.delete(r)
        else:
            r.retired = True
    ledger.mark(session, sub.workspace_id, FEDERATED_TABLE)
    session.flush()
    return n


def revoke(session: Session, user: Any, sub: ContextSubscription) -> dict[str, Any]:
    """Stop following (`sub` from `load`): the copies leave the subscriber's ledger (versions retired, history kept)."""
    from analystos.governance.audit import audit

    removed = _retire_copies(session, sub, delete=True)
    sub.status, sub.cursor = "revoked", 0
    audit(f"user:{user.id}", "context.subscription.revoked", workspace_id=sub.workspace_id, target=sub.id,
          decision="allow", details={"source_workspace_id": sub.source_workspace_id, "copies_removed": removed},
          session=session)
    return {**as_dict(sub), "copies_removed": removed}


def reset(session: Session, user: Any, sub: ContextSubscription) -> ContextSubscription:
    """Replay the source's feed from the start (`sub` from `load`; after the subject gained access, or to resume a
    suspended subscription): tombstones go, the cursor returns to 0, and the next sync re-checks the subject's access."""
    from analystos.governance.audit import audit

    if sub.status == "revoked":
        raise Conflict("a revoked subscription is renewed by subscribing again")
    for r in session.scalars(select(ContextFederatedEntry).where(ContextFederatedEntry.subscription_id == sub.id,
                                                                 ContextFederatedEntry.retired.is_(True))):
        session.delete(r)
    sub.status, sub.cursor, sub.last_error = "active", 0, None
    session.flush()
    audit(f"user:{user.id}", "context.subscription.resync", workspace_id=sub.workspace_id, target=sub.id,
          decision="allow", details={"source_workspace_id": sub.source_workspace_id}, session=session)
    return sub


# ------------------------------------------------------------------------------------------- sync
@dataclass
class _Snapshot:
    id: str
    workspace_id: str
    source_workspace_id: str
    subject_id: str
    cursor: int
    kinds: list[str] = field(default_factory=list)


def _suspend(subscription_id: str, exc: Exception) -> dict[str, Any]:
    from analystos.db.base import session_scope
    from analystos.governance.audit import audit

    reason = f"source access lost: {exc}"[:300]
    with session_scope() as s:
        sub = s.get(ContextSubscription, subscription_id)
        if sub is None:
            raise NotFound("subscription not found")
        retired = _retire_copies(s, sub, delete=False)
        sub.status, sub.last_error, sub.last_synced_at = "suspended", reason, utcnow()
        audit("system:context_federation", "context.subscription.suspended", workspace_id=sub.workspace_id,
              target=sub.id, decision="deny", reasons=[reason],
              details={"source_workspace_id": sub.source_workspace_id, "copies_retired": retired}, session=s)
        return {**as_dict(sub), "copies_retired": retired}


def _read(snap: _Snapshot, fn: Any) -> Any:
    from analystos.db.base import session_scope

    with as_source(snap.source_workspace_id), session_scope() as s:
        return fn(s, source_access(s, snap.subject_id, snap.source_workspace_id))


def _apply(snap: _Snapshot, items: list[dict[str, Any]], now: datetime, cursor: int | None) -> dict[str, int] | None:
    from analystos.db.base import session_scope

    with session_scope() as s:
        sub = s.get(ContextSubscription, snap.id)
        if sub is None or sub.status != "active":
            return None  # revoked or suspended meanwhile
        counts = apply(s, sub, items, now)
        if cursor is not None:
            sub.cursor = max(int(sub.cursor or 0), int(cursor))
            snap.cursor = int(sub.cursor)
        sub.last_synced_at, sub.last_error = now, None
        return counts


@owned_by("context_subscription", "subscription_id")
def sync_subscription(subscription_id: str, *, revalidate: bool = True) -> dict[str, Any]:
    """Follow the source's feed from the cursor (at most MAX_BATCHES batches), then re-check every live copy.
    Runs as the subscriber's tenant; each source read runs as the source's tenant (`as_source`)."""
    from analystos.db.base import session_scope

    now = utcnow()
    with session_scope() as s:
        sub = s.get(ContextSubscription, subscription_id)
        if sub is None:
            raise NotFound("subscription not found")
        if sub.status != "active":
            return {**as_dict(sub), "batches": 0}
        snap = _Snapshot(sub.id, sub.workspace_id, sub.source_workspace_id, sub.subject_id, int(sub.cursor or 0),
                         list(sub.kinds or []))
    totals = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}
    batches, more = 0, False
    try:
        while batches < MAX_BATCHES:
            out = _read(snap, lambda s, a: feed(s, a, since=snap.cursor, limit=BATCH, kinds=snap.kinds or None))
            counts = _apply(snap, out["items"], now, out["cursor"])
            if counts is None:
                break
            totals = {k: totals[k] + counts[k] for k in totals}
            batches, more = batches + 1, bool(out["more"])
            if not more:
                break
        if revalidate:
            with session_scope() as s:
                copies = dict(s.execute(select(ContextFederatedEntry.source_key, ContextFederatedEntry.source_version)
                                        .where(ContextFederatedEntry.subscription_id == snap.id,
                                               ContextFederatedEntry.retired.is_(False))).all())
            if copies:
                items = _read(snap, lambda s, a: current(s, a, copies, snap.kinds or None))
                counts = _apply(snap, items, now, None)
                if counts is not None:
                    totals = {k: totals[k] + counts[k] for k in totals}
    except (NotFound, Forbidden) as exc:
        return _suspend(snap.id, exc)
    with session_scope() as s:
        return {**as_dict(s.get(ContextSubscription, snap.id)), "batches": batches, "more": more, **totals}


@owned_by("context_subscription", "subscription_id")
def _record_failure(subscription_id: str, exc: Exception) -> None:
    from analystos.db.base import session_scope

    with session_scope() as s:
        sub = s.get(ContextSubscription, subscription_id)
        if sub is not None:
            sub.last_error, sub.last_synced_at = f"sync failed: {type(exc).__name__}"[:300], utcnow()


@platform_loop
def sync_due(now: datetime | None = None, *, limit: int = DUE_LIMIT) -> dict[str, int]:
    """Scheduler housekeeping: pick active subscriptions not synced within SYNC_INTERVAL (bookkeeping only, in
    system scope); each sync then runs as its subscriber's tenant. A failure is recorded and retried next interval."""
    from analystos.db.base import session_scope

    now = now or utcnow()
    with session_scope() as s:
        ids = list(s.scalars(select(ContextSubscription.id).where(
            ContextSubscription.status == "active",
            or_(ContextSubscription.last_synced_at.is_(None), ContextSubscription.last_synced_at < now - SYNC_INTERVAL))
            .order_by(ContextSubscription.last_synced_at.nulls_first(), ContextSubscription.id).limit(limit)))
    out = {"synced": 0, "suspended": 0, "failed": 0, "changes": 0}
    for sid in ids:
        try:
            r = sync_subscription(sid)
        except Exception as exc:  # noqa: BLE001 - one subscription never stops the others
            log.warning("context federation: sync of %s failed: %s", sid, type(exc).__name__)
            out["failed"] += 1
            try:
                _record_failure(sid, exc)
            except Exception:  # noqa: BLE001
                log.exception("context federation: could not record the failure of %s", sid)
            continue
        out["suspended" if r.get("status") == "suspended" else "synced"] += 1
        out["changes"] += sum(int(r.get(k) or 0) for k in ("added", "updated", "removed"))
    return out
