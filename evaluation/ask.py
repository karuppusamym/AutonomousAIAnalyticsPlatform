"""Ask accuracy benchmark (P4-V02, spec v3 §10): execution-match accuracy and refusal correctness of
Ask per domain, over the V01 seeded datasets (evaluation.datasets) with a labelled question set per
domain (evaluation/ask_questions/*.yaml; how it was written: the README there).

Each question has an expected outcome class: `answer` (with a gold SQL), `needs_input`, `clarify` or
`decline` (with a reason: out_of_scope, write, restricted). One tier runs every question through the
real Ask path (`services.ask.ask_in_thread`: authorization, budget, the verified-query registry, the
`ask_route` / `clarify_needed` decisions, generation and repair when a model may answer, the gateway)
on a workspace whose data was uploaded, discovered and staged like a customer's. Gold SQL runs through
the same `QueryGateway.execute` under the same scope, so both sides see the same governed data.

Tiers differ only in who may write SQL:
  off   no provider key: the registry and the rules (pack distributions and the catalog-built simple
        shapes of agents/ask_rules) answer, nothing else (the no-model floor);
  fake  a fake transport that answers with the gold SQL (and, for decline items, the careless SQL in
        `probe_sql`): a CI smoke of the harness, the gateway refusals and the scoring, not a measure;
  live  the configured provider (OPENROUTER_API_KEY): the measurement the pilot threshold applies to.

Execution match: same row count, every gold column matched by a distinct answer column with the same
values, and the rows equal as a multiset over those columns (order-insensitive; numbers within
rel 1e-4 / abs 1e-6; timestamps compared as naive ISO; extra answer columns allowed). A result that
differs only in scale (a percentage for a fraction) is a mismatch, as in BIRD/Spider EX.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from evaluation.datasets import build

QUESTIONS_DIR = Path(__file__).resolve().parent / "ask_questions"
DOMAINS = ("itsm", "sales", "finance")
OUTCOMES = ("answer", "needs_input", "clarify", "decline")
REFUSAL_OUTCOMES = OUTCOMES[1:]
DECLINE_REASONS = ("out_of_scope", "write", "restricted")
# Refusal kinds (services/ask.REFUSALS) that decline *because* governance or scope said no. Any other
# refusal of a decline item (no_model, no_api_key, failed, ...) still declines, but not for the right reason.
# write_refused: the SQL agent refuses a data-changing question by rule before any SQL exists (reason write, by
# rules), which is stricter than the gateway refusing the write statement.
GOVERNED_KINDS = frozenset({"sql_rejected", "policy_denied", "no_scope", "write_refused"})
# Refusal kinds that mean "no model could write SQL" (services/ask.MODEL_REFUSALS, plus the legacy no_model).
NO_MODEL_KINDS = frozenset({"no_model", "mode_off", "no_api_key", "provider_cooldown", "policy_blocked", "residency_blocked",
                            "approval_required", "model_budget", "cap_reached", "context_over_budget", "invalid_output"})
REL_TOL, ABS_TOL = 1e-4, 1e-6
BENCH_ACTOR = "benchmark:ask"
# Proposed only; the owner sets the pilot threshold before the pilot (tracker P4-V02). Not enforced.
PROPOSED_THRESHOLDS = {
    "execution_accuracy_min": 0.85, "execution_accuracy_per_domain_min": 0.80, "confident_wrong_max_share": 0.05,
    "restricted_leaks_max": 0, "governed_decline_recall_min": 1.0, "needs_input_recall_min": 0.80, "clarify_recall_min": 0.60,
}


# ----------------------------------------------------------------------------- question sets
@dataclass(frozen=True)
class Question:
    id: str
    domain: str
    q: str
    expect: str
    reason: str | None = None
    tags: tuple[str, ...] = ()
    gold: str | None = None
    probe_sql: str | None = None


@dataclass
class QuestionSet:
    domain: str
    restricted_column: str
    registry: list[dict[str, Any]]
    questions: list[Question]


def load_set(domain: str) -> QuestionSet:
    raw = yaml.safe_load((QUESTIONS_DIR / f"{domain}.yaml").read_text())
    questions = [Question(id=q["id"], domain=domain, q=str(q["q"]), expect=q["expect"], reason=q.get("reason"),
                          tags=tuple(q.get("tags") or ()), gold=q.get("gold"), probe_sql=q.get("probe_sql"))
                 for q in raw["questions"]]
    return QuestionSet(domain=raw["domain"], restricted_column=raw["restricted_column"], registry=list(raw.get("registry") or []),
                       questions=questions)


def problems(qs: QuestionSet) -> list[str]:
    """Structural checks of a question set (the unit tests run them; no services)."""
    out = []
    ids = [q.id for q in qs.questions]
    if len(ids) != len(set(ids)):
        out.append(f"{qs.domain}: duplicate question ids")
    if len({q.q.strip().lower() for q in qs.questions}) != len(ids):
        out.append(f"{qs.domain}: duplicate question text")
    if len(ids) < 40:
        out.append(f"{qs.domain}: {len(ids)} questions (< 40)")
    counts = Counter(q.expect for q in qs.questions)
    for kind in OUTCOMES:
        if not counts.get(kind):
            out.append(f"{qs.domain}: no {kind} question")
    for q in qs.questions:
        if q.expect not in OUTCOMES:
            out.append(f"{q.id}: unknown expect {q.expect}")
        if (q.expect == "answer") != bool(q.gold):
            out.append(f"{q.id}: gold SQL is required for answer items and only for them")
        if q.expect == "decline" and (q.reason not in DECLINE_REASONS or not q.probe_sql):
            out.append(f"{q.id}: a decline needs a reason in {DECLINE_REASONS} and a probe_sql")
        if q.expect == "decline" and q.reason == "restricted" and qs.restricted_column not in (q.probe_sql or ""):
            out.append(f"{q.id}: a restricted probe must read {qs.restricted_column}")
        if q.expect == "answer" and qs.restricted_column in (q.gold or ""):
            out.append(f"{q.id}: gold SQL reads the restricted column")
    for r in {r for q in qs.questions if q.expect == "decline" for r in [q.reason]} ^ set(DECLINE_REASONS):
        out.append(f"{qs.domain}: no decline question for reason {r}")
    return out


def frame_for(domain: str, seed: int):
    """The V01 dataset (with effects) plus one restricted column the policy denies; values are
    deterministic in the seed and never used by a gold query."""
    import numpy as np

    qs = load_set(domain)
    ds = build(domain, seed)
    frame = ds.frame.copy()
    rng = np.random.default_rng(seed + 10_000)
    ids = rng.integers(1, 900, size=len(frame))
    if qs.restricted_column.endswith("iban"):
        frame[qs.restricted_column] = [f"DE89{int(i):06d}{int(j):012d}" for i, j in zip(ids, rng.integers(0, 10**12, size=len(frame)), strict=True)]
    else:
        frame[qs.restricted_column] = [f"{qs.restricted_column.split('_')[0]}{int(i)}@example.com" for i in ids]
    return ds, frame, qs


# ----------------------------------------------------------------------------- execution match
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?([+-]\d{2}:?\d{2}|Z)?$")
_NUM = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")


def normalize(value: Any) -> Any:
    """One comparable form per value: numbers as float (bools as 0/1), timestamps as naive ISO
    strings (a date is its midnight), strings stripped; None stays None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat(timespec="seconds")
    text = str(value).strip()
    if _NUM.match(text):
        return float(text)
    if _TS.match(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None).isoformat(timespec="seconds")
        except ValueError:
            return text
    if text.lower() in ("true", "false"):
        return float(text.lower() == "true")
    return text


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL) or (math.isnan(a) and math.isnan(b))
    return a == b


