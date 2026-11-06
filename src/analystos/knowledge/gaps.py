"""Knowledge-gap mining (review P1-3): the questions people keep asking and Ask cannot resolve become one
`knowledge_gap` Note draft per term, in the review queue (knowledge/suggestions.py) — never knowledge by itself.

Per workspace, over the last N days (platform settings `knowledge_gaps`):

* an *outcome* is an Ask turn that was a `clarify`, a decline (`not_answerable`), or an answer that needed several
  SQL repairs (low confidence); a later, near-identical question in the same conversation marks it *rephrased*;
* the unresolved terms of an outcome are the words of its **redacted** question (`llm/redaction.py`) that match no
  table, column, business name, code value or glossary entry (`glossary_scan.ask_terms`);
* a term seen in >= k distinct conversations becomes a draft carrying counts, the outcome mix and example turn
  ids, routed to the owner of the assets those conversations touched where one is named.

Only question text (redacted) and counts are read: never a result row or value. The rules rung (term frequency) is
the whole job with models `off`; a model may only *word* the note (`knowledge_gap_note`), is shown screened
counts and redacted examples, and its output is screened and bounded, else the rules wording stands.
Background code: `mine_all` is a `@platform_loop` that picks workspaces; each is mined as its own tenant.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.core.ids import utcnow
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.knowledge import glossary_scan as gs
from analystos.skills import catalog as cat
from analystos.tenancy.background import as_owner_of, platform_loop

log = get_logger(__name__)
KIND = "knowledge_gap"
PURPOSE = "knowledge_gap_note"
PROMPT = "knowledge_gap_note.v1"
PROCESS = "process:analystos-gap-mining"
ORIGIN = "learning.knowledge_gap"
SUBJECT_PREFIX = "knowledge_gap:"
DECLINE_KINDS = frozenset({"not_answerable"})  # a refusal about the words; access, budget and setup refusals are not gaps
LOW_CONFIDENCE_REPAIRS = 2  # an ad-hoc model answer that took this many SQL repairs
REPHRASE_SIMILARITY = 0.5  # stem overlap of the next question in the same conversation
MAX_EXAMPLES = 3
MAX_TURNS = 5000  # one workspace, one pass (ADR-014: bounded)
NOTE_MAX = 400


@dataclass
class Gap:
    term: str
    conversations: set[str] = field(default_factory=set)
    turns: list[str] = field(default_factory=list)
    outcomes: Counter[str] = field(default_factory=Counter)
    rephrased: int = 0
    examples: list[dict[str, str]] = field(default_factory=list)
    assets: Counter[str] = field(default_factory=Counter)

    @property
    def key(self) -> str:
        return gs.norm(self.term)


# ------------------------------------------------------------------------------------ rules (pure)
def outcome_of(turn: dict[str, Any]) -> str | None:
    """clarify | decline | low_confidence, or None when the turn says nothing about a missing meaning."""
    status = turn.get("status")
    if status == "clarify":
        return "clarify"
    if status == "refused":
        return "decline" if (turn.get("refusal") or {}).get("kind") in DECLINE_KINDS else None
    if status == "answered" and turn.get("answered_by") == "model" and int(turn.get("repairs") or 0) >= LOW_CONFIDENCE_REPAIRS:
        return "low_confidence"
    return None


def _stems(text: str) -> set[str]:
    return gs._stem_set(text)  # noqa: SLF001 - the same stemming the scan's vocabulary uses


def _similar(a: str, b: str) -> bool:
    x, y = _stems(a), _stems(b)
    return bool(x and y) and len(x & y) / len(x | y) >= REPHRASE_SIMILARITY


def redacted_plain(question: str) -> tuple[str, str]:
    """(question for display, question for term extraction): values that identify a person or account are tokens."""
    from analystos.llm.redaction import PLACEHOLDER_PREFIX, redact_question

    redacted = redact_question(question or "").text
    return cat.screen_text(redacted, max_chars=160), re.sub(rf"{PLACEHOLDER_PREFIX}\d+", " ", redacted)


def collect(turns: list[dict[str, Any]], vocab: set[str]) -> dict[str, Gap]:
    """Group the unresolved terms of outcome turns by normalised term. `turns` are dicts of id, thread_id, seq,
    question, status, refusal, answered_by, repairs, assets; only their redacted question text is read."""
    by_thread: dict[str, list[dict[str, Any]]] = {}
    for t in turns:
        by_thread.setdefault(str(t.get("thread_id")), []).append(t)
    gaps: dict[str, Gap] = {}
    for thread, items in by_thread.items():
        items.sort(key=lambda t: (t.get("seq") or 0, str(t.get("id"))))
        thread_assets = Counter(a for t in items for a in t.get("assets") or [])
        for i, t in enumerate(items):
            outcome = outcome_of(t)
            if outcome is None:
                continue
            shown, plain = redacted_plain(str(t.get("question") or ""))
            refusal = t.get("refusal") or {}
            details = refusal.get("details") if isinstance(refusal.get("details"), dict) else refusal
            missing = [m.get("name") for m in (details.get("missing") or []) if isinstance(m, dict)]
            rephrased = i + 1 < len(items) and _similar(plain, redacted_plain(str(items[i + 1].get("question") or ""))[1])
            for term in gs.ask_terms(plain, vocab, missing):
                gap = gaps.setdefault(gs.norm(term), Gap(term))
                if not gap.key:
                    continue
                if str(t["id"]) not in gap.turns:
                    gap.turns.append(str(t["id"]))
                    gap.outcomes[outcome] += 1
                    gap.rephrased += int(rephrased)
                    if len(gap.examples) < MAX_EXAMPLES:
                        gap.examples.append({"turn_id": str(t["id"]), "question": shown})
                gap.conversations.add(thread)
                gap.assets.update(thread_assets)
    gaps.pop("", None)
    return gaps


def qualifying(gaps: dict[str, Gap], min_conversations: int) -> list[Gap]:
    """Terms seen in at least k distinct conversations, most-asked first (ties by term: deterministic)."""
    return sorted((g for g in gaps.values() if len(g.conversations) >= min_conversations),
                  key=lambda g: (-len(g.conversations), g.key))


def rules_note(gap: Gap, window_days: int) -> str:
    mix = ", ".join(f"{n} {name.replace('_', '-')}" for name, n in sorted(gap.outcomes.items()))
    extra = f"; {gap.rephrased} were asked again in other words" if gap.rephrased else ""
    return (f"People asked about “{gap.term}” in {len(gap.conversations)} conversations over the last {window_days} days "
            f"and Ask could not resolve it ({mix}{extra}). No table, column, glossary entry or note in this workspace defines it. "
            "Decide what it means (which records, which condition), then edit this note into a definition or add a glossary term.")


def decided(session: Session, workspace_id: str) -> dict[str, set[str]]:
    """Subjects of this kind already in the queue, by status: pending ones wait, rejected ones are never re-proposed
    (`rejected_values` also holds the wording), approved ones are knowledge now."""
    from analystos.db.models import KnowledgeSuggestion

    out: dict[str, set[str]] = {}
    for subject, status in session.execute(select(KnowledgeSuggestion.subject, KnowledgeSuggestion.status).where(
            KnowledgeSuggestion.workspace_id == workspace_id, KnowledgeSuggestion.kind == KIND)):
        out.setdefault(status, set()).add(subject)
    return out


# ------------------------------------------------------------------------------------ owner routing
def route(session: Session, workspace_id: str, gap: Gap) -> dict[str, Any] | None:
    """The named owner of the source holding most of the assets the conversations touched, else the workspace's;
    business owner first (the person who can say what a business term means). None when nobody is named."""
    from analystos.db.models import Source, SourceAsset, Workspace

    owners: dict[str, Any] | None = None
    where = "workspace"
    if gap.assets:
        sources = Counter()
        for asset_id, n in gap.assets.items():
            asset = session.get(SourceAsset, asset_id)
            if asset is not None:
                sources[asset.source_id] += n
        for source_id, _ in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])):
            src = session.get(Source, source_id)
            if src is not None and src.owners:
                owners, where = src.owners, f"source:{source_id}"
                break
    if not owners:
        ws = session.get(Workspace, workspace_id)
        owners = (ws.owners if ws is not None else None) or None
    for role in ("business", "technical"):
        o = (owners or {}).get(role)
        if isinstance(o, dict) and (o.get("user_id") or o.get("email") or o.get("name")):
            return {"role": role, "name": o.get("name"), "email": o.get("email"), "user_id": o.get("user_id"), "via": where}
    return None


# ------------------------------------------------------------------------------------ optional wording
def _payload(gap: Gap, window_days: int) -> str:
    return json.dumps({"term": cat.screen_text(gap.term, max_chars=80), "conversations": len(gap.conversations),
                       "days": window_days, "outcomes": dict(sorted(gap.outcomes.items())), "rephrased": gap.rephrased,
                       "examples": [e["question"] for e in gap.examples]}, separators=(",", ":"), sort_keys=True)


def validate_wording(data: Any) -> str | None:
    """A model's note text: screened, instruction-like or question-shaped text refused, at most NOTE_MAX characters."""
    raw = str(data.get("note") or "") if isinstance(data, dict) else ""
    if not raw or cat.has_injection(raw):
        return None
    text = cat.screen_text(raw, max_chars=NOTE_MAX)
    return text if text and not cat.is_placeholder_description(text) else None


