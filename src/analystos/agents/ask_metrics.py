"""Approved metrics a question names, matched without a model (PILOT-FIX-05, rehearsal C06).

`semantic/compiler.match_question` accepts only a question that *is* a metric phrase ("late delivery rate by sales
channel"). Real questions carry more words: "What is our late delivery rate by sales channel? Put it on a dashboard I
can share later." went to model-written SQL beside the approved Late Delivery Rate. This matcher reads such questions
deterministically:

1. Sentences that only ask for an output ("put it on a dashboard", "send it to me") are set aside: the answer's own
   actions do that.
2. A metric is a candidate when one of its names (name, display name, `ai_context` synonyms, glossary synonyms) appears
   in the question as a phrase, or every word of a several-word name does. The longest phrase wins. Candidates that
   tie are ranked by the `metric_match` decision (authority `rank`: it orders the candidates the rule found, it cannot
   add one); no clear first is a clarify, never a guess.
3. The rest of the sentence must be explained by the metric's own dimensions ("by sales channel"), a time grain
   ("per month", "monthly", "over time") or a period ("in Q3 2026") on the metric's time dimension. Polite and
   possessive words are ignored. Any other content word -> no match: the `semantic_query` model may still choose, else
   the ad-hoc path answers and says that an approved metric matched but did not cover the question.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from analystos.agents.ask_period import find_period

_SENTENCE = re.compile(r"(?<=[?.!;])\s+|\n+")
_OUTPUT_VERB = r"(?:put|add|pin|save|place|turn|make|create|build|send|share|export|email|post|draw|plot|chart|show)"
_OUTPUT = re.compile(r"^(?:and |then |also |please |can you |could you )*" + _OUTPUT_VERB + r"\b.*\b(?:dashboard|chart|report|"
                     r"pdf|slide|deck|email|slack|team|share|later|colleague|manager|graph)\b")
_LEAD = re.compile(r"^(?:(?:please|can you|could you|so|and|ok|okay|now|then)\s+)*(?:what is|what's|whats|what are|what was|"
                   r"what were|show me|show|give me|tell me|get me|get|how is|how are|how's|how was|how did|how does|"
                   r"calculate|compute|list|plot|chart|i want|i need|let me see)?\s*")
_IGNORED = frozenset({"our", "the", "a", "an", "my", "is", "are", "was", "were", "what", "s", "for", "of", "in", "on", "at",
                      "all", "overall", "current", "currently", "now", "today", "so", "far", "please", "and", "to", "it",
                      "its", "this", "that", "with", "across", "each", "every", "per", "by", "split", "broken", "down",
                      "grouped", "segmented", "based", "us", "me", "we", "do", "does", "did", "have", "has", "been",
                      "during", "within", "over", "time", "trend", "trends", "look", "like", "looks", "data"})
_GRAIN_WORDS = {"daily": "day", "weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year",
                "annual": "year", "annually": "year", "day": "day", "week": "week", "month": "month", "quarter": "quarter",
                "year": "year"}
_DIM_CUE = re.compile(r"\b(?:broken down by|grouped by|split by|segmented by|based on|for each|for every|in each|across|"
                      r"by|per|each|every)\s+")
MARGIN = 0.25  # how far the ranked first must lead the second on the 0..2 scale


def _words(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"[_\-]+", " ", str(text).casefold()))).strip()


@dataclass
class MetricMatch:
    """The rule's reading of a question: a query, a clarify between metrics, or a near miss to say out loud."""

    query: Any = None  # SemanticQuery
    metric: str | None = None
    tied: list[str] = field(default_factory=list)
    unexplained: list[str] = field(default_factory=list)  # a metric matched, these words were not covered
    output_request: str | None = None
    ranked_by: str | None = None


def metric_sentences(question: str) -> tuple[list[str], str | None]:
    """(the sentences to match, the first output-only request set aside)."""
    keep, output = [], None
    for part in [p.strip() for p in _SENTENCE.split(question or "") if p and p.strip()]:
        if _OUTPUT.match(_words(part)) and output is None:
            output = part
            continue
        keep.append(part)
    return keep, output


def lead_free(sentence: str) -> str:
    """A sentence in plain words without its lead-in ("what is", "show me")."""
    return _LEAD.sub("", _words(sentence), count=1).strip()