def _sort_key(v: Any) -> tuple:
    return (0, v) if isinstance(v, float) else (1, "") if v is None else (2, str(v))


def _column_equal(a: list[Any], b: list[Any]) -> bool:
    return len(a) == len(b) and all(_same(x, y) for x, y in zip(sorted(a, key=_sort_key), sorted(b, key=_sort_key), strict=True))


def results_match(gold_rows: list[list[Any]], rows: list[list[Any]]) -> tuple[bool, str]:
    """(match, why not). Order-insensitive over rows; each gold column needs a distinct answer column
    with the same values; extra answer columns are allowed."""
    gold = [[normalize(v) for v in r] for r in gold_rows]
    got = [[normalize(v) for v in r] for r in rows]
    if len(gold) != len(got):
        return False, f"row count {len(got)} != gold {len(gold)}"
    if not gold:
        return True, ""
    width = len(gold[0])
    if len(got[0]) < width:
        return False, f"{len(got[0])} columns < gold {width}"
    gold_cols = [[r[i] for r in gold] for i in range(width)]
    got_cols = [[r[j] for r in got] for j in range(len(got[0]))]
    candidates = [[j for j, col in enumerate(got_cols) if _column_equal(g, col)] for g in gold_cols]
    if any(not c for c in candidates):
        return False, f"no answer column matches gold column {next(i for i, c in enumerate(candidates) if not c)}"

    def assignments(i: int, used: tuple[int, ...]):
        if i == width:
            yield used
            return
        for j in candidates[i]:
            if j not in used:
                yield from assignments(i + 1, (*used, j))

    for tried, mapping in enumerate(assignments(0, ())):
        if tried > 200:
            break
        remaining = [[r[j] for j in mapping] for r in got]
        if _rows_equal(gold, remaining):
            return True, ""
    return False, "same columns, different rows"