def _wording(router: Any, workspace_id: str, gap: Gap, window_days: int, fallback: str) -> tuple[str, dict[str, Any]]:
    """(note text, provenance). `always` calls the model; `auto` and `off` keep the rules text and record the avoided call."""
    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.core.errors import AnalystOSError
    from analystos.llm.cache import estimate_tokens
    from analystos.runtime.context import workspace_call_ctx

    rules = {"source": "rule", "rule": "term_frequency"}
    if router is None:
        return fallback, rules
    system = prompt(PROMPT)
    version = prompt_version_id(PROMPT, system)
    payload = _payload(gap, window_days)
    ctx = workspace_call_ctx(workspace_id, agent_id="catalog_steward", prompt_version=version)
    mode = router.mode(PURPOSE)
    if mode != "always" or not router.available(PURPOSE, ctx):
        router.record_skip(PURPOSE, ctx, estimated_tokens=estimate_tokens(system + payload) + 200,
                           reason=f"mode={mode}: rules wording used for a knowledge-gap note")
        return fallback, rules
    try:
        resp = router.complete_json(PURPOSE, system, payload, ctx=ctx, max_tokens=300)
    except AnalystOSError as exc:
        log.info("knowledge-gap wording kept as rules text: %s", exc.message[:120])
        return fallback, rules
    text = validate_wording(getattr(resp, "data", None))
    if text is None:
        return fallback, rules
    return text, {"source": "model", "model": str(getattr(resp, "model", None) or "unknown"), "purpose": PURPOSE,
                  "prompt_version": version, "rule": "term_frequency"}


