"""Review P1-6: filtered push of the context change feed. Filter matching, the approval bound to (filters, destination),
metadata-only signed payloads, scope, bounded retries ending in a dead letter, and the internal schema-change consumer.
Fake HTTP transport only: no services."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from analystos.context import webhooks
from analystos.context.models import ContextLedgerChange, ContextLedgerEntry, ContextWebhook, ContextWebhookDelivery
from analystos.core.config import get_settings
from analystos.core.errors import Conflict, Forbidden, InvalidInput
from analystos.core.ids import stable_hash, utcnow
from analystos.db.base import session_scope
from analystos.db.models import Approval, User, Workspace, WorkspaceMember
from analystos.governance.approvals import decide
from analystos.services.deliveries import SIGNATURE_HEADER, TIMESTAMP_HEADER, WebhookTransport

WS, OTHER = "ws_cwh", "ws_cwh_other"
SECRET = "whsec-test-only-0123"
URL = "https://hooks.example.com/context"


def _public(host: str, port: int) -> list:
    return [ipaddress.ip_address("93.184.216.34")]


class Recorder:
    def __init__(self, status: int = 200):
        self.status, self.requests = status, []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={"ok": self.status < 400})

    def sender(self) -> WebhookTransport:
        return WebhookTransport(resolver=_public, http_transport=self.transport)


@pytest.fixture
def env(sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.governance import approvals
    from analystos.tenancy import background

    background.forget()
    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t] for t in
                                                                  ("context_webhook", "context_webhook_delivery")])
    monkeypatch.setattr(approvals, "utcnow", lambda: datetime.utcnow())  # SQLite stores expiry without its zone
    monkeypatch.setenv("ANALYSTOS_WEBHOOK_CTX", SECRET)
    s = get_settings()
    for key, value in {"outbound_private_hosts": "", "delivery_max_attempts": 3, "delivery_backoff_seconds": 60.0,
                       "delivery_backoff_max_seconds": 3600.0}.items():
        monkeypatch.setattr(s, key, value)
    with session_scope() as sess:
        for uid in ("u_owner", "u_approver", "u_viewer"):
            sess.add(User(id=uid, email=f"{uid}@x", name=uid, password_hash="x", is_admin=False, active=True, attributes={}))
        sess.flush()
        for ws in (WS, OTHER):
            sess.add(Workspace(id=ws, name=ws, description="", objective="", autonomy_level=3, status="active", settings={},
                               policy_version=1, created_by="u_owner"))
        sess.flush()
        sess.add_all([WorkspaceMember(workspace_id=WS, user_id="u_owner", role="owner"),
                      WorkspaceMember(workspace_id=WS, user_id="u_approver", role="approver"),
                      WorkspaceMember(workspace_id=WS, user_id="u_viewer", role="viewer"),
                      WorkspaceMember(workspace_id=OTHER, user_id="u_owner", role="owner")])
    yield
    background.forget()


def _u(s, uid):
    return s.get(User, uid)


_seq = {"n": 0}


def _change(ws: str = WS, kind: str = "glossary", op: str = "added", trust: str = "workspace", version: int = 1,
            ref: str | None = None, provenance: dict | None = None) -> int:
    """One ledger version and its change row, as the ledger would write them. Returns the change seq."""
    _seq["n"] += 1
    n = _seq["n"]
    ref = ref or f"ref_{n}"
    with session_scope() as s:
        entry = ContextLedgerEntry(id=f"cle_{n}", workspace_id=ws, kind=kind, ref_table="source_asset" if kind == "dataset"
                                   else "context_entry", ref_id=ref, version=version, trust=trust, title="SECRET TITLE",
                                   summary="SECRET SUMMARY", valid_from=utcnow(), source="human", content_hash=f"h{n}",
                                   provenance=provenance or {})
        s.add(entry)
        s.flush()
        change = ContextLedgerChange(workspace_id=ws, entry_id=entry.id, kind=kind, op=op, version=version)
        s.add(change)
        s.flush()
        return change.seq


def _hook(filters: dict | None = None, url: str = URL) -> str:
    with session_scope() as s:
        return webhooks.create(s, _u(s, "u_owner"), WS, name=f"hook {url}{filters}", url=url,
                               secret_ref="env:ANALYSTOS_WEBHOOK_CTX", filters=filters).id


def _approve(hook_id: str) -> None:
    with session_scope() as s:
        hook = s.get(ContextWebhook, hook_id)
        approval = decide(s, hook.approval_id, _u(s, "u_approver"), approve=True, reason="ok")
        webhooks.apply_decision(s, approval)


def _live(filters: dict | None = None) -> str:
    hook_id = _hook(filters)
    _approve(hook_id)
    return hook_id


def _deliveries(hook_id: str | None = None) -> list[ContextWebhookDelivery]:
    with session_scope() as s:
        stmt = select(ContextWebhookDelivery).order_by(ContextWebhookDelivery.seq)
        if hook_id:
            stmt = stmt.where(ContextWebhookDelivery.webhook_id == hook_id)
        rows = list(s.scalars(stmt))
        s.expunge_all()
    return rows


# ------------------------------------------------------------------------------------------------ filters
def test_filters_are_validated_and_canonical(env):
    assert webhooks.normalize_filters(None) == {"kinds": [], "changes": [], "tag_added": [], "schema_changed": False}
    f = webhooks.normalize_filters({"kinds": ["Metric", "dataset"], "changes": ["update"], "tag_added": ["PII"],
                                    "schema_changed": True})
    assert f == {"kinds": ["dataset", "metric"], "changes": ["update"], "tag_added": ["pii"], "schema_changed": True}
    for bad in ({"kinds": ["nope"]}, {"changes": ["added"]}, {"tag_added": ["secret"]}, {"schema_changed": "yes"},
                {"url": "x"}, {"kinds": "metric"}):
        with pytest.raises(InvalidInput):
            webhooks.normalize_filters(bad)


@pytest.mark.parametrize("filters,kind,op,signals,expected", [
    ({}, "glossary", "added", {}, True),
    ({"kinds": ["metric"]}, "glossary", "added", {}, False),
    ({"kinds": ["metric"]}, "metric", "changed", {}, True),
    ({"changes": ["create"]}, "metric", "changed", {}, False),
    ({"changes": ["update", "deprecate"]}, "metric", "retired", {}, True),
    ({"changes": ["deprecate"]}, "metric", "added", {}, False),
    ({"tag_added": ["pii"]}, "dataset", "changed", {"tag_added": ["pii"]}, True),
    ({"tag_added": ["pii"]}, "dataset", "changed", {"tag_added": ["restricted"]}, False),
    ({"tag_added": ["pii"]}, "dataset", "changed", {}, False),
    ({"schema_changed": True}, "dataset", "changed", {"schema_changed": True}, True),
    ({"schema_changed": True}, "dataset", "changed", {"schema_changed": False}, False),
    # the signal filters are alternatives; kinds and changes still narrow
    ({"tag_added": ["pii"], "schema_changed": True}, "dataset", "changed", {"schema_changed": True}, True),
    ({"kinds": ["dataset"], "tag_added": ["pii"]}, "metric", "changed", {"tag_added": ["pii"]}, False),
])
def test_filter_matching(env, filters, kind, op, signals, expected):
    assert webhooks.matches(webhooks.normalize_filters(filters), kind, op, signals) is expected


def _entry(version: int, provenance: dict, kind: str = "dataset") -> ContextLedgerEntry:
    return ContextLedgerEntry(workspace_id=WS, kind=kind, ref_table="source_asset", ref_id="a", version=version,
                              provenance=provenance)


def test_signals_come_from_the_versions_provenance(env):
    old = _entry(1, {"structure": "f1", "tags": []})
    new = _entry(2, {"structure": "f2", "tags": ["pii"]})
    change = ContextLedgerChange(workspace_id=WS, entry_id="x", kind="dataset", op="changed", version=2)
    assert webhooks.signals_of(change, new, old) == {"tag_added": ["pii"], "schema_changed": True}
    same = _entry(2, {"structure": "f1", "tags": []})
    assert webhooks.signals_of(change, same, old) == {"tag_added": [], "schema_changed": False}
    unknown = _entry(1, {})  # a version projected before fingerprints were recorded is not a schema change
    assert webhooks.signals_of(change, new, unknown)["schema_changed"] is False
    created = ContextLedgerChange(workspace_id=WS, entry_id="x", kind="dataset", op="added", version=1)
    assert webhooks.signals_of(created, _entry(1, {"structure": "f", "tags": ["pii"]}), None) == \
        {"tag_added": ["pii"], "schema_changed": False}
    assert webhooks.signals_of(change, _entry(2, {"tags": ["pii"]}, kind="metric"), None)["tag_added"] == []


def test_the_dataset_projection_records_structure_and_sensitive_tags(env, sqlite_db):
    from analystos.context import ledger
    from analystos.db import models
    from analystos.db.models import Source, SourceAsset, SourceColumn

    models.Base.metadata.create_all(sqlite_db.kw["bind"], tables=[models.Base.metadata.tables[t] for t in (
        "context_entry", "verified_query", "knowledge_suggestion", "request", "semantic_relationship_candidate",
        "knowledge_pack", "knowledge_document", "ask_thread", "conversation_summary", "conversation_correction")])
    with session_scope() as s:
        s.add(Source(id="src_c", workspace_id=WS, kind="postgres", name="c", config={}, status="ready", execution_mode="pushdown"))
        s.flush()
        s.add(SourceAsset(id="ast_c", source_id="src_c", workspace_id=WS, schema_name="crm", name="people", source_name="people",
                          kind="table", selected=True, fingerprint="fp1", row_count=1, stats={}))
        s.flush()
        s.add(SourceColumn(asset_id="ast_c", name="email", ordinal=0, data_type="text", tags=["pii"]))
        s.flush()
        ledger.reconcile(s, WS, ["source_asset"])
        entry = s.scalar(select(ContextLedgerEntry).where(ContextLedgerEntry.workspace_id == WS, ContextLedgerEntry.kind == "dataset"))
        assert entry.provenance["structure"] == "fp1" and entry.provenance["tags"] == ["pii"]
        s.get(SourceAsset, "ast_c").fingerprint = "fp2"
        s.flush()
        ledger.reconcile(s, WS, ["source_asset"])
        versions = list(s.scalars(select(ContextLedgerEntry).where(ContextLedgerEntry.kind == "dataset")
                                  .order_by(ContextLedgerEntry.version)))
        assert [v.provenance["structure"] for v in versions] == ["fp1", "fp2"]


# ------------------------------------------------------------------------------------------------ approval
def test_a_subscription_is_authorized_by_an_approval_bound_to_filters_and_destination(env):
    hook_id = _hook({"kinds": ["metric"]})
    with session_scope() as s:
        hook = s.get(ContextWebhook, hook_id)
        approval = s.get(Approval, hook.approval_id)
        assert (hook.status, approval.action, approval.risk_tier) == ("pending", webhooks.APPROVAL_ACTION, "high")
        assert approval.payload_hash == stable_hash(webhooks.authorization_payload(hook))
        assert approval.payload["filters"]["kinds"] == ["metric"] and approval.payload["url"] == URL
        assert not webhooks.is_authorized(hook) and hook.cursor is None


def test_nothing_is_delivered_before_an_approver_decides(env):
    hook_id = _hook()
    _change()
    assert webhooks.scan(hook_id) == 0
    assert _deliveries() == []


def test_the_requester_cannot_approve_their_own_subscription(env):
    hook_id = _hook()
    with session_scope() as s:
        hook = s.get(ContextWebhook, hook_id)
        with pytest.raises(Forbidden):
            decide(s, hook.approval_id, _u(s, "u_owner"), approve=True, reason="me")


def test_a_viewer_cannot_create_a_subscription(env):
    with session_scope() as s, pytest.raises(Forbidden):
        webhooks.create(s, _u(s, "u_viewer"), WS, name="x", url=URL, secret_ref="env:ANALYSTOS_WEBHOOK_CTX")


def test_destinations_pass_the_outbound_guard(env):
    with session_scope() as s:
        for url in ("http://hooks.example.com/x", "https://169.254.169.254/x", "https://localhost/x"):
            with pytest.raises(Exception, match="https|local|public"):
                webhooks.create(s, _u(s, "u_owner"), WS, name=url, url=url, secret_ref="env:ANALYSTOS_WEBHOOK_CTX")
        with pytest.raises(InvalidInput):
            webhooks.create(s, _u(s, "u_owner"), WS, name="bad ref", url=URL, secret_ref="env:SOURCE_PASSWORD")


def test_a_changed_filter_or_destination_needs_a_new_approval_and_refuses_queued_sends(env):
    hook_id = _live({"kinds": ["metric"]})
    _change(kind="metric")
    webhooks.scan(hook_id)
    assert [d.status for d in _deliveries(hook_id)] == ["queued"]
    with session_scope() as s:
        hook = s.get(ContextWebhook, hook_id)
        old_approval = hook.approval_id
        webhooks.update_hook(s, _u(s, "u_owner"), hook, filters={"kinds": ["metric", "glossary"]})
        assert (hook.status, hook.approval_id != old_approval) == ("pending", True)
        assert s.get(Approval, old_approval).status == "invalidated"
        webhooks.update_hook(s, _u(s, "u_owner"), hook, name="renamed")  # a rename needs nothing
        assert hook.status == "pending"
    assert [d.status for d in _deliveries(hook_id)] == ["refused"]
    _approve(hook_id)
    with session_scope() as s:
        before = s.get(ContextWebhook, hook_id).approval_id
        webhooks.update_hook(s, _u(s, "u_owner"), s.get(ContextWebhook, hook_id), name="again")
        assert s.get(ContextWebhook, hook_id).status == "authorized" and s.get(ContextWebhook, hook_id).approval_id == before


def test_a_row_edited_behind_the_api_is_refused_at_send(env):
    hook_id = _live({"kinds": ["metric"]})
    _change(kind="metric")
    webhooks.scan(hook_id)
    with session_scope() as s:
        s.get(ContextWebhook, hook_id).filters = {"kinds": [], "changes": [], "tag_added": [], "schema_changed": False}
    rec = Recorder()
    delivery = _deliveries(hook_id)[0]
    assert webhooks.attempt(delivery.id, transport=rec.sender()) == "refused"
    assert rec.requests == []
    with session_scope() as s:
        assert s.get(ContextWebhook, hook_id).status == "lapsed"


def test_revoking_refuses_what_is_queued(env):
    hook_id = _live()
    _change()
    webhooks.scan(hook_id)
    with session_scope() as s:
        webhooks.revoke(s, _u(s, "u_owner"), s.get(ContextWebhook, hook_id), "no longer needed")
    rec = Recorder()
    assert [d.status for d in _deliveries(hook_id)] == ["refused"]
    assert webhooks.attempt(_deliveries(hook_id)[0].id, transport=rec.sender()) is None and rec.requests == []
    assert webhooks.scan(hook_id) == 0


# ------------------------------------------------------------------------------------------------ delivery
def test_only_changes_after_authorization_are_pushed_and_only_matching_ones(env):
    _change(kind="metric")  # before the approval: never replayed
    hook_id = _live({"kinds": ["metric"], "changes": ["create", "deprecate"]})
    seqs = {"create": _change(kind="metric", op="added"), "update": _change(kind="metric", op="changed", version=2),
            "deprecate": _change(kind="metric", op="retired"), "glossary": _change(kind="glossary", op="added")}
    assert webhooks.scan(hook_id) == 2
    assert [d.seq for d in _deliveries(hook_id)] == [seqs["create"], seqs["deprecate"]]
    assert webhooks.scan(hook_id) == 0  # the cursor moved past everything read
    with session_scope() as s:
        assert s.get(ContextWebhook, hook_id).cursor == seqs["glossary"]


def test_a_delivery_is_signed_and_carries_ledger_metadata_only(env):
    hook_id = _live()
    seq = _change(kind="metric", op="changed", version=3)
    webhooks.scan(hook_id)
    rec = Recorder()
    delivery = _deliveries(hook_id)[0]
    assert webhooks.attempt(delivery.id, transport=rec.sender()) == "delivered"
    (request,) = rec.requests
    body = request.content
    ts, sig = request.headers[TIMESTAMP_HEADER], request.headers[SIGNATURE_HEADER]
    assert sig == "sha256=" + hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert request.headers["Idempotency-Key"] == delivery.idempotency_key and request.headers["host"] == "hooks.example.com"
    data = json.loads(body)
    assert data["change"] == {"seq": seq, "entry_id": delivery.entry_id, "kind": "metric", "change": "update", "version": 3,
                              "at": data["change"]["at"]}
    assert data["workspace_id"] == WS and data["signals"] == {"tag_added": [], "schema_changed": False}
    assert "SECRET" not in body.decode()  # no title, summary or body of the entry
    assert {"title", "summary", "trust", "refs", "provenance"}.isdisjoint(json.dumps(data))
    with session_scope() as s:
        row = s.get(ContextWebhookDelivery, delivery.id)
        assert (row.status, row.attempts) == ("delivered", 1) and row.delivered_at is not None


def test_scope_one_workspace_never_sees_anothers_changes_or_private_entries(env):
    hook_id = _live()
    _change(ws=OTHER, kind="metric")
    _change(trust="personal")
    _change(trust="suggested")
    mine = _change(kind="metric", trust="certified")
    assert webhooks.scan(hook_id) == 1
    assert [d.seq for d in _deliveries(hook_id)] == [mine]
    assert all(d.workspace_id == WS for d in _deliveries())


def test_dataset_signals_ride_the_payload_and_filter(env):
    hook_id = _live({"tag_added": ["pii"], "schema_changed": True})
    base = _change(kind="dataset", op="added", ref="asset1", provenance={"structure": "f1", "tags": []})
    _change(kind="dataset", op="changed", version=2, ref="asset1", provenance={"structure": "f1", "tags": []})  # nothing new
    # versions must be consecutive entries of the same ref: version 2 above compared with version 1 (the first row)
    schema = _change(kind="dataset", op="changed", version=3, ref="asset1", provenance={"structure": "f2", "tags": []})
    tag = _change(kind="dataset", op="changed", version=4, ref="asset1", provenance={"structure": "f2", "tags": ["pii"]})
    assert base < schema < tag
    assert webhooks.scan(hook_id) == 2
    by_seq = {d.seq: d.payload["signals"] for d in _deliveries(hook_id)}
    assert by_seq == {schema: {"tag_added": [], "schema_changed": True}, tag: {"tag_added": ["pii"], "schema_changed": False}}


def test_a_failing_destination_retries_with_backoff_then_dead_letters_and_can_be_redriven(env):
    hook_id = _live()
    _change()
    webhooks.scan(hook_id)
    rec = Recorder(status=503)
    delivery = _deliveries(hook_id)[0]
    t0 = utcnow()
    assert webhooks.attempt(delivery.id, transport=rec.sender(), now=t0) == "retrying"
    with session_scope() as s:
        row = s.get(ContextWebhookDelivery, delivery.id)
        assert row.attempts == 1 and row.last_error
        first_due = row.next_attempt_at
    assert webhooks.attempt(delivery.id, transport=rec.sender(), now=t0) is None  # not due yet
    t1 = t0 + timedelta(hours=2)
    assert webhooks.attempt(delivery.id, transport=rec.sender(), now=t1) == "retrying"
    with session_scope() as s:
        second_due = s.get(ContextWebhookDelivery, delivery.id).next_attempt_at
    assert (second_due.replace(tzinfo=None) - t1.replace(tzinfo=None)) > (first_due.replace(tzinfo=None) - t0.replace(tzinfo=None))
    assert webhooks.attempt(delivery.id, transport=rec.sender(), now=t1 + timedelta(hours=2)) == "dead_letter"
    assert len(rec.requests) == 3
    with session_scope() as s:
        listed = webhooks.list_deliveries(s, _u(s, "u_viewer"), WS, status="dead_letter")
        assert [d["id"] for d in listed] == [delivery.id] and listed[0]["attempts"] == 3
        webhooks.redrive(s, _u(s, "u_owner"), s.get(ContextWebhookDelivery, delivery.id))
    ok = Recorder()
    assert webhooks.attempt(delivery.id, transport=ok.sender(), now=utcnow() + timedelta(seconds=1)) == "delivered"


def test_a_permanent_rejection_is_dead_lettered_at_once(env):
    hook_id = _live()
    _change()
    webhooks.scan(hook_id)
    rec = Recorder(status=400)
    assert webhooks.attempt(_deliveries(hook_id)[0].id, transport=rec.sender()) == "dead_letter"
    assert len(rec.requests) == 1


def test_a_replayed_scan_queues_a_change_once(env):
    hook_id = _live()
    seq = _change()
    webhooks.scan(hook_id)
    with session_scope() as s:
        s.get(ContextWebhook, hook_id).cursor = seq - 1  # a crash before the cursor was saved
    assert webhooks.scan(hook_id) == 0
    assert len(_deliveries(hook_id)) == 1


# ------------------------------------------------------------------------------------------------ internal consumer
def test_a_schema_change_rechecks_the_assets_data_quality_monitors(env, sqlite_db, monkeypatch):
    from analystos.db import models
    from analystos.db.models import Monitor, Source, SourceAsset

    with session_scope() as s:
        s.add(Source(id="src_i", workspace_id=WS, kind="postgres", name="i", config={}, status="ready", execution_mode="pushdown"))
        s.flush()
        s.add(SourceAsset(id="ast_i", source_id="src_i", workspace_id=WS, schema_name="inv", name="stock", source_name="stock",
                          kind="table", selected=True, row_count=1, stats={}, fingerprint="f1"))  # the ledger hook projects it
        s.add_all([Monitor(id="mon_hit", workspace_id=WS, name="hit", kind="data_quality", config={"assets": ["inv.stock"]},
                           enabled=True, created_by="u_owner"),
                   Monitor(id="mon_other", workspace_id=WS, name="other", kind="data_quality", config={"assets": ["inv.other"]},
                           enabled=True, created_by="u_owner"),
                   Monitor(id="mon_off", workspace_id=WS, name="off", kind="data_quality", config={"assets": ["inv.stock"]},
                           enabled=False, created_by="u_owner")])
    assert models.Base.metadata.tables["monitor"] is not None
    assert webhooks.ensure_internal() == 1 and webhooks.ensure_internal() == 0
    with session_scope() as s:
        hook = s.scalar(select(ContextWebhook).where(ContextWebhook.kind == "internal"))
        internal_id = hook.id
        assert webhooks.is_authorized(hook) and hook.cursor is not None
        with pytest.raises(Conflict):
            webhooks.revoke(s, _u(s, "u_owner"), hook)
    seen: list[str] = []
    import analystos.services.monitors as monitors

    monkeypatch.setattr(monitors, "evaluate_monitor", lambda mid, trigger="manual", observation=None: seen.append((mid, trigger)))
    from analystos.db.models import SourceColumn

    with session_scope() as s:
        s.get(SourceAsset, "ast_i").fingerprint = "f2"  # the structure changed: version 2 of the dataset
    with session_scope() as s:  # a sensitive tag arrives: version 3, not a schema change
        s.add(SourceColumn(asset_id="ast_i", name="email", ordinal=0, data_type="text", tags=["pii"]))
        s.get(SourceAsset, "ast_i").description = "stock levels"
    out = webhooks.dispatch_due()
    assert out["queued"] == 1 and out.get("delivered") == 1
    assert seen == [("mon_hit", "schema_changed")]
    assert [d.webhook_id for d in _deliveries()] == [internal_id]


def test_the_background_entry_points_declare_their_tenancy():
    assert webhooks.scan.__owned_by__ == ("context_webhook", "webhook_id")
    assert webhooks.attempt.__owned_by__ == ("context_webhook_delivery", "delivery_id")
    import inspect

    assert "system_scope" in inspect.getsource(inspect.unwrap(webhooks.dispatch_due)) or hasattr(webhooks.dispatch_due, "__wrapped__")


def test_a_creator_who_cannot_see_an_entry_is_not_told_about_it_and_the_cursor_moves_on(env):
    from analystos.db.models import Source, SourceAsset

    with session_scope() as s:
        s.add(Source(id="src_a", workspace_id=WS, kind="postgres", name="a", config={}, status="ready", execution_mode="pushdown"))
        s.flush()
        s.add(SourceAsset(id="ast_a", source_id="src_a", workspace_id=WS, schema_name="hr", name="salaries", source_name="salaries",
                          kind="table", selected=True, row_count=1, stats={}))
        s.add(SourceAsset(id="ast_b", source_id="src_a", workspace_id=WS, schema_name="hr", name="offices", source_name="offices",
                          kind="table", selected=True, row_count=1, stats={}))
    hook_id = _live()
    from analystos.context import retrieval

    real = retrieval.access_for

    def narrowed(session, user, ws):  # noqa: ANN001
        a = real(session, user, ws)
        a.assets = {"hr.offices"}
        return a

    mp = pytest.MonkeyPatch()
    mp.setattr(retrieval, "access_for", narrowed)
    try:
        seen = _change(kind="dataset", op="added", ref="syn_b", provenance={"tags": ["pii"]})
        hidden = _change(kind="dataset", op="added", ref="syn_a", provenance={"tags": ["pii"]})
        _fix_refs({f"cle_{_seq['n'] - 1}": ["hr.offices"], f"cle_{_seq['n']}": ["hr.salaries"]})
        assert webhooks.scan(hook_id) == 1
    finally:
        mp.undo()
    assert [d.seq for d in _deliveries(hook_id)] == [seen]
    with session_scope() as s:
        assert s.get(ContextWebhook, hook_id).cursor == hidden


def _fix_refs(by_id: dict) -> None:
    with session_scope() as s:
        for eid, refs in by_id.items():
            s.get(ContextLedgerEntry, eid).refs = refs


def test_a_creator_who_left_the_workspace_suspends_the_subscription(env):
    hook_id = _live()
    _change()
    with session_scope() as s:
        s.query(WorkspaceMember).filter_by(workspace_id=WS, user_id="u_owner").delete()
    assert webhooks.scan(hook_id) == 0
    with session_scope() as s:
        hook = s.get(ContextWebhook, hook_id)
        assert hook.status == "lapsed" and not webhooks.is_authorized(hook)
    assert _deliveries(hook_id) == []