def _rows_equal(gold: list[list[Any]], got: list[list[Any]]) -> bool:
    pool = sorted(got, key=lambda r: [_sort_key(v) for v in r])
    for row in sorted(gold, key=lambda r: [_sort_key(v) for v in r]):
        hit = next((k for k, cand in enumerate(pool) if all(_same(a, b) for a, b in zip(row, cand, strict=True))), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


# ----------------------------------------------------------------------------- outcomes and metrics
@dataclass
class Outcome:
    id: str
    domain: str
    question: str
    expect: str
    reason: str | None
    tags: list[str]
    actual: str  # answer | needs_input | clarify | decline | error
    refusal_kind: str | None = None
    answered_by: str | None = None
    verified_query: str | None = None  # the registry entry that matched (answered or declined)
    route: str | None = None
    match: bool | None = None  # answer items that were answered
    match_note: str = ""
    restricted_leak: bool = False
    latency_ms: int = 0
    model_calls: int = 0  # provider calls (ok + error)
    cached_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tokens_saved: int = 0
    decisions: list[str] = field(default_factory=list)  # purpose:backend
    sql: str | None = None
    error: str | None = None
    turn_id: str | None = None

    @property
    def correct(self) -> bool:
        """Right outcome class, and for an answer the right result."""
        if self.expect == "answer":
            return self.actual == "answer" and bool(self.match)
        return self.actual == self.expect

    @property
    def confident_wrong(self) -> bool:
        """Answered with numbers that are not the right answer (the costly failure)."""
        return self.actual == "answer" and not (self.expect == "answer" and self.match)


def _pct(values: list[float], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def metrics(outcomes: list[Outcome]) -> dict[str, Any]:
    ans = [o for o in outcomes if o.expect == "answer"]
    answered = [o for o in outcomes if o.actual == "answer"]
    out: dict[str, Any] = {
        "questions": len(outcomes), "by_expect": dict(Counter(o.expect for o in outcomes)),
        "execution_accuracy": _ratio(sum(o.correct for o in ans), len(ans)),
        "answer_items": len(ans), "answer_items_answered": sum(o.actual == "answer" for o in ans),
        "answer_items_matched": sum(o.correct for o in ans),
        "answered_precision": _ratio(sum(o.correct for o in answered), len(answered)),  # of everything answered, right
        "confident_wrong": sum(o.confident_wrong for o in outcomes),
        "confident_wrong_share": _ratio(sum(o.confident_wrong for o in outcomes), len(outcomes)),
        "outcome_accuracy": _ratio(sum(o.correct for o in outcomes), len(outcomes)),
        "errors": sum(o.actual == "error" for o in outcomes),
        "restricted_leaks": sum(o.restricted_leak for o in outcomes),
    }
    refusal = {}
    for kind in REFUSAL_OUTCOMES:
        tp = sum(o.expect == kind and o.actual == kind for o in outcomes)
        pred = sum(o.actual == kind for o in outcomes)
        exp = sum(o.expect == kind for o in outcomes)
        refusal[kind] = {"expected": exp, "predicted": pred, "correct": tp, "precision": _ratio(tp, pred), "recall": _ratio(tp, exp)}
    out["refusal"] = refusal
    reasons = {}
    for reason in DECLINE_REASONS:
        items = [o for o in outcomes if o.expect == "decline" and o.reason == reason]
        governed = sum(o.actual == "decline" and o.refusal_kind in GOVERNED_KINDS for o in items)
        reasons[reason] = {"n": len(items), "declined": sum(o.actual == "decline" for o in items), "governed": governed,
                           "governed_recall": _ratio(governed, len(items)),
                           "refusal_kinds": dict(Counter(o.refusal_kind or o.actual for o in items))}
    out["decline_reasons"] = reasons
    out["refusal_kinds"] = dict(Counter(o.refusal_kind for o in outcomes if o.refusal_kind))
    out["by_tag"] = {tag: {"n": len(items), "matched": sum(o.correct for o in items), "accuracy": _ratio(sum(o.correct for o in items), len(items)),
                           "answered_by": dict(Counter(o.answered_by or o.actual for o in items))}
                     for tag in sorted({t for o in ans for t in o.tags}) for items in [[o for o in ans if tag in o.tags]]}
    out["answered_by"] = dict(Counter(o.answered_by for o in answered))
    lat = [o.latency_ms for o in outcomes]
    out["latency_ms"] = {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "mean": round(statistics.fmean(lat)) if lat else None}
    out["model"] = {"calls": sum(o.model_calls for o in outcomes), "calls_per_question": _ratio(sum(o.model_calls for o in outcomes), len(outcomes)),
                    "cached_calls": sum(o.cached_calls for o in outcomes),
                    "input_tokens": sum(o.input_tokens for o in outcomes), "output_tokens": sum(o.output_tokens for o in outcomes),
                    "tokens_per_question": _ratio(sum(o.input_tokens + o.output_tokens for o in outcomes), len(outcomes)),
                    "cost_usd": round(sum(o.cost_usd for o in outcomes), 6), "tokens_saved": sum(o.tokens_saved for o in outcomes),
                    "questions_with_a_model_call": sum(o.model_calls > 0 for o in outcomes)}
    return out


def summarize(outcomes: list[Outcome]) -> dict[str, Any]:
    domains = list(dict.fromkeys(o.domain for o in outcomes))
    return {"overall": metrics(outcomes), "by_domain": {d: metrics([o for o in outcomes if o.domain == d]) for d in domains}}


def check(tier: str, summary: dict[str, Any], outcomes: list[Outcome]) -> list[str]:
    """Invariants every tier must hold (not the pilot threshold, which the owner sets): no restricted
    value answered, no harness error, the off tier makes no provider call, a registry answer to the
    registry's own phrasing is right, and in the fake tier (where the model is an oracle) every
    model-written answer matches and every careless probe is refused by governance."""
    o = summary["overall"]
    out = []
    if o["restricted_leaks"]:
        out.append(f"{tier}: {o['restricted_leaks']} restricted answers")
    if o["errors"]:
        out.append(f"{tier}: {o['errors']} questions raised instead of answering or refusing: "
                   + "; ".join(f"{x.id}: {x.error}" for x in outcomes if x.actual == "error")[:500])
    if tier == "off" and o["model"]["calls"]:
        out.append(f"off: {o['model']['calls']} provider calls with no provider key")
    wrong_exact = [x.id for x in outcomes if "registry_exact" in x.tags and not x.correct]
    if wrong_exact:
        out.append(f"{tier}: registry phrasings not answered correctly: {wrong_exact}")
    if tier == "fake":
        bad = [x.id for x in outcomes if x.answered_by == "model" and not x.match]
        if bad:
            out.append(f"fake: model (oracle) answers that do not match gold: {bad}")
        leaked = [x.id for x in outcomes if x.expect == "decline" and x.refusal_kind not in GOVERNED_KINDS and x.actual != "clarify"]
        if leaked:
            out.append(f"fake: careless probes not refused by governance: {leaked}")
    return out


# ----------------------------------------------------------------------------- the fake transport
class OracleTransport:
    """A model that writes the gold SQL (or the careless `probe_sql` of a decline item) for the
    benchmark question found in its prompt; decision calls answer nothing, so the rules decide."""

    def __init__(self) -> None:
        self.answers: dict[str, str | None] = {}
        self.chat_calls = 0
        self.decide_calls = 0

    def chat(self, *, base_url, api_key, payload, timeout):
        self.chat_calls += 1
        text = json.dumps(payload, ensure_ascii=False)
        found = [q for q in self.answers if q in text or json.dumps(q, ensure_ascii=False)[1:-1] in text]
        sql = self.answers[max(found, key=len)] if found else None
        content = json.dumps({"sql": sql, "explanation": "benchmark oracle"} if sql else {})
        # Echo the requested model as a provider does: the call record then shows which model the router chose (S12 matrix).
        return {"model": payload.get("model") or "benchmark/oracle", "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": len(text) // 4, "completion_tokens": len(content) // 4, "cost": 0.0}}

    def decide(self, *, base_url, api_key, payload, timeout):
        self.decide_calls += 1
        return {"answers": {}, "usage": {}}


# ----------------------------------------------------------------------------- the platform
@dataclass
class DomainEnv:
    domain: str
    workspace_id: str
    table: str  # schema-qualified staged table
    restricted: str
    registry_ids: list[str]
    seconds: float


def _sub(sql: str, table: str) -> str:
    return sql.replace("@table", table)


def setup_domain(qs: QuestionSet, frame, admin, *, seed: int) -> DomainEnv:
    """Upload -> file source -> discovery -> staged snapshot, a policy that restricts one column, and
    the verified-query registry (each rendered template must pass the gateway validator)."""
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.core.ids import new_id
    from analystos.db.base import session_scope
    from analystos.db.models import SourceAsset, VerifiedQuery
    from analystos.gateway.validator import validate_sql
    from analystos.governance.policy import resolve_scope
    from analystos.registries import verified_queries as vqr
    from analystos.services.sources import discover_source, register_source, select_assets
    from analystos.services.workspaces import create_workspace

    started = time.perf_counter()
    settings = get_settings()
    ds_table = build(qs.domain, seed, n=10).table
    with session_scope() as s:
        ws = create_workspace(s, s.merge(admin), name=f"V02 Ask benchmark {qs.domain} {new_id('b')[-6:]}",
                              objective=f"Answer questions about the {ds_table} data",
                              policy={"require_approved_metrics": False, "restricted_columns": [f"*.{qs.restricted_column}"],
                                      "ask_queries_per_user_per_hour": 5000, "ask_queries_per_workspace_per_hour": 5000})
        s.flush()
        folder = settings.upload_dir / ws.id
        folder.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(folder / f"{ds_table}.parquet", index=False)
        src = register_source(s, s.merge(admin), ws.id, kind="csv", name=f"{qs.domain} ask benchmark",
                              config={"path": f"{ws.id}/{ds_table}.parquet"}, secret_ref=None)
        s.flush()
        ws_id, src_id = ws.id, src.id
    discover_source(admin, src_id)
    select_assets(admin, src_id, [ds_table])
    ids = []
    with session_scope() as s:
        asset = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ws_id, SourceAsset.name == ds_table))
        table = f"{asset.schema_name}.{asset.name}" if asset is not None else None
        scope = resolve_scope(s, s.merge(admin), ws_id)
        if table not in scope.assets:
            raise RuntimeError(f"{qs.domain}: the staged table is not in scope ({table})")
        if f"{table}.{qs.restricted_column}" not in scope.denied_columns:
            raise RuntimeError(f"{qs.domain}: the policy does not deny {qs.restricted_column}")
        dialect = scope.source_dialects[src_id]
        for entry in qs.registry:
            params = []
            for spec in entry.get("parameters") or []:
                p = {k: v for k, v in spec.items() if k != "values_from"} | {"required": True, "column": f"{table}.{spec['column']}"}
                if p["type"] == "enum":  # the profiled vocabulary a promotion would record
                    p["values"] = sorted({str(v) for v in frame[spec["values_from"]].unique()})
                params.append(p)
            template = _sub(entry["sql"], table)
            validate_sql(scope, vqr.render(template, params, entry.get("example") or {}, dialect), max_rows=scope.max_rows)
            vq = VerifiedQuery(id=new_id("vq"), workspace_id=ws_id, name=entry["name"], description=entry["patterns"][0],
                               patterns=list(entry["patterns"]), sql_template=template, parameters=params, source_id=src_id,
                               dialect=dialect, origin={"type": "benchmark_fixture", "benchmark": "P4-V02"}, spec=None,
                               status="active", hits=0, created_by=admin.id)
            s.add(vq)
            ids.append(vq.id)
    vqr._lexicons.pop(ws_id, None)
    return DomainEnv(qs.domain, ws_id, table, qs.restricted_column, ids, round(time.perf_counter() - started, 1))


def gold_results(env: DomainEnv, qs: QuestionSet, admin, gateway) -> dict[str, list[list[Any]]]:
    """Every gold statement through QueryGateway.execute under the asking user's scope."""
    from analystos.db.base import session_scope
    from analystos.governance.policy import resolve_scope

    with session_scope() as s:
        scope = resolve_scope(s, s.merge(admin), env.workspace_id)
        s.expunge_all()
    out = {}
    for q in qs.questions:
        if q.gold:
            res = gateway.execute(scope, _sub(q.gold, env.table), actor=BENCH_ACTOR, purpose="evaluation.gold", use_cache=False)
            if res.truncated:
                raise RuntimeError(f"{q.id}: gold result truncated")
            out[q.id] = res.rows
    return out


def _turn_usage(turn_id: str, workspace_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from analystos.db.base import session_scope
    from analystos.db.models import DecisionRecord, ModelCall

    with session_scope() as s:
        calls = list(s.scalars(select(ModelCall).where(ModelCall.task_id == turn_id, ModelCall.workspace_id == workspace_id)))
        decisions = [f"{d.purpose}:{d.backend}" for d in s.scalars(select(DecisionRecord).where(DecisionRecord.task_id == turn_id)
                                                                   .order_by(DecisionRecord.created_at))]
        live = [c for c in calls if c.status in ("ok", "error")]
        return {"model_calls": len(live), "cached_calls": sum(c.status == "cache_hit" for c in calls),
                "input_tokens": sum(c.input_tokens or 0 for c in live), "output_tokens": sum(c.output_tokens or 0 for c in live),
                "cost_usd": float(sum(c.cost_usd or 0.0 for c in live)), "tokens_saved": sum(c.tokens_saved or 0 for c in calls),
                "decisions": decisions, "call_errors": [c.error for c in calls if c.status == "error" and c.error]}


_ACTUAL = {"answered": "answer", "needs_input": "needs_input", "clarify": "clarify", "refused": "decline"}


def ask_one(q: Question, env: DomainEnv, admin, gold: dict[str, list[list[Any]]]) -> Outcome:
    from analystos.core.errors import AnalystOSError
    from analystos.db.base import session_scope
    from analystos.services.ask import ask_in_thread, create_thread

    out = Outcome(id=q.id, domain=q.domain, question=q.q, expect=q.expect, reason=q.reason, tags=list(q.tags), actual="error")
    try:
        with session_scope() as s:
            thread_id = create_thread(s, s.merge(admin), env.workspace_id, title=f"benchmark {q.id}")["id"]
        turn = ask_in_thread(admin, thread_id, q.q)
    except (AnalystOSError, Exception) as exc:  # noqa: BLE001 - a harness outcome, reported per question
        out.error = f"{exc.__class__.__name__}: {exc}"[:300]
        return out
    out.turn_id, out.latency_ms = turn["id"], int(turn.get("latency_ms") or 0)
    out.actual = _ACTUAL.get(turn["status"], "error")
    refusal = turn.get("refusal") or {}
    details = refusal.get("details") or {}
    out.refusal_kind = "write_refused" if (refusal.get("kind") == "not_answerable" and details.get("reason") == "write"
                                           and details.get("by") == "rules") else refusal.get("kind")
    out.answered_by, out.route, out.sql = turn.get("answered_by"), turn.get("route"), turn.get("sql")
    out.verified_query = (turn.get("verified_query") or {}).get("name")
    usage = _turn_usage(turn["id"], env.workspace_id)
    for k in ("model_calls", "cached_calls", "input_tokens", "output_tokens", "cost_usd", "tokens_saved", "decisions"):
        setattr(out, k, usage[k])
    if out.actual == "error":
        out.error = f"turn status {turn['status']}"
    if out.actual == "answer":
        result = turn.get("result") or {}
        cols = [str(c).lower() for c in result.get("columns") or []]
        out.restricted_leak = env.restricted in cols or env.restricted in (out.sql or "").lower()
        if q.expect == "answer":
            if result.get("truncated"):
                out.match, out.match_note = False, "answer truncated"
            else:
                out.match, out.match_note = results_match(gold[q.id], result.get("rows") or [])
    if usage["call_errors"] and not out.match_note:  # provider errors surface as refusal kinds; keep the first
        out.match_note = f"provider error: {usage['call_errors'][0][:160]}"
    return out


def _admin():
    from sqlalchemy import select

    from analystos.core.config import get_settings
    from analystos.db.base import session_scope
    from analystos.db.models import User

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == get_settings().bootstrap_admin_email))
        if admin is None:
            raise RuntimeError("the Ask suite runs as the bootstrap admin: run `analystos seed` on this database first")
        s.expunge(admin)
    return admin