# ------------------------------------------------------------------------------------ DB side
def _turns(session: Session, workspace_id: str, since: Any) -> list[dict[str, Any]]:
    """Recent turns with just what mining reads. No result rows: `result` and `sql` are not selected."""
    from analystos.db.models import AskTurn

    rows = session.execute(select(AskTurn.id, AskTurn.thread_id, AskTurn.seq, AskTurn.question, AskTurn.status,
                                  AskTurn.refusal, AskTurn.answered_by, AskTurn.provenance)
                           .where(AskTurn.workspace_id == workspace_id, AskTurn.created_at >= since)
                           .order_by(AskTurn.created_at.desc(), AskTurn.id.desc()).limit(MAX_TURNS))
    out = []
    for r in rows:
        prov = r.provenance or {}
        out.append({"id": r.id, "thread_id": r.thread_id, "seq": r.seq, "question": r.question, "status": r.status,
                    "refusal": r.refusal or {}, "answered_by": r.answered_by, "repairs": prov.get("repairs") or 0,
                    "assets": [a["asset_id"] for a in prov.get("assets") or [] if isinstance(a, dict) and a.get("asset_id")]})
    return out


def mine_workspace(session: Session, workspace_id: str, *, router: Any = None, now: Any = None,
                   window_days: int | None = None, min_conversations: int | None = None,
                   max_drafts: int | None = None) -> dict[str, Any]:
    """One workspace's pass; returns counts. Runs in the workspace's tenant binding (the caller's job)."""
    from analystos.knowledge.entries import visible_entries
    from analystos.knowledge.suggestions import field as sfield
    from analystos.knowledge.suggestions import propose, rejected_values
    from analystos.services.platform_settings import get as platform

    cfg = platform().knowledge_gaps
    days = window_days or cfg.window_days
    k = min_conversations or cfg.min_conversations
    cap = max_drafts or cfg.max_drafts_per_run
    now = now or utcnow()
    assets = gs._assets(session, workspace_id)  # noqa: SLF001
    known = gs.known_names(visible_entries(session, workspace_id, kinds=["term", "definition", "metric"]))
    vocab = gs.vocabulary(assets, known)
    gaps = collect(_turns(session, workspace_id, now - timedelta(days=days)), vocab)
    wanted = qualifying(gaps, k)
    seen = decided(session, workspace_id)
    out = {"terms": len(gaps), "qualifying": len(wanted), "proposed": 0, "skipped_pending": 0, "skipped_rejected": 0,
           "skipped_approved": 0}
    for gap in wanted:
        subject = SUBJECT_PREFIX + gap.key
        if subject in seen.get("pending", ()):
            out["skipped_pending"] += 1
            continue
        if subject in seen.get("rejected", ()):
            out["skipped_rejected"] += 1
            continue
        if subject in seen.get("approved", ()):
            out["skipped_approved"] += 1
            continue
        if out["proposed"] >= cap:
            break
        base = rules_note(gap, days)
        if gs.norm(base) in rejected_values(session, workspace_id, subject, "body"):
            out["skipped_rejected"] += 1
            continue
        text, prov = _wording(router, workspace_id, gap, days, base)
        evidence = {"term": gap.term, "conversations": len(gap.conversations), "questions": len(gap.turns),
                    "outcomes": dict(sorted(gap.outcomes.items())), "rephrased": gap.rephrased, "window_days": days,
                    "example_turn_ids": [e["turn_id"] for e in gap.examples], "examples": [e["question"] for e in gap.examples]}
        rule = {"source": "rule", "rule": "term_frequency"}
        owner = route(session, workspace_id, gap)
        fields = {"body": sfield(text, 0.5, **prov), "term": sfield(gap.term, 0.8, **rule),
                  "evidence": sfield(evidence, 1.0, **rule)}
        if owner:
            fields["routed_to"] = sfield(owner, 0.8, source="owners", via=owner["via"])
        row = propose(session, workspace_id, kind=KIND, subject=subject, title=f"Knowledge gap: “{gap.term}”", fields=fields,
                      origin=ORIGIN, proposed_by=f"model:{prov['model']}" if prov["source"] == "model" else PROCESS,
                      batch=f"gaps:{now.date().isoformat()}", path=f"notes/knowledge-gap-{gs.norm(gap.term).replace(' ', '-')}.md")
        out["proposed"] += int(row is not None)
    return out


@platform_loop
def mine_all(now: Any = None) -> dict[str, dict[str, Any]]:
    """Every active workspace, each mined as its own tenant; one failure never stops the others."""
    from analystos.db.models import Workspace
    from analystos.runtime.context import default_router

    with session_scope() as s:
        ids = list(s.scalars(select(Workspace.id).where(Workspace.status == "active", Workspace.deleted_at.is_(None))))
    router = default_router()
    out: dict[str, dict[str, Any]] = {}
    for ws in ids:
        try:
            with as_owner_of("workspace", ws), session_scope() as s:
                res = mine_workspace(s, ws, router=router, now=now)
            if res["proposed"]:
                out[ws] = res
        except Exception:  # noqa: BLE001
            log.exception("knowledge-gap mining for workspace %s failed", ws)
    return out


_last_pass: list[float] = []


def mine_quietly(every_seconds: float = 3600.0) -> dict[str, Any]:
    """From the scheduler loop: at most once per ``every_seconds`` per process; never raises."""
    if _last_pass and time.monotonic() - _last_pass[0] < every_seconds:
        return {}
    _last_pass[:] = [time.monotonic()]
    try:
        return mine_all()
    except Exception:  # noqa: BLE001
        log.exception("knowledge-gap mining failed")
        return {}