def _aliases(name: str, entry: dict[str, Any], synonyms: dict[str, str]) -> set[str]:
    d = entry["definition"]
    out = {_words(name), _words(d.get("display_name") or name)}
    ai = d.get("ai_context")
    if isinstance(ai, dict):
        out |= {_words(s) for s in ai.get("synonyms") or [] if isinstance(s, str) and s.strip()}
    out |= {_words(p) for p, m in synonyms.items() if m == name}
    return {a for a in out if a}


def _field(catalog: dict[str, Any], dataset: str, ref: str) -> dict[str, Any] | None:
    ds_name, _, fname = ref.rpartition(".")
    for d in catalog.get("datasets") or []:
        if d["name"] == (ds_name or dataset):
            return next((f for f in d.get("fields") or [] if f["name"] == fname), None)
    return None


def _is_time(f: dict[str, Any] | None) -> bool:
    return bool(f and isinstance(f.get("dimension"), dict) and f["dimension"].get("is_time"))


def _time_dimension(catalog: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """The metric's one time dimension: declared among its dimensions, else the dataset's only time field."""
    d = entry["definition"]
    declared = [r for r in d.get("dimensions") or [] if _is_time(_field(catalog, d.get("dataset") or "", r))]
    if len(declared) == 1:
        return declared[0]
    if declared:
        return None
    ds = next((x for x in catalog.get("datasets") or [] if x["name"] == d.get("dataset")), None)
    times = [f["name"] for f in (ds or {}).get("fields") or [] if _is_time(f)]
    return times[0] if len(times) == 1 else None


def _dimension(phrase: str, catalog: dict[str, Any], entry: dict[str, Any]) -> str | None:
    """The one declared dimension a phrase names (its field name or label; all of the phrase's words)."""
    want = set(_words(phrase).split()) - _IGNORED
    if not want:
        return None
    d = entry["definition"]
    exact, partial = [], []
    for ref in d.get("dimensions") or []:
        f = _field(catalog, d.get("dataset") or "", ref) or {}
        names = [_words(ref.split(".")[-1]), _words(f.get("label") or "")]
        for n in [n for n in names if n]:
            words = set(n.split())
            if want == words:
                exact.append(ref)
            elif want <= words:
                partial.append(ref)
    for found in (list(dict.fromkeys(exact)), list(dict.fromkeys(partial))):
        if len(found) == 1:
            return found[0]
        if found:
            return None
    return None


def candidates(text: str, metrics: dict[str, Any], synonyms: dict[str, str]) -> list[tuple[str, str]]:
    """(metric, alias) pairs named in the text: the longest phrase match, else every word of a several-word name."""
    padded = f" {text} "
    by_phrase: list[tuple[int, str, str]] = []
    for name, entry in metrics.items():
        for alias in _aliases(name, entry, synonyms):
            if f" {alias} " in padded:
                by_phrase.append((len(alias), name, alias))
    if by_phrase:
        best = max(n for n, _, _ in by_phrase)
        return list(dict.fromkeys((name, alias) for n, name, alias in by_phrase if n == best))
    words = set(text.split())
    covered = []
    for name, entry in metrics.items():
        for alias in _aliases(name, entry, synonyms):
            parts = set(alias.split()) - _IGNORED
            if len(parts) >= 2 and parts <= words:
                covered.append((len(parts), name, alias))
    if not covered:
        return []
    best = max(n for n, _, _ in covered)
    return list(dict.fromkeys((name, alias) for n, name, alias in covered if n == best))


def _rest(text: str, alias: str) -> str:
    padded = f" {text} "
    if f" {alias} " in padded:
        return padded.replace(f" {alias} ", " ", 1).strip()
    left = [w for w in text.split() if w not in set(alias.split())]
    return " ".join(left)


def _shape(rest: str, catalog: dict[str, Any], entry: dict[str, Any], question: str) -> tuple[dict[str, Any], list[str]]:
    """The dimensions, grain and period the rest of the sentence names, and the words it leaves unexplained."""
    query: dict[str, Any] = {"dimensions": []}
    period = find_period(rest)
    if period is not None:
        rest = (rest[:period.span[0]] + " " + rest[period.span[1]:]).strip()
    grain = None
    over = re.search(r"\b(?:over time|trend|trends|trending|time series)\b", rest)
    if over:
        grain, rest = "month", (rest[:over.start()] + " " + rest[over.end():]).strip()
    lead = re.match(r"^(daily|weekly|monthly|quarterly|yearly|annual)\b", rest)
    if lead:
        grain, rest = _GRAIN_WORDS[lead.group(1)], rest[lead.end():].strip()
    unexplained: list[str] = []
    pieces = _DIM_CUE.split(rest)
    head, groups = pieces[0], pieces[1:]
    unexplained += [w for w in head.split() if w not in _IGNORED]
    for group in groups:
        for phrase in [p for p in re.split(r"\s*(?:,|\band\b|&)\s*", group) if p.strip()]:
            p = phrase.strip()
            if p in _GRAIN_WORDS and p not in ("daily", "weekly", "monthly", "quarterly", "yearly", "annual", "annually"):
                grain = _GRAIN_WORDS[p]
                continue
            dim = _dimension(p, catalog, entry)
            if dim is None:
                unexplained += [w for w in p.split() if w not in _IGNORED]
            elif dim not in query["dimensions"]:
                query["dimensions"].append(dim)
    if grain or period is not None:
        time_dim = _time_dimension(catalog, entry)
        if time_dim is None:
            unexplained.append(period.label if period is not None else f"per {grain}")
        else:
            query["time"] = {"dimension": time_dim, **({"grain": grain} if grain else {}),
                             **({"start": period.start, "end": period.end} if period is not None else {})}
    return query, unexplained


def match(ctx: Any, question: str, catalog: dict[str, Any]) -> MetricMatch:
    """The rule's reading of a question against the caller's planner catalog (masked metrics are absent)."""
    from pydantic import ValidationError

    from analystos.contracts.semantic import SemanticQuery

    metrics = catalog.get("metrics") or {}
    if not metrics:
        return MetricMatch()
    sentences, output = metric_sentences(question)
    synonyms = catalog.get("synonyms") or {}
    for sentence in sentences:
        text = lead_free(sentence)
        found = candidates(text, metrics, synonyms)
        if not found:
            continue
        names = list(dict.fromkeys(n for n, _ in found))
        ranked_by = "rules"
        if len(names) > 1:
            names, ranked_by = _rank(ctx, question, names, metrics)
            if len(names) > 1:
                return MetricMatch(tied=names, output_request=output, ranked_by=ranked_by)
        name = names[0]
        alias = next(a for n, a in found if n == name)
        shape, unexplained = _shape(_rest(text, alias), catalog, metrics[name], question)
        if unexplained:
            return MetricMatch(metric=name, unexplained=unexplained, output_request=output, ranked_by=ranked_by)
        try:
            query = SemanticQuery(metrics=[name], **shape)
        except ValidationError:
            return MetricMatch(metric=name, unexplained=["the period"], output_request=output, ranked_by=ranked_by)
        return MetricMatch(query=query, metric=name, output_request=output, ranked_by=ranked_by)
    return MetricMatch(output_request=output)


def _rank(ctx: Any, question: str, names: list[str], metrics: dict[str, Any]) -> tuple[list[str], str]:
    """`metric_match` (authority rank) orders the tied candidates; a clear first wins, else they stay tied."""
    from analystos.agents.sql_agent import decide
    from analystos.decisions.types import Question

    # Names only: the rules backend scores word overlap, and a long description would lose to a short one.
    items = {n: f"{metrics[n]['definition'].get('display_name') or n} ({n})" for n in names}
    d = decide(ctx, "metric_match", {"question": question},
               Question.scores("Which approved metric does the question ask for?", items), {"candidates": names})
    scores = d.get("value") if isinstance(d.get("value"), dict) else {}
    ranked = sorted(names, key=lambda n: -float(scores.get(n) or 0.0))
    top = float(scores.get(ranked[0]) or 0.0)
    second = float(scores.get(ranked[1]) or 0.0) if len(ranked) > 1 else 0.0
    return ([ranked[0]] if top - second >= MARGIN else ranked), str(d.get("backend") or "rules")