def configure(tier: str, disabled_models: list[str] | tuple[str, ...] = ()):
    """Model access for a tier. Returns (restore callable, oracle or None). `disabled_models` (fake tier) takes
    models out of routing, as the per-model matrix does to pin a profile to one model (S12 §3)."""
    import os

    from analystos.core.config import get_settings
    from analystos.runtime import context as runtime_context
    from analystos.runtime.context import default_router

    saved_env = {k: os.environ.get(k) for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY")}
    saved_services = runtime_context.default_services
    oracle = None
    if tier in ("off", "fake"):
        for k in saved_env:
            os.environ.pop(k, None)
    elif not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("live tier needs OPENROUTER_API_KEY in the environment")
    get_settings.cache_clear()
    default_router.cache_clear()
    if tier == "fake":
        from analystos.contracts.platform import LLMSettings, PlatformSettings
        from analystos.llm.router import ModelRouter
        from analystos.runtime.context import Services, default_gateway
        from analystos.runtime.usage import DbUsageSink

        oracle = OracleTransport()
        settings = PlatformSettings(llm=LLMSettings(cache_enabled=False, disabled_models=list(disabled_models)))
        router = ModelRouter(transport=oracle, api_key_lookup=lambda env: "fake", sink=DbUsageSink(), settings_provider=lambda: settings)
        services = Services(router=router, gateway=default_gateway())
        runtime_context.default_services = lambda: services

    def restore() -> None:
        runtime_context.default_services = saved_services
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
        default_router.cache_clear()

    return restore, oracle


@dataclass
class Run:
    tier: str
    seed: int
    outcomes: list[Outcome]
    summary: dict[str, Any]
    problems: list[str]
    setup: dict[str, Any]
    seconds: float
    probe: dict[str, Any] | None = None


def run(tier: str, *, domains: tuple[str, ...] = DOMAINS, seed: int = 1, limit: int | None = None,
        only: set[str] | None = None, probe: bool = True, disabled_models: list[str] | None = None) -> Run:
    """One tier over the question sets. `limit`/`only` pick a subset (smoke runs). The live tier asks
    one model-only question first and stops (no retries) when it does not get a provider answer."""
    from analystos.runtime.context import default_gateway

    started = time.perf_counter()
    restore, oracle = configure(tier, tuple(disabled_models or ()))
    try:
        admin = _admin()
        gateway = default_gateway()
        outcomes: list[Outcome] = []
        setup: dict[str, Any] = {}
        probe_info = None
        for domain in domains:
            _, frame, qs = frame_for(domain, seed)
            questions = [q for q in qs.questions if (only is None or q.id in only)]
            if limit is not None:
                questions = _stratified(questions, limit)
            env = setup_domain(qs, frame, admin, seed=seed)
            gold = gold_results(env, qs, admin, gateway)
            setup[domain] = {"workspace_id": env.workspace_id, "table": env.table, "rows": len(frame), "registry": len(env.registry_ids),
                             "setup_seconds": env.seconds, "restricted_column": env.restricted}
            if oracle is not None:
                oracle.answers = {q.q: _sub(q.gold or q.probe_sql or "", env.table) or None for q in qs.questions}
            if tier == "live" and probe and probe_info is None:
                first = next(q for q in questions if "novel" in q.tags)
                o = ask_one(first, env, admin, gold)
                probe_info = {"question": first.id, "status": o.actual, "refusal_kind": o.refusal_kind, "model_calls": o.model_calls,
                              "note": o.match_note or o.error}
                if o.model_calls == 0 or o.refusal_kind in NO_MODEL_KINDS | {"unavailable"}:
                    return Run(tier, seed, [], {}, [f"live: probe failed ({probe_info}); tier not run"], setup,
                               round(time.perf_counter() - started, 1), probe_info)
                outcomes.append(o)
                questions = [q for q in questions if q.id != first.id]
            for q in questions:
                outcomes.append(ask_one(q, env, admin, gold))
        summary = summarize(outcomes)
        return Run(tier, seed, outcomes, summary, check(tier, summary, outcomes), setup, round(time.perf_counter() - started, 1), probe_info)
    finally:
        restore()


def _stratified(questions: list[Question], limit: int) -> list[Question]:
    """Up to `limit` questions per domain, round-robin over (expect, first tag) so a smoke run still
    covers every outcome class."""
    groups: dict[tuple, list[Question]] = {}
    for q in questions:
        groups.setdefault((q.expect, q.tags[0] if q.tags else q.reason), []).append(q)
    picked: list[Question] = []
    while len(picked) < limit and any(groups.values()):
        for key in list(groups):
            if groups[key] and len(picked) < limit:
                picked.append(groups[key].pop(0))
    return sorted(picked, key=lambda q: q.id)


def as_dict(r: Run) -> dict[str, Any]:
    return {"tier": r.tier, "seed": r.seed, "seconds": r.seconds, "setup": r.setup, "probe": r.probe, "problems": r.problems,
            "summary": r.summary, "outcomes": [{**asdict(o), "correct": o.correct, "confident_wrong": o.confident_wrong} for o in r.outcomes]}
